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


def _xml(classes: str) -> str:
    return f'<?xml version="1.0" ?><coverage><packages><package>{classes}</package></packages></coverage>'


def _cls(
    filename: str,
    line_rate: float,
    branch_rate: float | None = None,
    *,
    branch_lines: list[tuple[int, int]] | None = None,
    plain_lines: int = 3,
) -> str:
    """Build one <class> element.

    ``branch_lines`` is a list of (covered, total) condition fractions —
    the shape Cobertura uses for a branching line.
    """
    lines = "".join(f'<line number="{i}" hits="1"/>' for i in range(1, plain_lines + 1))
    n = plain_lines
    for covered, total in branch_lines or []:
        n += 1
        pct = int(covered / total * 100)
        lines += (
            f'<line number="{n}" hits="1" branch="true" '
            f'condition-coverage="{pct}% ({covered}/{total})"/>'
        )
    br_attr = "" if branch_rate is None else f' branch-rate="{branch_rate}"'
    return (
        f'<class filename="{filename}" line-rate="{line_rate}"{br_attr}>'
        f"<lines>{lines}</lines></class>"
    )


@pytest.fixture
def write_xml(tmp_path):
    def _write(body: str) -> Path:
        p = tmp_path / "coverage.xml"
        p.write_text(_xml(body), encoding="utf-8")
        return p

    return _write


class TestBranchDetection:
    """The behaviour #366 added — and the reason the gate changed."""

    def test_high_lines_low_branches_now_fails(self, write_xml) -> None:
        """The exact shape that used to pass silently.

        90% lines with 66% branches was the real state of
        ``db/postgres.py``. Before #366 the gate reported PASS on it.
        """
        p = write_xml(
            _cls("app/thing.py", 0.95, 0.6667, branch_lines=[(1, 2), (2, 2), (2, 2)])
        )
        files = gate.collect(p)
        assert len(files) == 1
        assert files[0].fails(line_threshold=90.0, branch_threshold=90.0) is True

    def test_high_lines_high_branches_passes(self, write_xml) -> None:
        p = write_xml(
            _cls("app/thing.py", 0.95, 0.95, branch_lines=[(2, 2), (2, 2), (2, 2)])
        )
        assert gate.collect(p)[0].fails(90.0, 90.0) is False

    def test_line_failure_still_detected(self, write_xml) -> None:
        """No regression: the original line gate must still bite."""
        p = write_xml(_cls("app/thing.py", 0.50, 1.0, branch_lines=[(2, 2)]))
        assert gate.collect(p)[0].fails(90.0, 90.0) is True

    def test_thresholds_are_independent(self, write_xml) -> None:
        """A relaxed branch bar must not relax the line bar, or vice versa."""
        p = write_xml(_cls("app/thing.py", 0.95, 0.70, branch_lines=[(1, 2), (2, 2)]))
        f = gate.collect(p)[0]
        assert f.fails(90.0, 90.0) is True  # branch too low
        assert f.fails(90.0, 60.0) is False  # branch bar lowered -> passes
        assert f.fails(99.0, 60.0) is True  # line bar raised -> fails again


class TestBranchlessFiles:
    """A file with no `if` must not be failed for a meaningless 0.0 rate."""

    def test_no_branch_constructs_is_not_a_branch_failure(self, write_xml) -> None:
        # Cobertura reports branch-rate="0.0" for a file with no branches.
        # Gating on that would fail every constants/model module in the tree.
        p = write_xml(_cls("app/constants.py", 1.0, 0.0, branch_lines=None))
        f = gate.collect(p)[0]
        assert f.branch_rate is None, "branchless file must record branch_rate=None"
        assert f.fails(90.0, 90.0) is False

    def test_branchless_file_still_gated_on_lines(self, write_xml) -> None:
        """Exempting branches must not accidentally exempt lines too."""
        p = write_xml(_cls("app/constants.py", 0.10, 0.0, branch_lines=None))
        assert gate.collect(p)[0].fails(90.0, 90.0) is True


class TestBranchCounting:
    """The 'Missing' column has to be arithmetically right to be actionable."""

    def test_counts_uncovered_branch_outcomes(self, write_xml) -> None:
        # 1/2 + 0/2 + 2/2  ->  6 total outcomes, 3 uncovered
        p = write_xml(
            _cls("app/thing.py", 0.95, 0.5, branch_lines=[(1, 2), (0, 2), (2, 2)])
        )
        f = gate.collect(p)[0]
        assert f.n_branches == 6
        assert f.missing_branches == 3

    def test_fully_covered_branches_report_zero_missing(self, write_xml) -> None:
        p = write_xml(_cls("app/thing.py", 1.0, 1.0, branch_lines=[(2, 2), (2, 2)]))
        assert gate.collect(p)[0].missing_branches == 0


class TestExemptions:
    def test_migrations_are_exempt(self, write_xml) -> None:
        p = write_xml(_cls("app/migrations/005_x.py", 0.1, 0.1, branch_lines=[(0, 2)]))
        assert gate.collect(p) == []

    def test_dunder_init_is_exempt(self, write_xml) -> None:
        p = write_xml(_cls("app/__init__.py", 0.1, 0.1, branch_lines=[(0, 2)]))
        assert gate.collect(p) == []

    def test_empty_files_are_skipped(self, write_xml) -> None:
        """0 measurable lines means nothing to say — not a 0% failure."""
        p = write_xml('<class filename="app/empty.py" line-rate="0.0"><lines/></class>')
        assert gate.collect(p) == []

    def test_non_exempt_file_is_collected(self, write_xml) -> None:
        """Guard against an over-broad exemption silently hiding real files."""
        p = write_xml(_cls("app/real.py", 0.95, 0.95, branch_lines=[(2, 2)]))
        assert [f.filename for f in gate.collect(p)] == ["app/real.py"]


class TestExitCodes:
    """The gate's contract with `make test` and CI is its exit code."""

    def test_missing_xml_returns_2(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(
            sys, "argv", ["gate", "--xml", str(tmp_path / "absent.xml")]
        )
        assert gate.main() == 2

    def test_clean_report_returns_0(self, monkeypatch, write_xml) -> None:
        p = write_xml(_cls("app/thing.py", 0.99, 0.99, branch_lines=[(2, 2)]))
        monkeypatch.setattr(sys, "argv", ["gate", "--xml", str(p)])
        assert gate.main() == 0

    def test_branch_offender_returns_1(self, monkeypatch, write_xml) -> None:
        """A branch-only failure must be a non-zero exit, or CI ignores it."""
        p = write_xml(_cls("app/thing.py", 0.99, 0.50, branch_lines=[(1, 2)]))
        monkeypatch.setattr(sys, "argv", ["gate", "--xml", str(p)])
        assert gate.main() == 1

    def test_custom_branch_threshold_is_honoured(self, monkeypatch, write_xml) -> None:
        p = write_xml(_cls("app/thing.py", 0.99, 0.80, branch_lines=[(4, 5)]))
        monkeypatch.setattr(
            sys, "argv", ["gate", "--xml", str(p), "--branch-threshold", "75"]
        )
        assert gate.main() == 0

    def test_failure_report_names_the_failing_dimension(
        self, monkeypatch, write_xml, capsys
    ) -> None:
        """[L]/[B] markers tell the reader WHICH bar was missed."""
        p = write_xml(_cls("app/thing.py", 0.99, 0.50, branch_lines=[(1, 2)]))
        monkeypatch.setattr(sys, "argv", ["gate", "--xml", str(p)])
        gate.main()
        err = capsys.readouterr().err
        assert "[B]" in err
        assert "app/thing.py" in err
