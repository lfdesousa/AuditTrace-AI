"""THE chokepoint: every pytest subprocess the harness invokes, for every
phase, goes through :func:`run_pytest` (review round 2, structural fix).

Round-1's independent review found blockers 1, 2 and 5 were ONE defect
class: each phase that invokes pytest (neuter, baseline, drift, arbitrate,
collect) built its OWN environment, and every new phase forgot part of what
an earlier phase had learned to set up --

* ``PYTHONPATH`` was pinned only for the neuter phase (``run_one_neuter``)
  and collect (``spec._collect_ids``). Baseline, drift and arbitrate
  imported the venv's editable install instead of the worktree under test
  -- a real, targeted RED (the edit applied and a mapped test failing)
  measured 0/510 failures in the full-scope run, because the full-scope
  run never saw the edit at all.
* The DSN and the foreign-durable-container watch were set up only in the
  neuter phase. Baseline and drift started zero throwaway containers of
  their own (they never call ``start_container``), so a mapped test's
  import-time fixture fell back to the PRODUCT'S OWN durable
  ``audittrace-acl-wu2a-pg-*`` container -- invisible to any check that
  only ran inside the neuter phase.

The fixture hid all of this because its toy modules (``guarded.py``) sit at
the repo root, while the product's sit under ``src/`` -- collect (which DID
pin ``PYTHONPATH``) was the only self-proof-exercised phase where the bug
would have shown up.

This module is now the ONLY place ``python -m pytest`` may be invoked
anywhere under ``scripts/neuter/`` -- enforced mechanically by
``tests/test_neuter_pytest_chokepoint.py``'s AST scan, not by convention.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.neuter import lock as lockmod
from scripts.neuter.pg import (
    PgHandle,
    db_snapshot,
    foreign_product_pg_container_running,
    poll_db_leak,
)

#: This module's own directory -- always added to ``PYTHONPATH`` so
#: ``-p neuter_pathcheck`` resolves as a top-level plugin module regardless
#: of which worktree/repo is under test.
_PLUGIN_DIR = Path(__file__).resolve().parent


def _log_line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open() as fh:
        return sum(1 for _ in fh)


def build_pythonpath(src_root: str) -> str:
    """The ONE ``PYTHONPATH`` construction rule, shared by every phase: the
    worktree's own source root first, then this package's directory (so the
    pathcheck plugin resolves) -- never the bare interpreter default, which
    would silently prefer an editable install of the SAME package name."""
    return f"{src_root}:{_PLUGIN_DIR}"


@dataclass
class PytestRunResult:
    exit_code: int | None
    timed_out: bool
    #: ``None`` when pathcheck wasn't configured for this call (collect-only).
    pathcheck_ok: bool | None
    db_before: tuple[frozenset[str], int] | None
    db_after: tuple[frozenset[str], int] | None
    db_leak: bool
    #: True if a foreign, DURABLE product Postgres container
    #: (``PRODUCT_PG_CONTAINER_PREFIXES``) was observed running EITHER
    #: before or after this invocation -- checked for every phase,
    #: regardless of whether this call was even given a ``pg_handle`` (the
    #: exact gap that let baseline/drift start one invisibly).
    foreign_pg_container: bool
    #: Populated only for ``collect_only=True`` calls.
    collect_stdout: str | None = None


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
    concern applied uniformly (SPEC v3 §6/§8/§9/§12, review round 2).

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

    try:
        env = dict(os.environ)
        resolved_src_root = src_root or f"{workdir}/src"
        env["PYTHONPATH"] = build_pythonpath(resolved_src_root)

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
            foreign_before = foreign_product_pg_container_running()
            result = subprocess.run(
                cmd, cwd=workdir, env=env, capture_output=True, text=True
            )
            foreign_after = foreign_product_pg_container_running()
            return PytestRunResult(
                exit_code=result.returncode,
                timed_out=False,
                pathcheck_ok=None,
                db_before=None,
                db_after=None,
                db_leak=False,
                # A DELTA, never bare presence: a foreign product container
                # that was ALREADY running before this call (a genuinely
                # unrelated, concurrent test elsewhere in the SAME `make
                # test` session -- e.g. another suite's own RLS/ACL fixture
                # -- and is STILL running after) is not something THIS
                # invocation caused, and flagging it would false-positive
                # on every self-proof run alongside real product tests.
                # Only a container that appears where NONE existed before
                # is the actual SPEC v3 §9 defect signature.
                foreign_pg_container=(not foreign_before) and foreign_after,
                collect_stdout=result.stdout,
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

        # SPEC v3 §9: EVERY phase with a real (non-fake) Postgres handle
        # gets the DSN -- regardless of which phase this is. A mapped
        # test's import-time fixture (tests/_pg_ephemeral.py's callers)
        # starts a DURABLE product container unless it finds
        # AUDITTRACE_TEST_POSTGRES_URL already set.
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
        # Checked unconditionally, for EVERY phase -- never gated on
        # whether THIS call was given a pg_handle. Baseline and drift call
        # this with no pg_handle of their own; that is exactly the gap
        # that let a foreign, durable product container run unnoticed.
        foreign_before = foreign_product_pg_container_running()

        exit_code: int | None = None
        timed_out = False
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

        foreign_after = foreign_product_pg_container_running()

        return PytestRunResult(
            exit_code=exit_code,
            timed_out=timed_out,
            pathcheck_ok=pathcheck_ok,
            db_before=db_before,
            db_after=db_after,
            db_leak=db_leak,
            # A DELTA, never bare presence -- see the collect-only branch's
            # comment above for why: a foreign product container already
            # running before this call, from an unrelated concurrent test
            # elsewhere in the SAME make-test session, must never be
            # attributed to THIS invocation.
            foreign_pg_container=(not foreign_before) and foreign_after,
        )
    finally:
        if probe_fd is not None:
            os.close(probe_fd)
