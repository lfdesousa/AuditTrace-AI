"""Parallel worker pool: apply, run, restore, record (SPEC v3 §6, §7).

Per neuter, strictly in order: ``git diff --quiet`` (else stop) -> apply
edits -> ``py_compile`` -> run (classified by ``classify.py``) -> ``git
checkout -- <file>`` -> ``git diff --quiet`` -> append row -> next. A dirty
restore stops the whole pool (every worker drains its queue, exit 4, the
dirty worktree is kept and logged) -- never a blanket ``git checkout -- .``,
which would silently erase a mapped test's own side effect along with the
neuter's edit and make every restore look clean (the falsifiable failure
mode proof c guards against).

Results are durable: one JSONL per worker, ``flush()`` + ``fsync()`` per
row, never ``/tmp``. Resume skips a neuter whose existing row already has
verdict in {RED, GREEN}, ``restored_clean``, the same ``sha``,
``neuter_hash``, and ``harness_version``; ERROR rows always re-run.

**Fails CLOSED (review round 1, blocker 1):** every neuter in the spec
must produce exactly one row. A crashed worker (a fake ``docker`` that
exits 1, a poisoned import, anything) is caught, logged to
``events.jsonl`` with its traceback, and its worktree/container cleaned up
-- but the crash can never make the pool silently exit 0 with rows
missing. After every worker joins, ``run_pool`` diffs the merged rows
against ``{n.id for n in spec.neuters}``; any gap is
:data:`EXIT_MISSING_ROWS`, the missing ids printed.

**Baseline-first (blocker 2):** before any worker even starts, the union
of every neuter's mapped tests is run ONCE, untouched, at the pinned sha.
Any non-passed outcome there is :data:`EXIT_BASELINE_FAILED` and no
neuter runs -- a neuter mapped to an already-failing test can never read
as a false RED.

**Sampled full-scope drift (blocker 3):** after the targeted pass, a
``random.Random(run_id)``-seeded sample of neuters is re-applied and run
over the FULL ``scope_files`` (not just their mapped tests); any
assertion-shaped failure OUTSIDE the mapped set is recorded as measured
``unmapped_red`` on that row -- never a hardcoded ``[]``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import multiprocessing as mp
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.neuter import lock as lockmod
from scripts.neuter.classify import classify
from scripts.neuter.junit import (
    mapped_failed_in_full,
    parse_junit,
    parse_junit_full,
    unmapped_assertion_shaped_failures,
)
from scripts.neuter.pg import (
    EXIT_NONDURABLE_SETTINGS,
    NonDurableSettingsError,
    PgHandle,
    assert_nondurable,
    db_snapshot,
    read_settings,
    start_container,
    stop_container,
)
from scripts.neuter.pytest_run import run_pytest
from scripts.neuter.spec import NeuterEntry, NeuterSpecFile

logger = logging.getLogger(__name__)

HARNESS_VERSION = "1"

EXIT_OK = 0
EXIT_SPEC_ERROR = 3
EXIT_DIRTY_TREE = 4
EXIT_BASELINE_FAILED = 5
EXIT_LOCK_HELD = 8
EXIT_MISSING_ROWS = 10
EXIT_WORKERS_UNMEASURED = 12
EXIT_EVIDENCE_NOT_EMPTY = 13
EXIT_DRIFT_UNACKNOWLEDGED = 14


class DirtyTreeError(Exception):
    """A worktree was dirty before a neuter started (uncommitted foreign state)."""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def evidence_dir_is_refused(evidence_dir: Path) -> bool:
    """SPEC v3 §7: ``/tmp`` (and ``tempfile.gettempdir()``) are refused as
    evidence dirs -- results must be durable, never in the one place every
    OS routinely reaps."""
    resolved = str(evidence_dir.resolve())
    refused_roots = {"/tmp", os.path.realpath(tempfile.gettempdir())}
    return any(
        resolved == root or resolved.startswith(root.rstrip("/") + "/")
        for root in refused_roots
    )


def compute_neuter_hash(entry: NeuterEntry) -> str:
    """``neuter_hash`` = sha256(file, edits, tests, engines) -- SPEC v3 §7."""
    payload = json.dumps(
        {
            "file": entry.file,
            "edits": [[e.old, e.new] for e in entry.edits],
            "tests": entry.tests,
            "engines": entry.engines,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def append_event(evidence_dir: Path, event: dict[str, Any]) -> None:
    """Append one structured event to ``events.jsonl`` (should-fix)."""
    row = {"ts": _now_iso(), **event}
    append_jsonl(evidence_dir / "events.jsonl", row)


def git_diff_quiet(workdir: Path, *paths: str) -> bool:
    return (
        subprocess.run(["git", "diff", "--quiet", *paths], cwd=workdir).returncode == 0
    )


def apply_edits(workdir: Path, entry: NeuterEntry) -> list[int]:
    path = workdir / entry.file
    source = path.read_text()
    matches = [source.count(e.old) for e in entry.edits]
    if any(c != 1 for c in matches):
        raise RuntimeError(
            f"{entry.id}: runtime match count {matches} != 1 (spec load should have caught this)"
        )
    for edit in entry.edits:
        source = source.replace(edit.old, edit.new, 1)
    path.write_text(source)
    return matches


def restore(workdir: Path, entry: NeuterEntry) -> bool:
    """``git checkout -- <file>`` (never ``-- .``) then an overall
    ``git diff --quiet`` -- proof c's guard."""
    subprocess.run(["git", "checkout", "--", entry.file], cwd=workdir, check=True)
    return git_diff_quiet(workdir)


