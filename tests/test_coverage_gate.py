"""Tests for the per-file coverage gate itself (#366).

The gate decides whether every other quality claim in this repo is
trustworthy, so it needs to be trustworthy first. Until 2026-07-20 it read
only Cobertura's ``line-rate`` and never looked at branches — a file could
sit at 90% lines and 66% branches and pass silently. The audit that found
this measured 20 of 59 files below 90% branch coverage with 167 uncovered
decision paths, none of which the gate had ever reported.

These tests pin the behaviour that matters:

  * branch failures are DETECTED (the whole point of the change);
  * line failures are still detected (no regression);
  * files with no branch constructs are not punished for a meaningless
    0.0 branch-rate;
  * the exemption list actually exempts;
  * the branch COUNTS in the report are arithmetically right, because
    "12 branches to cover" is what makes a failure actionable.

A gate that cannot fail is decoration, so several tests here assert the
failure path explicitly rather than only the happy one.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_GATE_PATH = Path(__file__).parent.parent / "scripts" / "check-per-file-coverage.py"


def _load_gate():
    """Import the gate script by path — it is a script, not a package module."""
    spec = importlib.util.spec_from_file_location("coverage_gate", _GATE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coverage_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()


def _file(
    filename: str,
    line_rate: float,
    branch_rate: float | None = None,
    *,
    branch_lines: list[tuple[int, int]] | None = None,
    plain_lines: int = 20,
) -> dict:
    """Build one ``files`` entry of coverage.py's JSON report.

    ``line_rate`` is a fraction of ``plain_lines`` statements covered;
    ``branch_lines`` is a list of (covered, total) branch outcomes, one pair
    per branching statement. ``branch_rate`` is accepted for call-site
    readability: the gate derives the rate from the counts, as the report does.
    """
    del branch_rate
    covered = round(line_rate * plain_lines)
    summary = {"num_statements": plain_lines, "covered_lines": covered}
    if branch_lines:
        summary["num_branches"] = sum(t for _, t in branch_lines)
        summary["covered_branches"] = sum(c for c, _ in branch_lines)
    return {filename: {"summary": summary}}


def _empty(filename: str) -> dict:
    return {filename: {"summary": {"num_statements": 0, "covered_lines": 0}}}


@pytest.fixture
def write_xml(tmp_path):
    """Write a coverage.json from ``_file`` entries (name kept for the tests)."""

    def _write(*entries: dict) -> Path:
        merged: dict = {}
        for entry in entries:
            merged.update(entry)
        p = tmp_path / "coverage.json"
        p.write_text(json.dumps({"files": merged}), encoding="utf-8")
        return p

    return _write


@pytest.fixture
def write_data(tmp_path):
    """Write a raw ``.coverage`` data file that measured exactly ``names``."""
    from coverage import CoverageData

    def _write(*names: str) -> Path:
        path = tmp_path / ".coverage"
        data = CoverageData(basename=str(path))
        data.add_lines({n: [1, 2] for n in names})
        data.write()
        return path

    return _write


class TestBranchDetection:
    """The behaviour #366 added — and the reason the gate changed."""

    def test_high_lines_low_branches_now_fails(self, write_xml) -> None:
        """The exact shape that used to pass silently.

        90% lines with 66% branches was the real state of
        ``db/postgres.py``. Before #366 the gate reported PASS on it.
        """
        p = write_xml(
            _file("app/thing.py", 0.95, 0.6667, branch_lines=[(1, 2), (2, 2), (2, 2)])
        )
        files = gate.collect(p)
        assert len(files) == 1
        assert files[0].fails(line_threshold=90.0, branch_threshold=90.0) is True

    def test_high_lines_high_branches_passes(self, write_xml) -> None:
        p = write_xml(
            _file("app/thing.py", 0.95, 0.95, branch_lines=[(2, 2), (2, 2), (2, 2)])
        )
        assert gate.collect(p)[0].fails(90.0, 90.0) is False

    def test_line_failure_still_detected(self, write_xml) -> None:
        """No regression: the original line gate must still bite."""
        p = write_xml(_file("app/thing.py", 0.50, 1.0, branch_lines=[(2, 2)]))
        assert gate.collect(p)[0].fails(90.0, 90.0) is True

    def test_thresholds_are_independent(self, write_xml) -> None:
        """A relaxed branch bar must not relax the line bar, or vice versa."""
        p = write_xml(_file("app/thing.py", 0.95, 0.70, branch_lines=[(1, 2), (2, 2)]))
        f = gate.collect(p)[0]
        assert f.fails(90.0, 90.0) is True  # branch too low
        assert f.fails(90.0, 60.0) is False  # branch bar lowered -> passes
        assert f.fails(99.0, 60.0) is True  # line bar raised -> fails again


