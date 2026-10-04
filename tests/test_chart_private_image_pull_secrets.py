"""Guard: every pod pulling a private ghcr.io mirror carries imagePullSecrets.

Origin: the v1.28.0 deploy preflight (2026-10-04). The four mirrors
(chroma, minio, mc, redis-exporter) are PRIVATE on ghcr.io; the chart wired
``global.imagePullSecrets`` into the subcharts and two Jobs but NOT into
``StatefulSet audittrace-chromadb``, ``StatefulSet audittrace-minio`` and
``Job audittrace-minio-bucket-init`` -> an anonymous pull (401) would have put
both stateful stores in ImagePullBackOff.

Design (each point is a falsifiability claim, proven by a test below):

* The set of "private-image pods" is DERIVED FROM THE RENDER (every pod
  template of every workload kind, containers AND initContainers), never listed
  by hand. An empty derivation fails (``test_empty_derivation_is_red``).
* The registry marked private is the ``ghcr.io/`` prefix (a superset of the
  required ``ghcr.io/lfdesousa/``; the chart uses ghcr.io for nothing else, so
  the broader prefix is the fail-closed choice).
* "Live-shaped" values: the deploy runner (scripts/deploy/runner) upgrades with
  ``--reset-then-reuse-values`` and carries no repo overlay for this key; the
  live release's user-supplied values contain
  ``global.imagePullSecrets: [{name: ghcr-pull-secret}]`` (``helm get values
  audittrace``). That exact shape is reproduced here with ``--set-json``.
* Each of the three templates is neutered INDIVIDUALLY (block stripped from a
  copy of the chart) and must turn the guard RED naming that pod.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parent.parent / "charts" / "audittrace"
PRIVATE_PREFIXES = ("ghcr.io/",)
POD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod"}
REQUIRED = {
    ("StatefulSet", "audittrace-chromadb"),
    ("StatefulSet", "audittrace-minio"),
    ("Job", "audittrace-minio-bucket-init"),
}
# Templates carrying the block, keyed by the pod each one renders.
TEMPLATE_FOR = {
    ("StatefulSet", "audittrace-chromadb"): "templates/chromadb/statefulset.yaml",
    ("StatefulSet", "audittrace-minio"): "templates/minio/statefulset.yaml",
    ("Job", "audittrace-minio-bucket-init"): "templates/minio/job-bucket-init.yaml",
}
LIVE_SHAPED = ["--set-json", 'global.imagePullSecrets=[{"name":"ghcr-pull-secret"}]']
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
BLOCK_RE = re.compile(
    r"^ *\{\{- with \.Values\.global\.imagePullSecrets \}\}\n.*?^ *\{\{- end \}\}\n",
    re.S | re.M,
)


def _render(chart: Path, extra: list[str], vault: bool = False) -> list[dict]:
    helm = shutil.which("helm")
    assert helm is not None, "helm is required (CI helm-lint job installs it)"
    cmd = [helm, "template", "audittrace", str(chart), "-n", "audittrace"]
    cmd += ["--set", f"vault.enabled={'true' if vault else 'false'}"]
    cmd += RENDER_ARGS + extra
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _pod_spec(doc: dict) -> dict | None:
    kind = doc.get("kind")
    if kind not in POD_KINDS:
        return None
    if kind == "Pod":
        return doc.get("spec") or {}
    spec = doc.get("spec") or {}
    if kind == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    return (spec.get("template") or {}).get("spec") or {}


def private_pods(docs: list[dict]) -> dict[tuple[str, str], dict]:
    """Derive {(kind, name): podspec} for pods with any private-registry image."""
    found: dict[tuple[str, str], dict] = {}
    for doc in docs:
        spec = _pod_spec(doc)
        if spec is None:
            continue
        containers = (spec.get("containers") or []) + (spec.get("initContainers") or [])
        if any(
            str(c.get("image", "")).startswith(PRIVATE_PREFIXES) for c in containers
        ):
            found[(doc["kind"], doc["metadata"]["name"])] = spec
    return found


def violations(docs: list[dict]) -> list[tuple[str, str]]:
    """Fail-closed check. Raises on an empty derivation (instrument is blind)."""
    pods = private_pods(docs)
    assert pods, "derivation found NO private-image pods: the guard is blind"
    return sorted(k for k, spec in pods.items() if not spec.get("imagePullSecrets"))


@pytest.fixture(scope="module", params=[False, True], ids=["vault-off", "vault-on"])
def live_docs(request: pytest.FixtureRequest) -> list[dict]:
    return _render(CHART_DIR, LIVE_SHAPED, vault=request.param)


def test_derived_set_contains_the_three_and_all_carry_secret(live_docs):
    pods = private_pods(live_docs)
    assert REQUIRED <= set(pods), f"derived {sorted(pods)} lacks a required pod"
    assert violations(live_docs) == []
    for spec in pods.values():
        assert spec["imagePullSecrets"] == [{"name": "ghcr-pull-secret"}]


def test_defaults_render_has_no_pull_secrets_and_is_noop():
    # Block is a no-op when global.imagePullSecrets is empty: no key emitted.
    docs = _render(CHART_DIR, [])
    pods = private_pods(docs)
    assert REQUIRED <= set(pods)
    for key in REQUIRED:
        assert "imagePullSecrets" not in pods[key]


@pytest.mark.parametrize("victim", sorted(TEMPLATE_FOR), ids=lambda k: k[1])
def test_neuter_each_template_is_red(victim, tmp_path):
    chart = tmp_path / "audittrace"
    shutil.copytree(CHART_DIR, chart)
    tpl = chart / TEMPLATE_FOR[victim]
    text = tpl.read_text()
    stripped, n = BLOCK_RE.subn("", text, count=1)
    assert n == 1, "neuter did not find the imagePullSecrets block"
    tpl.write_text(stripped)
    assert violations(_render(chart, LIVE_SHAPED)) == [victim]


def test_empty_derivation_is_red():
    with pytest.raises(AssertionError, match="blind"):
        violations([])
    # Registry mismatch (nothing private) must also be blind, not vacuously green.
    docs = [
        {
            "kind": "Deployment",
            "metadata": {"name": "x"},
            "spec": {
                "template": {"spec": {"containers": [{"image": "docker.io/a/b"}]}}
            },
        }
    ]
    with pytest.raises(AssertionError, match="blind"):
        violations(docs)


def test_derivation_covers_every_pod_kind_and_init_containers():
    def spec(images, init=()):
        return {
            "containers": [{"image": i} for i in images],
            "initContainers": [{"image": i} for i in init],
        }

    priv = "ghcr.io/lfdesousa/x:1"
    docs = [
        {"kind": "Deployment", "metadata": {"name": "d"}, "spec": {"template": {"spec": spec([priv])}}},
        {"kind": "DaemonSet", "metadata": {"name": "ds"}, "spec": {"template": {"spec": spec([priv])}}},
        {"kind": "Job", "metadata": {"name": "j"}, "spec": {"template": {"spec": spec([], [priv])}}},
        {
            "kind": "CronJob",
            "metadata": {"name": "cj"},
            "spec": {"jobTemplate": {"spec": {"template": {"spec": spec([priv])}}}},
        },
        {"kind": "Pod", "metadata": {"name": "p"}, "spec": spec([priv])},
        {"kind": "Service", "metadata": {"name": "s"}, "spec": {}},
    ]  # fmt: skip
    assert set(private_pods(docs)) == {
        ("Deployment", "d"),
        ("DaemonSet", "ds"),
        ("Job", "j"),
        ("CronJob", "cj"),
        ("Pod", "p"),
    }
    assert len(violations(docs)) == 5
