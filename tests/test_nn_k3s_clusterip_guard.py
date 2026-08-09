"""Tests for the installed shell entrypoint ``scripts/network/NN-k3s-clusterip-guard``.

REJECT finding (2026-08-09 independent review of SPEC #443 v2): the wrapper's
documented install method is a **copy** (``sudo cp ... /etc/NetworkManager/
dispatcher.d/90-k3s-clusterip-guard``), but its path resolution used
``readlink -f "${BASH_SOURCE[0]}"`` + ``../..`` — which only resolves back
into the repo when the installed file is a SYMLINK, not a copy. Under the
documented ``cp`` install, that walk-up lands on ``/etc``, so the wrapper
execs a Python module that never exists there and every NetworkManager event
silently no-ops. ``grep -rn "NN-k3s-clusterip-guard" tests/`` returned ZERO
hits before this file — the installed entrypoint had no coverage at all.

This file closes both gaps: it exercises the ACTUAL shell file (never a
paraphrase of it) the way NetworkManager would (``$1``=interface,
``$2``=action, invoked as a plain executable), under a install layout that
reproduces the documented ``cp`` method — the copy is placed in a directory
tree that is NOT inside this repo checkout, so ``BASH_SOURCE``-relative
resolution cannot accidentally succeed by virtue of running the test inside
the real repo.

Falsifiability: every "it locates the module" assertion below checks for a
MARKER FILE the stub Python module writes on invocation, not just the
wrapper's exit code — a wrapper that fails closed (exit 0, no-op, per its own
new safety branch) and a wrapper that genuinely located and ran the module
both exit 0, so exit code alone cannot tell them apart. If path resolution
regresses to the broken ``readlink -f "${BASH_SOURCE[0]}"``/``../..`` form,
the marker file is never written and every "locates the module" test here
fails.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

WRAPPER = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "network"
    / "NN-k3s-clusterip-guard"
)

_STUB_MODULE_SOURCE = """\
import json
import os
import sys

marker = os.environ["TEST_MARKER_FILE"]
with open(marker, "w", encoding="utf-8") as fh:
    json.dump({"argv": sys.argv[1:]}, fh)