class TestBranchlessFiles:
    """A file with no `if` must not be failed for a meaningless 0.0 rate."""

    def test_no_branch_constructs_is_not_a_branch_failure(self, write_xml) -> None:
        # Cobertura reports branch-rate="0.0" for a file with no branches.
        # Gating on that would fail every constants/model module in the tree.
        p = write_xml(_file("app/constants.py", 1.0, 0.0, branch_lines=None))
        f = gate.collect(p)[0]
        assert f.branch_rate is None, "branchless file must record branch_rate=None"
        assert f.fails(90.0, 90.0) is False

    def test_branchless_file_still_gated_on_lines(self, write_xml) -> None:
        """Exempting branches must not accidentally exempt lines too."""
        p = write_xml(_file("app/constants.py", 0.10, 0.0, branch_lines=None))
        assert gate.collect(p)[0].fails(90.0, 90.0) is True


class TestBranchCounting:
    """The 'Missing' column has to be arithmetically right to be actionable."""

    def test_counts_uncovered_branch_outcomes(self, write_xml) -> None:
        # 1/2 + 0/2 + 2/2  ->  6 total outcomes, 3 uncovered
        p = write_xml(
            _file("app/thing.py", 0.95, 0.5, branch_lines=[(1, 2), (0, 2), (2, 2)])
        )
        f = gate.collect(p)[0]
        assert f.n_branches == 6
        assert f.missing_branches == 3

    def test_fully_covered_branches_report_zero_missing(self, write_xml) -> None:
        p = write_xml(_file("app/thing.py", 1.0, 1.0, branch_lines=[(2, 2), (2, 2)]))
        assert gate.collect(p)[0].missing_branches == 0


class TestExemptions:
    def test_migrations_are_exempt(self, write_xml) -> None:
        p = write_xml(_file("app/migrations/005_x.py", 0.1, 0.1, branch_lines=[(0, 2)]))
        assert gate.collect(p) == []

    def test_dunder_init_is_exempt(self, write_xml) -> None:
        p = write_xml(_file("app/__init__.py", 0.1, 0.1, branch_lines=[(0, 2)]))
        assert gate.collect(p) == []

    def test_empty_files_are_skipped(self, write_xml) -> None:
        """0 measurable lines means nothing to say — not a 0% failure."""
        p = write_xml(_empty("app/empty.py"))
        assert gate.collect(p) == []

    def test_non_exempt_file_is_collected(self, write_xml) -> None:
        """Guard against an over-broad exemption silently hiding real files."""
        p = write_xml(_file("app/real.py", 0.95, 0.95, branch_lines=[(2, 2)]))
        assert [f.filename for f in gate.collect(p)] == ["app/real.py"]


