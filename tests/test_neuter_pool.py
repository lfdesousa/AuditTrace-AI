"""Coverage-closing unit tests for ``scripts/neuter/pool.py`` branches the
self-proofs (``tests/test_neuter_self_proofs.py``) don't reach directly --
mainly code that only runs inside a ``multiprocessing.Process`` child
(invisible to the parent's coverage measurement) and ``run_pool``'s rarer
guard branches.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from scripts.neuter import lock as lockmod
from scripts.neuter.pg import PgHandle
from scripts.neuter.pool import (
    EXIT_BASELINE_FAILED,
    EXIT_DRIFT_UNACKNOWLEDGED,
    EXIT_EVIDENCE_NOT_EMPTY,
    EXIT_MISSING_ROWS,
    DirtyTreeError,
    WorkerContext,
    _sample_pool_metrics,
    _worker_main,
    apply_edits,
    foreign_audittrace_container_exists,
    pool_exit_code,
    run_baseline,
    run_one_neuter,
    run_pool,
    sample_full_scope_drift,
    should_skip,
)
from scripts.neuter.spec import (
    Edit,
    GuardTestEntry,
    NeuterEntry,
    NeuterSpecFile,
    load_neuter_spec,
)
from tests._neuter_test_evidence import evidence_dir_for as _ev

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


def _sha(repo: Path) -> str:
    return _run_git(repo, "rev-parse", "HEAD").stdout.strip()


def _entry(**kwargs) -> NeuterEntry:
    base = dict(guard="G", clause="C", engines=["mock"], expect="RED")
    base.update(kwargs)
    return NeuterEntry(**base)


@contextlib.contextmanager
def _held_lock(path: Path):
    """A direct ``_worker_main(...)`` call (bypassing ``run_pool``) still
    constructs its ``WorkerContext`` with ``already_locked=True`` -- matching
    what a REAL worker always sees (``run_pool`` holds the lock for the
    whole run). These tests must therefore hold ``path`` themselves for the
    call's duration, or the chokepoint's own lock assertion (correctly)
    raises."""
    fd = lockmod.open_lock_file(path)
    lockmod.try_flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        os.close(fd)


def _ctx(repo: Path, evidence_dir: Path, **overrides) -> WorkerContext:
    base = dict(
        workdir=repo,
        worker_idx=1,
        python=PYTHON,
        run_id="pooltest",
        sha=_sha(repo),
        evidence_dir=evidence_dir,
        pg_handle=None,
        pathcheck_expect=str(repo),
        pathcheck_module="guarded",
        timeout_s=30,
        src_root=str(repo),
        lock_path=evidence_dir.parent / "test.lock",
    )
    base.update(overrides)
    return WorkerContext(**base)


# ───────────────────────── should_skip: existing is None ────────────────


def test_should_skip_none_existing_row_returns_false():
    entry = _entry(
        id="x", file="guarded.py", edits=[Edit(old="a", new="b")], tests=["t"]
    )
    assert should_skip(entry, None, sha="s", harness_version="1") is False


# ───────────────────────── apply_edits runtime guard ─────────────────────


def test_apply_edits_raises_runtime_error_on_bad_match_count(tmp_path):
    repo = _init_repo(tmp_path)
    entry = _entry(
        id="x",
        file="guarded.py",
        edits=[Edit(old="nonexistent_text", new="y")],
        tests=["t"],
    )
    with pytest.raises(RuntimeError, match="runtime match count"):
        apply_edits(repo, entry)


# ───────────────────── run_one_neuter: pre-existing dirty tree ──────────


def test_run_one_neuter_raises_dirty_tree_error_on_preexisting_dirt(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "guarded.py").write_text((repo / "guarded.py").read_text() + "\n# dirt\n")
    entry = _entry(
        id="x",
        file="guarded.py",
        edits=[Edit(old="    return x + 1", new="    return x + 2")],
        tests=["test_guarded.py::test_a"],
    )
    with pytest.raises(DirtyTreeError):
        run_one_neuter(_ctx(repo, _ev(tmp_path)), entry)


# ───────────────────── foreign_audittrace_container_exists ──────────────


def test_foreign_audittrace_container_exists_false_when_none(monkeypatch):
    # Deterministic even when a REAL sibling RLS-proof container (from
    # tests/test_rls_isolation.py etc., also prefixed "audittrace-") is
    # alive in the same pytest session -- never depend on ambient docker
    # state for a "false" assertion.
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert foreign_audittrace_container_exists("norun") is False


def test_foreign_audittrace_container_exists_true_for_a_foreign_name(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout="audittrace-someone-else-w1\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert foreign_audittrace_container_exists("myrun") is True


def test_foreign_audittrace_container_exists_false_on_nonzero_exit(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, stdout="", stderr="docker not found"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert foreign_audittrace_container_exists("myrun") is False


# ───────────────────────── _worker_main, called directly ────────────────


def _spec_two_neuters(repo: Path) -> NeuterSpecFile:
    e1 = NeuterEntry(
        id="w1",
        file="guarded.py",
        edits=[Edit(old="    return x + 1", new="    return x + 2")],
        tests=["test_guarded.py::test_a"],
        engines=["mock"],
        guard="G1",
        clause="",
        expect="RED",
    )
    e2 = NeuterEntry(
        id="w2",
        file="guarded.py",
        edits=[Edit(old="    return x * 2", new="    return x * 3")],
        tests=["test_guarded.py::test_b"],
        engines=["mock"],
        guard="G2",
        clause="",
        expect="RED",
    )
    return NeuterSpecFile(
        schema=3,
        sha=_sha(repo),
        scope_files=["test_guarded.py"],
        guard_tests=[
            GuardTestEntry(id="test_guarded.py::test_a", row="G1"),
            GuardTestEntry(id="test_guarded.py::test_b", row="G2"),
        ],
        neuters=[e1, e2],
        path=Path("unused.json"),
    )


def test_worker_main_direct_drains_queue_and_writes_rows(tmp_path):
    repo = _init_repo(tmp_path)
    spec = _spec_two_neuters(repo)
    neuters_by_id = {n.id: n for n in spec.neuters}
    q: queue.Queue[str | None] = queue.Queue()
    for n in spec.neuters:
        q.put(n.id)
    q.put(None)
    stop_flag = threading.Event()
    evidence = _ev(tmp_path)
    with _held_lock(tmp_path / "worker.lock"):
        _worker_main(
            1,
            q,
            spec,
            neuters_by_id,
            repo_dir=repo,
            parent_worktree_dir=tmp_path,
            evidence_dir=evidence,
            run_id="direct",
            python=PYTHON,
            timeout_s=30,
            tmpfs=False,
            fake_db=True,
            pathcheck_module="guarded",
            src_root_relative="",
            resume_rows={},
            stop_flag=stop_flag,
            lock_path=tmp_path / "worker.lock",
        )
    rows = [
        json.loads(line)
        for line in (evidence / "neuter_results_w1.jsonl").read_text().splitlines()
    ]
    assert {r["id"] for r in rows} == {"w1", "w2"}
    assert not stop_flag.is_set()
    # the worktree was created and cleaned up.
    assert _run_git(repo, "worktree", "list").stdout.count("\n") == 1


def test_worker_main_direct_skips_via_resume_rows(tmp_path):
    repo = _init_repo(tmp_path)
    spec = _spec_two_neuters(repo)
    neuters_by_id = {n.id: n for n in spec.neuters}
    from scripts.neuter.pool import compute_neuter_hash

    resume_rows = {
        "w1": {
            "verdict": "RED",
            "restored_clean": True,
            "sha": spec.sha,
            "harness_version": "1",
            "neuter_hash": compute_neuter_hash(neuters_by_id["w1"]),
        }
    }
    q: queue.Queue[str | None] = queue.Queue()
    q.put("w1")
    q.put("w2")
    q.put(None)
    stop_flag = threading.Event()
    evidence = _ev(tmp_path)
    with _held_lock(tmp_path / "worker.lock"):
        _worker_main(
            1,
            q,
            spec,
            neuters_by_id,
            repo_dir=repo,
            parent_worktree_dir=tmp_path,
            evidence_dir=evidence,
            run_id="direct",
            python=PYTHON,
            timeout_s=30,
            tmpfs=False,
            fake_db=True,
            pathcheck_module="guarded",
            src_root_relative="",
            resume_rows=resume_rows,
            stop_flag=stop_flag,
            lock_path=tmp_path / "worker.lock",
        )
    rows = [
        json.loads(line)
        for line in (evidence / "neuter_results_w1.jsonl").read_text().splitlines()
    ]
    assert {r["id"] for r in rows} == {"w2"}  # w1 skipped


def test_worker_main_direct_dirty_tree_error_stops_and_logs(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    spec = _spec_two_neuters(repo)
    neuters_by_id = {n.id: n for n in spec.neuters}

    def raise_dirty(ctx, entry):
        raise DirtyTreeError("simulated")

    monkeypatch.setattr("scripts.neuter.pool.run_one_neuter", raise_dirty)
    q: queue.Queue[str | None] = queue.Queue()
    q.put("w1")
    q.put(None)
    stop_flag = threading.Event()
    evidence = _ev(tmp_path)
    _worker_main(
        1,
        q,
        spec,
        neuters_by_id,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=evidence,
        run_id="direct",
        python=PYTHON,
        timeout_s=30,
        tmpfs=False,
        fake_db=True,
        pathcheck_module="guarded",
        src_root_relative="",
        resume_rows={},
        stop_flag=stop_flag,
        lock_path=tmp_path / "worker.lock",
    )
    assert stop_flag.is_set()
    rows = [
        json.loads(line)
        for line in (evidence / "neuter_results_w1.jsonl").read_text().splitlines()
    ]
    assert rows[0]["verdict"] == "DIRTY"


def test_worker_main_direct_restored_dirty_stops_pool(tmp_path):
    repo = _init_repo(tmp_path)
    dirty_entry = NeuterEntry(
        id="c1",
        file="test_guarded.py",
        edits=[
            Edit(
                old="def test_a():\n    assert guard_a(1) == 2",
                new=(
                    "def test_a():\n"
                    "    with open('data.txt', 'a') as fh:\n"
                    "        fh.write('x')\n"
                    "    assert guard_a(1) == 2"
                ),
            )
        ],
        tests=["test_guarded.py::test_a"],
        engines=["mock"],
        guard="G",
        clause="",
        expect="RED",
    )
    spec = NeuterSpecFile(
        schema=3,
        sha=_sha(repo),
        scope_files=["test_guarded.py"],
        guard_tests=[GuardTestEntry(id="test_guarded.py::test_a", row="G")],
        neuters=[dirty_entry],
        path=Path("unused.json"),
    )
    neuters_by_id = {n.id: n for n in spec.neuters}
    q: queue.Queue[str | None] = queue.Queue()
    q.put("c1")
    q.put(None)
    stop_flag = threading.Event()
    evidence = _ev(tmp_path)
    with _held_lock(tmp_path / "worker.lock"):
        _worker_main(
            1,
            q,
            spec,
            neuters_by_id,
            repo_dir=repo,
            parent_worktree_dir=tmp_path,
            evidence_dir=evidence,
            run_id="direct",
            python=PYTHON,
            timeout_s=30,
            tmpfs=False,
            fake_db=True,
            pathcheck_module="guarded",
            src_root_relative="",
            resume_rows={},
            stop_flag=stop_flag,
            lock_path=tmp_path / "worker.lock",
        )
    assert stop_flag.is_set()
    rows = [
        json.loads(line)
        for line in (evidence / "neuter_results_w1.jsonl").read_text().splitlines()
    ]
    assert rows[0]["restored_clean"] is False
    _run_git(repo, "checkout", "--", "data.txt")


# ─────────────────────────── run_pool-level branches ─────────────────────


def test_run_pool_dirty_restore_returns_exit_4(tmp_path):
    repo = _init_repo(tmp_path)
    dirty_entry = {
        "id": "c1",
        "file": "test_guarded.py",
        "edits": [
            {
                "old": "def test_a():\n    assert guard_a(1) == 2",
                "new": (
                    "def test_a():\n"
                    "    with open('data.txt', 'a') as fh:\n"
                    "        fh.write('x')\n"
                    "    assert guard_a(1) == 2"
                ),
            }
        ],
        "tests": ["test_guarded.py::test_a"],
        "engines": ["mock"],
        "guard": "G",
    }
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema": 3,
                "sha": _sha(repo),
                "scope_files": ["test_guarded.py"],
                "guard_tests": [{"id": "test_guarded.py::test_a", "row": "G"}],
                "neuters": [dirty_entry],
            }
        )
    )
    spec = load_neuter_spec(
        spec_path, repo_dir=repo, python=PYTHON, lock_path=tmp_path / "collect.lock"
    )
    exit_code = run_pool(
        spec,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=_ev(tmp_path),
        workers=1,
        python=PYTHON,
        fake_db=True,
        lock_path=tmp_path / "pool.lock",
        pathcheck_module="guarded",
        src_root_relative="",
    )
    assert exit_code == 4
    _run_git(repo, "worktree", "prune")


def test_pool_exit_code_pg_settings_takes_priority():
    rows = {
        "a": {"verdict": "ERROR", "error_reason": "pg_settings"},
        "b": {"verdict": "GREEN"},
    }
    assert pool_exit_code(rows) == 6


def test_pool_exit_code_error_verdict():
    rows = {"a": {"verdict": "ERROR", "error_reason": "call_exception"}}
    assert pool_exit_code(rows) == 9


def test_pool_exit_code_green_verdict():
    rows = {"a": {"verdict": "GREEN"}, "b": {"verdict": "RED"}}
    assert pool_exit_code(rows) == 2


def test_pool_exit_code_all_red_is_ok():
    rows = {"a": {"verdict": "RED"}}
    assert pool_exit_code(rows) == 0


def test_run_pool_error_verdict_returns_exit_9(tmp_path):
    repo = _init_repo(tmp_path)
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema": 3,
                "sha": _sha(repo),
                "scope_files": ["test_guarded.py"],
                "guard_tests": [{"id": "test_guarded.py::test_uses_seed", "row": "G"}],
                "neuters": [
                    {
                        "id": "i1",
                        "file": "guarded.py",
                        "edits": [
                            {
                                "old": "    return 41",
                                "new": "    raise RuntimeError('boom')",
                            }
                        ],
                        "tests": ["test_guarded.py::test_uses_seed"],
                        "engines": ["mock"],
                        "guard": "G",
                    }
                ],
            }
        )
    )
    spec = load_neuter_spec(
        spec_path, repo_dir=repo, python=PYTHON, lock_path=tmp_path / "collect.lock"
    )
    exit_code = run_pool(
        spec,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=_ev(tmp_path),
        workers=1,
        python=PYTHON,
        fake_db=True,
        lock_path=tmp_path / "pool.lock",
        pathcheck_module="guarded",
        src_root_relative="",
    )
    assert exit_code == 9


def test_run_pool_refuses_when_docker_build_running(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema": 3,
                "sha": _sha(repo),
                "scope_files": ["test_guarded.py"],
                "guard_tests": [{"id": "test_guarded.py::test_a", "row": "G"}],
                "neuters": [
                    {
                        "id": "d1",
                        "file": "guarded.py",
                        "edits": [
                            {"old": "    return x + 1", "new": "    return x + 2"}
                        ],
                        "tests": ["test_guarded.py::test_a"],
                        "engines": ["mock"],
                        "guard": "G",
                    }
                ],
            }
        )
    )
    spec = load_neuter_spec(
        spec_path, repo_dir=repo, python=PYTHON, lock_path=tmp_path / "collect.lock"
    )
    monkeypatch.setattr(lockmod, "foreign_docker_build_running", lambda: True)
    exit_code = run_pool(
        spec,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=_ev(tmp_path),
        workers=1,
        python=PYTHON,
        fake_db=True,
        lock_path=tmp_path / "pool.lock",
    )
    assert exit_code == 8


def test_run_pool_refuses_when_foreign_container_exists(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema": 3,
                "sha": _sha(repo),
                "scope_files": ["test_guarded.py"],
                "guard_tests": [{"id": "test_guarded.py::test_a", "row": "G"}],
                "neuters": [
                    {
                        "id": "d2",
                        "file": "guarded.py",
                        "edits": [
                            {"old": "    return x + 1", "new": "    return x + 2"}
                        ],
                        "tests": ["test_guarded.py::test_a"],
                        "engines": ["mock"],
                        "guard": "G",
                    }
                ],
            }
        )
    )
    spec = load_neuter_spec(
        spec_path, repo_dir=repo, python=PYTHON, lock_path=tmp_path / "collect.lock"
    )
    monkeypatch.setattr(
        "scripts.neuter.pool.foreign_audittrace_container_exists", lambda run_id: True
    )
    exit_code = run_pool(
        spec,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=_ev(tmp_path),
        workers=1,
        python=PYTHON,
        fake_db=False,
        lock_path=tmp_path / "pool.lock",
    )
    assert exit_code == 8


# ────────────── review round-1 blocker 1: fail-open (fake docker) ────────


def test_run_pool_fails_closed_when_every_worker_crashes(tmp_path, monkeypatch):
    """A fake ``docker`` that exits 1 crashes EVERY worker in
    ``start_container``. The pool must fail CLOSED: a non-zero exit and the
    missing ids listed -- never a silent exit 0 with 0 rows (the reviewer's
    x3 fixture attack)."""
    repo = _init_repo(tmp_path)
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 1\n")
    fake_docker.chmod(0o755)

    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema": 3,
                "sha": _sha(repo),
                "scope_files": ["test_guarded.py"],
                "guard_tests": [{"id": "test_guarded.py::test_a", "row": "G"}],
                "neuters": [
                    {
                        "id": "crash1",
                        "file": "guarded.py",
                        "edits": [
                            {"old": "    return x + 1", "new": "    return x + 2"}
                        ],
                        "tests": ["test_guarded.py::test_a"],
                        "engines": ["mock"],
                        "guard": "G",
                    }
                ],
            }
        )
    )
    spec = load_neuter_spec(
        spec_path, repo_dir=repo, python=PYTHON, lock_path=tmp_path / "collect.lock"
    )

    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    evidence = _ev(tmp_path)
    exit_code = run_pool(
        spec,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=evidence,
        workers=2,
        python=PYTHON,
        fake_db=False,
        lock_path=tmp_path / "pool.lock",
        skip_baseline=True,
        sample=0,
    )
    assert exit_code == EXIT_MISSING_ROWS
    # the crash is collected and surfaced, not silently dropped.
    events = [
        json.loads(line)
        for line in (evidence / "events.jsonl").read_text().splitlines()
    ]
    crashes = [e for e in events if e.get("event") == "worker_crashed"]
    assert len(crashes) == 2
    # Review round 4 should-fix: docker failures are a typed
    # DockerUnavailableError now, not a raw CalledProcessError.
    assert "DockerUnavailableError" in crashes[0]["error"]
    # no rows at all -- the neuter never produced any evidence.
    assert (
        not (evidence / "neuter_results.jsonl").exists()
        or (evidence / "neuter_results.jsonl").read_text().strip() == ""
    )
    # no worktrees leaked: only the main repo checkout remains.
    assert _run_git(repo, "worktree", "list").stdout.count("\n") == 1


# ─────────────── review round-1 blocker 2: baseline-first (§5) ───────────


def test_run_pool_baseline_failure_exits_5_never_red(tmp_path):
    """A no-op neuter mapped to a test that's ALREADY failing at the pinned
    sha must give exit 5 (baseline-first), never a false RED (the
    reviewer's PREFAIL fixture attack)."""
    repo = _init_repo(tmp_path)
    broken = (
        (repo / "test_guarded.py")
        .read_text()
        .replace(
            "def test_b():\n    assert guard_b(2) == 4",
            "def test_b():\n    assert guard_b(2) == 999  # already broken at the pinned sha",
        )
    )
    assert broken != (repo / "test_guarded.py").read_text()
    (repo / "test_guarded.py").write_text(broken)
    _run_git(repo, "commit", "-am", "break test_b before any neuter runs")

    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema": 3,
                "sha": _sha(repo),
                "scope_files": ["test_guarded.py"],
                "guard_tests": [{"id": "test_guarded.py::test_b", "row": "G"}],
                "neuters": [
                    {
                        "id": "noop-on-failing-baseline",
                        "file": "guarded.py",
                        "edits": [
                            {
                                "old": "    return x * 2",
                                "new": "    return x * 2  # no-op",
                            }
                        ],
                        "tests": ["test_guarded.py::test_b"],
                        "engines": ["mock"],
                        "guard": "G",
                    }
                ],
            }
        )
    )
    spec = load_neuter_spec(
        spec_path, repo_dir=repo, python=PYTHON, lock_path=tmp_path / "collect.lock"
    )
    evidence = _ev(tmp_path)
    exit_code = run_pool(
        spec,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=evidence,
        workers=1,
        python=PYTHON,
        fake_db=True,
        lock_path=tmp_path / "pool.lock",
        pathcheck_module="guarded",
        src_root_relative="",
    )
    assert exit_code == EXIT_BASELINE_FAILED
    # no neuter ever ran -- there is no RED row to point to.
    assert (
        not (evidence / "neuter_results.jsonl").exists()
        or (evidence / "neuter_results.jsonl").read_text().strip() == ""
    )


