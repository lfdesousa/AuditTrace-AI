"""Unit tests for the deterministic release-cut runner (#398).

Every external effect — subprocess (make/git) — is MOCKED. No real `make
release`, no real `make test`, no real git branch/commit/push happens in this
file. Where a mocked command would realistically mutate the filesystem (the
ADR-055 version bump), the test double performs the equivalent write itself
so downstream phases (the version-drift check) see a state consistent with
what the real command would have produced.

The reviewer's mandate is to prove each gate can FAIL, so every abort path
(R0 hard gates, R2 gate failure, R3 branch/commit failure, R4 push failure)
is exercised, plus the falsifiability test that proves the runner never
creates or pushes a git tag.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from scripts.release import runner
from scripts.release.runner import (
    BranchCommitFailedError,
    BumpFailedError,
    GateFailError,
    PreflightAbortError,
    PushFailedError,
    ReleaseConfig,
    ReleaseRunner,
    is_strictly_greater,
    parse_semver,
    read_chart_app_version,
    read_pyproject_version,
    staged_tag_command,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["x"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _write_repo(tmp_path, pyproject_version="1.0.0", chart_version="1.0.0"):
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "{pyproject_version}"\n'
    )
    chart_dir = tmp_path / "charts" / "audittrace"
    chart_dir.mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text(
        f'version: {chart_version}\nappVersion: "{chart_version}"\n'
    )
    return tmp_path


def _cfg(tmp_path, version="1.1.0", **kw):
    kw.setdefault("out_dir", tmp_path / "runs")
    kw.setdefault("repo_root", tmp_path)
    return ReleaseConfig(version=version, **kw)


_GATE_GREEN_STDOUT = (
    "...\n"
    + runner._PER_FILE_GATE_PASS_MARKER
    + " (12 files checked)\n"
    + "[no-skip-check] "
    + runner._ZERO_SKIP_PASS_MARKER
    + " in junit.xml. Good.\n"
)


class _Dispatcher:
    """Routes runner._run calls to canned CompletedProcess results.

    ``make release`` gets a REALISTIC side effect: it writes the bumped
    version into the fake repo's pyproject.toml + Chart.yaml, exactly like
    the real Makefile target would — so the R2 version-drift check (which
    reads those files directly, never via subprocess) sees a consistent
    post-bump state without this test file re-implementing the sed either.
    """

    def __init__(self, repo_root, version, rules=None, bump_ok=True, gate_ok=True):
        self.repo_root = repo_root
        self.version = version
        self.rules = rules or []
        self.bump_ok = bump_ok
        self.gate_ok = gate_ok
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        if "make release" in joined:
            if not self.bump_ok:
                return _proc(1, "", "make release boom")
            _write_repo(self.repo_root, self.version, self.version)
            return _proc(0, "release-prep done")
        if "make test" in joined:
            return (
                _proc(0, _GATE_GREEN_STDOUT) if self.gate_ok else _proc(1, "FAILED", "")
            )
        for needle, result in self.rules:
            if needle in joined:
                return result
        return _proc(0, "")


def _happy_dispatcher(tmp_path, version="1.1.0"):
    return _Dispatcher(
        tmp_path,
        version,
        rules=[
            ("rev-parse --abbrev-ref", _proc(0, "main\n")),
            ("status --porcelain", _proc(0, "")),
            ("rev-parse HEAD", _proc(0, "abc123def456\n")),
            ("git checkout -b", _proc(0, "")),
            ("git add", _proc(0, "")),
            ("git commit -m", _proc(0, "")),
            ("describe --tags", _proc(0, "v1.0.0\n")),
            ("git log", _proc(0, "def456 feat: something\nabc789 fix: other\n")),
            ("git push", _proc(0, "")),
        ],
    )


# ── pure semver helpers ───────────────────────────────────────────────────────


def test_parse_semver_valid():
    assert parse_semver("1.17.1") == (1, 17, 1)
    assert parse_semver("0.0.1") == (0, 0, 1)


@pytest.mark.parametrize(
    "bad",
    ["1.17", "v1.17.1", "1.17.1-rc1", "01.2.3", "1.2.x", "", "1.2.3.4"],
)
def test_parse_semver_invalid(bad):
    with pytest.raises(ValueError):
        parse_semver(bad)


def test_is_strictly_greater_true():
    assert is_strictly_greater("1.18.0", "1.17.1") is True


def test_is_strictly_greater_false_equal():
    assert is_strictly_greater("1.17.1", "1.17.1") is False


def test_is_strictly_greater_false_lesser():
    assert is_strictly_greater("1.17.0", "1.17.1") is False


def test_staged_tag_command_shape():
    cmd = staged_tag_command("1.18.0")
    assert cmd.startswith("git tag -a v1.18.0")
    assert "git push origin v1.18.0" in cmd
    assert "&&" in cmd


def test_read_pyproject_version(tmp_path):
    _write_repo(tmp_path, "2.3.4", "2.3.4")
    assert read_pyproject_version(tmp_path) == "2.3.4"


def test_read_chart_app_version(tmp_path):
    _write_repo(tmp_path, "2.3.4", "2.3.4")
    assert read_chart_app_version(tmp_path) == "2.3.4"


# ── the real (unmocked) subprocess wrapper + timestamp helper ────────────────


def test_run_wrapper_executes_real_subprocess(tmp_path):
    proc = runner._run(["python3", "-c", "print('hi')"], cwd=tmp_path)
    assert proc.returncode == 0
    assert "hi" in proc.stdout


def test_now_iso_is_iso_format():
    stamp = runner._now_iso()
    assert "T" in stamp
    assert stamp.count("-") >= 2


# ── R0 preflight ──────────────────────────────────────────────────────────────


def test_preflight_aborts_wrong_branch(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.0.0", "1.0.0")
    monkeypatch.setattr(
        runner,
        "_run",
        lambda cmd, **k: (
            _proc(0, "feature/x\n") if "abbrev-ref" in " ".join(cmd) else _proc(0, "")
        ),
    )
    r = ReleaseRunner(_cfg(tmp_path))
    with pytest.raises(PreflightAbortError, match="not on main"):
        r.phase_preflight()
    assert r.records[0].status == "aborted"


def test_preflight_aborts_when_branch_read_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda cmd, **k: _proc(1, "", "no repo"))
    r = ReleaseRunner(_cfg(tmp_path))
    with pytest.raises(PreflightAbortError, match="not on main"):
        r.phase_preflight()


def test_preflight_aborts_dirty_tree(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.0.0", "1.0.0")

    def fake_run(cmd, **k):
        joined = " ".join(cmd)
        if "abbrev-ref" in joined:
            return _proc(0, "main\n")
        if "porcelain" in joined:
            return _proc(0, " M some/tracked/file.py\n")
        return _proc(0, "")

    monkeypatch.setattr(runner, "_run", fake_run)
    r = ReleaseRunner(_cfg(tmp_path))
    with pytest.raises(PreflightAbortError, match="tracked changes"):
        r.phase_preflight()
    assert r.records[0].status == "aborted"


def _clean_main_run(**overrides):
    def fake_run(cmd, **k):
        joined = " ".join(cmd)
        if "abbrev-ref" in joined:
            return _proc(0, "main\n")
        if "porcelain" in joined:
            return _proc(0, "")
        if "rev-parse HEAD" in joined:
            return _proc(0, "abc123def456\n")
        return _proc(0, "")

    return fake_run


def test_preflight_aborts_invalid_semver(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.0.0", "1.0.0")
    monkeypatch.setattr(runner, "_run", _clean_main_run())
    r = ReleaseRunner(_cfg(tmp_path, version="not-a-version"))
    with pytest.raises(PreflightAbortError, match="not a valid"):
        r.phase_preflight()


def test_preflight_aborts_current_version_invalid_semver(tmp_path, monkeypatch):
    _write_repo(tmp_path, "garbage", "garbage")
    monkeypatch.setattr(runner, "_run", _clean_main_run())
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    with pytest.raises(PreflightAbortError, match="not valid semver"):
        r.phase_preflight()


def test_preflight_aborts_version_not_greater_equal(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.1.0", "1.1.0")
    monkeypatch.setattr(runner, "_run", _clean_main_run())
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    with pytest.raises(PreflightAbortError, match="not strictly greater"):
        r.phase_preflight()


def test_preflight_aborts_version_lower(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.1.0", "1.1.0")
    monkeypatch.setattr(runner, "_run", _clean_main_run())
    r = ReleaseRunner(_cfg(tmp_path, version="1.0.0"))
    with pytest.raises(PreflightAbortError, match="not strictly greater"):
        r.phase_preflight()


def test_preflight_ok(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.0.0", "1.0.0")
    monkeypatch.setattr(runner, "_run", _clean_main_run())
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    r.phase_preflight()
    assert r.records[0].status == "ok"
    assert r.base_commit == "abc123def456"


def test_preflight_ok_under_dry_run_too(tmp_path, monkeypatch):
    """R0 hard gates run for REAL even in --dry-run (idempotent-safe)."""
    _write_repo(tmp_path, "1.1.0", "1.1.0")
    monkeypatch.setattr(runner, "_run", _clean_main_run())
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0", dry_run=True))
    with pytest.raises(PreflightAbortError, match="not strictly greater"):
        r.phase_preflight()


# ── R1 bump ───────────────────────────────────────────────────────────────────


def _boom(cmd, **k):
    raise AssertionError(f"_run must NOT be called in this path: {cmd}")


def test_bump_dry_run_plans_without_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", _boom)
    r = ReleaseRunner(_cfg(tmp_path, dry_run=True))
    r.phase_bump()  # would raise via _boom if it shelled out
    assert r.records[-1].status == "planned"
    assert "make release VERSION=1.1.0" in r.records[-1].command


def test_bump_ok(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.0.0", "1.0.0")
    disp = _Dispatcher(
        tmp_path,
        "1.1.0",
        rules=[("status --porcelain", _proc(0, "M pyproject.toml\n"))],
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    r.phase_bump()
    assert r.records[-1].status == "ok"
    assert read_pyproject_version(tmp_path) == "1.1.0"  # side-effect applied


def test_bump_fails(tmp_path, monkeypatch):
    disp = _Dispatcher(tmp_path, "1.1.0", bump_ok=False)
    monkeypatch.setattr(runner, "_run", disp)
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    with pytest.raises(BumpFailedError):
        r.phase_bump()
    assert r.records[-1].status == "aborted"


# ── R2 gate ───────────────────────────────────────────────────────────────────


def test_gate_dry_run_plans_without_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", _boom)
    r = ReleaseRunner(_cfg(tmp_path, dry_run=True))
    r.phase_gate()
    assert r.records[-1].status == "planned"


def test_gate_ok(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.1.0", "1.1.0")  # already-bumped state
    monkeypatch.setattr(runner, "_run", lambda cmd, **k: _proc(0, _GATE_GREEN_STDOUT))
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    r.phase_gate()
    assert r.records[-1].status == "ok"
    assert r.gate_summary["ok"] is True


def test_gate_fails_on_nonzero_exit(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.1.0", "1.1.0")
    monkeypatch.setattr(runner, "_run", lambda cmd, **k: _proc(1, _GATE_GREEN_STDOUT))
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    with pytest.raises(GateFailError):
        r.phase_gate()
    assert r.gate_summary["tests_green"] is False
    assert r.records[-1].status == "aborted"


def test_gate_fails_missing_per_file_marker(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.1.0", "1.1.0")
    stdout = "No skipped tests. Good.\n"  # per-file marker missing
    monkeypatch.setattr(runner, "_run", lambda cmd, **k: _proc(0, stdout))
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    with pytest.raises(GateFailError):
        r.phase_gate()
    assert r.gate_summary["per_file_gate_pass"] is False


def test_gate_fails_missing_zero_skip_marker(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.1.0", "1.1.0")
    stdout = runner._PER_FILE_GATE_PASS_MARKER + "\n"  # zero-skip marker missing
    monkeypatch.setattr(runner, "_run", lambda cmd, **k: _proc(0, stdout))
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    with pytest.raises(GateFailError):
        r.phase_gate()
    assert r.gate_summary["zero_skip_pass"] is False


def test_gate_fails_version_drift(tmp_path, monkeypatch):
    # pyproject/chart still at the OLD version — as if `make release` never ran.
    _write_repo(tmp_path, "1.0.0", "1.0.0")
    monkeypatch.setattr(runner, "_run", lambda cmd, **k: _proc(0, _GATE_GREEN_STDOUT))
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    with pytest.raises(GateFailError):
        r.phase_gate()
    assert r.gate_summary["version_drift_pass"] is False


def test_version_drift_ok_missing_files_returns_false(tmp_path):
    # repo_root has no pyproject.toml / Chart.yaml at all -> OSError branch.
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    assert r._version_drift_ok() is False


# ── R3 branch + commit ────────────────────────────────────────────────────────


def test_branch_commit_dry_run_plans_without_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", _boom)
    r = ReleaseRunner(_cfg(tmp_path, dry_run=True))
    r.phase_branch_commit()
    assert r.records[-1].status == "planned"
    assert "chore/release-v1.1.0" in r.records[-1].command
    assert "Co-Authored-By" not in r.records[-1].command


def test_branch_commit_ok(tmp_path, monkeypatch):
    disp = _Dispatcher(
        tmp_path,
        "1.1.0",
        rules=[
            ("git checkout -b", _proc(0, "")),
            ("git add", _proc(0, "")),
            ("git commit -m", _proc(0, "")),
        ],
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    r.phase_branch_commit()
    assert r.branch_name == "chore/release-v1.1.0"
    assert r.records[-1].status == "ok"
    checkout_calls = [c for c in disp.calls if c[:2] == ["git", "checkout"]]
    add_calls = [c for c in disp.calls if c[:2] == ["git", "add"]]
    commit_calls = [c for c in disp.calls if c[:2] == ["git", "commit"]]
    assert checkout_calls and add_calls and commit_calls
    assert set(runner.BUMP_FILES).issubset(set(add_calls[0][2:]))
    assert "chore(release): v1.1.0" in commit_calls[0]
    assert not any("Co-Authored-By" in part for part in commit_calls[0])


def test_branch_commit_checkout_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner,
        "_run",
        lambda cmd, **k: _proc(1, "", "boom") if "checkout" in cmd else _proc(0, ""),
    )
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    with pytest.raises(BranchCommitFailedError):
        r.phase_branch_commit()
    assert r.records[-1].status == "aborted"


def test_branch_commit_add_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner,
        "_run",
        lambda cmd, **k: _proc(1, "", "boom") if "add" in cmd else _proc(0, ""),
    )
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    with pytest.raises(BranchCommitFailedError):
        r.phase_branch_commit()


def test_branch_commit_commit_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner,
        "_run",
        lambda cmd, **k: _proc(1, "", "boom") if "commit" in cmd else _proc(0, ""),
    )
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    with pytest.raises(BranchCommitFailedError):
        r.phase_branch_commit()


# ── R4 push + record ──────────────────────────────────────────────────────────


def test_push_and_record_dry_run_plans_without_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", _boom)
    r = ReleaseRunner(_cfg(tmp_path, dry_run=True))
    r.branch_name = "chore/release-v1.1.0"
    result = r.phase_push_and_record()
    assert result is None
    assert r.records[-1].status == "planned"
    assert "NEVER" in r.records[-1].detail
    assert not list((tmp_path / "runs").glob("*.json"))


def test_push_and_record_ok(tmp_path, monkeypatch):
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    r.base_commit = "abc123def456"
    r.branch_name = "chore/release-v1.1.0"
    r.gate_summary = {"ok": True}

    def fake_run(cmd, **k):
        joined = " ".join(cmd)
        if "git push" in joined:
            return _proc(0, "")
        if "describe --tags" in joined:
            return _proc(0, "v1.0.0\n")
        if "git log" in joined:
            return _proc(0, "abc111 fix: x\nabc222 feat: y\n")
        return _proc(0, "")

    monkeypatch.setattr(runner, "_run", fake_run)
    report = r.phase_push_and_record()
    assert report is not None
    assert report["tag_pushed"] is False
    assert report["published"] is None
    assert report["previous_tag"] == "v1.0.0"
    assert report["bundled_commits"] == ["abc111 fix: x", "abc222 feat: y"]
    out = list((tmp_path / "runs").glob("release-1.1.0-*.json"))
    assert len(out) == 1
    data = json.loads(out[0].read_text())
    assert data["tag_pushed"] is False
    assert data["published"] is None
    assert out[0].with_suffix(".txt").exists()


def test_push_and_record_no_previous_tag(tmp_path, monkeypatch):
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    r.base_commit = "abc123def456"

    def fake_run(cmd, **k):
        joined = " ".join(cmd)
        if "describe --tags" in joined:
            return _proc(128, "", "fatal: No tags can describe")
        return _proc(0, "")

    monkeypatch.setattr(runner, "_run", fake_run)
    report = r.phase_push_and_record()
    assert report["previous_tag"] is None
    assert report["bundled_commits"] == []


def test_push_and_record_log_fails_after_tag_found(tmp_path, monkeypatch):
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    r.base_commit = "abc123def456"

    def fake_run(cmd, **k):
        joined = " ".join(cmd)
        if "describe --tags" in joined:
            return _proc(0, "v1.0.0\n")
        if "git log" in joined:
            return _proc(1, "", "boom")
        return _proc(0, "")

    monkeypatch.setattr(runner, "_run", fake_run)
    report = r.phase_push_and_record()
    assert report["previous_tag"] == "v1.0.0"
    assert report["bundled_commits"] == []


def test_push_fails(tmp_path, monkeypatch):
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    r.base_commit = "abc123def456"
    monkeypatch.setattr(runner, "_run", lambda cmd, **k: _proc(1, "", "network down"))
    with pytest.raises(PushFailedError):
        r.phase_push_and_record()
    assert r.records[-1].status == "aborted"
    assert not list((tmp_path / "runs").glob("*.json"))


# ── full run() orchestration ──────────────────────────────────────────────────


def test_run_aborts_at_preflight_no_further_calls(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.0.0", "1.0.0")
    calls: list[list[str]] = []

    def fake_run(cmd, **k):
        calls.append(list(cmd))
        return _proc(0, "feature/x\n")  # wrong branch; every call would say so

    monkeypatch.setattr(runner, "_run", fake_run)
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    report = r.run()
    assert report["aborted"] is True
    names = [p["name"] for p in report["phases"]]
    assert names == [runner.PHASES[0]]
    # ONLY the branch check ran — no further read (status/version) attempted.
    assert len(calls) == 1


def test_run_gate_failure_stops_before_branch_commit(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.0.0", "1.0.0")
    disp = _Dispatcher(tmp_path, "1.1.0", gate_ok=False)
    disp.rules = [
        ("rev-parse --abbrev-ref", _proc(0, "main\n")),
        ("status --porcelain", _proc(0, "")),
        ("rev-parse HEAD", _proc(0, "abc123def456\n")),
    ]
    monkeypatch.setattr(runner, "_run", disp)
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    report = r.run()
    assert report["aborted"] is True
    names = [p["name"] for p in report["phases"]]
    assert names == [runner.PHASES[0], runner.PHASES[1], runner.PHASES[2]]
    # No branch/commit/push command was EVER issued.
    assert not any(c[:2] == ["git", "checkout"] for c in disp.calls)
    assert not any(c[:2] == ["git", "commit"] for c in disp.calls)
    assert not any(c[:2] == ["git", "push"] for c in disp.calls)


def test_run_bump_failure_stops_before_gate(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.0.0", "1.0.0")
    disp = _Dispatcher(tmp_path, "1.1.0", bump_ok=False)
    disp.rules = [
        ("rev-parse --abbrev-ref", _proc(0, "main\n")),
        ("status --porcelain", _proc(0, "")),
        ("rev-parse HEAD", _proc(0, "abc123def456\n")),
    ]
    monkeypatch.setattr(runner, "_run", disp)
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    report = r.run()
    assert report["aborted"] is True
    names = [p["name"] for p in report["phases"]]
    assert names == [runner.PHASES[0], runner.PHASES[1]]
    assert not any("make test" in " ".join(c) for c in disp.calls)


def test_run_branch_commit_failure_stops_before_push(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.0.0", "1.0.0")
    disp = _happy_dispatcher(tmp_path, "1.1.0")

    def failing_checkout(cmd, **k):
        if "checkout" in cmd:
            return _proc(1, "", "boom")
        return disp(cmd, **k)

    monkeypatch.setattr(runner, "_run", failing_checkout)
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    report = r.run()
    assert report["aborted"] is True
    names = [p["name"] for p in report["phases"]]
    assert names == [
        runner.PHASES[0],
        runner.PHASES[1],
        runner.PHASES[2],
        runner.PHASES[3],
    ]


def test_run_full_success(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.0.0", "1.0.0")
    disp = _happy_dispatcher(tmp_path, "1.1.0")
    monkeypatch.setattr(runner, "_run", disp)
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    report = r.run()

    assert report["aborted"] is False
    assert report["version"] == "1.1.0"
    assert report["branch"] == "chore/release-v1.1.0"
    assert report["gate_summary"]["ok"] is True
    assert report["tag_pushed"] is False
    assert report["published"] is None
    assert "git tag" in report["staged_tag_command"]
    assert "v1.1.0" in report["staged_tag_command"]
    names = [p["name"] for p in report["phases"]]
    assert names == list(runner.PHASES)
    out = list((tmp_path / "runs").glob("release-1.1.0-*.json"))
    assert len(out) == 1


def test_run_dry_run_full_plan_no_mutation(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.0.0", "1.0.0")
    monkeypatch.setattr(runner, "_run", _clean_main_run())
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0", dry_run=True))
    report = r.run()

    assert report["aborted"] is False
    assert report["dry_run"] is True
    names = [p["name"] for p in report["phases"]]
    assert names == list(runner.PHASES)
    statuses = [p["status"] for p in report["phases"]]
    assert statuses[0] == "ok"  # R0 always executes for real
    assert statuses[1:] == ["planned", "planned", "planned", "planned"]
    # nothing was written to disk — "mutates nothing" is interpreted fail-closed.
    assert not (tmp_path / "runs").exists() or not list((tmp_path / "runs").glob("*"))
    # pyproject.toml on disk is untouched.
    assert read_pyproject_version(tmp_path) == "1.0.0"


# ── falsifiability: the runner NEVER creates/pushes a git tag ────────────────


def test_falsifiability_never_pushes_a_tag(tmp_path, monkeypatch):
    """Prove the runner does not invoke `git tag` or push the vX.Y.Z tag ref
    anywhere across a full, otherwise-successful run.

    Neutering the guard — e.g. adding a ``_run(["git", "tag", ...])`` /
    ``_run(["git", "push", "origin", f"v{version}"])`` call anywhere in
    :mod:`scripts.release.runner` — turns this test RED, because every
    invocation of the (patched) ``_run`` is captured and asserted against
    below.
    """
    _write_repo(tmp_path, "1.0.0", "1.0.0")
    disp = _happy_dispatcher(tmp_path, "1.1.0")
    monkeypatch.setattr(runner, "_run", disp)
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0"))
    report = r.run()

    assert report["aborted"] is False  # sanity: the run actually completed

    tag_ref = "v1.1.0"
    for call in disp.calls:
        assert not (call[:2] == ["git", "tag"]), f"git tag invoked: {call}"
        if call[:2] == ["git", "push"]:
            assert tag_ref not in call, f"tag ref pushed: {call}"

    # The staged command is DATA in the report, never an executed argv.
    assert report["staged_tag_command"] == staged_tag_command("1.1.0")
    executed_joined = [" ".join(c) for c in disp.calls]
    assert not any("git tag" in j for j in executed_joined)


def test_falsifiability_dry_run_never_pushes_a_tag(tmp_path, monkeypatch):
    _write_repo(tmp_path, "1.0.0", "1.0.0")
    calls: list[list[str]] = []

    def spy(cmd, **k):
        calls.append(list(cmd))
        return _clean_main_run()(cmd, **k)

    monkeypatch.setattr(runner, "_run", spy)
    r = ReleaseRunner(_cfg(tmp_path, version="1.1.0", dry_run=True))
    r.run()
    assert not any(c[:2] == ["git", "tag"] for c in calls)
    assert not any(c[:2] == ["git", "push"] for c in calls)


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_build_parser_requires_version():
    parser = runner.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_config_from_args_defaults():
    parser = runner.build_parser()
    args = parser.parse_args(["--version", "9.9.9"])
    cfg = runner.config_from_args(args)
    assert cfg.version == "9.9.9"
    assert cfg.dry_run is False


def test_config_from_args_dry_run_and_out_dir(tmp_path):
    parser = runner.build_parser()
    args = parser.parse_args(
        ["--version", "9.9.9", "--dry-run", "--out-dir", str(tmp_path / "runs")]
    )
    cfg = runner.config_from_args(args)
    assert cfg.dry_run is True
    assert cfg.out_dir == tmp_path / "runs"


def test_print_plan_prints_phases(capsys):
    report = {
        "dry_run": True,
        "version": "9.9.9",
        "phases": [
            {
                "name": "R0-preflight",
                "status": "ok",
                "detail": "clean",
                "command": None,
            },
            {
                "name": "R1-bump",
                "status": "planned",
                "detail": "would bump",
                "command": "make release VERSION=9.9.9",
            },
        ],
        "tag_pushed": False,
        "published": None,
        "staged_tag_command": "git tag -a v9.9.9 ...",
    }
    runner.print_plan(report)
    out = capsys.readouterr().out
    assert "release plan" in out
    assert "R1-bump" in out
    assert "make release VERSION=9.9.9" in out
    assert "tag_pushed: False" in out


def test_main_aborts_on_wrong_branch_returns_2(capsys):
    """Real (unmocked) invocation against THIS actual worktree: whatever
    branch is currently checked out is (by construction of this test suite
    running mid-feature-branch-work) never `main`, so R0 aborts before any
    mutation is even attempted — a genuinely safe, read-only assertion."""
    rc = runner.main(["--version", "999.0.0", "--dry-run"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "release plan" in out


def test_main_dry_run_happy_path(monkeypatch, capsys):
    # Fake a clean `main` checkout; read_pyproject_version reads the REAL
    # (much lower) current version from this repo's actual pyproject.toml,
    # so a comfortably higher --version clears R0. Dry-run then stops before
    # any R1-R4 subprocess or file write.
    monkeypatch.setattr(runner, "_run", _clean_main_run())
    rc = runner.main(["--version", "999.0.0", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "release plan (dry-run=True)" in out
    assert "R4-push-record" in out
