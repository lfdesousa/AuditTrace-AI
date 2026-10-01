"""THE chokepoint: every pytest subprocess the harness invokes, for every
phase, goes through :func:`run_pytest` (review round 2, structural fix;
review round 3, RUNTIME totality + a continuous container watch).

Round-1's independent review found blockers 1, 2 and 5 were ONE defect
class: each phase that invokes pytest (neuter, baseline, drift, arbitrate,
collect) built its OWN environment, and every new phase forgot part of what
an earlier phase had learned to set up. Round 2 introduced this module as
the single place every phase invokes pytest, guarded by an AST scan
(``tests/test_neuter_pytest_chokepoint.py``).

Round 3's independent review found that STATIC enumeration keeps losing:
the AST scan caught only 1 of 5 realistic bypass shapes (``pytest.main``,
a module-level constant handed to ``subprocess.run``, ``os.system``,
``subprocess.call``), and the container watch's before/after snapshot
missed the product's actual pattern (a durable container started at
TEST-BODY time and torn down via ``atexit`` -- gone by the time the "after"
snapshot ran). Round 3's principle: **enforce at runtime, don't enumerate.**

Two runtime mechanisms now do the real work (the AST scan stays as a
CHEAP, secondary, compile-time check -- see requirement 2 below):

1. **A per-invocation chokepoint token.** ``run_pytest`` writes a fresh
   UUID to a throwaway file (``NEUTER_CHOKEPOINT_TOKEN_FILE``) and passes
   the SAME value via ``NEUTER_CHOKEPOINT_TOKEN``. The FIRST time
   ``run_pytest`` actually RUNS (not merely when this module is imported --
   a critical correction, see below), it makes
   ``NEUTER_CHOKEPOINT_REQUIRED=1`` STICKY in ``os.environ`` for the rest
   of the CURRENT process, precisely so a bypass that constructs its OWN
   subprocess/``pytest.main()`` call (never routing through ``run_pytest``
   again) still inherits the "this session MUST carry a valid token"
   requirement. The mandatory ``neuter_pathcheck`` plugin (loaded via
   ``PYTEST_PLUGINS``, made sticky the same way) fails any SUBSEQUENT
   pytest session in this process at ``pytest_configure`` if
   ``NEUTER_CHOKEPOINT_REQUIRED`` is set but the token is missing or
   doesn't match the recorded file -- so a bypass that skips this module
   entirely never even gets a token to omit; it simply fails by
   construction the moment pytest starts.

   **Critical correction (found running the definitive full-suite
   ``make test``):** an EARLIER revision made this sticky at MODULE IMPORT
   time instead. That poisoned ANY process that merely IMPORTED this
   module for an unrelated reason -- including ``make test``'s own
   ``hold-shared`` wrapper (``scripts/neuter/runner.py`` imports this
   module at its own top) and, transitively, every test in the 6500-test
   suite that shells out to ANOTHER command which happens to invoke pytest
   itself (e.g. ``make release``'s own "drift gate" step, run from inside
   ``tests/test_release_bump_files_ssot.py``) -- none of which ever calls
   ``run_pytest`` at all. Scoping the stickiness to "the first REAL call"
   instead of "the mere import" keeps every genuine neuter invocation
   (``run``/``arbitrate``/``report`` always call ``load_neuter_spec`` --
   hence ``run_pytest`` -- before any worker starts) exactly as protected,
   while a process that only ever IMPORTS this module without ever
   actually running a neuter phase stays completely inert.
2. **A continuous, event-driven container watch.** Round 2's before/after
   ``docker ps`` snapshot missed a container started and torn down (via
   ``atexit``) ENTIRELY WITHIN the pytest call. This module now streams
   ``docker events --filter event=start`` in a background thread for the
   FULL DURATION of the subprocess (or the in-process collect call), so a
   foreign product container is caught the INSTANT it starts, regardless
   of whether it is gone before the call ends.

This module is still the ONLY place ``python -m pytest`` may be invoked
anywhere under ``scripts/neuter/`` -- enforced by the AST scan AND, now,
by the fact that anything else fails at runtime regardless.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from scripts.neuter import lock as lockmod
from scripts.neuter.pg import PgHandle, db_snapshot, poll_db_leak

#: This module's own directory -- always added to ``PYTHONPATH`` so
#: ``neuter_pathcheck`` resolves as a top-level plugin module regardless of
#: which worktree/repo is under test.
_PLUGIN_DIR = Path(__file__).resolve().parent

#: The product's own DURABLE ephemeral-Postgres container name prefixes
#: (mirrors ``pg.PRODUCT_PG_CONTAINER_PREFIXES`` -- kept here too so the
#: continuous watch thread has no import-time dependency on anything that
#: could itself be neutered out from under it).
_PRODUCT_PG_CONTAINER_PREFIXES = (
    "audittrace-acl-wu2a-pg-",
    "audittrace-rls-pg-",
    "audittrace-console-store-pg-",
)


@contextlib.contextmanager
def chokepoint_scope():
    """Review round 3, requirement B2 (twice-corrected): make the
    chokepoint REQUIRED for every pytest session that starts in THIS
    process for the DURATION of this ``with`` block -- sticky in
    ``os.environ``, not a per-call argument, precisely so a bypass that
    constructs its OWN subprocess/``pytest.main()`` call (never routing
    through :func:`run_pytest` again) still inherits the "this session
    MUST carry a valid token" requirement -- then RESTORES whatever
    ``os.environ`` held before, so this process's chokepoint state never
    outlives the caller that asked for it.

    Entered by ``_cmd_run``/``_cmd_arbitrate``/``_cmd_report`` (the CLI
    commands that own an ENTIRE neuter invocation start to finish) around
    their whole body -- never called from inside :func:`run_pytest`
    itself.

    Two corrections on top of the original design, both found running the
    definitive full-suite ``make test``:

    1. Setting this at MODULE IMPORT time poisoned ANY process that merely
       imported this module for an unrelated reason -- including
       ``scripts/neuter/runner.py``'s own ``hold-shared`` wrapper, which
       ``make test`` uses to run the product's own, entirely un-gated
       pytest suite.
    2. Even set explicitly-but-never-undone from inside a caller, a TEST
       that exercises a REAL CLI command (``main(["run", ...])``,
       necessary to test the command itself) left the sticky vars in
       ``os.environ`` for the REST of that pytest SESSION -- poisoning
       every LATER test in the same ``make test`` run that shells out to
       something which itself invokes pytest (e.g.
       ``test_release_bump_files_ssot.py``'s own ``make release``
       subprocess, whose "drift gate" step runs pytest internally). A
       real, standalone CLI process exits when its command finishes
       anyway, so restoring on exit changes nothing for actual production
       use, while making every unit test that exercises
       ``_cmd_run``/``_cmd_arbitrate``/``_cmd_report`` directly leave
       ``os.environ`` exactly as it found it.
    """
    saved_required = os.environ.get("NEUTER_CHOKEPOINT_REQUIRED")
    saved_plugins = os.environ.get("PYTEST_PLUGINS")
    saved_pythonpath = os.environ.get("PYTHONPATH")
    inserted_syspath = str(_PLUGIN_DIR) not in sys.path
    try:
        os.environ.setdefault("NEUTER_CHOKEPOINT_REQUIRED", "1")
        os.environ.setdefault("PYTEST_PLUGINS", "neuter_pathcheck")
        existing_pythonpath = os.environ.get("PYTHONPATH", "")
        if str(_PLUGIN_DIR) not in existing_pythonpath.split(os.pathsep):
            os.environ["PYTHONPATH"] = (
                f"{_PLUGIN_DIR}{os.pathsep}{existing_pythonpath}"
                if existing_pythonpath
                else str(_PLUGIN_DIR)
            )
        # `PYTHONPATH` only affects a CHILD interpreter's import
        # resolution at its own startup -- an IN-PROCESS bypass
        # (`pytest.main()` called directly, never a subprocess) needs
        # `neuter_pathcheck` importable via THIS interpreter's own
        # `sys.path` right now, or pytest's plugin manager fails with a
        # bare `ImportError` before `pytest_configure` ever runs (still a
        # fail-by-construction outcome, just a less diagnostic one than
        # the plugin's own `pytest.exit` message).
        if str(_PLUGIN_DIR) not in sys.path:
            sys.path.insert(0, str(_PLUGIN_DIR))
        yield
    finally:
        if saved_required is None:
            os.environ.pop("NEUTER_CHOKEPOINT_REQUIRED", None)
        else:
            os.environ["NEUTER_CHOKEPOINT_REQUIRED"] = saved_required
        if saved_plugins is None:
            os.environ.pop("PYTEST_PLUGINS", None)
        else:
            os.environ["PYTEST_PLUGINS"] = saved_plugins
        if saved_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = saved_pythonpath
        if inserted_syspath:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(_PLUGIN_DIR))


def _log_line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open() as fh:
        return sum(1 for _ in fh)


def _marker_was_written(marker_file: str, token: str) -> bool:
    """Review round 4, requirement E2: did ``neuter_pathcheck.pytest_
    configure`` actually run and validate during this invocation? The
    marker file starts EMPTY (created by ``run_pytest()`` itself, never
    the child); the plugin only writes the token to it after its OWN
    checks pass. If a stray ``-p no:neuter_pathcheck`` (or any other way
    the plugin fails to load/run) reached this call, the file stays empty
    -- a fact the plugin itself can never detect from the inside, but
    ``run_pytest()`` can, from the outside, after the child exits."""
    try:
        return Path(marker_file).read_text() == token
    except OSError:
        return False


def build_pythonpath(src_root: str) -> str:
    """The ONE ``PYTHONPATH`` construction rule, shared by every phase: the
    worktree's own source root first, then this package's directory (so the
    pathcheck plugin resolves) -- never the bare interpreter default, which
    would silently prefer an editable install of the SAME package name."""
    return f"{src_root}:{_PLUGIN_DIR}"


@contextlib.contextmanager
def _continuous_foreign_container_watch():
    """Streams ``docker events --filter event=start`` for the DURATION of
    the ``with`` block, in a background thread, and yields a callable that
    reports whether a foreign product container was seen starting at any
    point during the block -- even if it was gone before the block ended
    (review round 3, requirement B1: a before/after snapshot misses a
    container started and torn down via ``atexit`` entirely within a
    single pytest call).

    Falls back to reporting ``False`` (never raises) if the ``docker``
    binary is unavailable or the events stream can't be started -- the
    same fail-open-on-tooling-absence posture the rest of this module's
    docker interactions already have (``--no-db``/fake mode never touches
    docker at all).
    """
    seen = threading.Event()
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            [
                "docker",
                "events",
                "--filter",
                "type=container",
                "--filter",
                "event=start",
                "--format",
                "{{.Actor.Attributes.name}}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        proc = None

    stop = threading.Event()

    def _watch() -> None:
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            if stop.is_set():
                break
            name = line.strip()
            if any(name.startswith(p) for p in _PRODUCT_PG_CONTAINER_PREFIXES):
                seen.set()

    thread = threading.Thread(target=_watch, daemon=True)
    thread.start()
    # docker events needs a brief moment to actually attach to the event
    # stream before it will see anything -- without this, a container
    # started immediately after entering the `with` block can race the
    # watcher's own startup and be missed.
    time.sleep(0.15)
    try:
        yield lambda: seen.is_set()
    finally:
        stop.set()
        if proc is not None:
            proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2)
        thread.join(timeout=2)


@dataclass
class PytestRunResult:
    exit_code: int | None
    timed_out: bool
    #: ``None`` when pathcheck wasn't configured for this call (collect-only).
    pathcheck_ok: bool | None
    db_before: tuple[frozenset[str], int] | None
    db_after: tuple[frozenset[str], int] | None
    db_leak: bool
    #: True if a foreign, DURABLE product Postgres container was seen
    #: STARTING at any point during this invocation (a continuous,
    #: event-driven watch spanning the whole call -- review round 3,
    #: requirement B1) -- checked for every phase, regardless of whether
    #: this call was even given a ``pg_handle``.
    foreign_pg_container: bool
    #: The fresh per-invocation chokepoint token this call issued (review
    #: round 3, requirement B2) -- carried on the row for audit purposes.
    chokepoint_token: str = ""
    #: Populated only for ``collect_only=True`` calls.
    collect_stdout: str | None = None
    #: False iff ``neuter_pathcheck.pytest_configure`` never wrote the
    #: marker file this call handed its child (review round 4, requirement
    #: E2) -- meaning the plugin never ran AT ALL this invocation (e.g. a
    #: stray ``-p no:neuter_pathcheck`` reached ``pytest_args``), which the
    #: plugin itself can never detect from the inside. ``run_pytest()``
    #: checks this AFTER the child exits, regardless of its exit code.
    chokepoint_marker_ok: bool = True


def run_pytest(
    *,
    workdir: Path,
    pytest_args: list[str],
    python: str,
    lock_path: Path,
    timeout_s: int = 900,
    junit_path: Path | None = None,
    pg_handle: PgHandle | None = None,
    check_db_leak: bool = False,
    pathcheck_module: str = "audittrace",
    pathcheck_expect: str | None = None,
    pathcheck_log: Path | None = None,
    src_root: str | None = None,
    collect_only: bool = False,
    already_locked: bool = False,
) -> PytestRunResult:
    """Invoke ``python -m pytest`` exactly once, with every environment
    concern applied uniformly (SPEC v3 §6/§8/§9/§12, review rounds 2-3).

    ``lock_path``/``already_locked``: enforces the heavy cap. When
    ``already_locked`` is ``False`` (the default -- a standalone caller,
    e.g. a direct unit test, that has not itself taken the pool's
    exclusive lock), this function acquires ``lock_path`` itself
    (non-blocking) around the invocation and releases it after, so a
    caller can never silently run pytest while ``make test``'s
    ``hold-shared`` (or a concurrent pool run) holds the SAME lock file
    exclusively. When ``already_locked`` is ``True`` (the CLI commands --
    ``run``/``arbitrate``/``report`` -- which take the lock ONCE at their
    own top, before calling this for collect/baseline/drift/neuter/
    arbitrate), this function only ASSERTS the lock is currently held by
    probing a fresh file descriptor (never re-acquires on top of the
    caller's own fd, which would self-conflict).
    """
    if already_locked:
        lockmod.assert_lock_held(lock_path)
        probe_fd = None
    else:
        probe_fd = lockmod.open_lock_file(lock_path)
        try:
            lockmod.try_flock(probe_fd, fcntl.LOCK_EX)
        except lockmod.LockHeldError:
            os.close(probe_fd)
            raise

    token = uuid.uuid4().hex
    token_fd, token_file = tempfile.mkstemp(prefix="neuter-chokepoint-")
    marker_fd, marker_file = tempfile.mkstemp(prefix="neuter-chokepoint-marker-")
    os.close(marker_fd)  # start EMPTY -- the plugin writes to it, not us
    try:
        with os.fdopen(token_fd, "w") as fh:
            fh.write(token)

        try:
            env = dict(os.environ)
            # Review round 3 regression (found dogfooding this round's own
            # self-neuters, nested 3+ levels deep): `env = dict(os.environ)`
            # inherits WHATEVER a pool worker's own mapped-test session
            # already carries -- NEUTER_PATHCHECK_EXPECT/_MODULE/_LOG,
            # AUDITTRACE_TEST_POSTGRES_URL, NEUTER_FAKE_PG_STATE. A nested
            # call inside one of that worker's mapped tests (a self-proof
            # that itself invokes run_pytest()/load_neuter_spec() for a
            # COMPLETELY UNRELATED toy fixture, with no pg_handle/pathcheck
            # args of its own) must never silently pick up the OUTER
            # caller's values for these -- each is explicitly cleared here
            # and only re-set below from THIS call's own arguments, so a
            # caller that passes nothing gets a clean slate, not stale
            # inherited state pointing at an unrelated worktree/container.
            for _stale_key in (
                "NEUTER_PATHCHECK_EXPECT",
                "NEUTER_PATHCHECK_MODULE",
                "NEUTER_PATHCHECK_LOG",
                "AUDITTRACE_TEST_POSTGRES_URL",
                "NEUTER_FAKE_PG_STATE",
                "NEUTER_CHOKEPOINT_SKIP",
            ):
                env.pop(_stale_key, None)
            resolved_src_root = src_root or f"{workdir}/src"
            env["PYTHONPATH"] = build_pythonpath(resolved_src_root)
            # Review round 3, requirement B2 (UNCONDITIONAL since round 4
            # requirement E1): every session this call spawns must carry a
            # fresh token AND the file it was recorded to --
            # `neuter_pathcheck`'s `pytest_configure` fails the session
            # outright if either is missing or they don't match, with no
            # opt-out flag to strip. It ALSO records a marker (review round
            # 4 requirement E2) this function checks AFTER the child exits,
            # catching the plugin never running at all (e.g. a stray
            # `-p no:neuter_pathcheck`).
            env["NEUTER_CHOKEPOINT_TOKEN"] = token
            env["NEUTER_CHOKEPOINT_TOKEN_FILE"] = token_file
            env["NEUTER_CHOKEPOINT_MARKER_FILE"] = marker_file
            env["PYTEST_PLUGINS"] = "neuter_pathcheck"

            if collect_only:
                cmd = [
                    python,
                    "-m",
                    "pytest",
                    "--collect-only",
                    "-q",
                    "-o",
                    "addopts=",
                    *pytest_args,
                ]
                # Review round 3 blocker 1: --collect-only still IMPORTS
                # every test module -- an import-time side effect can
                # start a durable product container exactly like a full
                # run can, so collect gets the SAME DSN passthrough.
                if pg_handle is not None and not pg_handle.fake:
                    env["AUDITTRACE_TEST_POSTGRES_URL"] = pg_handle.dsn
                elif (
                    pg_handle is not None
                    and pg_handle.fake
                    and pg_handle.fake_state_path is not None
                ):
                    env["NEUTER_FAKE_PG_STATE"] = str(pg_handle.fake_state_path)
                with _continuous_foreign_container_watch() as foreign_seen:
                    result = subprocess.run(
                        cmd, cwd=workdir, env=env, capture_output=True, text=True
                    )
                return PytestRunResult(
                    exit_code=result.returncode,
                    timed_out=False,
                    pathcheck_ok=None,
                    db_before=None,
                    db_after=None,
                    db_leak=False,
                    foreign_pg_container=foreign_seen(),
                    chokepoint_token=token,
                    collect_stdout=result.stdout,
                    chokepoint_marker_ok=_marker_was_written(marker_file, token),
                )

            cmd = [
                python,
                "-m",
                "pytest",
                *pytest_args,
                "-q",
                "--no-cov",
                "-o",
                "addopts=",
                "-rfE",
                "-p",
                "no:cacheprovider",
            ]
            if junit_path is not None:
                junit_path.parent.mkdir(parents=True, exist_ok=True)
                cmd.append(f"--junitxml={junit_path}")

            before_lines = 0
            if pathcheck_expect is not None:
                if pathcheck_log is None:
                    raise ValueError("pathcheck_expect given without pathcheck_log")
                before_lines = _log_line_count(pathcheck_log)
                env["NEUTER_PATHCHECK_MODULE"] = pathcheck_module
                env["NEUTER_PATHCHECK_EXPECT"] = pathcheck_expect
                env["NEUTER_PATHCHECK_LOG"] = str(pathcheck_log)
                cmd += ["-p", "neuter_pathcheck"]

            # SPEC v3 §9: EVERY phase with a real (non-fake) Postgres
            # handle gets the DSN -- regardless of which phase this is. A
            # mapped test's import-time OR test-body fixture
            # (tests/_pg_ephemeral.py's callers) starts a DURABLE product
            # container unless it finds AUDITTRACE_TEST_POSTGRES_URL
            # already set.
            real_pg = pg_handle is not None and not pg_handle.fake
            if real_pg:
                env["AUDITTRACE_TEST_POSTGRES_URL"] = pg_handle.dsn  # type: ignore[union-attr]
            if (
                pg_handle is not None
                and pg_handle.fake
                and pg_handle.fake_state_path is not None
            ):
                env["NEUTER_FAKE_PG_STATE"] = str(pg_handle.fake_state_path)

            db_before = (
                db_snapshot(pg_handle)
                if (check_db_leak and pg_handle is not None)
                else None
            )

            exit_code: int | None = None
            timed_out = False
            with _continuous_foreign_container_watch() as foreign_seen:
                try:
                    result = subprocess.run(
                        cmd,
                        cwd=workdir,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=timeout_s,
                    )
                    exit_code = result.returncode
                except subprocess.TimeoutExpired:
                    timed_out = True

            pathcheck_ok: bool | None = None
            if pathcheck_expect is not None:
                pathcheck_ok = _log_line_count(pathcheck_log) > before_lines  # type: ignore[arg-type]

            db_leak = False
            db_after = None
            if check_db_leak and pg_handle is not None:
                db_leak = poll_db_leak(pg_handle, db_before)  # type: ignore[arg-type]
                db_after = db_snapshot(pg_handle)

            return PytestRunResult(
                exit_code=exit_code,
                timed_out=timed_out,
                pathcheck_ok=pathcheck_ok,
                db_before=db_before,
                db_after=db_after,
                db_leak=db_leak,
                foreign_pg_container=foreign_seen(),
                chokepoint_token=token,
                chokepoint_marker_ok=_marker_was_written(marker_file, token),
            )
        finally:
            if probe_fd is not None:
                os.close(probe_fd)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(token_file)
        with contextlib.suppress(OSError):
            os.unlink(marker_file)