# ────────── review round-1 blocker 3: X6 undetectable (sampled drift) ────


def test_run_pool_drift_sample_measures_real_unmapped_red(tmp_path):
    """A neuter that breaks ``guard_b`` but is mapped only to ``test_a``
    (wrong mapping, X6-shaped) reads GREEN on the targeted pass -- the
    sampled full-scope drift check must MEASURE that ``test_b`` also fails
    and record it as ``unmapped_red``, never a hardcoded ``[]``."""
    repo = _init_repo(tmp_path)
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema": 3,
                "sha": _sha(repo),
                "scope_files": ["test_guarded.py"],
                "guard_tests": [{"id": "test_guarded.py::test_a", "row": "G"}],
                "neuters": [
                    {
                        "id": "x6-wrong-mapping",
                        "file": "guarded.py",
                        "edits": [
                            {"old": "    return x * 2", "new": "    return x * 3"}
                        ],
                        "tests": ["test_guarded.py::test_a"],
                        "engines": ["mock"],
                        "guard": "G",
                    }
                ],
            }
        )
    )
    spec = load_neuter_spec(
        spec_path, repo_dir=repo, python=PYTHON, lock_path=tmp_path / "collect.lock"
    )
    evidence = _ev(tmp_path)
    exit_code = run_pool(
        spec,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=evidence,
        workers=1,
        python=PYTHON,
        fake_db=True,
        lock_path=tmp_path / "pool.lock",
        pathcheck_module="guarded",
        src_root_relative="",
        sample=1.0,  # sample every neuter -- deterministic for this 1-neuter spec
    )
    # Review round 2 blocker 4: a non-empty unmapped_red is unacknowledged
    # drift, not clean -- even though this row's OWN targeted verdict is
    # GREEN, the exit code must surface the drift, not the plain
    # vacuous-GREEN code.
    assert exit_code == EXIT_DRIFT_UNACKNOWLEDGED
    rows = {
        json.loads(line)["id"]: json.loads(line)
        for line in (evidence / "neuter_results.jsonl").read_text().splitlines()
    }
    row = rows["x6-wrong-mapping"]
    assert row["verdict"] == "GREEN"
    assert row["drift_sampled"] is True
    assert row["unmapped_red"] != []
    assert any("test_b" in u for u in row["unmapped_red"])


