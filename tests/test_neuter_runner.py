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
from tests._neuter_test_evidence import evidence_dir_for as _ev

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


def test_hold_shared_child_never_inherits_the_chokepoint_env(tmp_path, monkeypatch):
    """Critical regression, found running the definitive `make test`: THIS
    module (`runner.py`) imports `scripts.neuter.pytest_run` at its own
    top, which makes NEUTER_CHOKEPOINT_REQUIRED=1 and
    PYTEST_PLUGINS=neuter_pathcheck sticky in the CURRENT process's own
    `os.environ` -- true for every process that ever imports `runner.py`,
    including the `hold-shared` wrapper `make test` itself uses to run the
    product's own, entirely un-gated pytest suite. Without this fix, that
    child inherits both vars, its own `neuter_pathcheck` plugin auto-loads,
    and the WHOLE `make test` run fails at `pytest_configure` before a
    single test runs (REQUIRED is set, but no token was ever recorded for
    that session)."""
    monkeypatch.setenv("AUDITTRACE_NEUTER_LOCK", str(tmp_path / "pool.lock"))
    monkeypatch.setenv("NEUTER_CHOKEPOINT_REQUIRED", "1")
    monkeypatch.setenv("PYTEST_PLUGINS", "neuter_pathcheck")
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, sys\n"
        "sys.exit(\n"
        "    1\n"
        "    if os.environ.get('NEUTER_CHOKEPOINT_REQUIRED')\n"
        "    or os.environ.get('PYTEST_PLUGINS')\n"
        "    else 0\n"
        ")\n"
    )
    assert main(["hold-shared", "--", PYTHON, str(probe)]) == 0


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
            str(_ev(tmp_path)),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
            "--workers",
            "1",
            "--no-db",
            "--lock-path",
            str(tmp_path / "collect.lock"),
        ]
    )
    assert rc == 3
    assert "spec load error" in capsys.readouterr().err


def test_cmd_run_full_pool_via_cli(tmp_path, repo):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    evidence = _ev(tmp_path)
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
    evidence = _ev(tmp_path)
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
            "--lock-path",
            str(tmp_path / "collect.lock"),
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
            "--lock-path",
            str(tmp_path / "collect.lock"),
        ]
    )
    assert rc_verify == 0


def test_cmd_report_ack_errors_records_ids_in_events_and_trailer(tmp_path, repo):
    """Review round 3 should-fix: ``--ack-errors``/``--ack-drift`` record
    the ACTUAL acknowledged ids, both in ``events.jsonl`` (durable,
    append-only) and in the report's own ACKNOWLEDGED section + trailer --
    never a bare flag."""
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    evidence = _ev(tmp_path)
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
    # Force the one row ERROR, as if a real crash/timeout had produced it --
    # this test cares about the ack plumbing, not how a row becomes ERROR.
    results_path = evidence / "neuter_results.jsonl"
    rows = [json.loads(line) for line in results_path.read_text().splitlines()]
    rows[0]["verdict"] = "ERROR"
    rows[0]["error_reason"] = "forced_for_test"
    results_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

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
            "--ack-errors",
            "--lock-path",
            str(tmp_path / "collect.lock"),
        ]
    )
    assert rc == 0
    trailer = (evidence / "per_guard_table.md").read_text()
    assert f"- error: {rows[0]['id']}" in trailer
    assert f"acked_error_ids={rows[0]['id']}" in trailer.splitlines()[-1]

    events = [
        json.loads(line)
        for line in (evidence / "events.jsonl").read_text().splitlines()
    ]
    ack_events = [e for e in events if e.get("event") == "ack"]
    assert len(ack_events) == 1
    assert ack_events[0]["acked_error_ids"] == [rows[0]["id"]]
    assert ack_events[0]["acked_drift_ids"] == []

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
            "--ack-errors",
            "--lock-path",
            str(tmp_path / "collect.lock"),
        ]
    )
    assert rc_verify == 0

    # Verifying WITHOUT the flag against the now-acknowledged report text
    # fails closed -- the report on disk carries the acked id, so a fresh,
    # unacknowledged regeneration no longer matches it byte-for-byte.
    rc_verify_unacked = main(
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
            "--lock-path",
            str(tmp_path / "collect.lock"),
        ]
    )
    assert rc_verify_unacked == 7


