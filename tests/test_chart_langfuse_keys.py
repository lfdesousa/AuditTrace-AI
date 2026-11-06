"""WP-13 -- the Langfuse key pair is never a chart value, a script literal or a
tracked-file literal.

Hermetic: ``helm template`` only (a chart-only invocation the hermetic guard
allows) plus ``git ls-files``; no ``kubectl``, no cluster, no Vault. Pinned:

* **Both delivery branches** (``memoryServer.langfuseKeys.vaultSecret``, default
  false): the four cells ``vault.enabled`` x flag render exactly what ADDENDUM-3
  B1 measured -- ``valueFrom.secretKeyRef`` on the operator-created Secret in
  three cells, the env entries ABSENT and a ``with secret
  "kv/data/audittrace/langfuse/keys"`` Vault Agent block in the fourth; the
  policy line (``langfuse/*``) renders in both vault cells; the Kind-smoke
  values (Langfuse off) render no key env at all.
* **No literal anywhere**: no rendered string of the Langfuse key shape in any
  cell; no ``LANGFUSE_*_KEY`` env entry with a literal ``value``; each env name
  at most once per container; ``FASTAPI_OTEL_AUTO_CONFIGURE`` absent.
* **The two rollback/forward overlays** (one key each) behave as named.
* **Repo-wide re-introduction guard**: every tracked file is scanned for the
  Langfuse key shape (no exclusion list); fail closed on an empty scan;
  positive control on a ``tmp_path`` file.

Falsifiability (each alone, RED then restored): a key-shaped literal in
``memoryServer.env`` (cell test), the same planted in ``scripts/`` (repo scan),
the Langfuse gate dropped from the helper (Kind row), the ``with secret`` block
without the policy line (policy test), the overlay edited (overlay pin).

Keys in this file are assembled at runtime (ADDENDUM-2 S1); nothing key-shaped
is committed. Mocks: none (helm and git are real, read-only).
"""

from __future__ import annotations

import functools
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "charts" / "audittrace"
CI_VALUES = REPO_ROOT / "tests" / "integration" / "fixtures" / "values-ci.yaml"
KV_PATH = "kv/data/audittrace/langfuse/keys"
SECRET_NAME = "audittrace-langfuse"
KEY_ENVS = ("AUDITTRACE_LANGFUSE_PUBLIC_KEY", "AUDITTRACE_LANGFUSE_SECRET_KEY")