def test_run_pool_ack_drift_records_an_ack_event(tmp_path):
    """Review round 4 should-fix: the POOL's own --ack-drift (distinct from
    `report`'s) must ALSO be recorded in events.jsonl -- never reflected
    only in the exit code."""
    repo = _init_repo(tmp_path)
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema": 3,
                "sha": _sha(repo),
                "scope_files": ["test_guarded.py"],
                "guard_tests": [{"id": "test_guarded.py::test_a", "row": "G"}],
                "neuters": [
                    {
                        "id": "x6-wrong-mapping",
                        "file": "guarded.py",
                        "edits": [
                            {"old": "    return x * 2", "new": "    return x * 3"}
                        ],
                        "tests": ["test_guarded.py::test_a"],
                        "engines": ["mock"],
                        "guard": "G",
                    }
                ],
            }
        )
    )
    spec = load_neuter_spec(
        spec_path, repo_dir=repo, python=PYTHON, lock_path=tmp_path / "collect.lock"
    )
    evidence = _ev(tmp_path)
    exit_code = run_pool(
        spec,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=evidence,
        workers=1,
        python=PYTHON,
        fake_db=True,
        lock_path=tmp_path / "pool.lock",
        pathcheck_module="guarded",
        src_root_relative="",
        sample=1.0,
        ack_drift=True,
    )
    # the row's own verdict is GREEN (vacuous) -- ack_drift only suppresses
    # the DRIFT exit code (14), not the separate vacuous-GREEN one (2).
    assert exit_code == 2
    events = [
        json.loads(line)
        for line in (evidence / "events.jsonl").read_text().splitlines()
    ]
    ack_events = [
        e for e in events if e.get("event") == "ack" and e.get("phase") == "pool"
    ]
    assert len(ack_events) == 1
    assert ack_events[0]["ack_drift"] is True
    assert ack_events[0]["acked_drift_ids"] == ["x6-wrong-mapping"]