def test_cmd_arbitrate_writes_arbitration_row(tmp_path, repo):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    evidence = _ev(tmp_path)
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
            "--lock-path",
            str(tmp_path / "arb.lock"),
            "--no-db",
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


def test_cmd_arbitrate_watch_unproven_forces_error(tmp_path, repo, monkeypatch):
    """Review round 6 blocker fix: arbitrate's own ``classify()`` call must
    receive ``pytest_result.watch_attached`` -- if the foreign-container
    watch's readiness probe never proved live during arbitrate's
    full-scope run, THIS phase (the authoritative tie-break) must
    classify ERROR ``watch_unproven`` too, never a silent authoritative
    RED/GREEN riding on an unproven watch."""
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    evidence = _ev(tmp_path)
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

    class _FakeResult:
        exit_code = 1
        timed_out = False
        foreign_pg_container = False
        chokepoint_marker_ok = True
        watch_attached = False

    def fake_run_pytest(*, junit_path, **kwargs):
        # Mark the mapped test FAILED in the full-scope junit so the
        # pre-existing `not_reproduced` override (SPEC v3 S5, triggered
        # when the full-scope run never reproduces an originally-RED
        # harness verdict) does not mask `watch_unproven` -- this test is
        # isolating THIS phase's own watch-readiness check, not that one.
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        junit_path.write_text(
            '<?xml version="1.0"?><testsuite tests="1">'
            '<testcase classname="test_mod" name="test_f">'
            '<failure message="assert 2 == 3"/>'
            "</testcase>"
            "</testsuite>"
        )
        return _FakeResult()

    monkeypatch.setattr("scripts.neuter.runner.run_pytest", fake_run_pytest)
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
            "--lock-path",
            str(tmp_path / "arb.lock"),
            "--no-db",
        ]
    )
    assert rc == 0
    rows = [
        json.loads(line)
        for line in (evidence / "arbitration.jsonl").read_text().splitlines()
    ]
    assert rows[0]["id"] == "n1"
    assert rows[0]["authoritative_verdict"] == "ERROR"
    assert rows[0]["authoritative_error_reason"] == "watch_unproven"


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
                str(_ev(tmp_path)),
                "--neuters",
                str(spec_path),
                "--repo-dir",
                str(repo),
                "--python",
                PYTHON,
                "--lock-path",
                str(tmp_path / "arb.lock"),
                "--no-db",
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
            str(_ev(tmp_path)),
            "--neuters",
            str(spec_path),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
            "--lock-path",
            str(tmp_path / "arb.lock"),
            "--no-db",
        ]
    )
    assert rc == 0
    rows = [
        json.loads(line)
        for line in (_ev(tmp_path) / "arbitration.jsonl").read_text().splitlines()
    ]
    assert rows[0]["authoritative_verdict"] == "ERROR"


def test_cmd_report_with_reviewer_guard_tests(tmp_path, repo):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    evidence = _ev(tmp_path)
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
            "--lock-path",
            str(tmp_path / "collect.lock"),
        ]
    )
    assert rc == 0
    text = (evidence / "per_guard_table.md").read_text()
    assert "test_extra" in text


