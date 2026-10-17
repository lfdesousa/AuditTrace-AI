#!/usr/bin/env python3
"""Per-file coverage gate — lines AND branches.

Reads coverage.xml (Cobertura format produced by pytest-cov) and fails if
ANY non-trivial source file is below the configured per-file thresholds.

This complements pytest-cov's --cov-fail-under, which only checks the
TOTAL average and lets individual files rot below the gate as long as the
project-wide number stays high. The user wants senior-level discipline:
each component needs to stand on its own.

**Why branches are gated too (#366, 2026-07-20).** Until now the gate read
only Cobertura's ``line-rate``. That let a file sit at 90% lines and 66%
branches and pass silently — and branches are where the decision paths
live: ``if row is None``, ``if not authorized``, ``elif summarised is
False``. A gate that cannot see untested decision paths is under-powered
for a system whose whole claim is evidentiary rigour. The audit that found
this measured 20 of 59 files below 90% branch coverage, with 167 uncovered
branches, none of which the gate had ever mentioned.

Both rates are always reported, so the numbers stay visible even when they
pass.

Skipped from the gate:
  - Empty files (0 statements)
  - Migrations (alembic generated, not worth testing)
  - __init__.py files (usually empty re-exports)
  - Files with no branch constructs at all (branch-rate is meaningless
    there; Cobertura reports 0.0 or omits the attribute)

Usage:
    python scripts/check-per-file-coverage.py
    python scripts/check-per-file-coverage.py --threshold 95
    python scripts/check-per-file-coverage.py --branch-threshold 80
    python scripts/check-per-file-coverage.py --xml /path/to/coverage.xml
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

DEFAULT_THRESHOLD = 90.0
DEFAULT_BRANCH_THRESHOLD = 90.0
DEFAULT_XML = "coverage.xml"

# Path fragments that are exempt from the gate (matched as substrings).
EXEMPT_FRAGMENTS = (
    "/migrations/",
    "/__init__.py",
)


@dataclass(frozen=True)
class FileCoverage:
    """One source file's measured coverage."""

    filename: str
    line_rate: float
    branch_rate: float | None  # None when the file has no branches
    n_lines: int
    n_branches: int
    missing_branches: int

    def fails(self, line_threshold: float, branch_threshold: float) -> bool:
        if self.line_rate < line_threshold:
            return True
        if self.branch_rate is not None and self.branch_rate < branch_threshold:
            return True
        return False


def _is_exempt(filename: str) -> bool:
    return any(frag in filename for frag in EXEMPT_FRAGMENTS)


def _branch_counts(lines: ET.Element) -> tuple[int, int]:
    """Return (total_branches, missing_branches) for a <lines> element.

    Cobertura encodes per-line branch data as ``condition-coverage="50% (1/2)"``.
    Summing the parenthesised fractions gives exact counts, which is more
    useful in the failure report than a bare percentage: "12 branches to
    cover" is actionable, "68.42%" is not.
    """
    total = missing = 0
    for line in lines.iter("line"):
        if line.get("branch") != "true":
            continue
        cond = line.get("condition-coverage", "")
        if "(" not in cond:
            continue
        covered_str, total_str = cond.split("(")[1].rstrip(")").split("/")
        total += int(total_str)
        missing += int(total_str) - int(covered_str)
    return total, missing


def collect(xml_path: Path) -> list[FileCoverage]:
    """Parse coverage.xml into per-file records, skipping exempt entries."""
    root = ET.parse(xml_path).getroot()
    out: list[FileCoverage] = []

    for cls in root.iter("class"):
        filename = cls.get("filename", "")
        if not filename or _is_exempt(filename):
            continue

        lines = cls.find("lines")
        if lines is None:
            continue
        n_lines = sum(1 for _ in lines.iter("line"))
        if n_lines == 0:
            continue

        n_branches, missing = _branch_counts(lines)
        # A file with no branch constructs has a meaningless branch-rate
        # (Cobertura reports 0.0). Gating on it would fail every constant
        # module in the tree.
        branch_rate: float | None = None
        if n_branches:
            branch_rate = float(cls.get("branch-rate", "0.0")) * 100.0

        out.append(
            FileCoverage(
                filename=filename,
                line_rate=float(cls.get("line-rate", "0.0")) * 100.0,
                branch_rate=branch_rate,
                n_lines=n_lines,
                n_branches=n_branches,
                missing_branches=missing,
            )
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Per-file LINE coverage percentage (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--branch-threshold",
        type=float,
        default=DEFAULT_BRANCH_THRESHOLD,
        help=(
            f"Per-file BRANCH coverage percentage (default: {DEFAULT_BRANCH_THRESHOLD})"
        ),
    )
    parser.add_argument(
        "--xml",
        type=Path,
        default=Path(DEFAULT_XML),
        help=f"Path to coverage.xml (default: {DEFAULT_XML})",
    )
    args = parser.parse_args()

    if not args.xml.exists():
        print(f"error: {args.xml} not found. Run `make test` first.", file=sys.stderr)
        return 2

    files = collect(args.xml)
    offenders = [f for f in files if f.fails(args.threshold, args.branch_threshold)]

    if not offenders:
        with_branches = sum(1 for f in files if f.branch_rate is not None)
        print(
            f"per-file coverage gate: PASS ({len(files)} files checked, "
            f"lines >= {args.threshold:.0f}%, "
            f"branches >= {args.branch_threshold:.0f}% "
            f"on {with_branches} file(s) with branches)"
        )
        return 0

    print(
        f"per-file coverage gate: FAIL "
        f"({len(offenders)}/{len(files)} file(s) below threshold)",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    print(
        f"  {'File':<52} {'Lines':>8} {'Branch':>8} {'Missing':>8}",
        file=sys.stderr,
    )
    print(f"  {'-' * 52} {'-' * 8} {'-' * 8} {'-' * 8}", file=sys.stderr)
    for f in sorted(offenders, key=lambda x: (x.branch_rate or 100.0, x.line_rate)):
        branch = "  n/a  " if f.branch_rate is None else f"{f.branch_rate:>7.2f}%"
        # Flag which dimension actually failed so the fix is unambiguous.
        marks = []
        if f.line_rate < args.threshold:
            marks.append("L")
        if f.branch_rate is not None and f.branch_rate < args.branch_threshold:
            marks.append("B")
        print(
            f"  {f.filename:<52} {f.line_rate:>7.2f}% {branch} "
            f"{f.missing_branches:>8} [{'+'.join(marks)}]",
            file=sys.stderr,
        )
    print(file=sys.stderr)
    print(
        "  [L] line coverage below threshold   [B] branch coverage below threshold",
        file=sys.stderr,
    )
    print(
        "  'Missing' counts uncovered branch outcomes — that many "
        "decision paths are untested.",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    print(
        f"Each component must stand on its own at {args.threshold:.0f}% lines "
        f"and {args.branch_threshold:.0f}% branches. "
        "Add tests or document the exemption.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