def test_generate_report_refuses_unconfirmed_green(tmp_path):
    """Blocker 3, enforced in code: ``--verify`` fails while a GREEN row has
    no full-scope arbitration confirming it GREEN -- convention (a human
    remembering to arbitrate) is not enough."""
    from scripts.neuter.report import generate_report, verify_report, write_report

    repo = _init_repo(tmp_path)
    entry = NeuterEntry(
        id="g1",
        file="guarded.py",
        edits=[Edit(old="    return x + 1", new="    return x + 1  # noop")],
        tests=["test_guarded.py::test_a"],
        engines=["mock"],
        guard="G",
        clause="",
        expect="RED",
    )
    spec = NeuterSpecFile(
        schema=3,
        sha=_sha(repo),
        scope_files=["test_guarded.py"],
        guard_tests=[GuardTestEntry(id="test_guarded.py::test_a", row="G")],
        neuters=[entry],
        path=tmp_path / "unused.json",
    )
    evidence = _ev(tmp_path)
    evidence.mkdir(exist_ok=True)
    row = {
        "id": "g1",
        "verdict": "GREEN",
        "file": "guarded.py",
        "tests_expected": 1,
        "tests_collected": 1,
        "failed": [],
        "failure_types": [],
        "error_reason": None,
        "secs": 1.0,
        "worker": 1,
        "restored_clean": True,
        "pg_settings": None,
        "unmapped_red": [],
        "run_id": "r1",
    }
    (evidence / "neuter_results.jsonl").write_text(json.dumps(row) + "\n")
    text = generate_report(evidence, spec)
    assert "unconfirmed_green_n=1" in text.splitlines()[-1]
    write_report(evidence, spec)
    assert verify_report(evidence, spec) != 0

    # now arbitrate it as GREEN -- the report must confirm it clean.
    arb = {
        "id": "g1",
        "harness_verdict": "GREEN",
        "authoritative_verdict": "GREEN",
        "defect_ref": None,
    }
    (evidence / "arbitration.jsonl").write_text(json.dumps(arb) + "\n")
    write_report(evidence, spec)
    assert verify_report(evidence, spec) == 0