#: the Langfuse key shape: ``pk-lf-<uuid>`` / ``sk-lf-<uuid>``
KEY_SHAPE = re.compile(
    rb"[ps]k-lf-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Throwaway placeholders + FQDN-only host fields the chart needs to render.
_BASE = (
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
)  # fmt: skip


def runtime_key(prefix: str) -> str:
    """A key of the real shape assembled at runtime (never a source literal)."""
    return prefix + "-lf-" + str(uuid.uuid4())


@functools.cache
def _render(args: tuple[str, ...]) -> tuple[str, tuple[dict[str, Any], ...]]:
    helm = shutil.which("helm")
    assert helm is not None, "helm is required (the CI `test` job uses the runner's)"
    proc = subprocess.run(  # noqa: S603 - fixed argv, chart-only subcommand
        [helm, "template", "audittrace", str(CHART_DIR), "-n", "audittrace", *_BASE, *args],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr[-1500:]
    docs = tuple(d for d in yaml.safe_load_all(proc.stdout) if d)
    assert docs, "empty render"
    return proc.stdout, docs


def _memory_server(docs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    (dep,) = [
        d
        for d in docs
        if d.get("kind") == "Deployment"
        and d["metadata"]["name"].endswith("memory-server")
    ]
    return dep  # type: ignore[no-any-return]


def _env(dep: dict[str, Any]) -> list[dict[str, Any]]:
    return dep["spec"]["template"]["spec"]["containers"][0]["env"]  # type: ignore[no-any-return]


def _lf_env(dep: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["name"]: e for e in _env(dep) if e["name"] in KEY_ENVS}


def _env_template(dep: dict[str, Any]) -> str:
    ann = dep["spec"]["template"]["metadata"].get("annotations", {})
    return str(ann.get("vault.hashicorp.com/agent-inject-template-env", ""))


def _server_hcl(docs: Iterable[dict[str, Any]]) -> str:
    (cm,) = [
        d
        for d in docs
        if d.get("kind") == "ConfigMap"
        and d["metadata"]["name"].endswith("-vault-policies")
    ]
    return str(cm["data"]["audittrace-server.hcl"])


_HCL_PATH = re.compile(r'^\s*path\s+"([^"]+)"\s*\{', re.MULTILINE)
_WITH_SECRET = re.compile(r'with secret "([^"]+)"')


def _hcl_paths(hcl: str) -> list[str]:
    """The ``path "..."`` entries of a rendered Vault policy (HCL)."""
    return _HCL_PATH.findall(hcl)


def _covered(secret_path: str, policy_paths: Iterable[str]) -> bool:
    """Vault policy semantics, minimal: an exact match, or a policy path ending in
    ``*`` whose prefix (the path minus the ``*``) the secret path starts with."""
    for pol in policy_paths:
        if pol == secret_path:
            return True
        if pol.endswith("*") and secret_path.startswith(pol[:-1]):
            return True
    return False


F, T = "false", "true"


def _cell(vault: str, flag: str) -> tuple[str, ...]:
    return (
        "--set", f"vault.enabled={vault}",
        "--set", f"memoryServer.langfuseKeys.vaultSecret={flag}",
    )  # fmt: skip


CELLS = {
    "vault=false,flag=false": _cell(F, F),
    "vault=false,flag=true": _cell(F, T),
    "vault=true,flag=false": _cell(T, F),
    "vault=true,flag=true": _cell(T, T),
}


def _secret_refs(dep: dict[str, Any]) -> dict[str, str]:
    out = {}
    for name, entry in _lf_env(dep).items():
        assert "value" not in entry, f"{name} must not carry a literal value"
        ref = entry["valueFrom"]["secretKeyRef"]
        assert "optional" not in ref, "optional must stay unset (fail visibly)"
        out[name] = f"{ref['name']}/{ref['key']}"
    return out


SECRET_REFS = {
    "AUDITTRACE_LANGFUSE_PUBLIC_KEY": f"{SECRET_NAME}/public_key",
    "AUDITTRACE_LANGFUSE_SECRET_KEY": f"{SECRET_NAME}/secret_key",
}


@pytest.mark.parametrize(
    ("cell", "from_vault"),
    [
        ("vault=false,flag=false", False),
        ("vault=false,flag=true", False),  # the flag is meaningless without Vault
        ("vault=true,flag=false", False),
        ("vault=true,flag=true", True),
    ],
)
def test_four_cells_render_the_measured_shape(cell: str, from_vault: bool) -> None:
    stdout, docs = _render(CELLS[cell])
    dep = _memory_server(docs)
    template = _env_template(dep)
    if from_vault:
        assert _lf_env(dep) == {}, "env entries must be absent from the Deployment"
        assert f'with secret "{KV_PATH}"' in template
        assert (
            "AUDITTRACE_LANGFUSE_PUBLIC_KEY='{{ .Data.data.public_key }}'" in template
        )
        assert (
            "AUDITTRACE_LANGFUSE_SECRET_KEY='{{ .Data.data.secret_key }}'" in template
        )
    else:
        assert _secret_refs(dep) == SECRET_REFS
        assert KV_PATH not in template
    assert not KEY_SHAPE.search(stdout.encode()), "key-shaped string in the render"
    assert "FASTAPI_OTEL_AUTO_CONFIGURE" not in stdout


def test_hcl_parser_finds_the_existing_policy_paths() -> None:
    """Positive control: the parser is not vacuous -- it finds the chart's real
    policy paths, the Langfuse one among them, and the coverage predicate is
    exact on the cases that matter (a wrong prefix is NOT covered)."""
    _, docs = _render(CELLS["vault=true,flag=false"])
    paths = _hcl_paths(_server_hcl(docs))
    assert len(paths) >= 3, paths
    assert KV_PATH.replace("/keys", "/*") in paths
    assert _covered(KV_PATH, paths)
    assert _covered("kv/data/a/b", ["kv/data/a/b"])  # exact
    assert _covered("kv/data/a/b", ["kv/data/a/*"])  # prefix glob
    assert not _covered(
        "kv/data/audittrace/langfuse/keys", ["kv/data/audittrace-x/langfuse/*"]
    )
    assert not _covered("kv/data/a/b", ["kv/data/a/c"])  # near-miss exact
    assert not _covered("kv/data/a/b", [])


@pytest.mark.parametrize("cell", ["vault=true,flag=false", "vault=true,flag=true"])
def test_policy_line_renders_in_both_vault_cells(cell: str) -> None:
    _, docs = _render(CELLS[cell])
    assert _covered(KV_PATH, _hcl_paths(_server_hcl(docs)))


@pytest.mark.parametrize("cell", list(CELLS))
def test_every_with_secret_path_has_a_policy_line(cell: str) -> None:
    """Falsifiability of the HCL line: a ``with secret`` block without its policy
    path would be refused by Vault at pass 2 (the agent cannot read it). Checked
    against the PARSED ``path "..."`` entries (exact or ``*``-prefix), so a policy
    line under a wrong mount/prefix is not covered (reviewer neuter R-4)."""
    _, docs = _render(CELLS[cell])
    if not cell.startswith("vault=true"):
        # vault=false renders no policies ConfigMap at all
        assert not [
            d for d in docs if d["metadata"]["name"].endswith("-vault-policies")
        ]
        return
    template = _env_template(_memory_server(docs))
    policy_paths = _hcl_paths(_server_hcl(docs))
    secret_paths = _WITH_SECRET.findall(template)
    assert (KV_PATH in secret_paths) == cell.endswith("flag=true")
    for path in secret_paths:
        assert _covered(path, policy_paths), f"no policy line for {path}"


@pytest.mark.parametrize("cell", list(CELLS))
def test_no_literal_key_env_and_no_duplicate_env_names(cell: str) -> None:
    stdout, docs = _render(CELLS[cell])
    for doc in docs:
        spec = (doc.get("spec") or {}).get("template", {}).get("spec", {})
        for container in spec.get("containers", []):
            names = [e["name"] for e in container.get("env", [])]
            assert len(names) == len(set(names)), (
                f"duplicate env in {doc['metadata']['name']}"
            )
            for e in container.get("env", []):
                if re.fullmatch(r"AUDITTRACE_LANGFUSE_.*_KEY", e["name"]):
                    assert "value" not in e
    assert not KEY_SHAPE.search(stdout.encode())


def test_secret_name_is_parameterised() -> None:
    _, docs = _render(
        (*_cell(F, F), "--set", "memoryServer.langfuseKeys.secretName=other-lf")
    )
    refs = _secret_refs(_memory_server(docs))
    assert refs == {
        "AUDITTRACE_LANGFUSE_PUBLIC_KEY": "other-lf/public_key",
        "AUDITTRACE_LANGFUSE_SECRET_KEY": "other-lf/secret_key",
    }


def test_langfuse_off_renders_no_key_env() -> None:
    """The Kind smoke (`values-ci.yaml`: vault false, Langfuse disabled) has no such
    Secret: an ungated ``secretKeyRef`` would wedge ``helm install --wait``."""
    stdout, docs = _render(("-f", str(CI_VALUES)))
    assert _lf_env(_memory_server(docs)) == {}
    assert not KEY_SHAPE.search(stdout.encode())
    _, docs_off = _render(
        (*_cell(F, F), "--set", "memoryServer.env.AUDITTRACE_LANGFUSE_ENABLED=false")
    )
    assert _lf_env(_memory_server(docs_off)) == {}


# ── the overlays ────────────────────────────────────────────────────────────

OFF = CHART_DIR / "values-langfuse-vault-off.yaml"
ON = CHART_DIR / "values-langfuse-vault-on.yaml"


@pytest.mark.parametrize(("path", "flag"), [(OFF, False), (ON, True)])
def test_overlays_are_exactly_one_key(path: Path, flag: bool) -> None:
    assert yaml.safe_load(path.read_text()) == {
        "memoryServer": {"langfuseKeys": {"vaultSecret": flag}}
    }


def test_overlays_drive_pass_two_and_the_rollback() -> None:
    on_args = ("--set", "vault.enabled=true", "-f", str(ON))
    off_after_on = ("--set", "vault.enabled=true", "-f", str(ON), "-f", str(OFF))
    dep_on = _memory_server(_render(on_args)[1])
    assert _lf_env(dep_on) == {} and KV_PATH in _env_template(dep_on)
    # the sanctioned rollback: the -off overlay LAST wins over a stored true
    dep_back = _memory_server(_render(off_after_on)[1])
    assert _secret_refs(dep_back) == SECRET_REFS and KV_PATH not in _env_template(
        dep_back
    )


def test_flag_defaults_to_false_and_values_carry_no_key() -> None:
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
    ms = values["memoryServer"]
    assert ms["langfuseKeys"]["vaultSecret"] is False
    assert ms["langfuseKeys"]["secretName"] == SECRET_NAME
    assert not set(KEY_ENVS) & set(ms["env"]), "keys must not be chart values"
    assert ms["env"]["AUDITTRACE_LANGFUSE_ENABLED"] == "true"


def test_setup_vault_seeds_the_path_the_template_reads() -> None:
    script = (REPO_ROOT / "scripts" / "setup-vault.sh").read_text()
    seeded = re.findall(r"^seed_kv (\S+) ", script, re.MULTILINE)
    assert "langfuse/keys" in seeded, seeded
    # the template reads KV v2 data path kv/data/audittrace/<p>; the seed writes
    # kv/audittrace/<p> (mount kv, KV v2 inserts /data/)
    assert f"kv/data/audittrace/{seeded[seeded.index('langfuse/keys')]}" == KV_PATH
    line = next(
        ln for ln in script.splitlines() if ln.startswith("seed_kv langfuse/keys ")
    )
    assert "public_key=langfuse-public-key.txt" in line
    assert "secret_key=langfuse-secret-key.txt" in line


def test_airplane_script_reads_keys_from_env_or_private_file() -> None:
    text = (REPO_ROOT / "scripts" / "phase4-airplane-test.sh").read_text()
    assert 'LF_PK="${LANGFUSE_PUBLIC_KEY:-}"' in text
    assert 'LF_SK="${LANGFUSE_SECRET_KEY:-}"' in text
    assert "langfuse-public-key.txt" in text and "langfuse-secret-key.txt" in text
    assert "$LF_SECRETS_DIR" in text
    bash = shutil.which("bash")
    assert bash is not None
    proc = subprocess.run(  # noqa: S603 - fixed argv, syntax check only
        [bash, "-n", str(REPO_ROOT / "scripts" / "phase4-airplane-test.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


# ── the repo-wide re-introduction guard ─────────────────────────────────────


def tracked_files(root: Path = REPO_ROOT) -> list[Path]:
    git = shutil.which("git")
    assert git is not None, "git is required for the tracked-file scan"
    proc = subprocess.run(  # noqa: S603 - fixed argv, read-only
        [git, "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        check=True,
    )
    return [root / n.decode() for n in proc.stdout.split(b"\0") if n]


def scan(paths: Iterable[Path]) -> tuple[int, list[str]]:
    """(files scanned, files holding a key-shaped string) -- no exclusion list."""
    scanned, hits = 0, []
    for path in paths:
        if not path.is_file():  # a tracked path deleted in the working tree
            continue
        scanned += 1
        if KEY_SHAPE.search(path.read_bytes()):
            hits.append(str(path))
    return scanned, hits


def test_no_tracked_file_holds_a_langfuse_key() -> None:
    scanned, hits = scan(tracked_files())
    assert scanned > 100, (
        f"scan covered {scanned} files; refusing to pass (fail closed)"
    )
    assert hits == []


def test_scan_positive_control_flags_a_planted_key(tmp_path: Path) -> None:
    planted = tmp_path / "planted.txt"
    planted.write_text("token = " + runtime_key("sk") + "\n")
    clean = tmp_path / "clean.txt"
    clean.write_text("pk-lf-placeholder and sk-lf-test\n")
    scanned, hits = scan([planted, clean])
    assert scanned == 2
    assert hits == [str(planted)]


def test_scan_counts_zero_files_for_an_empty_repo(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603 - throwaway repo under tmp_path
        [git, "init", "-q", str(tmp_path)], check=True, capture_output=True
    )
    assert scan(tracked_files(tmp_path)) == (0, [])  # the caller's >100 floor is RED
