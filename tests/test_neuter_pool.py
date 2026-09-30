"""Coverage-closing unit tests for ``scripts/neuter/pool.py`` branches the
self-proofs (``tests/test_neuter_self_proofs.py``) don't reach directly --
mainly code that only runs inside a ``multiprocessing.Process`` child
(invisible to the parent's coverage measurement) and ``run_pool``'s rarer
guard branches.
"""

from __future__ import annotations

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
    EXIT_MISSING_ROWS,
    DirtyTreeError,
    WorkerContext,
    _worker_main,
    apply_edits,
    foreign_audittrace_container_exists,
    pool_exit_code,
    run_one_neuter,
    run_pool,
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
    spec = load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)
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
    spec = load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)
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
    spec = load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)
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
    spec = load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)
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
    spec = load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)

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
    assert "CalledProcessError" in crashes[0]["error"]
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
    spec = load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)
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
    spec = load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)
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
    assert exit_code == 2  # GREEN present
    rows = {
        json.loads(line)["id"]: json.loads(line)
        for line in (evidence / "neuter_results.jsonl").read_text().splitlines()
    }
    row = rows["x6-wrong-mapping"]
    assert row["verdict"] == "GREEN"
    assert row["drift_sampled"] is True
    assert row["unmapped_red"] != []
    assert any("test_b" in u for u in row["unmapped_red"])


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

    monkeypatch.setattr("scripts.neuter.pool.subprocess.run", fake_run)
    monkeypatch.setattr(
        "scripts.neuter.pool.foreign_product_pg_container_running", lambda: False
    )
    real_shaped_handle = PgHandle(
        name="w1", dsn="postgresql+psycopg2://x/y", fake=False
    )
    ctx = _ctx(repo, _ev(tmp_path), pg_handle=real_shaped_handle)
    run_one_neuter(ctx, entry)
    assert (
        captured_env.get("AUDITTRACE_TEST_POSTGRES_URL") == "postgresql+psycopg2://x/y"
    )