# ───────── review round-1 blocker 5: §9 -- mock engines get the DSN ──────


def test_mock_engine_worker_gets_dsn_when_a_real_pg_handle_exists(
    tmp_path, monkeypatch
):
    """§9: a MOCK-engine neuter's worker still gets ``AUDITTRACE_TEST_POSTGRES_URL``
    when the worker holds a real (non-fake) Postgres -- so a test file that
    starts a DURABLE product container at import time finds the var
    already set and reuses the worker's own throwaway container instead."""
    repo = _init_repo(tmp_path)
    entry = _entry(
        id="mock1",
        file="guarded.py",
        edits=[Edit(old="    return x + 1", new="    return x + 1  # noop")],
        tests=["test_guarded.py::test_a"],
        engines=["mock"],
    )
    captured_env: dict[str, str] = {}
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == [PYTHON, "-m"] and "pytest" in cmd:
            captured_env.update(kwargs.get("env") or {})
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("scripts.neuter.pytest_run.subprocess.run", fake_run)
    monkeypatch.setattr(
        "scripts.neuter.pytest_run._continuous_foreign_container_watch",
        lambda: contextlib.nullcontext(lambda: False),
    )
    real_shaped_handle = PgHandle(
        name="w1", dsn="postgresql+psycopg2://x/y", fake=False
    )
    ctx = _ctx(repo, _ev(tmp_path), pg_handle=real_shaped_handle)
    run_one_neuter(ctx, entry)
    assert (
        captured_env.get("AUDITTRACE_TEST_POSTGRES_URL") == "postgresql+psycopg2://x/y"
    )


