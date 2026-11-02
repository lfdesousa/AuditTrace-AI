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
* a fetch / token creeps back in   -> ``test_workflow_*`` / ``test_script_*`` RED
* a symlink is accepted             -> ``test_symlink_*`` RED
* the duplicate check fails open   -> ``test_malformed_pin_fails`` empty-first cases RED
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
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
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
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
        # F1: an EMPTY first value must still count as seen.
        (f"repo=\nrepo=a/b\ncommit={COMMIT}\nsha256={_SHA}\n", "duplicate repo"),
        (
            f"repo=Owner/r\ncommit=\ncommit={COMMIT}\nsha256={_SHA}\n",
            "duplicate commit",
        ),
        (
            f"repo=Owner/r\ncommit={COMMIT}\nsha256=\nsha256={_SHA}\n",
            "duplicate sha256",
        ),
        (f"repo=../..x\ncommit={COMMIT}\nsha256={_SHA}\n", "repo must be"),
        (f"repo=Owner/.hidden\ncommit={COMMIT}\nsha256={_SHA}\n", "repo must be"),
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


def test_symlinked_vendored_file_is_refused(tmp_path: Path) -> None:
    """F2: git would publish the link text, not the contract."""
    root = _make_root(tmp_path)
    real = tmp_path / "elsewhere.yaml"
    real.write_bytes(BODY)  # same content: sha256 would match if followed
    (root / VENDORED).unlink()
    (root / VENDORED).symlink_to(real)
    res = _run(root)
    assert res.returncode == 5, (res.stdout, res.stderr)
    assert "vendored copy is a symlink" in res.stderr


def test_symlinked_pin_file_is_refused(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    real = tmp_path / "elsewhere.source"
    real.write_text(_good_pin(_SHA))
    (root / PIN).unlink()
    (root / PIN).symlink_to(real)
    res = _run(root)
    assert res.returncode == 5, (res.stdout, res.stderr)
    assert "pin file is a symlink" in res.stderr


def test_committed_files_are_regular_files() -> None:
    assert not (ROOT / VENDORED).is_symlink()
    assert not (ROOT / PIN).is_symlink()


def _strip_comments(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _keys(node: object) -> set[str]:
    """Every mapping key anywhere in a parsed YAML tree."""
    found: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(str(k))
            found |= _keys(v)
    elif isinstance(node, list):
        for v in node:
            found |= _keys(v)
    return found


def test_workflow_is_exactly_the_offline_pin_check() -> None:
    """F3: no env at ANY level, no secrets/vars/ROOT, one exact run line."""
    text = WORKFLOW.read_text()
    code = _strip_comments(text)
    for banned in (
        "secrets.",
        "vars.",
        "CONTRACTS_SYNC_ROOT",
        "${{",
        "CONTRACT_SYNC_TOKEN",
    ):
        assert banned not in code, banned
    assert "lfdesousa/audittrace-content-control" not in text
    wf = yaml.safe_load(text)
    assert "env" not in _keys(wf), "no env at workflow, job or step level"
    # PyYAML parses the bare key ``on`` as boolean True.
    top = {"on" if k is True else k for k in wf}
    assert top == {"name", "on", "permissions", "jobs"}
    assert wf["permissions"] == {"contents": "read"}
    (job,) = wf["jobs"].values()
    assert set(job) == {"runs-on", "steps"}
    assert job["steps"] == [
        {"uses": "actions/checkout@v6"},
        {
            "name": "Verify vendored contract against its pin",
            "run": "./scripts/check-contracts-sync.sh",
        },
    ]


def test_workflow_triggers_are_pr_and_push_only() -> None:
    wf = yaml.safe_load(WORKFLOW.read_text())
    triggers = wf.get("on", wf.get(True))
    assert set(triggers) == {"pull_request", "push"}


_NETWORK_TOKENS = re.compile(
    r"(?<![\w./-])(curl|wget|gh|nc|ncat|socat|ssh|scp|sftp|telnet|python3?|perl|ruby"
    r"|node|php|lua|openssl)(?![\w-])"
    r"|\bgit\s+(fetch|clone|ls-remote|pull|remote)\b|/dev/(tcp|udp)/|https?://",
)


def test_script_code_has_no_network_capable_command() -> None:
    """F3: broad denylist over the script's non-comment text."""
    hits = _NETWORK_TOKENS.findall(_strip_comments(SCRIPT.read_text()))
    assert not hits, hits


def test_script_runs_with_only_an_allowlist_of_commands(tmp_path: Path) -> None:
    """F3: the script succeeds with ONLY dirname/sha256sum/awk reachable on PATH
    (bash itself is invoked by absolute path), so it cannot shell out to a
    network tool; and, where the kernel allows it, also inside an empty
    network namespace. No skip: the namespace leg is simply additional."""
    shims = tmp_path / "bin"
    shims.mkdir()
    for tool in ("dirname", "sha256sum", "awk"):
        found = shutil.which(tool)
        assert found, tool
        (shims / tool).symlink_to(found)
    bash = shutil.which("bash")
    assert bash
    # The hermetic guard requires its own shim dir at PATH[0] (it holds only
    # kubectl/helm shims); the allowlist dir follows and nothing else is on PATH.
    base = hermetic_env(GH_TOKEN="", GITHUB_TOKEN="")
    env = hermetic_env(
        PATH=os.pathsep.join([base["PATH"].split(os.pathsep)[0], str(shims)]),
        GH_TOKEN="",
        GITHUB_TOKEN="",
    )
    res = subprocess.run(
        [bash, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    unshare = shutil.which("unshare")
    if unshare:
        probe = subprocess.run(
            [unshare, "-rn", "true"], capture_output=True, check=False, timeout=60
        )
        if probe.returncode == 0:
            netns = subprocess.run(
                [unshare, "-rn", bash, str(SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            assert netns.returncode == 0, netns.stderr
