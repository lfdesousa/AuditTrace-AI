"""Unit tests for ``scripts/neuter/pytest_run.py``'s foreign-container watch
-- found running the definitive final ``make test``: a bare before/after
``docker ps`` snapshot false-positived on EVERY neuter self-proof whenever
an unrelated, concurrent product test elsewhere in the SAME ``make test``
session happened to have a real ``audittrace-acl-wu2a-pg-*``-shaped
container running throughout (present before AND after, unrelated to the
neuter under test) -- AND, separately (review round 3), missed a container
started and torn down via ``atexit`` entirely WITHIN one pytest call. Round
3 replaced the snapshot with :func:`scripts.neuter.pytest_run.
_continuous_foreign_container_watch`, a ``docker events``-streaming context
manager that is active for the FULL DURATION of the call -- these tests
stub that context manager directly (a real ``docker events`` subprocess is
exercised live in T1/the self-proof suite, not here) so this module's own
plumbing (that ``run_pytest`` reads the watch's own verdict, in both the
full-run and ``collect_only`` branches) is proven without a docker
dependency.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.neuter import lock as lockmod
from scripts.neuter.pytest_run import run_pytest


@contextlib.contextmanager
def _fake_watch(seen: bool):
    """Stand-in for ``_continuous_foreign_container_watch`` that reports a
    fixed, pre-determined verdict regardless of what happens inside the
    ``with`` block -- exactly the shape ``run_pytest`` consumes it as
    (``with _continuous_foreign_container_watch() as foreign_seen: ...
    foreign_seen()``)."""
    yield lambda: seen


FIXTURE_DIR = Path(__file__).parent / "neuter_fixture"
PYTHON = sys.executable


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("guarded.py", "test_guarded.py", "data.txt"):
        shutil.copy(FIXTURE_DIR / name, repo / name)
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "neuter@test.invalid")
    _run_git(repo, "config", "user.name", "neuter")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def _isolated_lock(tmp_path):
    return tmp_path / "test.lock"


def test_foreign_container_already_present_before_and_after_is_not_flagged(
    tmp_path, monkeypatch, _isolated_lock
):
    """The exact round-2 regression, restated for the round-3 watch: a
    foreign product container that was ALREADY running before this call
    (unrelated, concurrent activity elsewhere) and is never (re)STARTED
    during the call must never be attributed to THIS invocation -- no
    ``docker events`` start event fires for a container that merely keeps
    running, so the watch correctly reports ``False``."""
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(
        "scripts.neuter.pytest_run._continuous_foreign_container_watch",
        lambda: _fake_watch(False),
    )
    result = run_pytest(
        workdir=repo,
        pytest_args=["test_guarded.py::test_a"],
        python=PYTHON,
        lock_path=_isolated_lock,
        junit_path=tmp_path / "junit.xml",
    )
    assert result.foreign_pg_container is False


def test_foreign_container_appearing_during_the_call_is_flagged(
    tmp_path, monkeypatch, _isolated_lock
):
    """The genuine defect signature: a foreign product container STARTS at
    some point during this invocation -- including one started and torn
    down via ``atexit`` entirely within the call, which a before/after
    snapshot would have missed but the continuous watch does not."""
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(
        "scripts.neuter.pytest_run._continuous_foreign_container_watch",
        lambda: _fake_watch(True),
    )
    result = run_pytest(
        workdir=repo,
        pytest_args=["test_guarded.py::test_a"],
        python=PYTHON,
        lock_path=_isolated_lock,
        junit_path=tmp_path / "junit.xml",
    )
    assert result.foreign_pg_container is True


def test_foreign_container_absent_throughout_is_not_flagged(
    tmp_path, monkeypatch, _isolated_lock
):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(
        "scripts.neuter.pytest_run._continuous_foreign_container_watch",
        lambda: _fake_watch(False),
    )
    result = run_pytest(
        workdir=repo,
        pytest_args=["test_guarded.py::test_a"],
        python=PYTHON,
        lock_path=_isolated_lock,
        junit_path=tmp_path / "junit.xml",
    )
    assert result.foreign_pg_container is False


def test_foreign_container_collect_only_uses_the_same_delta(
    tmp_path, monkeypatch, _isolated_lock
):
    """The collect-only branch wraps its subprocess call in the SAME
    continuous watch -- proven independently."""
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(
        "scripts.neuter.pytest_run._continuous_foreign_container_watch",
        lambda: _fake_watch(True),
    )
    result = run_pytest(
        workdir=repo,
        pytest_args=["test_guarded.py"],
        python=PYTHON,
        lock_path=_isolated_lock,
        collect_only=True,
    )
    assert result.foreign_pg_container is True


def test_assert_lock_held_enforced_for_a_standalone_caller(tmp_path):
    """``already_locked=False`` (the default) self-acquires ``lock_path``:
    proven here by holding it externally first and confirming the call
    fails closed rather than silently proceeding."""
    repo = _init_repo(tmp_path)
    lock_path = tmp_path / "held.lock"
    fd = lockmod.open_lock_file(lock_path)
    import fcntl

    lockmod.try_flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(lockmod.LockHeldError):
            run_pytest(
                workdir=repo,
                pytest_args=["test_guarded.py::test_a"],
                python=PYTHON,
                lock_path=lock_path,
                junit_path=tmp_path / "junit.xml",
            )
    finally:
        import os

        os.close(fd)