def py_compile_ok(workdir: Path, entry: NeuterEntry, python: str) -> bool:
    result = subprocess.run(
        [python, "-m", "py_compile", entry.file], cwd=workdir, capture_output=True
    )
    return result.returncode == 0


@dataclass
class WorkerContext:
    workdir: Path
    worker_idx: int
    python: str
    run_id: str
    sha: str
    evidence_dir: Path
    pg_handle: PgHandle | None
    pathcheck_expect: str
    lock_path: Path
    pathcheck_module: str = "audittrace"
    timeout_s: int = 900
    #: PYTHONPATH root prepended before the tested package resolves (real
    #: worker worktrees use ``<workdir>/src``; the self-proof fixture's
    #: toy ``guarded`` module sits at its repo root instead).
    src_root: str | None = None
    #: ``True`` when the CALLER (``_worker_main``, itself inside
    #: ``run_pool``'s own long-held lock) already holds ``lock_path`` --
    #: the chokepoint then only ASSERTS it, never re-acquires (which would
    #: self-conflict on a second file descriptor). ``False`` (default) is
    #: for a standalone caller (the self-proof fixture calls
    #: ``run_one_neuter`` directly, with no outer pool lock held) -- the
    #: chokepoint acquires+releases ``lock_path`` itself around this one
    #: invocation.
    already_locked: bool = False


def _base_row(
    ctx: WorkerContext, entry: NeuterEntry, matches: list[int], harness_version: str
) -> dict[str, Any]:
    return {
        "schema": 3,
        "run_id": ctx.run_id,
        "sha": ctx.sha,
        "harness_version": harness_version,
        "id": entry.id,
        "neuter_hash": compute_neuter_hash(entry),
        "worker": ctx.worker_idx,
        "file": entry.file,
        "matches": matches,
        "tests_expected": len(entry.tests),
        # Review round 3 should-fix: recorded so `should_skip` can refuse a
        # resume that would otherwise let a `--no-db` (fake) row satisfy a
        # real-Postgres run, or vice versa -- the two modes exercise
        # different code paths and must never be treated as equivalent.
        "fake_db": ctx.pg_handle.fake if ctx.pg_handle is not None else None,
    }


