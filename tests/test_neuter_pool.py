"""Coverage-closing unit tests for ``scripts/neuter/pool.py`` branches the
self-proofs (``tests/test_neuter_self_proofs.py``) don't reach directly --
mainly code that only runs inside a ``multiprocessing.Process`` child
(invisible to the parent's coverage measurement) and ``run_pool``'s rarer
guard branches.
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from scripts.neuter import lock as lockmod
from scripts.neuter.pool import (
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
        run_one_neuter(_ctx(repo, tmp_path / "ev"), entry)


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
    evidence = tmp_path / "ev"
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
    evidence = tmp_path / "ev"
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
    evidence = tmp_path / "ev"
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
    evidence = tmp_path / "ev"
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
        evidence_dir=tmp_path / "ev",
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
        evidence_dir=tmp_path / "ev",
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
        evidence_dir=tmp_path / "ev",
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
        evidence_dir=tmp_path / "ev",
        workers=1,
        python=PYTHON,
        fake_db=False,
        lock_path=tmp_path / "pool.lock",
    )
    assert exit_code == 8
