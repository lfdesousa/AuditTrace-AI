"""Self-proofs a-n (SPEC v3 §11): real triggers in a throwaway git repo,
each with the neuter that must redden it. Every proof runs the harness's
OWN code (``run_one_neuter`` / ``run_pool`` / ``spec.load_neuter_spec`` /
``lock.py``) against a real git repository and real ``pytest`` subprocess
copied from ``tests/neuter_fixture/`` -- never a mock of pytest's own
behaviour. Docker is faked only via ``pg.py``'s ``fake=True`` seam.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.neuter import lock as lockmod
from scripts.neuter.pg import PgHandle, start_container
from scripts.neuter.pool import (
    WorkerContext,
    compute_neuter_hash,
    run_one_neuter,
    run_pool,
    should_skip,
)
from scripts.neuter.report import generate_report
from scripts.neuter.spec import (
    Edit,
    GuardTestEntry,
    NeuterEntry,
    SpecLoadError,
    load_neuter_spec,
)

FIXTURE_DIR = Path(__file__).parent / "neuter_fixture"
PYTHON = sys.executable
FIXTURE_POOL_KWARGS = {"pathcheck_module": "guarded", "src_root_relative": ""}


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


def _ctx(
    repo: Path,
    evidence_dir: Path,
    *,
    pg_handle: PgHandle | None = None,
    timeout_s: int = 30,
    src_root: str | None = None,
) -> WorkerContext:
    return WorkerContext(
        workdir=repo,
        worker_idx=1,
        python=PYTHON,
        run_id="selfproof",
        sha=_sha(repo),
        evidence_dir=evidence_dir,
        pg_handle=pg_handle,
        pathcheck_expect=src_root or str(repo),
        pathcheck_module="guarded",
        timeout_s=timeout_s,
        src_root=src_root or str(repo),
    )


def _entry(**kwargs) -> NeuterEntry:
    base = dict(guard="G", clause="C", engines=["mock"], expect="RED")
    base.update(kwargs)
    return NeuterEntry(**base)


# ─────────────────────────── a. wrong mapping ────────────────────────────


def test_proof_a_wrong_mapping(tmp_path):
    repo = _init_repo(tmp_path)
    entry = _entry(
        id="a-wrong-mapping",
        file="guarded.py",
        edits=[Edit(old="    return x * 2", new="    return x * 3")],  # breaks guard_b
        tests=["test_guarded.py::test_a"],  # WRONG: test_a only exercises guard_a
    )
    row = run_one_neuter(_ctx(repo, tmp_path / "ev"), entry)
    assert row["verdict"] == "GREEN"
    assert row["vacuous"] is True


def test_proof_a_pool_exit_code_is_2(tmp_path):
    repo = _init_repo(tmp_path)
    evidence = tmp_path / "ev"
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
                        "id": "a-wrong-mapping",
                        "file": "guarded.py",
                        "edits": [
                            {"old": "    return x * 2", "new": "    return x * 3"}
                        ],
                        "tests": ["test_guarded.py::test_a"],
                        "engines": ["mock"],
                        "guard": "G",
                        "clause": "C",
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
        evidence_dir=evidence,
        workers=1,
        python=PYTHON,
        fake_db=True,
        lock_path=tmp_path / "pool.lock",
        **FIXTURE_POOL_KWARGS,
    )
    assert exit_code == 2


# ───────────────────────── b. text mismatch ──────────────────────────────


def test_proof_b_zero_match_exits_3(tmp_path):
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
                        "id": "b-zero-match",
                        "file": "guarded.py",
                        "edits": [{"old": "nonexistent_text_xyz", "new": "whatever"}],
                        "tests": ["test_guarded.py::test_a"],
                        "engines": ["mock"],
                    }
                ],
            }
        )
    )
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "match_count"
    assert not (tmp_path / "ev").exists()
    assert (
        _run_git(repo, "worktree", "list").stdout.count("\n") == 1
    )  # only the main worktree


def test_proof_b_two_match_exits_3(tmp_path):
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
                        "id": "b-two-match",
                        "file": "guarded.py",
                        "edits": [
                            {"old": "return", "new": "return "}
                        ],  # `return` appears 5x
                        "tests": ["test_guarded.py::test_a"],
                        "engines": ["mock"],
                    }
                ],
            }
        )
    )
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "match_count"


# ──────────────────────────── c. dirty tree ──────────────────────────────


def test_proof_c_dirty_tree_stops_restore(tmp_path):
    repo = _init_repo(tmp_path)
    entry = _entry(
        id="c-dirty-side-effect",
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
    )
    row = run_one_neuter(_ctx(repo, tmp_path / "ev"), entry)
    assert row["restored_clean"] is False
    status = _run_git(repo, "status", "--porcelain").stdout
    assert "data.txt" in status
    # never `git checkout -- .` -- that would hide the leaked write too.
    _run_git(repo, "checkout", "--", "data.txt")


# ────────────────────────── d. wrong import path ─────────────────────────


def test_proof_d_wrong_import_path(tmp_path):
    repo = _init_repo(tmp_path)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    shutil.copy(FIXTURE_DIR / "guarded.py", decoy / "guarded.py")

    entry = _entry(
        id="d-noop",
        file="guarded.py",
        edits=[Edit(old="    return x + 1", new="    return x + 1  # noop")],
        tests=["test_guarded.py::test_a"],
    )
    ctx = _ctx(repo, tmp_path / "ev", src_root=str(decoy))  # wrong PYTHONPATH root
    row = run_one_neuter(ctx, entry)
    assert row["verdict"] == "ERROR"
    assert row["error_reason"] == "pathcheck"
    log = tmp_path / "ev" / "pathcheck_w1.log"
    assert not log.exists() or log.read_text() == ""


# ──────────────────────────────── e. resume ──────────────────────────────


def test_proof_e_should_skip_rules(tmp_path):
    entry = _entry(
        id="e-resume",
        file="guarded.py",
        edits=[Edit(old="    return x + 1", new="    return x + 2")],
        tests=["test_guarded.py::test_a"],
    )
    good_row = {
        "verdict": "RED",
        "restored_clean": True,
        "sha": "shaX",
        "harness_version": "1",
        "neuter_hash": compute_neuter_hash(entry),
    }
    assert should_skip(entry, good_row, sha="shaX", harness_version="1") is True
    assert should_skip(entry, good_row, sha="shaY", harness_version="1") is False
    assert should_skip(entry, good_row, sha="shaX", harness_version="2") is False
    assert (
        should_skip(
            entry, {**good_row, "verdict": "ERROR"}, sha="shaX", harness_version="1"
        )
        is False
    )
    assert (
        should_skip(
            entry,
            {**good_row, "restored_clean": False},
            sha="shaX",
            harness_version="1",
        )
        is False
    )

    changed = _entry(
        id="e-resume",
        file="guarded.py",
        edits=[Edit(old="    return x + 1", new="    return x + 3")],
        tests=["test_guarded.py::test_a"],
    )
    assert should_skip(changed, good_row, sha="shaX", harness_version="1") is False


def test_proof_e_resume_reruns_exactly_the_edited_one(tmp_path):
    repo = _init_repo(tmp_path)
    evidence = tmp_path / "ev"
    spec_dict = {
        "schema": 3,
        "sha": _sha(repo),
        "scope_files": ["test_guarded.py"],
        "guard_tests": [
            {"id": "test_guarded.py::test_a", "row": "G1"},
            {"id": "test_guarded.py::test_b", "row": "G2"},
        ],
        "neuters": [
            {
                "id": "e1",
                "file": "guarded.py",
                "edits": [{"old": "    return x + 1", "new": "    return x + 2"}],
                "tests": ["test_guarded.py::test_a"],
                "engines": ["mock"],
                "guard": "G1",
            },
            {
                "id": "e2",
                "file": "guarded.py",
                "edits": [{"old": "    return x * 2", "new": "    return x * 3"}],
                "tests": ["test_guarded.py::test_b"],
                "engines": ["mock"],
                "guard": "G2",
            },
        ],
    }
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(spec_dict))
    spec = load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)

    run_pool(
        spec,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=evidence,
        workers=1,
        python=PYTHON,
        fake_db=True,
        lock_path=tmp_path / "pool.lock",
        **FIXTURE_POOL_KWARGS,
    )
    rows_by_id = {
        json.loads(line)["id"]: json.loads(line)
        for line in (evidence / "neuter_results.jsonl").read_text().splitlines()
    }
    assert set(rows_by_id) == {"e1", "e2"}
    e1_first_finished = rows_by_id["e1"]["finished_at"]

    # bump e1's edit -> its neuter_hash changes -> it must re-run; e2 must be skipped.
    spec_dict["neuters"][0]["edits"][0]["new"] = "    return x + 20"
    # `old` still matches once in guarded.py's committed tree.
    spec_path.write_text(json.dumps(spec_dict))
    spec2 = load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)
    run_pool(
        spec2,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=evidence,
        workers=1,
        python=PYTHON,
        fake_db=True,
        resume=True,
        lock_path=tmp_path / "pool.lock",
        **FIXTURE_POOL_KWARGS,
    )
    rows_by_id_2 = {
        json.loads(line)["id"]: json.loads(line)
        for line in (evidence / "neuter_results.jsonl").read_text().splitlines()
    }
    assert rows_by_id_2["e1"]["finished_at"] != e1_first_finished  # re-ran
    assert (
        rows_by_id_2["e2"]["finished_at"] == rows_by_id["e2"]["finished_at"]
    )  # skipped


# ──────────────────────────────── f. DB leak ─────────────────────────────


def test_proof_f_db_leak(tmp_path):
    repo = _init_repo(tmp_path)
    evidence = tmp_path / "ev"
    evidence.mkdir()
    pg_handle = start_container(
        "selfproof", 1, fake=True, fake_dir=evidence / "fake_pg"
    )
    entry = _entry(
        id="f-db-leak",
        file="test_guarded.py",
        edits=[
            Edit(
                old="def test_a():\n    assert guard_a(1) == 2",
                new=(
                    "def test_a():\n"
                    "    import json, os\n"
                    "    p = os.environ['AUDITTRACE_NEUTER_FAKE_PG_STATE']\n"
                    "    state = json.loads(open(p).read())\n"
                    "    state['schemata'].append('leaked_schema')\n"
                    "    open(p, 'w').write(json.dumps(state))\n"
                    "    assert guard_a(1) == 2"
                ),
            )
        ],
        tests=["test_guarded.py::test_a"],
        engines=["postgres"],
    )
    row = run_one_neuter(_ctx(repo, evidence, pg_handle=pg_handle), entry)
    assert row["verdict"] == "ERROR"
    assert row["error_reason"] == "db_leak"
    assert row["db_leak"] is True


# ──────────────────────────────── g. closure ─────────────────────────────


def test_proof_g_unmapped_guard_test_id_exits_3(tmp_path):
    repo = _init_repo(tmp_path)
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema": 3,
                "sha": _sha(repo),
                "scope_files": ["test_guarded.py"],
                "guard_tests": [
                    {"id": "test_guarded.py::test_a", "row": "G1"},
                    {
                        "id": "test_guarded.py::test_b",
                        "row": "G2",
                    },  # not in any neuter's `tests`
                ],
                "neuters": [
                    {
                        "id": "g1",
                        "file": "guarded.py",
                        "edits": [
                            {"old": "    return x + 1", "new": "    return x + 2"}
                        ],
                        "tests": ["test_guarded.py::test_a"],
                        "engines": ["mock"],
                    }
                ],
            }
        )
    )
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "closure"


def test_proof_g_entry_without_row_exits_3(tmp_path):
    repo = _init_repo(tmp_path)
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema": 3,
                "sha": _sha(repo),
                "scope_files": ["test_guarded.py"],
                "guard_tests": [{"id": "test_guarded.py::test_a"}],  # no `row`
                "neuters": [
                    {
                        "id": "g2",
                        "file": "guarded.py",
                        "edits": [
                            {"old": "    return x + 1", "new": "    return x + 2"}
                        ],
                        "tests": ["test_guarded.py::test_a"],
                        "engines": ["mock"],
                    }
                ],
            }
        )
    )
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "guard_tests_row"


def test_proof_g_reviewer_guard_tests_diff_is_report_only(tmp_path):
    repo = _init_repo(tmp_path)
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema": 3,
                "sha": _sha(repo),
                "scope_files": ["test_guarded.py"],
                "guard_tests": [{"id": "test_guarded.py::test_a", "row": "G1"}],
                "neuters": [
                    {
                        "id": "g3",
                        "file": "guarded.py",
                        "edits": [
                            {"old": "    return x + 1", "new": "    return x + 2"}
                        ],
                        "tests": ["test_guarded.py::test_a"],
                        "engines": ["mock"],
                        "guard": "G1",
                    }
                ],
            }
        )
    )
    spec = load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)
    reviewer_guard_tests = [
        GuardTestEntry(id="test_guarded.py::test_a", row="G1"),
        GuardTestEntry(id="test_guarded.py::test_b", row="G2"),
    ]
    evidence = tmp_path / "ev"
    evidence.mkdir()
    (evidence / "neuter_results.jsonl").write_text("")
    text = generate_report(evidence, spec, reviewer_guard_tests=reviewer_guard_tests)
    assert "test_guarded.py::test_b" in text  # the diff is visible, not fatal


# ──────────────────────────────── h. settings ────────────────────────────


def test_proof_h_nondurable_settings_violation(tmp_path):
    repo = _init_repo(tmp_path)
    evidence = tmp_path / "ev"
    evidence.mkdir()
    pg_handle = start_container(
        "selfproof", 1, fake=True, fake_dir=evidence / "fake_pg"
    )
    state_path = pg_handle.fake_state_path
    state = json.loads(state_path.read_text())
    state["settings"]["fsync"] = "on"  # simulate a container that ignored -c fsync=off
    state_path.write_text(json.dumps(state))

    entry = _entry(
        id="h-settings",
        file="guarded.py",
        edits=[Edit(old="    return x + 1", new="    return x + 1  # noop")],
        tests=["test_guarded.py::test_a"],
        engines=["postgres"],
    )
    row = run_one_neuter(_ctx(repo, evidence, pg_handle=pg_handle), entry)
    assert row["verdict"] == "ERROR"
    assert row["error_reason"] == "pg_settings"
    assert row["pg_settings"]["fsync"] == "on"


# ───────────────────────────── i. fixture raise ──────────────────────────


def test_proof_i_fixture_crash_is_error_never_red(tmp_path):
    repo = _init_repo(tmp_path)
    entry = _entry(
        id="i-fixture-raise",
        file="guarded.py",
        edits=[Edit(old="    return 41", new="    raise RuntimeError('boom')")],
        tests=["test_guarded.py::test_uses_seed"],
    )
    row = run_one_neuter(_ctx(repo, tmp_path / "ev"), entry)
    assert row["verdict"] == "ERROR"
    assert row["error_reason"] == "setup_or_teardown"


# ────────────────────────────────── j. hang ──────────────────────────────


def test_proof_j_timeout(tmp_path):
    repo = _init_repo(tmp_path)
    entry = _entry(
        id="j-hang",
        file="test_guarded.py",
        edits=[Edit(old="    slow(0.01)", new="    slow(5)")],
        tests=["test_guarded.py::test_uses_slow"],
    )
    row = run_one_neuter(_ctx(repo, tmp_path / "ev", timeout_s=2), entry)
    assert row["verdict"] == "ERROR"
    assert row["error_reason"] == "timeout"
    assert row["timeout"] is True


# ───────────────────────── k. syntax error (SF-B) ────────────────────────


def test_proof_k_syntax_error_short_circuits(tmp_path):
    repo = _init_repo(tmp_path)
    entry = _entry(
        id="k-syntax",
        file="guarded.py",
        edits=[
            Edit(old="def guard_a(x: int) -> int:", new="def guard_a(x: int -> int:")
        ],
        tests=["test_guarded.py::test_a"],
    )
    row = run_one_neuter(_ctx(repo, tmp_path / "ev"), entry)
    assert row["verdict"] == "ERROR"
    assert row["error_reason"] == "nocompile"
    assert row["error_reasons"] == ["nocompile"]
    assert row["tests_collected"] is None


# ──────────────────────────── l. precedence (SF-B) ───────────────────────


def test_proof_l_collection_error_precedence(tmp_path):
    repo = _init_repo(tmp_path)
    entry = _entry(
        id="l-precedence",
        file="guarded.py",
        edits=[
            Edit(
                old="def guard_a(x: int) -> int:",
                new="def guard_a_renamed(x: int) -> int:",
            )
        ],
        tests=["test_guarded.py::test_a", "test_guarded.py::test_b"],
    )
    row = run_one_neuter(_ctx(repo, tmp_path / "ev"), entry)
    assert row["verdict"] == "ERROR"
    assert row["error_reason"] == "collected"
    reasons = row["error_reasons"]
    assert (
        reasons.index("collected")
        < reasons.index("missing")
        < reasons.index("exit_code")
    )


# ───────────────────────────── m. heavy cap (BL-1) ───────────────────────


def test_proof_m_pool_refuses_while_hold_shared_running(tmp_path):
    repo = _init_repo(tmp_path)
    lock_path = tmp_path / "pool.lock"
    child = subprocess.Popen(
        [PYTHON, "-m", "scripts.neuter.runner", "hold-shared", "--", "sleep", "2"],
        env={**os.environ, "AUDITTRACE_NEUTER_LOCK": str(lock_path)},
    )
    try:
        time.sleep(0.4)  # let the child acquire LOCK_SH
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
                            "id": "m1",
                            "file": "guarded.py",
                            "edits": [
                                {"old": "    return x + 1", "new": "    return x + 2"}
                            ],
                            "tests": ["test_guarded.py::test_a"],
                            "engines": ["mock"],
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
            lock_path=lock_path,
            **FIXTURE_POOL_KWARGS,
        )
        assert exit_code == 8
        assert (
            _run_git(repo, "worktree", "list").stdout.count("\n") == 1
        )  # no worker worktree created
    finally:
        child.wait(timeout=10)

    # after the child exits, the pool starts.
    spec = load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)
    exit_code = run_pool(
        spec,
        repo_dir=repo,
        parent_worktree_dir=tmp_path,
        evidence_dir=tmp_path / "ev2",
        workers=1,
        python=PYTHON,
        fake_db=True,
        lock_path=lock_path,
        **FIXTURE_POOL_KWARGS,
    )
    assert exit_code != 8


def test_proof_m_sf2_isolated_lock_path_survives_an_outer_shared_default_lock(
    tmp_path, monkeypatch
):
    """SF-2: every harness test uses ``tmp_path`` for its OWN lock, so an
    outer ``make test``-shaped ``hold-shared`` holding the real, shared
    default lock path must never affect it."""
    monkeypatch.setenv("AUDITTRACE_NEUTER_LOCK", str(tmp_path / "outer-default.lock"))
    outer_fd = lockmod.open_lock_file(lockmod.resolve_lock_path())
    lockmod.try_flock(outer_fd, fcntl.LOCK_SH)  # simulates `make test`'s hold-shared
    try:
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
                            "id": "sf2",
                            "file": "guarded.py",
                            "edits": [
                                {"old": "    return x + 1", "new": "    return x + 2"}
                            ],
                            "tests": ["test_guarded.py::test_a"],
                            "engines": ["mock"],
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
            lock_path=tmp_path / "isolated-pool.lock",  # NEVER the default/outer path
            **FIXTURE_POOL_KWARGS,
        )
        assert exit_code != 8
    finally:
        os.close(outer_fd)


# ───────────────────────── n. call exception (SF-A) ──────────────────────


def test_proof_n_call_exception_never_red(tmp_path):
    repo = _init_repo(tmp_path)
    entry = _entry(
        id="n-call-exception",
        file="guarded.py",
        edits=[
            Edit(
                old='def raiser() -> None:\n    """Called from a test body. Proof n\'s self-neuter makes this raise\n    ``RuntimeError`` to prove a non-assertion call-phase exception\n    classifies ``ERROR call_exception``, never ``RED``."""\n    return None',
                new="def raiser() -> None:\n    raise RuntimeError('boom')",
            )
        ],
        tests=["test_guarded.py::test_uses_raiser"],
    )
    row = run_one_neuter(_ctx(repo, tmp_path / "ev"), entry)
    assert row["verdict"] == "ERROR"
    assert row["error_reason"] == "call_exception"
    assert row["failure_types"] == ["RuntimeError"]


def test_proof_n_sibling_assert_flip_is_red(tmp_path):
    repo = _init_repo(tmp_path)
    entry = _entry(
        id="n-assert-flip",
        file="guarded.py",
        edits=[Edit(old="    return x + 1", new="    return x + 2")],
        tests=["test_guarded.py::test_a"],
    )
    row = run_one_neuter(_ctx(repo, tmp_path / "ev"), entry)
    assert row["verdict"] == "RED"
    assert row["failure_types"] == ["AssertionError"]


def test_proof_n_sibling_raises_removed_is_red_failed(tmp_path):
    repo = _init_repo(tmp_path)
    entry = _entry(
        id="n-raises-removed",
        file="guarded.py",
        edits=[Edit(old='    raise ValueError("boom")', new="    return None")],
        tests=["test_guarded.py::test_maybe_raise_shape"],
    )
    row = run_one_neuter(_ctx(repo, tmp_path / "ev"), entry)
    assert row["verdict"] == "RED"
    assert row["failure_types"] == ["Failed"]