def run_one_neuter(
    ctx: WorkerContext, entry: NeuterEntry, harness_version: str = HARNESS_VERSION
) -> dict[str, Any]:
    """The per-neuter unit (§6 order, §4 classification). Real subprocesses,
    real git -- this is what both the multi-worker pool and the self-proof
    fixture call."""
    started = _now_iso()
    t0 = time.time()

    if not git_diff_quiet(ctx.workdir):
        raise DirtyTreeError(f"{ctx.workdir} dirty before {entry.id}")

    matches = apply_edits(ctx.workdir, entry)
    nocompile = not py_compile_ok(ctx.workdir, entry, ctx.python)

    exit_code: int | None = None
    timed_out = False
    pathcheck_ok = True
    db_before = db_after = None
    db_leak = False
    foreign_pg_container = False
    pg_settings = None
    junit_result = None
    junit_path = (
        ctx.evidence_dir
        / "junit"
        / f"{entry.id.replace('/', '_')}.w{ctx.worker_idx}.xml"
    )
    pathcheck_log = ctx.evidence_dir / f"pathcheck_w{ctx.worker_idx}.log"

    if not nocompile:
        want_pg_check = "postgres" in entry.engines and ctx.pg_handle is not None
        if want_pg_check:
            pg_settings = read_settings(ctx.pg_handle)
            try:
                assert_nondurable(pg_settings)
            except NonDurableSettingsError:
                # §8: the instrument reads the server back and refuses before
                # ever running a test against a container that silently
                # ignored the non-durable flags -- a mapping/environment
                # defect, not a test outcome, so the run never starts.
                restored_clean = restore(ctx.workdir, entry)
                row = _base_row(ctx, entry, matches, harness_version)
                row.update(
                    {
                        "tests_collected": None,
                        "outcomes": {},
                        "failed": [],
                        "failure_msgs": {},
                        "failure_types": [],
                        "errors": [],
                        "verdict": "ERROR",
                        "error_reason": "pg_settings",
                        "error_reasons": ["pg_settings"],
                        "vacuous": False,
                        "unmapped_red": [],
                        "drift_sampled": False,
                        "secs": round(time.time() - t0, 3),
                        "started_at": started,
                        "finished_at": _now_iso(),
                        "exit_code": None,
                        "timeout": False,
                        "restored_clean": restored_clean,
                        "db_before": None,
                        "db_after": None,
                        "db_leak": False,
                        "pg_settings": pg_settings,
                    }
                )
                return row
            db_before = db_snapshot(ctx.pg_handle)

        # THE chokepoint (review round 2): every pytest invocation, every
        # phase, funnels through here -- pins PYTHONPATH, passes the DSN,
        # watches for a foreign durable container, enforces the lock.
        src_root = ctx.src_root or f"{ctx.workdir}/src"
        pytest_result = run_pytest(
            workdir=ctx.workdir,
            pytest_args=list(entry.tests),
            python=ctx.python,
            lock_path=ctx.lock_path,
            timeout_s=ctx.timeout_s,
            junit_path=junit_path,
            pg_handle=ctx.pg_handle,
            check_db_leak=want_pg_check,
            pathcheck_module=ctx.pathcheck_module,
            pathcheck_expect=ctx.pathcheck_expect,
            pathcheck_log=pathcheck_log,
            src_root=src_root,
            already_locked=ctx.already_locked,
        )
        exit_code = pytest_result.exit_code
        timed_out = pytest_result.timed_out
        pathcheck_ok = bool(pytest_result.pathcheck_ok)
        db_before = pytest_result.db_before or db_before
        db_after = pytest_result.db_after
        db_leak = pytest_result.db_leak
        foreign_pg_container = pytest_result.foreign_pg_container

        junit_result = parse_junit(
            junit_path if junit_path.exists() else None, entry.tests
        )

    restored_clean = restore(ctx.workdir, entry)

    verdict = classify(
        entry.tests,
        junit_result,
        tests_expected=len(entry.tests),
        nocompile=nocompile,
        pathcheck_ok=pathcheck_ok,
        db_leak=db_leak,
        timed_out=timed_out,
        exit_code=exit_code,
        foreign_pg_container=foreign_pg_container,
    )
    secs = round(time.time() - t0, 3)

    row = _base_row(ctx, entry, matches, harness_version)
    row.update(
        {
            "tests_collected": verdict.tests_collected,
            "outcomes": verdict.outcomes,
            "failed": verdict.failed,
            "failure_msgs": verdict.failure_msgs,
            "failure_types": verdict.failure_types,
            "errors": verdict.errors,
            "verdict": verdict.verdict,
            "error_reason": verdict.error_reason,
            "error_reasons": verdict.error_reasons,
            "vacuous": verdict.vacuous,
            "unmapped_red": [],
            "drift_sampled": False,
            "secs": secs,
            "started_at": started,
            "finished_at": _now_iso(),
            "exit_code": exit_code,
            "timeout": timed_out,
            "restored_clean": restored_clean,
            "db_before": sorted(db_before[0]) if db_before else None,
            "db_after": sorted(db_after[0]) if db_after else None,
            "db_leak": db_leak,
            "pg_settings": pg_settings,
        }
    )
    return row


def should_skip(
    entry: NeuterEntry,
    existing: dict[str, Any] | None,
    *,
    sha: str,
    harness_version: str,
    fake_db: bool | None = None,
) -> bool:
    """Resume rule (§7): skip iff an existing row is RED/GREEN, restored
    clean, same sha, same neuter_hash, same harness_version, same DB mode.
    ERROR rows always re-run.

    ``fake_db`` (review round 3 should-fix): a row produced under
    ``--no-db`` (fake Postgres) exercises a DIFFERENT code path than one
    produced against a real container -- a resume must never let a row
    from one mode silently satisfy the other. ``None`` (the default)
    preserves the exact prior behaviour for callers that never pass it
    (existing rows with no ``fake_db`` field, from before this round, also
    read back as ``None`` and keep resuming as before -- comparing
    ``None == None`` -- rather than being invalidated retroactively)."""
    if existing is None:
        return False
    if existing.get("verdict") not in ("RED", "GREEN"):
        return False
    if not existing.get("restored_clean"):
        return False
    if existing.get("sha") != sha:
        return False
    if existing.get("harness_version") != harness_version:
        return False
    stored_fake_db = existing.get("fake_db")
    # A row from BEFORE this round (or a caller that legitimately doesn't
    # know/care about DB mode) records/passes `None` -- that's "no
    # information to compare", not "the modes differ", so it never refuses
    # a resume on its own. Only an EXPLICIT mismatch (both sides recorded,
    # and they disagree) refuses.
    if stored_fake_db is not None and fake_db is not None and stored_fake_db != fake_db:
        return False
    if existing.get("neuter_hash") != compute_neuter_hash(entry):
        return False
    return True


