"""Unit tests for ``scripts/neuter/runner.py`` -- the CLI entry point
(SPEC v3 §13)."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys

import pytest

from scripts.neuter import lock as lockmod
from scripts.neuter.runner import main

PYTHON = sys.executable


def _run_git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "mod.py").write_text("def f(x):\n    return x + 1\n")
    (r / "test_mod.py").write_text(
        "from mod import f\n\n\ndef test_f():\n    assert f(1) == 2\n"
    )
    _run_git(r, "init", "-q")
    _run_git(r, "config", "user.email", "a@b.c")
    _run_git(r, "config", "user.name", "a")
    _run_git(r, "add", "-A")
    _run_git(r, "commit", "-q", "-m", "init")
    return r


def _sha(repo):
    return _run_git(repo, "rev-parse", "HEAD").stdout.strip()


def test_hold_shared_no_argv_returns_2(capsys):
    assert main(["hold-shared"]) == 2


def test_hold_shared_runs_child_and_returns_its_exit_code(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDITTRACE_NEUTER_LOCK", str(tmp_path / "pool.lock"))
    assert main(["hold-shared", "--", "true"]) == 0
    assert main(["hold-shared", "--", "false"]) == 1


def test_hold_shared_refuses_while_pool_holds_exclusive(tmp_path, monkeypatch):
    lock_path = tmp_path / "pool.lock"
    monkeypatch.setenv("AUDITTRACE_NEUTER_LOCK", str(lock_path))
    fd = lockmod.open_lock_file(lock_path)
    lockmod.try_flock(fd, fcntl.LOCK_EX)
    try:
        assert main(["hold-shared", "--", "true"]) == 8
    finally:
        os.close(fd)


def test_assert_idle_no_file_returns_0(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDITTRACE_NEUTER_LOCK", str(tmp_path / "missing.lock"))
    assert main(["assert-idle"]) == 0


def test_assert_idle_refuses_while_pool_holds_exclusive(tmp_path, monkeypatch):
    lock_path = tmp_path / "pool.lock"
    monkeypatch.setenv("AUDITTRACE_NEUTER_LOCK", str(lock_path))
    fd = lockmod.open_lock_file(lock_path)
    lockmod.try_flock(fd, fcntl.LOCK_EX)
    try:
        assert main(["assert-idle"]) == 8
    finally:
        os.close(fd)


def test_assert_idle_ok_after_release(tmp_path, monkeypatch):
    lock_path = tmp_path / "pool.lock"
    monkeypatch.setenv("AUDITTRACE_NEUTER_LOCK", str(lock_path))
    fd = lockmod.open_lock_file(lock_path)
    os.close(fd)
    assert main(["assert-idle"]) == 0


def _spec_dict(repo):
    return {
        "schema": 3,
        "sha": _sha(repo),
        "scope_files": ["test_mod.py"],
        "guard_tests": [{"id": "test_mod.py::test_f", "row": "R1"}],
        "neuters": [
            {
                "id": "n1",
                "file": "mod.py",
                "edits": [{"old": "    return x + 1", "new": "    return x + 2"}],
                "tests": ["test_mod.py::test_f"],
                "engines": ["mock"],
                "guard": "G",
                "clause": "C",
            }
        ],
    }


def test_cmd_run_spec_load_error_exits_3(tmp_path, repo, capsys):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps({"schema": 99}))
    rc = main(
        [
            "run",
            "--sha",
            _sha(repo),
            "--neuters",
            str(spec_path),
            "--evidence",
            str(tmp_path / "ev"),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
            "--workers",
            "1",
            "--no-db",
        ]
    )
    assert rc == 3
    assert "spec load error" in capsys.readouterr().err


def test_cmd_run_full_pool_via_cli(tmp_path, repo):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    evidence = tmp_path / "ev"
    rc = main(
        [
            "run",
            "--sha",
            _sha(repo),
            "--neuters",
            str(spec_path),
            "--evidence",
            str(evidence),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
            "--workers",
            "1",
            "--no-db",
            "--pathcheck-module",
            "mod",
            "--src-root-relative",
            "",
            "--lock-path",
            str(tmp_path / "pool.lock"),
        ]
    )
    assert rc == 0
    rows = [
        json.loads(line)
        for line in (evidence / "neuter_results.jsonl").read_text().splitlines()
    ]
    assert rows[0]["verdict"] == "RED"


def test_cmd_report_writes_and_verifies(tmp_path, repo):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    evidence = tmp_path / "ev"
    main(
        [
            "run",
            "--sha",
            _sha(repo),
            "--neuters",
            str(spec_path),
            "--evidence",
            str(evidence),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
            "--workers",
            "1",
            "--no-db",
            "--pathcheck-module",
            "mod",
            "--src-root-relative",
            "",
            "--lock-path",
            str(tmp_path / "pool.lock"),
        ]
    )
    rc = main(
        [
            "report",
            "--evidence",
            str(evidence),
            "--neuters",
            str(spec_path),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
        ]
    )
    assert rc == 0
    assert (evidence / "per_guard_table.md").exists()
    rc_verify = main(
        [
            "report",
            "--evidence",
            str(evidence),
            "--neuters",
            str(spec_path),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
            "--verify",
        ]
    )
    assert rc_verify == 0


def test_cmd_arbitrate_writes_arbitration_row(tmp_path, repo):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    evidence = tmp_path / "ev"
    main(
        [
            "run",
            "--sha",
            _sha(repo),
            "--neuters",
            str(spec_path),
            "--evidence",
            str(evidence),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
            "--workers",
            "1",
            "--no-db",
            "--pathcheck-module",
            "mod",
            "--src-root-relative",
            "",
            "--lock-path",
            str(tmp_path / "pool.lock"),
        ]
    )
    rc = main(
        [
            "arbitrate",
            "--ids",
            "n1",
            "--evidence",
            str(evidence),
            "--neuters",
            str(spec_path),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
        ]
    )
    assert rc == 0
    rows = [
        json.loads(line)
        for line in (evidence / "arbitration.jsonl").read_text().splitlines()
    ]
    assert rows[0]["id"] == "n1"
    assert rows[0]["authoritative_verdict"] == "RED"
    assert rows[0]["harness_verdict"] == "RED"


def test_cmd_arbitrate_raises_on_preexisting_dirty_repo(tmp_path, repo):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    (repo / "mod.py").write_text((repo / "mod.py").read_text() + "\n# dirt\n")
    with pytest.raises(RuntimeError, match="dirty before arbitrating"):
        main(
            [
                "arbitrate",
                "--ids",
                "n1",
                "--evidence",
                str(tmp_path / "ev"),
                "--neuters",
                str(spec_path),
                "--repo-dir",
                str(repo),
                "--python",
                PYTHON,
            ]
        )


def test_cmd_arbitrate_nocompile_is_authoritative(tmp_path, repo):
    spec = _spec_dict(repo)
    spec["neuters"][0]["edits"][0]["new"] = "    return x <> 2"  # syntax error
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(spec))
    rc = main(
        [
            "arbitrate",
            "--ids",
            "n1",
            "--evidence",
            str(tmp_path / "ev"),
            "--neuters",
            str(spec_path),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
        ]
    )
    assert rc == 0
    rows = [
        json.loads(line)
        for line in (tmp_path / "ev" / "arbitration.jsonl").read_text().splitlines()
    ]
    assert rows[0]["authoritative_verdict"] == "ERROR"


def test_cmd_report_with_reviewer_guard_tests(tmp_path, repo):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    evidence = tmp_path / "ev"
    main(
        [
            "run",
            "--sha",
            _sha(repo),
            "--neuters",
            str(spec_path),
            "--evidence",
            str(evidence),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
            "--workers",
            "1",
            "--no-db",
            "--pathcheck-module",
            "mod",
            "--src-root-relative",
            "",
            "--lock-path",
            str(tmp_path / "pool.lock"),
        ]
    )
    guard_tests_path = tmp_path / "reviewer_guard_tests.json"
    guard_tests_path.write_text(
        json.dumps(
            [
                {"id": "test_mod.py::test_f", "row": "R1"},
                {"id": "test_mod.py::test_extra", "row": "R2"},
            ]
        )
    )
    rc = main(
        [
            "report",
            "--evidence",
            str(evidence),
            "--neuters",
            str(spec_path),
            "--guard-tests",
            str(guard_tests_path),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
        ]
    )
    assert rc == 0
    text = (evidence / "per_guard_table.md").read_text()
    assert "test_extra" in text