class TestExitCodes:
    """The gate's contract with `make test` and CI is its exit code."""

    def test_missing_report_returns_2(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(
            sys, "argv", ["gate", "--json", str(tmp_path / "absent.json")]
        )
        assert gate.main() == 2

    def test_missing_data_file_returns_2(
        self, monkeypatch, write_xml, tmp_path
    ) -> None:
        """The completeness guard cannot run without the raw data: fail closed."""
        p = write_xml(_file("app/thing.py", 0.99, 0.99, branch_lines=[(2, 2)]))
        monkeypatch.setattr(
            sys,
            "argv",
            ["gate", "--json", str(p), "--data-file", str(tmp_path / "absent")],
        )
        assert gate.main() == 2

    def test_clean_report_returns_0(self, monkeypatch, write_xml, write_data) -> None:
        p = write_xml(_file("app/thing.py", 0.99, 0.99, branch_lines=[(2, 2)]))
        d = write_data("app/thing.py")
        monkeypatch.setattr(
            sys, "argv", ["gate", "--json", str(p), "--data-file", str(d)]
        )
        assert gate.main() == 0

    def test_branch_offender_returns_1(
        self, monkeypatch, write_xml, write_data
    ) -> None:
        """A branch-only failure must be a non-zero exit, or CI ignores it."""
        p = write_xml(_file("app/thing.py", 0.99, 0.50, branch_lines=[(1, 2)]))
        d = write_data("app/thing.py")
        monkeypatch.setattr(
            sys, "argv", ["gate", "--json", str(p), "--data-file", str(d)]
        )
        assert gate.main() == 1

    def test_custom_branch_threshold_is_honoured(
        self, monkeypatch, write_xml, write_data
    ) -> None:
        p = write_xml(_file("app/thing.py", 0.99, 0.80, branch_lines=[(4, 5)]))
        d = write_data("app/thing.py")
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "gate",
                "--json",
                str(p),
                "--data-file",
                str(d),
                "--branch-threshold",
                "75",
            ],
        )
        assert gate.main() == 0

    def test_failure_report_names_the_failing_dimension(
        self, monkeypatch, write_xml, write_data, capsys
    ) -> None:
        """[L]/[B] markers tell the reader WHICH bar was missed."""
        p = write_xml(_file("app/thing.py", 0.99, 0.50, branch_lines=[(1, 2)]))
        d = write_data("app/thing.py")
        monkeypatch.setattr(
            sys, "argv", ["gate", "--json", str(p), "--data-file", str(d)]
        )
        gate.main()
        err = capsys.readouterr().err
        assert "[B]" in err
        assert "app/thing.py" in err


class TestCompleteness:
    """LC-9 gate fix C4: the gated set is complete and keyed by full path.

    coverage.py's Cobertura writer keyed files by a source-root-shortened bare
    name, so same-named files in different directories overwrote each other
    and the gated set depended on PYTHONHASHSEED (190 vs 193 files at one
    commit). The JSON report keeps full paths; this guard proves the report
    still carries every measured file.
    """

    def test_same_basename_in_two_directories_are_both_gated(self, write_xml) -> None:
        p = write_xml(
            _file("scripts/deploy/memory.py", 0.99, 0.99, branch_lines=[(2, 2)]),
            _file("scripts/release/memory.py", 0.10, 0.10, branch_lines=[(0, 2)]),
        )
        assert sorted(f.filename for f in gate.collect(p)) == [
            "scripts/deploy/memory.py",
            "scripts/release/memory.py",
        ]

    def test_missing_from_report_names_the_dropped_file(self) -> None:
        assert gate.missing_from_report(
            ["scripts/deploy/memory.py", "scripts/release/memory.py"],
            ["memory.py"],
        ) == ["scripts/deploy/memory.py", "scripts/release/memory.py"]

    def test_complete_report_has_nothing_missing(self) -> None:
        names = ["a/x.py", "b/x.py"]
        assert gate.missing_from_report(names, names + ["c/unexecuted.py"]) == []

    def test_a_collapsed_report_fails_closed_with_exit_3(
        self, monkeypatch, write_xml, write_data, capsys
    ) -> None:
        """The report carries a bare-name key (the C4 defect) for two measured
        files: the gate must refuse, not shrink the gated set silently."""
        p = write_xml(_file("memory.py", 0.99, 0.99, branch_lines=[(2, 2)]))
        d = write_data("scripts/deploy/memory.py", "scripts/release/memory.py")
        monkeypatch.setattr(
            sys, "argv", ["gate", "--json", str(p), "--data-file", str(d)]
        )
        assert gate.main() == 3
        err = capsys.readouterr().err
        assert "completeness guard" in err
        assert "scripts/deploy/memory.py" in err

    def test_list_files_writes_the_sorted_gated_set(
        self, monkeypatch, write_xml, write_data, tmp_path
    ) -> None:
        p = write_xml(
            _file("z/b.py", 0.99, 0.99, branch_lines=[(2, 2)]),
            _file("a/b.py", 0.99, 0.99, branch_lines=[(2, 2)]),
            _file("a/__init__.py", 0.1, 0.1, branch_lines=[(0, 2)]),
        )
        d = write_data("z/b.py", "a/b.py", "a/__init__.py")
        out = tmp_path / "gated.txt"
        monkeypatch.setattr(
            sys,
            "argv",
            ["gate", "--json", str(p), "--data-file", str(d), "--list-files", str(out)],
        )
        assert gate.main() == 0
        assert out.read_text().splitlines() == ["a/b.py", "z/b.py"]