def load_existing_rows(evidence_dir: Path) -> dict[str, dict[str, Any]]:
    """Read every ``neuter_results_w*.jsonl`` under ``evidence_dir``, keyed
    by neuter id (last row wins)."""
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(evidence_dir.glob("neuter_results_w*.jsonl")):
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                rows[row["id"]] = row
    return rows


def foreign_audittrace_container_exists(run_id: str) -> bool:
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    own_prefix = f"audittrace-neuter-{run_id}-"
    return any(
        name.startswith("audittrace-") and not name.startswith(own_prefix)
        for name in result.stdout.splitlines()
    )


def run_baseline(
    spec: NeuterSpecFile,
    *,
    repo_dir: Path,
    parent_worktree_dir: Path,
    evidence_dir: Path,
    run_id: str,
    python: str,
    timeout_s: int,
    lock_path: Path,
    fake_db: bool = False,
    tmpfs: bool = False,
) -> tuple[bool, str]:
    """SPEC v3 §5: before any neuter runs, every mapped test must PASS at
    the pinned sha with NO edit applied. Returns ``(ok, detail)``.

    Runs through the SAME chokepoint every other phase uses (review round
    2): the venv's editable install would otherwise be imported instead of
    THIS worktree, and no foreign-durable-container watch would ever run
    for baseline at all.

    Review round 3 blocker 1: baseline now gets its OWN real (or fake, in
    ``--no-db`` mode) Postgres container and passes its DSN through the
    chokepoint -- REMOVING THE CAUSE, not just watching for it. A mapped
    test's import/test-body fixture (``tests/_pg_ephemeral.py``'s callers)
    finds ``AUDITTRACE_TEST_POSTGRES_URL`` already set and never starts its
    own durable ``audittrace-acl-wu2a-pg-*`` container in the first place.
    """
    worktree = parent_worktree_dir / f"at-nt-{run_id}-baseline"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), spec.sha],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    pg_handle = start_container(
        f"{run_id}-baseline",
        0,
        tmpfs=tmpfs,
        fake=fake_db,
        fake_dir=evidence_dir / "fake_pg" if fake_db else None,
    )
    try:
        all_tests = sorted({t for n in spec.neuters for t in n.tests})
        junit_path = evidence_dir / "junit" / "baseline.xml"
        pytest_result = run_pytest(
            workdir=worktree,
            pytest_args=all_tests,
            python=python,
            lock_path=lock_path,
            timeout_s=timeout_s,
            junit_path=junit_path,
            pg_handle=pg_handle,
            already_locked=True,
        )
        if pytest_result.foreign_pg_container:
            return False, "a foreign, durable product Postgres container was observed"
        if pytest_result.timed_out:
            return False, f"baseline timed out after {timeout_s}s"
        if pytest_result.exit_code not in (0,):
            full = parse_junit_full(junit_path if junit_path.exists() else None)
            failed = sorted(
                "::".join(k) for k, o in full.by_key.items() if o.outcome != "passed"
            )
            return False, f"exit={pytest_result.exit_code} failed={failed}"
        return True, ""
    finally:
        try:
            stop_container(pg_handle)
        except Exception:  # noqa: BLE001 - cleanup must not itself crash
            logger.error("baseline: failed to stop pg container", exc_info=True)
        if worktree.exists():
            if git_diff_quiet(worktree):
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=repo_dir,
                    capture_output=True,
                )
            else:
                # Should-fix (review round 2, deviation 2): a dirty
                # baseline worktree is kept and LOGGED, the same fail-safe
                # rule §6/proof c gives every worker -- previously this
                # left the dirt silently, with no event at all.
                logger.error("baseline: worktree %s left dirty, kept", worktree)
                append_event(
                    evidence_dir,
                    {
                        "event": "worktree_kept_dirty",
                        "phase": "baseline",
                        "path": str(worktree),
                    },
                )


def _mark_drift_unresolved(row: dict[str, Any], reason: str) -> None:
    """Review round 3 blocker 3: a drift measurement that could not be
    TRUSTED (dirty worktree, a foreign container observed, or the
    full-scope run never reproducing an originally-RED verdict) must NEVER
    pass silently as a flag riding along a RED/GREEN verdict -- it
    overwrites the row's own verdict to ERROR outright, so
    ``pool_exit_code``/``report --verify``'s existing ``error_n > 0``
    handling (never silent, requires ``--ack-errors``) covers it for free,
    instead of needing a SEPARATE, easy-to-forget check."""
    row["verdict"] = "ERROR"
    row["error_reason"] = reason
    row["error_reasons"] = [*row.get("error_reasons", []), reason]
    row[f"drift_{reason}"] = True