# ─────────── review round 2 coverage-closing: new blocker/should-fix code ──


def test_run_pool_refuses_nonempty_evidence_dir_without_resume(tmp_path):
    """Blocker 3 should-fix: a non-empty evidence dir from an earlier run
    must never be silently reused without --resume."""
    repo = _init_repo(tmp_path)
    spec = _spec_two_neuters(repo)
    evidence = _ev(tmp_path)
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "neuter_results_w1.jsonl").write_text('{"id": "stale"}\n')
    exit_code = run_pool(
        spec,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=evidence,
        workers=1,
        python=PYTHON,
        fake_db=True,
        lock_path=tmp_path / "pool.lock",
        pathcheck_module="guarded",
        src_root_relative="",
        resume=False,
    )
    assert exit_code == EXIT_EVIDENCE_NOT_EMPTY


def test_sample_pool_metrics_writes_one_sample(tmp_path):
    """Direct call (the background thread's own body never runs inside a
    normal test's lifetime -- ``interval_s`` defaults to 5s): proves the
    real ``/proc/meminfo`` + ``docker ps`` + jsonl-append path works, not
    just that the thread can be started."""
    evidence = _ev(tmp_path)
    stop = threading.Event()
    stop.set()  # `stop.wait(interval_s)` returns True immediately -> one pass, then exit
    # Force one iteration before the stop check: call the loop body directly
    # by using an Event that is NOT set yet, with interval_s=0, then set it
    # from a timer so the loop runs exactly once.
    stop2 = threading.Event()

    def _set_soon():
        stop2.set()

    timer = threading.Timer(0.05, _set_soon)
    timer.start()
    _sample_pool_metrics(evidence, stop2, interval_s=0.01)
    timer.join()
    metrics_path = evidence / "pool_metrics.jsonl"
    assert metrics_path.exists()
    rows = [json.loads(line) for line in metrics_path.read_text().splitlines() if line]
    assert len(rows) >= 1
    assert "avail_mb" in rows[0]
    assert "containers" in rows[0]


def test_worker_main_direct_crash_is_caught_and_logged(tmp_path, monkeypatch):
    """A non-``DirtyTreeError`` exception ANYWHERE in the worker's setup
    (here: ``start_container``) must be caught, logged as ``worker_crashed``,
    and never propagate -- an uncaught exception here would silently kill
    the ``multiprocessing.Process`` with the parent none the wiser
    (blocker 1's fail-open)."""
    repo = _init_repo(tmp_path)
    spec = _spec_two_neuters(repo)
    neuters_by_id = {n.id: n for n in spec.neuters}

    def raise_on_start(*args, **kwargs):
        raise RuntimeError("simulated container start failure")

    monkeypatch.setattr("scripts.neuter.pool.start_container", raise_on_start)
    q: queue.Queue[str | None] = queue.Queue()
    q.put("w1")
    q.put(None)
    stop_flag = threading.Event()
    evidence = _ev(tmp_path)
    with _held_lock(tmp_path / "worker.lock"):
        _worker_main(
            1,
            q,
            spec,
            neuters_by_id,
            repo_dir=repo,
            parent_worktree_dir=tmp_path,
            evidence_dir=evidence,
            run_id="direct",
            python=PYTHON,
            timeout_s=30,
            tmpfs=False,
            fake_db=True,
            pathcheck_module="guarded",
            src_root_relative="",
            resume_rows={},
            stop_flag=stop_flag,
            lock_path=tmp_path / "worker.lock",
        )
    events = [
        json.loads(line)
        for line in (evidence / "events.jsonl").read_text().splitlines()
    ]
    crashes = [e for e in events if e.get("event") == "worker_crashed"]
    assert len(crashes) == 1
    assert "simulated container start failure" in crashes[0]["error"]


def test_worker_main_direct_stop_container_failure_is_logged(tmp_path, monkeypatch):
    """``stop_container`` raising in the worker's cleanup must be logged,
    never allowed to mask the row/crash the process."""
    repo = _init_repo(tmp_path)
    spec = _spec_two_neuters(repo)
    neuters_by_id = {n.id: n for n in spec.neuters}
    real_start = __import__(
        "scripts.neuter.pool", fromlist=["start_container"]
    ).start_container

    def fake_stop(handle):
        raise RuntimeError("simulated docker rm failure")

    monkeypatch.setattr("scripts.neuter.pool.stop_container", fake_stop)
    q: queue.Queue[str | None] = queue.Queue()
    q.put("w1")
    q.put("w2")
    q.put(None)
    stop_flag = threading.Event()
    evidence = _ev(tmp_path)
    with _held_lock(tmp_path / "worker.lock"):
        _worker_main(
            1,
            q,
            spec,
            neuters_by_id,
            repo_dir=repo,
            parent_worktree_dir=tmp_path,
            evidence_dir=evidence,
            run_id="direct",
            python=PYTHON,
            timeout_s=30,
            tmpfs=False,
            fake_db=True,
            pathcheck_module="guarded",
            src_root_relative="",
            resume_rows={},
            stop_flag=stop_flag,
            lock_path=tmp_path / "worker.lock",
        )
    assert real_start is not None  # sanity: the real function still exists
    rows = [
        json.loads(line)
        for line in (evidence / "neuter_results_w1.jsonl").read_text().splitlines()
    ]
    assert {r["id"] for r in rows} == {"w1", "w2"}


