"""Guard: the chart has ONE Postgres image, and it is the pinned 18.3 build.

Origin: CHART-PG-TAG-FALLBACK-16 (operator, 2026-11-01). The summariser-role
Job carried its own ``default "16"`` image tag next to the Bitnami
StatefulSet, and ``values.yaml`` set no ``postgresql.image.tag`` at all, so
the chart's own default (the subchart's mislabelled ``17.6.0-debian-12-r4``)
was neither what production runs nor what docker-compose runs.

Design (each point is a falsifiability claim, proven by a neuter below):

* The set of Postgres-bearing containers is DERIVED FROM THE RENDER (every
  pod template of every workload kind, containers and initContainers): those
  whose image repository name contains ``postgres`` plus those whose command
  runs ``psql``. An empty derivation, or one that lacks the StatefulSet or the
  summariser Job, fails (the guard cannot go blind).
* The render is exercised with default values, with EVERY ``values-*.yaml`` in
  the chart directory (globbed, never listed) and with the live-structure
  fixture. All of them must yield exactly one image, equal to ``PINNED``.
* A static scan forbids any ``default "<number>"`` fallback on a Postgres
  image line anywhere in ``charts/audittrace/`` (the old fallback is dead at
  render time because the subchart default is merged into ``.Values``, so only
  a source scan can catch its re-introduction).
* The Job template must FAIL the render (``required``) when a key is missing.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
CHART_DIR = REPO / "charts" / "audittrace"
PINNED = "ghcr.io/lfdesousa/audittrace-postgresql:18.3-bitnami-frozen-apr17"
PINNED_REPO = PINNED.rsplit(":", 1)[0]
DIGEST = "sha256:" + "a" * 64
FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "live-structure-values-nonsecret.yaml"
)
POD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod"}
STS = ("StatefulSet", "audittrace-postgresql", "postgresql")
JOB = ("Job", "audittrace-ensure-summariser-role", "psql")
JOB_TEMPLATE = Path("templates") / "postgres" / "job-summariser-role.yaml"
# Postgres client binaries: a container running any of these is a Postgres container.
PG_CLIENT_TOKENS = ("psql", "pg_dump", "pg_restore", "pg_isready", "pg_basebackup")
# Different binary, different image (bitnamilegacy/postgres-exporter): not "the
# Postgres image", so excluded by EXACT repository name, never by substring.
EXCLUDED_REPOS = {"postgres-exporter"}
# values files in the repo that do NOT target this chart. Every other file
# matched by the discovery globs below is swept.
NOT_THIS_CHART = {
    # installs the audittrace-content-control chart, not charts/audittrace
    "values-cc-ci.yaml": "targets the content-control chart",
}
VALUES_GLOBS = (
    "charts/audittrace/values-*.yaml",
    "tests/integration/fixtures/values-*.yaml",
    "tests/fixtures/*values*.yaml",
)
# Same required-secret args as `make helm-lint`.
RENDER_ARGS = [
    "--set", "secrets.minio.secretKey=ci-test",
    "--set", "secrets.minio.kmsKey=ci-test",
    "--set", "secrets.chromadb.token=ci-test",
    "--set", "secrets.keycloak.adminPassword=ci-test",
    "--set", "secrets.postgres.appPassword=ci-test",
    "--set", "secrets.postgres.password=ci-test",
    "--set", "secrets.redis.password=ci-test",
    "--set", "secrets.summariser.password=ci-test",
    "--set", "externalLLM.host=llm.test.invalid",
    "--set", "observability.external.langfuseHost=langfuse.test.invalid",
    "--set", "observability.external.tempoHost=tempo.test.invalid",
    "--set", "observability.external.lokiHost=loki.test.invalid",
]  # fmt: skip
FALLBACK_RE = re.compile(r'default\s+"v?\d+(?:\.\d+)*"')


def values_files(repo: Path = REPO) -> list[Path]:
    """Every values file in the repo that targets charts/audittrace (discovered)."""
    found = sorted(
        {f for g in VALUES_GLOBS for f in repo.glob(g) if f.name not in NOT_THIS_CHART}
    )
    assert any(f.name == "values-ci.yaml" for f in found), "values-ci.yaml not found"
    assert any(f.name == "values-laptop.yaml" for f in found), "laptop values not found"
    return found


def render(chart: Path, extra: list[str] | None = None) -> list[dict]:
    helm = shutil.which("helm")
    assert helm is not None, "helm is required (CI `test` job uses the runner's helm)"
    cmd = [helm, "template", "audittrace", str(chart), "-n", "audittrace"]
    cmd += RENDER_ARGS + (extra or [])
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _containers(doc: dict) -> list[dict]:
    if doc.get("kind") not in POD_KINDS:
        return []
    spec = doc.get("spec") or {}
    if doc["kind"] == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    if doc["kind"] != "Pod":
        spec = spec.get("template", {}).get("spec") or {}
    return (spec.get("containers") or []) + (spec.get("initContainers") or [])


def _repo_name(image: str) -> str:
    return image.rsplit("/", 1)[-1].split(":", 1)[0].split("@", 1)[0]


def postgres_images(docs: list[dict]) -> dict[tuple[str, str, str], str]:
    """{(kind, workload, container): image} for EVERY container that is, or runs, Postgres.

    Keyed per container: a second container in the same workload can never
    overwrite or hide another one.
    """
    found: dict[tuple[str, str, str], str] = {}
    for doc in docs:
        for c in _containers(doc):
            image = str(c.get("image", ""))
            repo = _repo_name(image)
            if repo in EXCLUDED_REPOS:
                continue
            cmd_text = " ".join(
                str(x) for x in (c.get("command") or []) + (c.get("args") or [])
            )
            runs_psql = any(t in cmd_text for t in PG_CLIENT_TOKENS)
            if "postgres" in repo or runs_psql:
                found[(doc["kind"], doc["metadata"]["name"], str(c.get("name")))] = (
                    image
                )
    return found


def violations(docs: list[dict], expected: str = PINNED) -> list[str]:
    """Fail-closed: [] only when STS and Job exist and EVERY container equals `expected`."""
    found = postgres_images(docs)
    if STS not in found or JOB not in found:
        return [f"blind: derivation lacks {STS} or {JOB}: {sorted(found)}"]
    return sorted(f"{'/'.join(k)}={v}" for k, v in found.items() if v != expected)


def fallback_hits(chart: Path) -> list[str]:
    """Postgres image lines carrying a ``default "<version>"`` fallback."""
    hits = []
    for path in sorted(chart.rglob("*")):
        if not path.is_file() or path.suffix not in {".yaml", ".tpl", ".txt"}:
            continue
        if "charts" in path.relative_to(chart).parts[:1]:  # vendored subcharts
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            low = line.lower()
            in_pg_dir = path.relative_to(chart).parts[:2] == ("templates", "postgres")
            pg_line = "postgres" in low or (in_pg_dir and "image" in low)
            if pg_line and FALLBACK_RE.search(line):
                hits.append(f"{path.relative_to(chart)}:{n}")
    return hits


@pytest.fixture(scope="module")
def sweep() -> dict[str, list[dict]]:
    out = {"<defaults>": render(CHART_DIR)}
    for vf in values_files():
        out[str(vf.relative_to(REPO))] = render(CHART_DIR, ["-f", str(vf)])
    return out


def test_pinned_image_is_what_compose_runs() -> None:
    compose = (REPO / "docker-compose.yml").read_text()
    assert (
        compose.count(PINNED) >= 2
    )  # the services `postgres` and the migrate/seed one


def test_sweep_covers_the_kind_smoke_values_and_excludes_only_the_cc_chart() -> None:
    names = {f.name for f in values_files()}
    assert {"values-ci.yaml", "values-laptop.yaml"} <= names
    assert "values-cc-ci.yaml" not in names and set(NOT_THIS_CHART) == {
        "values-cc-ci.yaml"
    }
    on_disk = {f.name for g in VALUES_GLOBS for f in REPO.glob(g)}
    assert on_disk - names == set(NOT_THIS_CHART)  # nothing is skipped silently


def test_every_values_file_yields_one_pinned_image(
    sweep: dict[str, list[dict]],
) -> None:
    assert len(sweep) >= 7
    for name, docs in sweep.items():
        assert violations(docs) == [], f"{name}: {violations(docs)}"
        assert set(postgres_images(docs).values()) == {PINNED}, name


def test_no_version_fallback_in_chart_sources() -> None:
    assert fallback_hits(CHART_DIR) == []


def test_global_form_of_the_insecure_images_opt_in_is_not_set() -> None:
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
    assert "security" not in (values.get("global") or {})
    assert values["postgresql"]["global"]["security"]["allowInsecureImages"] is True


def test_missing_key_fails_the_render_not_defaults() -> None:
    for key in ("registry", "repository", "tag"):
        with pytest.raises(subprocess.CalledProcessError) as exc:
            render(CHART_DIR, ["--set", f"postgresql.image.{key}="])
        assert f"postgresql.image.{key} is required" in exc.value.stderr, key


def test_digest_replaces_the_tag_like_the_subchart() -> None:
    docs = render(CHART_DIR, ["--set", f"postgresql.image.digest={DIGEST}"])
    assert violations(docs, f"{PINNED_REPO}@{DIGEST}") == []


def test_registry_mirror_profile_gives_statefulset_and_job_one_image() -> None:
    mirror = "registry.example.internal"
    docs = render(CHART_DIR, ["--set", f"global.imageRegistry={mirror}"])
    expected = f"{mirror}/lfdesousa/audittrace-postgresql:18.3-bitnami-frozen-apr17"
    assert violations(docs, expected) == []
    images = postgres_images(docs)
    assert images[STS] == images[JOB] == expected


def test_postgres_exporter_is_excluded_by_name_and_hides_nothing() -> None:
    docs = render(CHART_DIR, ["--set", "postgresql.metrics.enabled=true"])
    exporters = [
        c["image"]
        for d in docs
        for c in _containers(d)
        if _repo_name(c["image"]) in EXCLUDED_REPOS
    ]
    assert exporters, "exporter not rendered: the exclusion is untested"
    assert violations(docs) == []
    assert postgres_images(docs)[STS] == PINNED  # the exporter did not replace it


# ---------------------------------------------------------------- neuters
@pytest.fixture()
def chart_copy(tmp_path: Path) -> Path:
    dest = tmp_path / "audittrace"
    shutil.copytree(CHART_DIR, dest)
    return dest


def _edit(path: Path, old: str | re.Pattern[str], new: str) -> None:
    text = path.read_text()
    patched = (
        old.sub(new, text) if isinstance(old, re.Pattern) else text.replace(old, new)
    )
    assert patched != text, "neuter did not change the file: the test is vacuous"
    path.write_text(patched)


IMAGE_LINE_RE = re.compile(r'^( *)image: \{\{ include "common\.images\.image".*$', re.M)


def _line_of(chart: Path) -> int:
    for n, line in enumerate((chart / JOB_TEMPLATE).read_text().splitlines(), 1):
        if 'default "16"' in line:
            return n
    raise AssertionError("no fallback line")


def test_neuter_readd_default_16_is_red(chart_copy: Path) -> None:
    """Old fallback re-added: render stays green (dead default); scan is RED."""
    assert fallback_hits(chart_copy) == []
    _edit(
        chart_copy / JOB_TEMPLATE,
        IMAGE_LINE_RE,
        r'\1image: "{{ $pgImage.registry }}/{{ $pgImage.repository }}:{{ $pgImage.tag | default "16" }}"',
    )
    assert fallback_hits(chart_copy) == [
        f"{JOB_TEMPLATE.as_posix()}:{_line_of(chart_copy)}"
    ]


def test_neuter_job_only_values_override_is_red(chart_copy: Path) -> None:
    """Job reads an independent key; a values file overriding only it is RED."""
    _edit(
        chart_copy / JOB_TEMPLATE,
        IMAGE_LINE_RE,
        r'\1image: "{{ $pgImage.registry }}/{{ $pgImage.repository }}'
        r':{{ .Values.postgresql.jobTag | default $pgImage.tag }}"',
    )
    assert violations(render(chart_copy)) == []  # independent key unset: one image
    only_job = chart_copy / "values-job-only.yaml"
    only_job.write_text("postgresql:\n  jobTag: '16'\n")
    docs = render(chart_copy, ["-f", str(only_job)])
    assert violations(docs) == [
        f"Job/audittrace-ensure-summariser-role/psql={PINNED_REPO}:16"
    ]


def test_neuter_hardcoded_job_image_is_red(chart_copy: Path) -> None:
    _edit(
        chart_copy / JOB_TEMPLATE,
        IMAGE_LINE_RE,
        r'\1image: "docker.io/bitnamilegacy/postgresql:17.6.0-debian-12-r4"',
    )
    assert len(violations(render(chart_copy))) == 1


def test_neuter_job_ignores_global_registry_is_red(chart_copy: Path) -> None:
    """B15: hand-assembled Job image ignores global.imageRegistry and diverges."""
    _edit(
        chart_copy / JOB_TEMPLATE,
        IMAGE_LINE_RE,
        r'\1image: "{{ $pgImage.registry }}/{{ $pgImage.repository }}:{{ $pgImage.tag }}"',
    )
    mirror = "registry.example.internal"
    docs = render(chart_copy, ["--set", f"global.imageRegistry={mirror}"])
    expected = f"{mirror}/lfdesousa/audittrace-postgresql:18.3-bitnami-frozen-apr17"
    assert violations(docs, expected) == [
        f"Job/audittrace-ensure-summariser-role/psql={PINNED}"
    ]


def test_neuter_second_container_before_the_pinned_one_is_red(chart_copy: Path) -> None:
    """B10: an off-image container ahead of the pinned psql container, same Job."""
    _edit(
        chart_copy / JOB_TEMPLATE,
        "        - name: psql\n",
        '        - name: sneaky\n          image: postgres:16\n          command: ["psql", "-c", "select 1"]\n'
        "        - name: psql\n",
    )
    assert violations(render(chart_copy)) == [
        "Job/audittrace-ensure-summariser-role/sneaky=postgres:16"
    ]


def test_neuter_values_ci_tag_16_is_red(tmp_path: Path) -> None:
    """B16: the Kind-smoke values file pinning another tag is caught by the sweep."""
    src = REPO / "tests" / "integration" / "fixtures" / "values-ci.yaml"
    bad = tmp_path / "values-ci.yaml"
    text = src.read_text()
    patched = text.replace("tag: 18.3-bitnami-frozen-apr17", 'tag: "16"')
    assert patched != text, "values-ci.yaml no longer pins the tag: update the neuter"
    bad.write_text(patched)
    docs = render(CHART_DIR, ["-f", str(bad)])
    assert len(violations(docs)) == 2


def test_neuter_tag_removed_is_red(chart_copy: Path) -> None:
    """Without the values.yaml tag the subchart default (17.6.0) leaks in."""
    _edit(chart_copy / "values.yaml", "    tag: 18.3-bitnami-frozen-apr17\n", "")
    bad = violations(render(chart_copy))
    assert len(bad) == 2 and all("17.6.0-debian-12-r4" in b for b in bad), bad


def test_neuter_required_removed_renders_garbage(chart_copy: Path) -> None:
    """With `required` stripped an empty key renders instead of failing."""
    _edit(
        chart_copy / JOB_TEMPLATE,
        re.compile(r'required "[^"]*" (\$pgImage\.\w+)'),
        r"\1",
    )
    docs = render(chart_copy, ["--set", "postgresql.image.registry="])
    assert violations(docs) != []


def test_neuter_scoped_insecure_images_opt_in_removed_fails_render(
    chart_copy: Path,
) -> None:
    """The non-Bitnami default image needs the (subchart-scoped) opt-in to render."""
    _edit(
        chart_copy / "values.yaml",
        "      allowInsecureImages: true\n",
        "      allowInsecureImages: false\n",
    )
    with pytest.raises(subprocess.CalledProcessError):
        render(chart_copy)


def test_pg_client_binary_on_another_image_is_detected() -> None:
    """Should-fix B3/B4: pg_dump / pg_isready on a non-postgres image is flagged."""
    doc = {
        "kind": "Job",
        "metadata": {"name": "x"},
        "spec": {
            "template": {
                "spec": {
                    "initContainers": [
                        {
                            "name": "wait",
                            "image": "os-shell:1",
                            "command": ["pg_isready"],
                        }
                    ],
                    "containers": [
                        {
                            "name": "dump",
                            "image": "pgclient:16",
                            "args": ["pg_dump", "db"],
                        }
                    ],
                }
            }
        },
    }
    assert postgres_images([doc]) == {
        ("Job", "x", "wait"): "os-shell:1",
        ("Job", "x", "dump"): "pgclient:16",
    }
