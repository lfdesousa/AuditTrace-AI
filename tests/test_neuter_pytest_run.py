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
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.neuter import lock as lockmod
from scripts.neuter.pytest_run import PytestRunResult, chokepoint_scope, run_pytest

_DOCKER_AVAILABLE = shutil.which("docker") is not None
if _DOCKER_AVAILABLE:
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=10)
    except Exception:
        _DOCKER_AVAILABLE = False

requires_docker = pytest.mark.skipif(not _DOCKER_AVAILABLE, reason="docker unavailable")


@contextlib.contextmanager
def _fake_watch(seen: bool, watch_attached: bool = True):
    """Stand-in for ``_continuous_foreign_container_watch`` that reports a
    fixed, pre-determined verdict regardless of what happens inside the
    ``with`` block -- exactly the shape ``run_pytest`` consumes it as
    (``with _continuous_foreign_container_watch() as (foreign_seen,
    watch_attached): ... foreign_seen()``). ``watch_attached`` defaults to
    ``True`` -- round 6's readiness-proof boolean -- so callers that only
    care about ``seen`` need not think about it."""
    yield lambda: seen, watch_attached


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


def test_collect_only_populates_watch_attached_from_the_same_watch(
    tmp_path, monkeypatch, _isolated_lock
):
    """Review round 7, O-1a (orchestrator-confirmed blocker): the
    collect-only construction site (``run_pytest()``'s ``collect_only``
    branch) must populate ``PytestRunResult.watch_attached`` from the SAME
    watch the full-run site uses -- dropping the explicit
    ``watch_attached=watch_attached`` pass there previously left all 120
    targeted tests GREEN, because the dataclass field defaulted silently
    to ``True``. The field is now REQUIRED (no default), so that drop is
    an immediate ``TypeError`` raised from inside ``run_pytest()`` itself
    -- this test exercises exactly the collect-only code path end-to-end,
    with the fake watch set to the NON-default ``False`` so a reintroduced
    default could never make it pass by coincidence either."""
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(
        "scripts.neuter.pytest_run._continuous_foreign_container_watch",
        lambda: _fake_watch(True, watch_attached=False),
    )
    result = run_pytest(
        workdir=repo,
        pytest_args=["test_guarded.py"],
        python=PYTHON,
        lock_path=_isolated_lock,
        collect_only=True,
    )
    assert result.watch_attached is False


def test_pytest_run_result_requires_watch_attached_explicitly():
    """Review round 7 (O-1a, orchestrator-confirmed blocker): ``watch_
    attached`` has NO default -- dropping the explicit pass at EITHER
    construction site in ``run_pytest()`` (collect-only or the full run)
    must be an immediate ``TypeError``, never a silent, fail-open
    ``True``. A structural, dataclass-level proof complementing the two
    end-to-end tests above/below that exercise the real construction
    sites."""
    with pytest.raises(TypeError, match="watch_attached"):
        PytestRunResult(  # type: ignore[call-arg]
            exit_code=0,
            timed_out=False,
            pathcheck_ok=None,
            db_before=None,
            db_after=None,
            db_leak=False,
            foreign_pg_container=False,
        )