def _baseline_spec(
    repo: Path, *, test_id: str = "test_guarded.py::test_a"
) -> NeuterSpecFile:
    entry = _entry(
        id="b1",
        file="guarded.py",
        edits=[Edit(old="    return x + 1", new="    return x + 2")],
        tests=[test_id],
    )
    return NeuterSpecFile(
        schema=3,
        sha=_sha(repo),
        scope_files=["test_guarded.py"],
        guard_tests=[GuardTestEntry(id=test_id, row="G")],
        neuters=[entry],
        path=Path("unused.json"),
    )


def test_run_baseline_success_path(tmp_path):
    repo = _init_repo(tmp_path)
    spec = _baseline_spec(repo)
    with _held_lock(tmp_path / "baseline.lock"):
        ok, detail = run_baseline(
            spec,
            repo_dir=repo,
            parent_worktree_dir=tmp_path,
            evidence_dir=_ev(tmp_path),
            run_id="baseline-direct",
            python=PYTHON,
            timeout_s=30,
            lock_path=tmp_path / "baseline.lock",
        )
    assert ok is True
    assert detail == ""


def test_run_baseline_reports_failure_detail(tmp_path, monkeypatch):
    """The mapped test is already broken at the pinned sha: baseline must
    report the failing id(s) in its detail string, never just a bare
    exit code."""
    repo = _init_repo(tmp_path)
    (repo / "test_guarded.py").write_text(
        (repo / "test_guarded.py").read_text()
        + "\n\ndef test_always_fails():\n    assert False\n"
    )
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "add a failing test")
    spec = _baseline_spec(repo, test_id="test_guarded.py::test_always_fails")
    with _held_lock(tmp_path / "baseline.lock"):
        ok, detail = run_baseline(
            spec,
            repo_dir=repo,
            parent_worktree_dir=tmp_path,
            evidence_dir=_ev(tmp_path),
            run_id="baseline-fail",
            python=PYTHON,
            timeout_s=30,
            lock_path=tmp_path / "baseline.lock",
        )
    assert ok is False
    assert "test_always_fails" in detail


def test_run_baseline_refuses_on_foreign_pg_container(tmp_path, monkeypatch):
    """Review round 2 blocker 5: baseline goes through the SAME
    foreign-durable-container watch every phase does now."""
    repo = _init_repo(tmp_path)
    spec = _baseline_spec(repo)

    class _FakeResult:
        exit_code = 0
        timed_out = False
        foreign_pg_container = True
        chokepoint_marker_ok = True

    monkeypatch.setattr(
        "scripts.neuter.pool.run_pytest", lambda **kwargs: _FakeResult()
    )
    ok, detail = run_baseline(
        spec,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=_ev(tmp_path),
        run_id="baseline-foreign",
        python=PYTHON,
        timeout_s=30,
        lock_path=tmp_path / "baseline.lock",
    )
    assert ok is False
    assert "foreign" in detail


def test_run_baseline_refuses_on_chokepoint_marker_missing(tmp_path, monkeypatch):
    """Review round 5 should-fix: the marker check (round 4, requirement
    E2) was only ever consumed at the neuter and arbitrate phases --
    baseline's own pytest invocation could silently run with
    neuter_pathcheck disabled and this phase would never notice."""
    repo = _init_repo(tmp_path)
    spec = _baseline_spec(repo)

    class _FakeResult:
        exit_code = 0
        timed_out = False
        foreign_pg_container = False
        chokepoint_marker_ok = False

    monkeypatch.setattr(
        "scripts.neuter.pool.run_pytest", lambda **kwargs: _FakeResult()
    )
    ok, detail = run_baseline(
        spec,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=_ev(tmp_path),
        run_id="baseline-no-marker",
        python=PYTHON,
        timeout_s=30,
        lock_path=tmp_path / "baseline.lock",
    )
    assert ok is False
    assert "chokepoint marker" in detail


def test_sample_full_scope_drift_not_reproduced(tmp_path, monkeypatch):
    """Review round 2 blocker 1: a row that was RED on its targeted run,
    but whose full-scope drift re-run shows the SAME mapped test as
    PASSED (i.e. never reproduced the edit), must be flagged
    ``drift_not_reproduced`` -- never a silent, falsely-clean
    ``unmapped_red=[]``."""
    repo = _init_repo(tmp_path)
    spec = _spec_two_neuters(repo)
    rows = {
        "w1": {"verdict": "RED"},
        "w2": {"verdict": "RED"},
    }

    class _FakeResult:
        exit_code = 0
        timed_out = False
        foreign_pg_container = False
        chokepoint_marker_ok = True

    def fake_run_pytest(*, junit_path, **kwargs):
        # write a junit file where NEITHER mapped test failed.
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        junit_path.write_text(
            '<?xml version="1.0"?><testsuite tests="2">'
            '<testcase classname="test_guarded" name="test_a"/>'
            '<testcase classname="test_guarded" name="test_b"/>'
            "</testsuite>"
        )
        return _FakeResult()

    monkeypatch.setattr("scripts.neuter.pool.run_pytest", fake_run_pytest)
    sample_full_scope_drift(
        spec,
        rows,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=_ev(tmp_path),
        run_id="drift-nr",
        python=PYTHON,
        timeout_s=30,
        sample=1.0,
        lock_path=tmp_path / "drift.lock",
    )
    assert rows["w1"].get("drift_not_reproduced") is True
    assert rows["w2"].get("drift_not_reproduced") is True
    assert "unmapped_red" not in rows["w1"]
    # Review round 3 blocker 3: not_reproduced is never a silent flag on an
    # otherwise-RED row -- the row's own verdict is overwritten to ERROR,
    # so it surfaces through the EXISTING error_n > 0 / --ack-errors gate
    # `report --verify` already enforces, rather than riding along on a
    # RED verdict that reads as "clean".
    assert rows["w1"]["verdict"] == "ERROR"
    assert rows["w2"]["verdict"] == "ERROR"
    assert rows["w1"]["error_reason"] == "not_reproduced"


