"""Vendored-contract pin check (option A, 2026-11-02).

``scripts/check-contracts-sync.sh`` verifies, offline and tokenless, that the
vendored ``contracts/v1.yaml`` still equals the sha256 recorded in its sibling
``v1.yaml.source`` pin. The public repo never fetches the private
content-control repo; the canonical-vs-vendored drift detector lives on the
private side.

Falsifiability (each guard has a test that fails when the guard is removed):

* the script skips the comparison  -> ``test_tampered_vendored_file_fails`` RED
* the script ignores the pin       -> ``test_pin_sha_mismatch_fails`` RED
* the script tolerates a bad pin   -> ``test_malformed_pin_fails`` RED
* the script tolerates no pin      -> ``test_missing_pin_fails`` RED
* a fetch / token creeps back in   -> ``test_no_network_no_token_*`` RED
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests._hermetic_env import hermetic_env

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check-contracts-sync.sh"
VENDORED = "docs/reference/content-control/contracts/v1.yaml"
PIN = VENDORED + ".source"
WORKFLOW = ROOT / ".github" / "workflows" / "contract-sync.yml"

BODY = b"openapi: 3.0.0\ninfo: {title: fixture}\n"
COMMIT = "a" * 40


def _good_pin(sha: str) -> str:
    return f"# fixture pin\nrepo=Owner/some-repo\ncommit={COMMIT}\nsha256={sha}\n"


def _make_root(
    tmp_path: Path, *, vendored: bytes | None = BODY, pin: str | None = ""
) -> Path:
    """Build a fixture root. ``pin=""`` means the correct pin for ``vendored``."""
    root = tmp_path / "root"
    (root / Path(VENDORED).parent).mkdir(parents=True)
    if vendored is not None:
        (root / VENDORED).write_bytes(vendored)
    if pin == "":
        pin = _good_pin(hashlib.sha256(BODY).hexdigest())
    if pin is not None:
        (root / PIN).write_text(pin)
    return root


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    env = hermetic_env(
        CONTRACTS_SYNC_ROOT=str(root),
        # A fetch/token path must not be reachable: blank the old inputs.
        GH_TOKEN="",
        GITHUB_TOKEN="",
    )
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def test_good_pin_passes(tmp_path: Path) -> None:
    res = _run(_make_root(tmp_path))
    assert res.returncode == 0, res.stderr
    assert "matches its pin" in res.stdout


def test_tampered_vendored_file_fails(tmp_path: Path) -> None:
    res = _run(_make_root(tmp_path, vendored=BODY + b"# tampered\n"))
    assert res.returncode == 1
    assert "MISMATCH" in res.stderr


def test_pin_sha_mismatch_fails(tmp_path: Path) -> None:
    pin = _good_pin(hashlib.sha256(b"something else").hexdigest())
    res = _run(_make_root(tmp_path, pin=pin))
    assert res.returncode == 1
    assert "MISMATCH" in res.stderr


def test_missing_pin_fails(tmp_path: Path) -> None:
    res = _run(_make_root(tmp_path, pin=None))
    assert res.returncode == 3
    assert "pin file missing" in res.stderr


def test_missing_vendored_file_fails(tmp_path: Path) -> None:
    res = _run(_make_root(tmp_path, vendored=None))
    assert res.returncode == 2
    assert "vendored copy missing" in res.stderr


_SHA = hashlib.sha256(BODY).hexdigest()


@pytest.mark.parametrize(
    ("pin", "needle"),
    [
        ("# only a comment\n", "repo must be"),
        (f"repo=Owner/r\ncommit={COMMIT}\n", "sha256 must be"),
        (f"repo=Owner/r\nsha256={_SHA}\n", "commit must be"),
        (f"commit={COMMIT}\nsha256={_SHA}\n", "repo must be"),
        (f"repo=Owner/r\ncommit=97588dc\nsha256={_SHA}\n", "commit must be"),
        (f"repo=Owner/r\ncommit={COMMIT}\nsha256={_SHA[:-1]}\n", "sha256 must be"),
        (f"repo=Owner/r\ncommit={COMMIT}\nsha256={_SHA.upper()}\n", "sha256 must be"),
        (f"repo=no-slash\ncommit={COMMIT}\nsha256={_SHA}\n", "repo must be"),
        (f"repo=Owner/r\ncommit={COMMIT}\nsha256={_SHA}\nextra=1\n", "unrecognised"),
        (f"repo=Owner/r\ncommit={COMMIT}\nsha256={_SHA}\nrepo=O/x\n", "duplicate repo"),
        (
            f"repo=Owner/r\ncommit={COMMIT}\ncommit={COMMIT}\nsha256={_SHA}\n",
            "duplicate commit",
        ),
        (
            f"repo=Owner/r\ncommit={COMMIT}\nsha256={_SHA}\nsha256={_SHA}\n",
            "duplicate sha256",
        ),
    ],
)
def test_malformed_pin_fails(tmp_path: Path, pin: str, needle: str) -> None:
    res = _run(_make_root(tmp_path, pin=pin))
    assert res.returncode == 4, (res.stdout, res.stderr)
    assert "pin malformed" in res.stderr
    assert needle in res.stderr


def test_pin_without_trailing_newline_is_parsed(tmp_path: Path) -> None:
    pin = _good_pin(_SHA).rstrip("\n")
    assert _run(_make_root(tmp_path, pin=pin)).returncode == 0


def test_committed_pin_matches_committed_vendored_file() -> None:
    """The real tree: the shipped vendored file equals the shipped pin."""
    res = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert res.returncode == 0, res.stderr
    pin = (ROOT / PIN).read_text()
    assert re.search(r"^repo=AllaboutdataEu/audittrace-content-control$", pin, re.M)
    assert re.search(r"^commit=[0-9a-f]{40}$", pin, re.M)


def test_no_network_no_token_in_script_or_workflow() -> None:
    """Public CI never reaches the private repo: no fetch, no token, no old slug."""
    script = SCRIPT.read_text()
    workflow = WORKFLOW.read_text()
    for blob in (script, workflow):
        assert "CONTRACT_SYNC_TOKEN" not in blob
        assert "lfdesousa/audittrace-content-control" not in blob
        assert "raw.githubusercontent.com" not in blob
    code = "\n".join(
        ln for ln in script.splitlines() if not ln.lstrip().startswith("#")
    )
    assert not re.search(r"\b(curl|wget)\b", code)
    wf = yaml.safe_load(workflow)
    assert "secrets." not in workflow.replace("# ", "#").split("\njobs:")[1]
    for job in wf["jobs"].values():
        for step in job["steps"]:
            assert "env" not in step, "no token/env wiring on the pin check"


def test_workflow_triggers_are_pr_and_push_only() -> None:
    wf = yaml.safe_load(WORKFLOW.read_text())
    triggers = wf.get("on", wf.get(True))
    assert set(triggers) == {"pull_request", "push"}
    assert wf["permissions"] == {"contents": "read"}