def sample_full_scope_drift(
    spec: NeuterSpecFile,
    rows: dict[str, dict[str, Any]],
    *,
    repo_dir: Path,
    parent_worktree_dir: Path,
    evidence_dir: Path,
    run_id: str,
    python: str,
    timeout_s: int,
    sample: float,
    lock_path: Path,
    fake_db: bool = False,
    tmpfs: bool = False,
) -> None:
    """SPEC v3 §5 sampled full-scope drift check: for a ``random.Random(run_id)``
    seeded sample of neuters, re-apply the edit and run the FULL
    ``scope_files`` (not just the mapped tests) THROUGH THE CHOKEPOINT.
    Any assertion-shaped failure OUTSIDE the mapped set is MEASURED and
    written back onto that row's ``unmapped_red`` -- never a constant.
    Mutates ``rows`` in place.

    Review round 3 blocker 1: drift gets its OWN real (or fake) Postgres
    container, DSN passed through the chokepoint -- removing the cause of
    an import/test-body fixture starting a durable product container,
    rather than only watching for one.

    Review round 3 blocker 3 (``not_reproduced`` was a silent flag): every
    condition that makes a drift measurement UNTRUSTWORTHY -- a dirty
    worktree before or after, a foreign container observed during the
    full-scope run, or the full-scope run never reproducing an originally
    RED verdict -- now overwrites the row's verdict to ERROR via
    :func:`_mark_drift_unresolved`, never a silent flag on a RED/GREEN row.
    """
    if sample <= 0 or not spec.neuters:
        return
    n_sample = max(1, math.ceil(sample * len(spec.neuters)))
    rng = random.Random(run_id)
    sampled = rng.sample(spec.neuters, k=min(n_sample, len(spec.neuters)))

    worktree = parent_worktree_dir / f"at-nt-{run_id}-drift"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), spec.sha],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    pg_handle = start_container(
        f"{run_id}-drift",
        0,
        tmpfs=tmpfs,
        fake=fake_db,
        fake_dir=evidence_dir / "fake_pg" if fake_db else None,
    )
    try:
        for entry in sampled:
            row = rows.get(entry.id)
            if row is None or row.get("verdict") not in ("RED", "GREEN"):
                continue  # ERROR/DIRTY rows are not drift-sampled
            if not git_diff_quiet(worktree):
                logger.error(
                    "drift sample: %s left %s dirty before start", entry.id, worktree
                )
                _mark_drift_unresolved(row, "dirty_before")
                continue
            apply_edits(worktree, entry)
            if not py_compile_ok(worktree, entry, python):
                subprocess.run(
                    ["git", "checkout", "--", entry.file], cwd=worktree, check=True
                )
                continue
            junit_path = (
                evidence_dir / "junit" / f"drift_{entry.id.replace('/', '_')}.xml"
            )
            drift_result = run_pytest(
                workdir=worktree,
                pytest_args=list(spec.scope_files),
                python=python,
                lock_path=lock_path,
                timeout_s=timeout_s,
                junit_path=junit_path,
                pg_handle=pg_handle,
                already_locked=True,
            )
            subprocess.run(
                ["git", "checkout", "--", entry.file], cwd=worktree, check=True
            )
            if not git_diff_quiet(worktree):
                logger.error(
                    "drift sample: %s left %s dirty after restore", entry.id, worktree
                )
                append_event(
                    evidence_dir,
                    {
                        "event": "worktree_kept_dirty",
                        "phase": "drift",
                        "id": entry.id,
                        "path": str(worktree),
                    },
                )
                _mark_drift_unresolved(row, "dirty_after")
                break  # this worktree is no longer trustworthy for the rest of the sample
            if drift_result.foreign_pg_container:
                _mark_drift_unresolved(row, "foreign_pg_container")
                continue
            full = parse_junit_full(junit_path if junit_path.exists() else None)
            if row.get("verdict") == "RED" and not mapped_failed_in_full(
                full, entry.tests
            ):
                # The full-scope run never reproduced the original RED at
                # all -- an unreliable measurement, not a clean one.
                _mark_drift_unresolved(row, "not_reproduced")
                continue
            unmapped_red = unmapped_assertion_shaped_failures(full, entry.tests)
            row["unmapped_red"] = unmapped_red
            row["drift_sampled"] = True
    finally:
        try:
            stop_container(pg_handle)
        except Exception:  # noqa: BLE001 - cleanup must not itself crash
            logger.error("drift: failed to stop pg container", exc_info=True)
        if worktree.exists():
            if git_diff_quiet(worktree):
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=repo_dir,
                    capture_output=True,
                )
            else:
                # Should-fix: a dirty drift worktree is kept and logged,
                # the same fail-safe rule baseline/every worker already
                # gets -- previously this was force-removed silently.
                logger.error("drift: worktree %s left dirty, kept", worktree)
                append_event(
                    evidence_dir,
                    {
                        "event": "worktree_kept_dirty",
                        "phase": "drift",
                        "path": str(worktree),
                    },
                )