def test_collect_only_clears_a_stale_pathcheck_env_from_an_outer_caller(
    tmp_path, monkeypatch, _isolated_lock
):
    """Review round 3 regression, found dogfooding this round's own
    self-neuters (nested 3 levels deep): a POOL WORKER runs with
    ``NEUTER_PATHCHECK_EXPECT``/``_MODULE``/``_LOG`` set in its OWN
    environment (for its OWN mapped-test run) -- if one of that worker's
    mapped tests is ITSELF a self-proof that calls ``_collect_ids`` for a
    COMPLETELY UNRELATED toy fixture, ``env = dict(os.environ)`` inherits
    those THREE stale vars, and (since ``PYTEST_PLUGINS=neuter_pathcheck``
    is sticky and auto-loads regardless of ``collect_only``) the nested
    collect-only subprocess's ``pytest_configure`` tries to import the
    OUTER caller's module against the OUTER caller's expected path from
    inside the UNRELATED collect -- crashing before printing a single
    collected id. Observed live: this made ``_collect_ids`` return an
    empty set, misclassified three levels up as a plain ``uncollected``
    ``SpecLoadError`` instead of the real cause."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("NEUTER_PATHCHECK_EXPECT", "/some/outer/caller/path")
    monkeypatch.setenv("NEUTER_PATHCHECK_MODULE", "some_outer_module")
    monkeypatch.setenv("NEUTER_PATHCHECK_LOG", str(tmp_path / "outer.log"))

    result = run_pytest(
        workdir=repo,
        pytest_args=["test_guarded.py"],
        python=PYTHON,
        lock_path=_isolated_lock,
        collect_only=True,
    )
    ids = {
        line.strip()
        for line in (result.collect_stdout or "").splitlines()
        if "::" in line
    }
    assert result.exit_code == 0
    assert "test_guarded.py::test_a" in ids


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


@requires_docker
def test_continuous_watch_catches_a_container_removed_via_atexit_live(tmp_path):
    """The ONE test in this module that exercises the REAL, unmocked
    :func:`scripts.neuter.pytest_run._continuous_foreign_container_watch`
    against an ACTUAL docker daemon -- the reviewer's exact round-3
    regression signature: a durable, product-shaped Postgres container is
    started at TEST-BODY time in the mapped test's own pytest subprocess
    and torn down via ``atexit`` before that subprocess exits, so it is
    GONE by the time ``run_pytest`` returns. Every other test in this file
    stubs the watch directly (fast, no docker dependency) -- none of them
    can prove the watch's OWN internals still work, or catch a neuter that
    breaks JUST those internals (e.g. hard-coding its reported verdict) --
    this is the guard test SPEC v3 §11's self-neuter NE-6 (round 3) maps
    to, precisely so that neuter goes RED."""
    repo = tmp_path / "repo"
    repo.mkdir()
    fixture = repo / "test_atexit_container.py"
    fixture.write_text(
        "import atexit, os, subprocess, time\n"
        '_NAME = f"audittrace-acl-wu2a-pg-rev{os.getpid()}"\n'
        "def test_starts_a_durable_container_removed_at_atexit():\n"
        "    subprocess.run(\n"
        '        ["docker", "run", "-d", "--rm", "--name", _NAME, "postgres:16"],\n'
        "        check=True, capture_output=True, timeout=120,\n"
        "    )\n"
        "    atexit.register(\n"
        '        lambda: subprocess.run(["docker", "rm", "-f", _NAME], capture_output=True)\n'
        "    )\n"
        "    time.sleep(0.5)\n"
        "    assert True\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@test.invalid"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    result = run_pytest(
        workdir=repo,
        pytest_args=[
            "test_atexit_container.py::test_starts_a_durable_container_removed_at_atexit"
        ],
        python=PYTHON,
        lock_path=tmp_path / "atexit.lock",
        timeout_s=120,
    )
    assert result.exit_code == 0
    assert result.foreign_pg_container is True
    # Review round 6 N-P1 self-neuter target: `attached.set()` -> `pass`
    # (the reviewer's exact neuter) makes the readiness probe NEVER prove
    # the watch attached, which this assertion catches directly -- before
    # round 6, nothing in this file observed `watch_attached` at all, so
    # that neuter went completely unnoticed (10/10 still GREEN).
    assert result.watch_attached is True
    # ...and the container really is gone -- proving the snapshot-style
    # before/after check this replaced would have missed it entirely.
    ps = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "name=audittrace-acl-wu2a-pg-rev",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
    )
    assert ps.stdout.strip() == ""


@requires_docker
def test_slow_docker_events_attach_yields_watch_unproven_not_silent_clean(
    tmp_path, monkeypatch
):
    """Review round 6 blocker reproduction, mirroring the reviewer's own
    ``slow_events_run.py``/``docker`` shim: a ``docker`` wrapper on PATH
    that delays ONLY ``docker events``'s attach (simulating a
    slow/contended daemon) past the readiness probe's 5s bound -- a
    durable, foreign ``audittrace-acl-wu2a-pg-*`` container started the
    whole time must now surface as ``watch_attached=False`` (ERROR
    ``watch_unproven`` once classified), NEVER the pre-round-6 silent
    ``foreign_pg_container=False, exit_code=0`` the reviewer measured live
    (``readiness_failopen_measured.txt``)."""
    real_docker = shutil.which("docker")
    assert real_docker is not None  # requires_docker already gates this
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "docker"
    shim.write_text(
        "#!/bin/bash\n"
        "# builder repro of the reviewer's docker-events-is-slow shim:\n"
        "# `docker events` attaches only well after the readiness probe's\n"
        "# own 5s bound AND the mapped test's container has already fully\n"
        "# started (margin: 5s probe + ~2-3s container create/sleep,\n"
        "# measured) -- everything else passes through unchanged.\n"
        'if [ "$1" = "events" ]; then sleep 12; fi\n'
        f'exec {real_docker} "$@"\n'
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")

    repo = tmp_path / "repo"
    repo.mkdir()
    name = f"audittrace-acl-wu2a-pg-rev6slow{os.getpid()}"
    fixture = repo / "test_slow_durable.py"
    fixture.write_text(
        "import atexit, os, subprocess, time\n"
        f'_NAME = "{name}"\n'
        "def test_starts_a_durable_container_immediately():\n"
        "    subprocess.run(\n"
        '        ["docker", "run", "-d", "--rm", "--name", _NAME, "postgres:16"],\n'
        "        check=True, capture_output=True, timeout=120,\n"
        "    )\n"
        "    atexit.register(\n"
        '        lambda: subprocess.run(["docker", "rm", "-f", _NAME], capture_output=True)\n'
        "    )\n"
        "    time.sleep(0.5)\n"
        "    assert True\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@test.invalid"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    try:
        result = run_pytest(
            workdir=repo,
            pytest_args=[
                "test_slow_durable.py::test_starts_a_durable_container_immediately"
            ],
            python=PYTHON,
            lock_path=tmp_path / "slow.lock",
            timeout_s=120,
        )
        assert result.exit_code == 0
        # THE regression, now caught: pre-round-6, `foreign_pg_container`
        # alone would read `False` here (fail-open). Round 6 exposes the
        # unproven watch via `watch_attached=False` so the caller can fail
        # closed instead of trusting this silent "clean".
        assert result.watch_attached is False
        assert result.foreign_pg_container is False
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)


def test_chokepoint_scope_restores_prior_values_that_were_already_set(monkeypatch):
    """Branch coverage for chokepoint_scope()'s restore path: when
    NEUTER_CHOKEPOINT_REQUIRED/PYTEST_PLUGINS/PYTHONPATH were ALREADY set
    (and the plugin dir already on sys.path) before entering the scope,
    exiting it must restore those EXACT prior values, not merely pop them
    -- the "else" branch of every restore, as distinct from the "was
    absent" branch every other test in this module exercises implicitly."""
    monkeypatch.setenv("NEUTER_CHOKEPOINT_REQUIRED", "prior-required")
    monkeypatch.setenv("PYTEST_PLUGINS", "prior-plugin")
    plugin_dir = str(
        Path(sys.modules["scripts.neuter.pytest_run"].__file__).resolve().parent
    )
    monkeypatch.setenv("PYTHONPATH", f"{plugin_dir}:/somewhere/else")
    already_inserted = plugin_dir not in sys.path
    if already_inserted:
        sys.path.insert(0, plugin_dir)
    try:
        with chokepoint_scope():
            assert os.environ["NEUTER_CHOKEPOINT_REQUIRED"] == "prior-required"
            assert os.environ["PYTEST_PLUGINS"] == "prior-plugin"
        assert os.environ["NEUTER_CHOKEPOINT_REQUIRED"] == "prior-required"
        assert os.environ["PYTEST_PLUGINS"] == "prior-plugin"
        assert os.environ["PYTHONPATH"] == f"{plugin_dir}:/somewhere/else"
        assert plugin_dir in sys.path
    finally:
        if already_inserted:
            with contextlib.suppress(ValueError):
                sys.path.remove(plugin_dir)


def test_chokepoint_scope_pops_vars_that_were_absent_before(monkeypatch):
    """The other half: when the three vars were ABSENT before entering the
    scope, exiting it must leave them absent again (not "1"/leftover)."""
    monkeypatch.delenv("NEUTER_CHOKEPOINT_REQUIRED", raising=False)
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    with chokepoint_scope():
        assert os.environ["NEUTER_CHOKEPOINT_REQUIRED"] == "1"
    assert "NEUTER_CHOKEPOINT_REQUIRED" not in os.environ
    assert "PYTEST_PLUGINS" not in os.environ
    assert "PYTHONPATH" not in os.environ


def test_pathcheck_expect_without_pathcheck_log_raises(tmp_path, _isolated_lock):
    """Branch coverage: pathcheck_expect given without pathcheck_log is a
    programmer error, fails closed with ValueError before spawning anything."""
    repo = _init_repo(tmp_path)
    with pytest.raises(
        ValueError, match="pathcheck_expect given without pathcheck_log"
    ):
        run_pytest(
            workdir=repo,
            pytest_args=["test_guarded.py::test_a"],
            python=PYTHON,
            lock_path=_isolated_lock,
            pathcheck_expect="/some/path",
        )