def test_sample_full_scope_drift_foreign_pg_container(tmp_path, monkeypatch):
    """Review round 2 blocker 5: drift also refuses to trust a measurement
    taken while a foreign, durable product Postgres container was seen."""
    repo = _init_repo(tmp_path)
    spec = _spec_two_neuters(repo)
    rows = {"w1": {"verdict": "RED"}, "w2": {"verdict": "RED"}}

    class _FakeResult:
        exit_code = 0
        timed_out = False
        foreign_pg_container = True
        chokepoint_marker_ok = True

    monkeypatch.setattr(
        "scripts.neuter.pool.run_pytest", lambda **kwargs: _FakeResult()
    )
    sample_full_scope_drift(
        spec,
        rows,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=_ev(tmp_path),
        run_id="drift-fpc",
        python=PYTHON,
        timeout_s=30,
        sample=1.0,
        lock_path=tmp_path / "drift.lock",
    )
    assert rows["w1"].get("drift_foreign_pg_container") is True
    assert "unmapped_red" not in rows["w1"]


def test_sample_full_scope_drift_chokepoint_marker_missing(tmp_path, monkeypatch):
    """Review round 5 should-fix: drift's own full-scope pytest invocation
    could silently run with neuter_pathcheck disabled and this phase would
    never notice -- same class of gap as neuter/arbitrate already closed
    in round 4."""
    repo = _init_repo(tmp_path)
    spec = _spec_two_neuters(repo)
    rows = {"w1": {"verdict": "RED"}, "w2": {"verdict": "RED"}}

    class _FakeResult:
        exit_code = 0
        timed_out = False
        foreign_pg_container = False
        chokepoint_marker_ok = False

    monkeypatch.setattr(
        "scripts.neuter.pool.run_pytest", lambda **kwargs: _FakeResult()
    )
    sample_full_scope_drift(
        spec,
        rows,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=_ev(tmp_path),
        run_id="drift-marker",
        python=PYTHON,
        timeout_s=30,
        sample=1.0,
        lock_path=tmp_path / "drift.lock",
    )
    assert rows["w1"].get("drift_chokepoint_marker_missing") is True
    assert rows["w1"]["verdict"] == "ERROR"
    assert "unmapped_red" not in rows["w1"]


def test_sample_full_scope_drift_is_seeded_from_run_id(tmp_path, monkeypatch):
    """NE-3 should-fix: the drift sample must be REPRODUCIBLE for the same
    run_id (proving it's actually seeded from run_id, the way
    ``random.Random(run_id)`` promises) and (with high probability, for a
    10-of-3 sample) DIFFERENT for a different run_id -- a neuter that drops
    the seed (``random.Random()``) would make the SAME run_id's selection
    vary run to run, which this test would catch."""
    repo = _init_repo(tmp_path)
    neuters = [
        _entry(
            id=f"n{i}",
            file="guarded.py",
            edits=[Edit(old="    return x + 1", new=f"    return x + 1  # {i}")],
            tests=["test_guarded.py::test_a"],
        )
        for i in range(10)
    ]
    spec = NeuterSpecFile(
        schema=3,
        sha=_sha(repo),
        scope_files=["test_guarded.py"],
        guard_tests=[GuardTestEntry(id="test_guarded.py::test_a", row="G")],
        neuters=neuters,
        path=Path("unused.json"),
    )

    class _FakeResult:
        exit_code = 0
        timed_out = False
        foreign_pg_container = False
        chokepoint_marker_ok = True

    def fake_run_pytest(*, junit_path, **kwargs):
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        # test_a shows as FAILED so `mapped_failed_in_full` is satisfied for
        # every row's own verdict=RED -- else the not_reproduced branch
        # fires instead of setting drift_sampled, leaving this test's
        # selection sets always empty regardless of the seed.
        junit_path.write_text(
            '<?xml version="1.0"?><testsuite tests="1">'
            '<testcase classname="test_guarded" name="test_a">'
            '<failure message="assert 1 == 2"></failure>'
            "</testcase></testsuite>"
        )
        return _FakeResult()

    monkeypatch.setattr("scripts.neuter.pool.run_pytest", fake_run_pytest)
    monkeypatch.setattr("scripts.neuter.pool.apply_edits", lambda workdir, entry: [1])
    monkeypatch.setattr(
        "scripts.neuter.pool.py_compile_ok", lambda workdir, entry, python: True
    )
    monkeypatch.setattr("scripts.neuter.pool.git_diff_quiet", lambda *a, **k: True)

    def sampled_ids(run_id: str) -> set[str]:
        rows = {n.id: {"verdict": "RED"} for n in neuters}
        sample_full_scope_drift(
            spec,
            rows,
            repo_dir=repo,
            parent_worktree_dir=tmp_path,
            evidence_dir=_ev(tmp_path, str(run_id)),
            run_id=run_id,
            python=PYTHON,
            timeout_s=30,
            sample=0.3,
            lock_path=tmp_path / "drift.lock",
        )
        return {nid for nid, r in rows.items() if r.get("drift_sampled")}

    first = sampled_ids("run-alpha")
    second = sampled_ids("run-alpha")
    third = sampled_ids("run-beta")
    assert first == second, "same run_id must select the SAME sample"
    assert first != third, "a different run_id should (with high probability) differ"