def test_cmd_arbitrate_tests_expected_matches_full_scope_not_mapped_count(tmp_path):
    """``arbitrate`` runs over the FULL ``scope_files``, not just the
    neuter's mapped ``tests`` -- junit's own collected count (many more
    than the one mapped test) must not be compared against
    ``len(entry.tests)`` (1), or every arbitration would spuriously go
    ERROR ``collected`` regardless of the actual outcome. Found running
    the T1 oracle for real (SPEC v3 §12)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("def f(x):\n    return x + 1\n")
    (repo / "test_mod.py").write_text(
        "from mod import f\n\n\n"
        "def test_f():\n    assert f(1) == 2\n\n\n"
        "def test_g():\n    assert f(2) == 3\n"
    )
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "a@b.c")
    _run_git(repo, "config", "user.name", "a")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "init")

    spec = {
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
            }
        ],
    }
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(spec))
    rc = main(
        [
            "arbitrate",
            "--ids",
            "n1",
            "--evidence",
            str(_ev(tmp_path)),
            "--neuters",
            str(spec_path),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
            "--lock-path",
            str(tmp_path / "arb.lock"),
            "--no-db",
        ]
    )
    assert rc == 0
    rows = [
        json.loads(line)
        for line in (_ev(tmp_path) / "arbitration.jsonl").read_text().splitlines()
    ]
    assert rows[0]["authoritative_verdict"] == "RED"
    assert rows[0]["authoritative_error_reason"] is None


# ─────────────────── should-fix: --sha honoured, /tmp refused ────────────


def test_cmd_run_sha_mismatch_refused(tmp_path, repo):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    rc = main(
        [
            "run",
            "--sha",
            "0" * 40,
            "--neuters",
            str(spec_path),
            "--evidence",
            str(_ev(tmp_path)),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
            "--workers",
            "1",
            "--no-db",
            "--lock-path",
            str(tmp_path / "collect.lock"),
        ]
    )
    assert rc == 3


def test_cmd_run_evidence_under_tmp_refused(tmp_path, repo, capsys):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    rc = main(
        [
            "run",
            "--sha",
            _sha(repo),
            "--neuters",
            str(spec_path),
            "--evidence",
            "/tmp/some-neuter-run",
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
            "--workers",
            "1",
            "--no-db",
            "--lock-path",
            str(tmp_path / "collect.lock"),
        ]
    )
    assert rc == 3
    assert "refused" in capsys.readouterr().err


def test_cmd_run_guard_tests_closure_ok_and_persisted(tmp_path, repo):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    guard_tests_path = tmp_path / "guard_tests.json"
    guard_tests_path.write_text(
        json.dumps([{"id": "test_mod.py::test_f", "row": "R1"}])
    )
    evidence = _ev(tmp_path)
    rc = main(
        [
            "run",
            "--sha",
            _sha(repo),
            "--neuters",
            str(spec_path),
            "--guard-tests",
            str(guard_tests_path),
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
    assert (evidence / "guard_tests_reviewer.json").exists()


def test_cmd_run_guard_tests_closure_mismatch_refused(tmp_path, repo, capsys):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    guard_tests_path = tmp_path / "guard_tests.json"
    guard_tests_path.write_text(
        json.dumps([{"id": "test_mod.py::test_OTHER", "row": "R1"}])
    )
    rc = main(
        [
            "run",
            "--sha",
            _sha(repo),
            "--neuters",
            str(spec_path),
            "--guard-tests",
            str(guard_tests_path),
            "--evidence",
            str(_ev(tmp_path)),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
            "--workers",
            "1",
            "--no-db",
            "--lock-path",
            str(tmp_path / "collect.lock"),
        ]
    )
    assert rc == 3
    assert "does not close over union(tests)" in capsys.readouterr().err


# ──────────────── should-fix: arbitrate takes the lock ───────────────────


def test_cmd_arbitrate_refuses_while_lock_held(tmp_path, repo):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    lock_path = tmp_path / "pool.lock"
    fd = lockmod.open_lock_file(lock_path)
    lockmod.try_flock(fd, fcntl.LOCK_EX)
    try:
        rc = main(
            [
                "arbitrate",
                "--ids",
                "n1",
                "--evidence",
                str(_ev(tmp_path)),
                "--neuters",
                str(spec_path),
                "--repo-dir",
                str(repo),
                "--python",
                PYTHON,
                "--lock-path",
                str(lock_path),
                "--no-db",
            ]
        )
        assert rc == 8
    finally:
        os.close(fd)


def test_cmd_arbitrate_reads_merged_results_when_no_per_worker_files(tmp_path, repo):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    evidence = _ev(tmp_path)
    evidence.mkdir(exist_ok=True)
    (evidence / "neuter_results.jsonl").write_text(
        json.dumps({"id": "n1", "verdict": "RED"}) + "\n"
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
            "--lock-path",
            str(tmp_path / "arb.lock"),
            "--no-db",
        ]
    )
    assert rc == 0
    rows = [
        json.loads(line)
        for line in (evidence / "arbitration.jsonl").read_text().splitlines()
    ]
    assert rows[0]["harness_verdict"] == "RED"


def test_cmd_arbitrate_x6_never_reports_authoritative_green(tmp_path):
    """Blocker 3, through the real CLI: a wrong-mapping neuter (X6) that
    leaves an UNMAPPED test failing in the full scope must never come back
    ``authoritative_verdict: GREEN`` from ``arbitrate``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text(
        "def f(x):\n    return x + 1\n\n\ndef g(x):\n    return x * 2\n"
    )
    (repo / "test_mod.py").write_text(
        "from mod import f, g\n\n\n"
        "def test_f():\n    assert f(1) == 2\n\n\n"
        "def test_g():\n    assert g(2) == 4\n"
    )
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "a@b.c")
    _run_git(repo, "config", "user.name", "a")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "init")

    spec_dict = {
        "schema": 3,
        "sha": _sha(repo),
        "scope_files": ["test_mod.py"],
        "guard_tests": [{"id": "test_mod.py::test_f", "row": "R1"}],
        "neuters": [
            {
                "id": "x6",
                "file": "mod.py",
                # breaks g(), but mapped only to test_f (which exercises f()).
                "edits": [{"old": "    return x * 2", "new": "    return x * 3"}],
                "tests": ["test_mod.py::test_f"],
                "engines": ["mock"],
                "guard": "G",
            }
        ],
    }
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(spec_dict))
    evidence = _ev(tmp_path)
    evidence.mkdir(exist_ok=True)
    (evidence / "neuter_results.jsonl").write_text(
        json.dumps({"id": "x6", "verdict": "GREEN"}) + "\n"
    )
    rc = main(
        [
            "arbitrate",
            "--ids",
            "x6",
            "--evidence",
            str(evidence),
            "--neuters",
            str(spec_path),
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
            "--lock-path",
            str(tmp_path / "arb.lock"),
            "--no-db",
        ]
    )
    assert rc == 0
    rows = [
        json.loads(line)
        for line in (evidence / "arbitration.jsonl").read_text().splitlines()
    ]
    assert rows[0]["authoritative_verdict"] != "GREEN"
    assert rows[0]["authoritative_verdict"] == "DRIFT"
    assert any("test_g" in u for u in rows[0]["unmapped_red"])


def test_cmd_report_picks_up_guard_tests_reviewer_json_automatically(tmp_path, repo):
    spec_path = tmp_path / "neuters.json"
    spec_path.write_text(json.dumps(_spec_dict(repo)))
    evidence = _ev(tmp_path)
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
            str(tmp_path / "collect.lock"),
        ]
    )
    evidence.mkdir(exist_ok=True)
    (evidence / "guard_tests_reviewer.json").write_text(
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
            "--repo-dir",
            str(repo),
            "--python",
            PYTHON,
            "--lock-path",
            str(tmp_path / "collect.lock"),
        ]
    )
    assert rc == 0
    text = (evidence / "per_guard_table.md").read_text()
    assert "test_extra" in text