def _sample_pool_metrics(
    evidence_dir: Path, stop: threading.Event, interval_s: float = 5.0
) -> None:
    """Background thread: append memory/load/container samples to
    ``pool_metrics.jsonl`` every ``interval_s`` while the pool runs
    (should-fix)."""
    while not stop.wait(interval_s):
        try:
            meminfo_lines = Path("/proc/meminfo").read_text().splitlines()
            total = int(
                next(ln for ln in meminfo_lines if ln.startswith("MemTotal:")).split()[
                    1
                ]
            )
            avail = int(
                next(
                    ln for ln in meminfo_lines if ln.startswith("MemAvailable:")
                ).split()[1]
            )
            load1 = os.getloadavg()[0]
            containers = subprocess.run(
                ["docker", "ps", "-q"], capture_output=True, text=True, timeout=5
            )
            n_containers = (
                len(containers.stdout.split()) if containers.returncode == 0 else 0
            )
            append_jsonl(
                evidence_dir / "pool_metrics.jsonl",
                {
                    "ts": _now_iso(),
                    "avail_mb": avail // 1024,
                    "used_mb": (total - avail) // 1024,
                    "load1": load1,
                    "containers": n_containers,
                },
            )
        except Exception:  # noqa: BLE001 - best-effort metrics, never fatal
            logger.debug("pool_metrics sample failed", exc_info=True)


def _worker_main(
    worker_idx: int,
    queue: mp.Queue[str | None],
    spec: NeuterSpecFile,
    neuters_by_id: dict[str, NeuterEntry],
    *,
    repo_dir: Path,
    parent_worktree_dir: Path,
    evidence_dir: Path,
    run_id: str,
    python: str,
    timeout_s: int,
    tmpfs: bool,
    fake_db: bool,
    pathcheck_module: str,
    src_root_relative: str,
    resume_rows: dict[str, Any],
    stop_flag: mp.Event,  # type: ignore[type-arg]
    lock_path: Path,
) -> None:
    worktree = parent_worktree_dir / f"at-nt-{run_id}-w{worker_idx}"
    out_path = evidence_dir / f"neuter_results_w{worker_idx}.jsonl"
    worktree_created = False
    pg_handle: PgHandle | None = None
    try:
        append_event(evidence_dir, {"event": "worker_start", "worker": worker_idx})
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), spec.sha],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        worktree_created = True
        pg_handle = start_container(
            run_id,
            worker_idx,
            tmpfs=tmpfs,
            fake=fake_db,
            fake_dir=evidence_dir / "fake_pg" if fake_db else None,
        )
        src_root = (
            str(worktree / src_root_relative) if src_root_relative else str(worktree)
        )
        ctx = WorkerContext(
            workdir=worktree,
            worker_idx=worker_idx,
            python=python,
            run_id=run_id,
            sha=spec.sha,
            evidence_dir=evidence_dir,
            pg_handle=pg_handle,
            pathcheck_expect=src_root,
            pathcheck_module=pathcheck_module,
            timeout_s=timeout_s,
            src_root=src_root,
            lock_path=lock_path,
            already_locked=True,
        )
        while not stop_flag.is_set():
            neuter_id = queue.get()
            if neuter_id is None:
                return
            entry = neuters_by_id[neuter_id]
            if should_skip(
                entry,
                resume_rows.get(neuter_id),
                sha=spec.sha,
                harness_version=HARNESS_VERSION,
                fake_db=fake_db,
            ):
                continue
            try:
                row = run_one_neuter(ctx, entry)
            except DirtyTreeError as exc:
                logger.error(
                    "worker %d: dirty tree, stopping pool: %s", worker_idx, exc
                )
                stop_flag.set()
                append_jsonl(
                    out_path,
                    {
                        "id": neuter_id,
                        "verdict": "DIRTY",
                        "restored_clean": False,
                        "worker": worker_idx,
                    },
                )
                append_event(
                    evidence_dir,
                    {
                        "event": "worker_dirty_stop",
                        "worker": worker_idx,
                        "id": neuter_id,
                    },
                )
                return
            append_jsonl(out_path, row)
            if not row["restored_clean"]:
                logger.error(
                    "worker %d: restore left %s dirty, stopping pool",
                    worker_idx,
                    neuter_id,
                )
                stop_flag.set()
                append_event(
                    evidence_dir,
                    {
                        "event": "worker_dirty_stop",
                        "worker": worker_idx,
                        "id": neuter_id,
                    },
                )
                return
    except Exception as exc:  # noqa: BLE001 - MUST be caught: an uncaught
        # exception here kills this mp.Process silently and the parent
        # would never know a single row is missing (blocker 1: fail-open).
        append_event(
            evidence_dir,
            {
                "event": "worker_crashed",
                "worker": worker_idx,
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            },
        )
        logger.error("worker %d crashed: %s", worker_idx, exc, exc_info=True)
    finally:
        if pg_handle is not None:
            try:
                stop_container(pg_handle)
            except Exception:  # noqa: BLE001 - cleanup must not itself crash
                logger.error(
                    "worker %d: failed to stop pg container", worker_idx, exc_info=True
                )
        if worktree_created and worktree.exists():
            clean = git_diff_quiet(worktree)
            if clean:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=repo_dir,
                    capture_output=True,
                )
            else:
                # §6 proof c / should-fix: a dirty worktree is KEPT and
                # logged, never force-removed -- force-removing here would
                # erase the evidence the dirty-restore path exists to keep.
                logger.error(
                    "worker %d: worktree %s left dirty, kept", worker_idx, worktree
                )
                append_event(
                    evidence_dir,
                    {
                        "event": "worktree_kept_dirty",
                        "worker": worker_idx,
                        "path": str(worktree),
                    },
                )
        append_event(evidence_dir, {"event": "worker_end", "worker": worker_idx})


