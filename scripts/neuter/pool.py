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
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import multiprocessing as mp
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.neuter import lock as lockmod
from scripts.neuter.classify import classify
from scripts.neuter.junit import parse_junit
from scripts.neuter.pg import (
    EXIT_NONDURABLE_SETTINGS,
    NonDurableSettingsError,
    PgHandle,
    assert_nondurable,
    db_snapshot,
    poll_db_leak,
    read_settings,
    start_container,
    stop_container,
)
from scripts.neuter.spec import NeuterEntry, NeuterSpecFile

logger = logging.getLogger(__name__)

HARNESS_VERSION = "1"

EXIT_OK = 0
EXIT_DIRTY_TREE = 4
EXIT_LOCK_HELD = 8


class DirtyTreeError(Exception):
    """A worktree was dirty before a neuter started (uncommitted foreign state)."""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def git_diff_quiet(workdir: Path, *paths: str) -> bool:
    return (
        subprocess.run(["git", "diff", "--quiet", *paths], cwd=workdir).returncode == 0
    )


def _read_file(workdir: Path, relpath: str) -> str:
    return (workdir / relpath).read_text()


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


def _log_line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open() as fh:
        return sum(1 for _ in fh)


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
    pathcheck_module: str = "audittrace"
    timeout_s: int = 900
    #: PYTHONPATH root prepended before the tested package resolves (real
    #: worker worktrees use ``<workdir>/src``; the self-proof fixture's
    #: toy ``guarded`` module sits at its repo root instead).
    src_root: str | None = None


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
    pg_settings = None
    junit_result = None
    junit_path = (
        ctx.evidence_dir
        / "junit"
        / f"{entry.id.replace('/', '_')}.w{ctx.worker_idx}.xml"
    )
    pathcheck_log = ctx.evidence_dir / f"pathcheck_w{ctx.worker_idx}.log"

    if not nocompile:
        want_pg = "postgres" in entry.engines and ctx.pg_handle is not None
        if want_pg:
            pg_settings = read_settings(ctx.pg_handle)
            try:
                assert_nondurable(pg_settings)
            except NonDurableSettingsError:
                # §8: the instrument reads the server back and refuses before
                # ever running a test against a container that silently
                # ignored the non-durable flags -- a mapping/environment
                # defect, not a test outcome, so the run never starts.
                restored_clean = restore(ctx.workdir, entry)
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
            db_before = db_snapshot(ctx.pg_handle)

        junit_path.parent.mkdir(parents=True, exist_ok=True)
        before_lines = _log_line_count(pathcheck_log)
        env = dict(os.environ)
        src_root = ctx.src_root or f"{ctx.workdir}/src"
        env["PYTHONPATH"] = f"{src_root}:{Path(__file__).resolve().parent}"
        env["AUDITTRACE_NEUTER_PATHCHECK_MODULE"] = ctx.pathcheck_module
        env["AUDITTRACE_NEUTER_PATHCHECK_EXPECT"] = ctx.pathcheck_expect
        env["AUDITTRACE_NEUTER_PATHCHECK_LOG"] = str(pathcheck_log)
        if want_pg and ctx.pg_handle is not None and not ctx.pg_handle.fake:
            env["AUDITTRACE_TEST_POSTGRES_URL"] = ctx.pg_handle.dsn
        if (
            ctx.pg_handle is not None
            and ctx.pg_handle.fake
            and ctx.pg_handle.fake_state_path is not None
        ):
            env["AUDITTRACE_NEUTER_FAKE_PG_STATE"] = str(ctx.pg_handle.fake_state_path)

        cmd = [
            ctx.python,
            "-m",
            "pytest",
            *entry.tests,
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            "-p",
            "neuter_pathcheck",
            "-rfE",
            f"--junitxml={junit_path}",
        ]
        try:
            result = subprocess.run(
                cmd,
                cwd=ctx.workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=ctx.timeout_s,
            )
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            timed_out = True

        pathcheck_ok = _log_line_count(pathcheck_log) > before_lines

        if want_pg and ctx.pg_handle is not None:
            db_leak = poll_db_leak(ctx.pg_handle, db_before)
            db_after = db_snapshot(ctx.pg_handle)

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
    )
    secs = round(time.time() - t0, 3)

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


def should_skip(
    entry: NeuterEntry,
    existing: dict[str, Any] | None,
    *,
    sha: str,
    harness_version: str,
) -> bool:
    """Resume rule (§7): skip iff an existing row is RED/GREEN, restored
    clean, same sha, same neuter_hash, same harness_version. ERROR rows
    always re-run."""
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
) -> None:
    worktree = parent_worktree_dir / f"at-nt-{run_id}-w{worker_idx}"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), spec.sha],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    pg_handle = start_container(
        run_id,
        worker_idx,
        tmpfs=tmpfs,
        fake=fake_db,
        fake_dir=evidence_dir / "fake_pg" if fake_db else None,
    )
    out_path = evidence_dir / f"neuter_results_w{worker_idx}.jsonl"
    src_root = str(worktree / src_root_relative) if src_root_relative else str(worktree)
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
    )
    try:
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
                return
            append_jsonl(out_path, row)
            if not row["restored_clean"]:
                logger.error(
                    "worker %d: restore left %s dirty, stopping pool",
                    worker_idx,
                    neuter_id,
                )
                stop_flag.set()
                return
    finally:
        stop_container(pg_handle)
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=repo_dir,
            capture_output=True,
        )


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
) -> int:
    """Orchestrate the whole pool run (§6). Returns the process exit code."""
    python = python or f"{repo_dir}/.venv/bin/python"
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    evidence_dir.mkdir(parents=True, exist_ok=True)

    resolved_lock = lockmod.resolve_lock_path(str(lock_path) if lock_path else None)
    lock_fd = lockmod.open_lock_file(resolved_lock)
    try:
        lockmod.try_flock(lock_fd, fcntl.LOCK_EX)
    except lockmod.LockHeldError:
        print(f"neuter pool: lock held at {resolved_lock}", file=sys.stderr)
        os.close(lock_fd)
        return EXIT_LOCK_HELD

    try:
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
                ),
            )
            for i in range(workers)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join()

        if stop_flag.is_set():
            return EXIT_DIRTY_TREE

        merged = evidence_dir / "neuter_results.jsonl"
        rows = load_existing_rows(evidence_dir)
        with open(merged, "w") as fh:
            for row in rows.values():
                fh.write(json.dumps(row) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

        return pool_exit_code(rows)
    finally:
        os.close(lock_fd)


def pool_exit_code(rows: dict[str, dict[str, Any]]) -> int:
    """The whole run's exit code from its merged rows (§4 M7, §8): a
    ``pg_settings`` row takes priority (exit 6); else any ``ERROR`` row
    exits 9; else any ``GREEN`` (vacuous) row exits 2; else 0."""
    if any(r.get("error_reason") == "pg_settings" for r in rows.values()):
        return EXIT_NONDURABLE_SETTINGS
    verdicts = {r.get("verdict") for r in rows.values()}
    if "ERROR" in verdicts:
        return 9
    if "GREEN" in verdicts:
        return 2
    return EXIT_OK
