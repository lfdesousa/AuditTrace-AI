#!/usr/bin/env python3
"""Per-file coverage gate — lines AND branches.

Reads coverage.json (coverage.py's JSON report, produced by pytest-cov) and
fails if ANY non-trivial source file is below the configured per-file
thresholds.

**Why JSON and not coverage.xml (LC-9 gate fix C4, 2026-10-28).** coverage.py
7.16.2's Cobertura writer (``xmlreport.py``) shortens every filename against
the configured source roots and keys the class elements by the SHORTENED name.
Files that shorten to the same bare name (``memory.py`` in scripts/deploy and
scripts/release, ``runner.py`` in scripts/curator, scripts/neuter and
scripts/release, several ``__init__.py``) overwrite each other, and because the
source roots are iterated in hash-randomised order the set of files the gate
saw changed with ``PYTHONHASHSEED`` (190 vs 193 gated files at one commit,
measured). The JSON report keys every file by its full repo-relative path, so
two files can never collapse.

**The completeness guard.** The gate also reads the raw measurement data
(``.coverage``) and refuses to pass unless EVERY measured file appears in the
report under its own full path. A report that dropped, merged or renamed a
measured file therefore fails closed (exit 3) instead of silently shrinking
the gated set.

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
  - Files with no branch constructs at all (the branch rate is meaningless
    there)

Usage:
    python scripts/check-per-file-coverage.py
    python scripts/check-per-file-coverage.py --threshold 95
    python scripts/check-per-file-coverage.py --branch-threshold 80
    python scripts/check-per-file-coverage.py --json /path/to/coverage.json
    python scripts/check-per-file-coverage.py --list-files gated-files.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_THRESHOLD = 90.0
DEFAULT_BRANCH_THRESHOLD = 90.0
DEFAULT_JSON = "coverage.json"
DEFAULT_DATA = ".coverage"

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


def _load_report(json_path: Path) -> dict[str, dict]:
    """Return the report's ``files`` mapping (full repo-relative path -> record)."""
    files = json.loads(json_path.read_text(encoding="utf-8")).get("files", {})
    return dict(files)


def collect(json_path: Path) -> list[FileCoverage]:
    """Parse coverage.json into per-file records, skipping exempt entries."""
    out: list[FileCoverage] = []

    for filename, record in sorted(_load_report(json_path).items()):
        if _is_exempt(filename):
            continue
        summary = record["summary"]
        n_lines = int(summary["num_statements"])
        if n_lines == 0:
            continue

        n_branches = int(summary.get("num_branches", 0))
        covered_branches = int(summary.get("covered_branches", 0))
        # A file with no branch constructs has a meaningless branch rate.
        # Gating on it would fail every constant module in the tree.
        branch_rate: float | None = None
        if n_branches:
            branch_rate = covered_branches / n_branches * 100.0

        out.append(
            FileCoverage(
                filename=filename,
                line_rate=int(summary["covered_lines"]) / n_lines * 100.0,
                branch_rate=branch_rate,
                n_lines=n_lines,
                n_branches=n_branches,
                missing_branches=n_branches - covered_branches,
            )
        )
    return out


def measured_files(data_path: Path) -> set[str]:
    """Every file coverage.py recorded executing, as a repo-relative path.

    Read from the raw ``.coverage`` data, an instrument independent of the
    report writer whose keying this guard polices.
    """
    from coverage import CoverageData

    data = CoverageData(basename=str(data_path))
    data.read()
    return {os.path.relpath(f) for f in data.measured_files()}


def missing_from_report(measured: Iterable[str], reported: Iterable[str]) -> list[str]:
    """Measured files the report does not carry under their own full path."""
    return sorted(set(measured) - set(reported))


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
        "--json",
        type=Path,
        default=Path(DEFAULT_JSON),
        help=f"Path to coverage.json (default: {DEFAULT_JSON})",
    )
    parser.add_argument(
        "--data-file",
        type=Path,
        default=Path(DEFAULT_DATA),
        help=f"Raw coverage data for the completeness guard (default: {DEFAULT_DATA})",
    )
    parser.add_argument(
        "--list-files",
        type=Path,
        default=None,
        help="Write the sorted list of GATED files to this path (determinism proof)",
    )
    args = parser.parse_args()

    for required in (args.json, args.data_file):
        if not required.exists():
            print(
                f"error: {required} not found. Run `make test` first.", file=sys.stderr
            )
            return 2

    measured = measured_files(args.data_file)
    measured_count = len(measured)
    dropped = missing_from_report(measured, _load_report(args.json))
    if dropped:
        print(
            f"per-file coverage gate: FAIL (completeness guard: {len(dropped)} "
            "measured file(s) are missing from the report under their own path, "
            "so they would not be gated)",
            file=sys.stderr,
        )
        for name in dropped:
            print(f"  {name}", file=sys.stderr)
        return 3

    files = collect(args.json)
    if args.list_files is not None:
        args.list_files.write_text(
            "".join(f"{f.filename}\n" for f in files), encoding="utf-8"
        )
    offenders = [f for f in files if f.fails(args.threshold, args.branch_threshold)]

    if not offenders:
        with_branches = sum(1 for f in files if f.branch_rate is not None)
        print(
            f"per-file coverage gate: PASS ({len(files)} files checked, "
            f"{measured_count} measured files all reported, "
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
