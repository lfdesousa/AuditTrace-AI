"""ADDENDUM A1-4: ``scripts/ci/run-tests.sh``'s sentinel-kubectl log is
ENFORCEMENT, not a warning — if it is non-empty after pytest finishes, the
script itself exits non-zero and names every call it caught.

Review REJECT (decisions-in/2026-10-11-REVIEW-bff-bump-ci-fix.md, N1):
"A fake pytest that calls `kubectl get ns || true` with clean junit and
coverage gives '✅ Tests passed', rc 0, plus a warning." This file drives
the REAL ``scripts/ci/run-tests.sh`` (copied verbatim into a scratch root,
never reimplemented) with a FAKE ``pytest`` standing in for the real one —
isolating the script's OWN shim-log-enforcement logic from the Python-
level conftest.py guard entirely (the fake pytest calls ``kubectl``
directly, as a raw shell command, with no pytest/conftest.py machinery
involved at all).
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_SCRIPT = REPO_ROOT / "scripts" / "ci" / "run-tests.sh"
REAL_SKIP_CHECK = REPO_ROOT / "scripts" / "check-no-skipped-tests.py"


def _scratch_script_root(tmp_path: Path) -> Path:
    """A throwaway root containing a verbatim copy of run-tests.sh and its
    skip-check dependency, at the same relative layout, so `cd
    "$REPO_ROOT"` + the script's own relative references resolve."""
    root = tmp_path / "scratch-repo"
    (root / "scripts" / "ci").mkdir(parents=True)
    shutil.copy(REAL_SCRIPT, root / "scripts" / "ci" / "run-tests.sh")
    shutil.copy(REAL_SKIP_CHECK, root / "scripts" / "check-no-skipped-tests.py")
    return root


def _fake_pytest(bin_dir: Path, *, leak: bool) -> Path:
    """Stands in for the real `pytest` binary resolve_tool() finds on
    PATH. Writes a minimal, clean junit.xml to whatever `--junit-xml=`
    path it was given, optionally calls `kubectl get nodes` (ignoring its
    exit code, exactly like the review's own repro), then exits 0 — a
    test suite that "passed" while silently leaking to a real cluster
    tool."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    tool = bin_dir / "pytest"
    leak_line = "kubectl get nodes || true" if leak else ": # no leak"
    tool.write_text(
        "#!/usr/bin/env bash\n"
        'junit_path=""\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        '    --junit-xml=*) junit_path="${arg#--junit-xml=}" ;;\n'
        "  esac\n"
        "done\n"
        f"{leak_line}\n"
        'cat > "$junit_path" <<XML\n'
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuites><testsuite name="fake" tests="1" failures="0" errors="0" skipped="0">\n'
        '<testcase classname="fake" name="test_fake" time="0.0"/>\n'
        "</testsuite></testsuites>\n"
        "XML\n"
        "exit 0\n"
    )
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return tool


def _fake_python_passthrough(bin_dir: Path) -> Path:
    """resolve_tool() also looks up `python` (for the skip-check /
    per-file-coverage calls) — point it at the REAL interpreter running
    this test, so `scripts/check-no-skipped-tests.py` actually runs."""
    import sys

    bin_dir.mkdir(parents=True, exist_ok=True)
    tool = bin_dir / "python"
    tool.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return tool


def _run_script(
    root: Path, bin_dir: Path, *, subset: bool = True
) -> subprocess.CompletedProcess[str]:
    import os

    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    argv = ["bash", str(root / "scripts" / "ci" / "run-tests.sh")]
    if subset:
        argv += ["--subset", "test_fake"]
    return subprocess.run(
        argv, cwd=root, env=env, capture_output=True, text=True, timeout=60
    )


def test_a_leaking_fake_pytest_makes_run_tests_sh_exit_nonzero(tmp_path: Path):
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    _fake_pytest(bin_dir, leak=True)
    _fake_python_passthrough(bin_dir)

    result = _run_script(root, bin_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "sentinel kubectl was invoked" in result.stderr
    assert "get nodes" in result.stderr


def test_a_clean_fake_pytest_leaves_run_tests_sh_exit_zero(tmp_path: Path):
    """Negative control: without the leak, the same harness must pass —
    proves the failure above is caused by the leak, not by the scratch
    scaffolding itself."""
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    _fake_pytest(bin_dir, leak=False)
    _fake_python_passthrough(bin_dir)

    result = _run_script(root, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Tests passed" in result.stdout
    assert "sentinel kubectl was invoked" not in result.stderr