sys.exit(int(os.environ.get("TEST_STUB_EXIT_CODE", "0")))
"""


def _install_wrapper_copy(tmp_path: Path) -> Path:
    """Copy (not symlink) the REAL wrapper into a dir tree outside the repo.

    Mirrors the documented ``sudo cp ... /etc/NetworkManager/dispatcher.d/
    90-k3s-clusterip-guard`` install: the resulting file lives two directory
    levels below an unrelated tmp root, so a ``../..`` walk-up from its own
    location lands outside ``tmp_path`` entirely — never back at the repo,
    and never (by construction) at ``fake_repo`` either.
    """
    dispatcher_d = tmp_path / "etc" / "NetworkManager" / "dispatcher.d"
    dispatcher_d.mkdir(parents=True)
    installed = dispatcher_d / "90-k3s-clusterip-guard"
    installed.write_bytes(WRAPPER.read_bytes())
    installed.chmod(0o755)
    return installed


def _install_stub_repo(tmp_path: Path, *, exit_code: int = 0) -> Path:
    """Build a fake repo checkout containing only the dispatcher module path
    the wrapper execs, so these tests never invoke the REAL Python dispatcher
    (which has its own, separately-tested, real-world side effects).
    """
    fake_repo = tmp_path / "fake-repo"
    module_dir = fake_repo / "scripts" / "network"
    module_dir.mkdir(parents=True)
    (module_dir / "k3s_clusterip_dispatcher.py").write_text(_STUB_MODULE_SOURCE)
    return fake_repo


def _run_wrapper(
    tmp_path: Path,
    interface: str,
    action: str,
    *,
    repo_dir: Path | None,
    exit_code: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    installed = _install_wrapper_copy(tmp_path)
    _install_stub_repo(tmp_path, exit_code=exit_code)
    marker = tmp_path / "marker.json"
    env = {
        "PATH": "/usr/bin:/bin",
        "TEST_MARKER_FILE": str(marker),
        "TEST_STUB_EXIT_CODE": str(exit_code),
    }
    if repo_dir is not None:
        env["AUDITTRACE_REPO_DIR"] = str(repo_dir)
    proc = subprocess.run(
        ["bash", str(installed), interface, action],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc, marker


# ── locates the module under the documented cp-install layout ──────────────


def test_wrapper_locates_module_via_env_var_for_relevant_action(tmp_path: Path) -> None:
    fake_repo = tmp_path / "fake-repo"
    proc, marker = _run_wrapper(tmp_path, "wlan0", "up", repo_dir=fake_repo)

    assert proc.returncode == 0, proc.stderr
    assert marker.exists(), (
        "stub module was never invoked — path resolution failed to find it "
        f"under AUDITTRACE_REPO_DIR (stderr: {proc.stderr!r})"
    )
    assert json.loads(marker.read_text()) == {"argv": ["wlan0", "up"]}


def test_wrapper_locates_module_via_env_var_for_irrelevant_action(
    tmp_path: Path,
) -> None:
    # The wrapper itself does not filter by action — that policy lives in
    # HANDLED_ACTIONS inside the Python dispatcher. The wrapper must locate
    # and invoke the module regardless of which action NM fired.
    fake_repo = tmp_path / "fake-repo"
    proc, marker = _run_wrapper(tmp_path, "eth0", "dhcp4-change", repo_dir=fake_repo)

    assert proc.returncode == 0, proc.stderr
    assert marker.exists()
    assert json.loads(marker.read_text()) == {"argv": ["eth0", "dhcp4-change"]}


def test_wrapper_propagates_nonzero_module_exit_code(tmp_path: Path) -> None:
    # A guard-detected-unhealthy-restart-failed outcome exits 1 (see
    # k3s_clusterip_dispatcher.main); the wrapper must not swallow it.
    fake_repo = tmp_path / "fake-repo"
    proc, marker = _run_wrapper(
        tmp_path, "wlan0", "up", repo_dir=fake_repo, exit_code=1
    )

    assert proc.returncode == 1
    assert marker.exists()


def test_wrapper_copy_location_cannot_reach_module_via_relative_walk_up(
    tmp_path: Path,
) -> None:
    """Structural proof the documented install layout defeats a
    BASH_SOURCE-relative resolution: two levels above the installed copy is
    NOT the fake repo and does not contain the module. If the wrapper ever
    regresses to resolving its module from its own location instead of
    ``AUDITTRACE_REPO_DIR``, this establishes why that regression is real
    (the other tests in this file are what actually catch it firing).
    """
    installed = _install_wrapper_copy(tmp_path)
    _install_stub_repo(tmp_path)
    walked_up = installed.parent.parent.parent
    assert not (
        walked_up / "scripts" / "network" / "k3s_clusterip_dispatcher.py"
    ).exists()


# ── fails closed (never crashes the NM event) when the module can't be found ─


def test_wrapper_fails_closed_when_repo_dir_has_no_module(tmp_path: Path) -> None:
    missing_repo = tmp_path / "nowhere"
    proc, marker = _run_wrapper(tmp_path, "wlan0", "up", repo_dir=missing_repo)

    assert proc.returncode == 0, proc.stderr
    assert not marker.exists(), "module must not have been invoked"
    assert "dispatcher module not found" in proc.stderr
    assert "AUDITTRACE_REPO_DIR" in proc.stderr


@pytest.mark.parametrize("argv", [[], ["wlan0"]])
def test_wrapper_tolerates_missing_positional_args(
    tmp_path: Path, argv: list[str]
) -> None:
    # NM always supplies both args, but the wrapper defaults them (`${1:-}`
    # `${2:-}`) rather than failing under `set -u` if ever invoked bare or
    # with only one arg — mirrors the Python dispatcher's own `nargs="?"`
    # tolerance for interface/action.
    fake_repo = tmp_path / "fake-repo"
    installed = _install_wrapper_copy(tmp_path)
    _install_stub_repo(tmp_path)
    marker = tmp_path / "marker.json"
    proc = subprocess.run(
        ["bash", str(installed), *argv],
        env={
            "PATH": "/usr/bin:/bin",
            "TEST_MARKER_FILE": str(marker),
            "AUDITTRACE_REPO_DIR": str(fake_repo),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert marker.exists()
    expected_argv = (argv + ["", ""])[:2]
    assert json.loads(marker.read_text()) == {"argv": expected_argv}