def run_pool(
    spec: NeuterSpecFile,
    *,
    repo_dir: Path,
    parent_worktree_dir: Path,
    evidence_dir: Path,
    workers: int = 5,
    python: str | None = None,
    timeout_s: int = 900,
    tmpfs: bool = False,
    resume: bool = False,
    run_id: str | None = None,
    lock_path: Path | None = None,
    pathcheck_module: str = "audittrace",
    src_root_relative: str = "src",
    fake_db: bool = False,
    sample: float = 0.10,
    i_measured_it: bool = False,
    skip_baseline: bool = False,
    ack_drift: bool = False,
) -> int:
    """Orchestrate the whole pool run (§6). Returns the process exit code."""
    python = python or f"{repo_dir}/.venv/bin/python"
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%d%H%M%S")

    if evidence_dir_is_refused(evidence_dir):
        print(
            f"neuter pool: evidence dir refused (/tmp): {evidence_dir}", file=sys.stderr
        )
        return EXIT_SPEC_ERROR

    # Review round 2, blocker 3 (should-fix half): a non-empty evidence dir
    # from an EARLIER run must never be silently reused -- a second,
    # fully-crashed run into the same directory must not inherit the
    # first run's rows. `--resume` is the only sanctioned way to point a
    # run at an evidence dir that already has content.
    if (
        not resume
        and evidence_dir.exists()
        and any(evidence_dir.glob("neuter_results*.jsonl"))
    ):
        print(
            f"neuter pool: {evidence_dir} already has neuter_results*.jsonl "
            "(pass --resume to reuse it)",
            file=sys.stderr,
        )
        return EXIT_EVIDENCE_NOT_EMPTY
    evidence_dir.mkdir(parents=True, exist_ok=True)

    if workers > 10 and not i_measured_it:
        print(
            "neuter pool: --workers > 10 requires --i-measured-it (reason logged)",
            file=sys.stderr,
        )
        return EXIT_WORKERS_UNMEASURED

    resolved_lock = lockmod.resolve_lock_path(str(lock_path) if lock_path else None)
    lock_fd = lockmod.open_lock_file(resolved_lock)
    try:
        lockmod.try_flock(lock_fd, fcntl.LOCK_EX)
    except lockmod.LockHeldError:
        print(f"neuter pool: lock held at {resolved_lock}", file=sys.stderr)
        os.close(lock_fd)
        return EXIT_LOCK_HELD

    metrics_stop = threading.Event()
    metrics_thread = threading.Thread(
        target=_sample_pool_metrics, args=(evidence_dir, metrics_stop), daemon=True
    )
    metrics_thread.start()

    try:
        append_event(
            evidence_dir, {"event": "pool_start", "run_id": run_id, "workers": workers}
        )
        if lockmod.foreign_docker_build_running():
            print(
                "neuter pool: a docker build/buildx process is running", file=sys.stderr
            )
            return EXIT_LOCK_HELD
        if not fake_db and foreign_audittrace_container_exists(run_id):
            print(
                "neuter pool: a foreign audittrace-* container exists", file=sys.stderr
            )
            return EXIT_LOCK_HELD

        if not skip_baseline:
            baseline_ok, detail = run_baseline(
                spec,
                repo_dir=repo_dir,
                parent_worktree_dir=parent_worktree_dir,
                evidence_dir=evidence_dir,
                run_id=run_id,
                python=python,
                timeout_s=timeout_s,
                lock_path=resolved_lock,
                fake_db=fake_db,
                tmpfs=tmpfs,
            )
            append_event(
                evidence_dir,
                {"event": "baseline", "ok": baseline_ok, "detail": detail[:2000]},
            )
            if not baseline_ok:
                print(
                    f"neuter pool: baseline failed at {spec.sha}: {detail}",
                    file=sys.stderr,
                )
                return EXIT_BASELINE_FAILED

        resume_rows = load_existing_rows(evidence_dir) if resume else {}
        neuters_by_id = {n.id: n for n in spec.neuters}

        manager = mp.Manager()
        queue: mp.Queue[str | None] = manager.Queue()
        stop_flag = manager.Event()
        for neuter in spec.neuters:
            queue.put(neuter.id)
        for _ in range(workers):
            queue.put(None)

        procs = [
            mp.Process(
                target=_worker_main,
                args=(i + 1, queue, spec, neuters_by_id),
                kwargs=dict(
                    repo_dir=repo_dir,
                    parent_worktree_dir=parent_worktree_dir,
                    evidence_dir=evidence_dir,
                    run_id=run_id,
                    python=python,
                    timeout_s=timeout_s,
                    tmpfs=tmpfs,
                    fake_db=fake_db,
                    pathcheck_module=pathcheck_module,
                    src_root_relative=src_root_relative,
                    resume_rows=resume_rows,
                    stop_flag=stop_flag,
                    lock_path=resolved_lock,
                ),
            )
            for i in range(workers)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join()

        # Review round 2, blocker 3: filter to THIS run's own rows
        # (matching run_id) before anything downstream treats a row as
        # satisfying the count. A row left over from an earlier, unrelated
        # invocation into the SAME evidence dir must never count -- the
        # reviewer's stale-row attack (a first run completes, a second,
        # fully-crashed run reuses the dir) exited 0 on the FIRST run's
        # rows before this filter existed. `--resume`'s legitimately
        # skip-eligible prior rows are explicitly carried forward (their
        # sha/hash/harness_version were already re-validated by
        # `should_skip` before the worker decided to skip them).
        all_rows = load_existing_rows(evidence_dir)
        rows = {
            rid: row for rid, row in all_rows.items() if row.get("run_id") == run_id
        }
        if resume:
            for rid, row in resume_rows.items():
                if rid in rows:
                    continue
                entry = neuters_by_id.get(rid)
                if entry is not None and should_skip(
                    entry,
                    row,
                    sha=spec.sha,
                    harness_version=HARNESS_VERSION,
                    fake_db=fake_db,
                ):
                    rows[rid] = row

        if sample > 0:
            sample_full_scope_drift(
                spec,
                rows,
                repo_dir=repo_dir,
                parent_worktree_dir=parent_worktree_dir,
                evidence_dir=evidence_dir,
                run_id=run_id,
                python=python,
                timeout_s=timeout_s,
                sample=sample,
                lock_path=resolved_lock,
                fake_db=fake_db,
                tmpfs=tmpfs,
            )

        merged = evidence_dir / "neuter_results.jsonl"
        with open(merged, "w") as fh:
            for row in rows.values():
                fh.write(json.dumps(row) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

        append_event(
            evidence_dir, {"event": "pool_end", "run_id": run_id, "rows": len(rows)}
        )

        if stop_flag.is_set():
            return EXIT_DIRTY_TREE

        # Blocker 1: fail CLOSED. Every neuter in the spec must have
        # produced exactly one row -- a crashed worker (or any other
        # cause) leaving gaps is never silently exit 0.
        expected_ids = {n.id for n in spec.neuters}
        missing = sorted(expected_ids - set(rows.keys()))
        if missing:
            crashes = [
                json.loads(line)
                for line in (evidence_dir / "events.jsonl").read_text().splitlines()
                if '"worker_crashed"' in line
            ]
            for c in crashes:
                print(
                    f"neuter pool: worker {c.get('worker')} crashed: {c.get('error')}",
                    file=sys.stderr,
                )
            print(
                f"neuter pool: {len(missing)} neuter(s) never produced a row: {missing}",
                file=sys.stderr,
            )
            return EXIT_MISSING_ROWS

        return pool_exit_code(rows, ack_drift=ack_drift)
    finally:
        metrics_stop.set()
        os.close(lock_fd)


def pool_exit_code(rows: dict[str, dict[str, Any]], *, ack_drift: bool = False) -> int:
    """The whole run's exit code from its merged rows (§4 M7, §8): a
    ``pg_settings`` row takes priority (exit 6); else any row with a
    non-empty ``unmapped_red`` is UNACKNOWLEDGED DRIFT (exit 14) unless
    ``ack_drift`` -- review round 2 blocker 4: a RED row with drift is not
    "clean" just because its own targeted verdict happened to be RED; the
    neuter's ``tests`` mapping is incomplete either way, and that must be
    surfaced, not silently folded into a plain RED/GREEN summary; else any
    ``ERROR`` row exits 9; else any ``GREEN`` (vacuous) row exits 2; else 0.
    """
    if any(r.get("error_reason") == "pg_settings" for r in rows.values()):
        return EXIT_NONDURABLE_SETTINGS
    if not ack_drift and any(r.get("unmapped_red") for r in rows.values()):
        return EXIT_DRIFT_UNACKNOWLEDGED
    verdicts = {r.get("verdict") for r in rows.values()}
    if "ERROR" in verdicts:
        return 9
    if "GREEN" in verdicts:
        return 2
    return EXIT_OK
