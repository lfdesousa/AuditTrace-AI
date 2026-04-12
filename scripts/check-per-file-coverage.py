#!/usr/bin/env python3
"""Per-file coverage gate.

Reads coverage.xml (Cobertura format produced by pytest-cov) and fails if
ANY non-trivial source file is below the configured per-file threshold.

This complements pytest-cov's --cov-fail-under, which only checks the
TOTAL average and lets individual files rot below the gate as long as the
project-wide number stays high. The user wants senior-level discipline:
each component needs to stand on its own.

Skipped from the gate:
  - Empty files (0 statements)
  - Migrations (alembic generated, not worth testing)
  - __init__.py files (usually empty re-exports)

Usage:
    python scripts/check-per-file-coverage.py
    python scripts/check-per-file-coverage.py --threshold 95
    python scripts/check-per-file-coverage.py --xml /path/to/coverage.xml
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_THRESHOLD = 90.0
DEFAULT_XML = "coverage.xml"

# Path fragments that are exempt from the gate (matched as substrings).
EXEMPT_FRAGMENTS = (
    "/migrations/",
    "/__init__.py",
)


def _is_exempt(filename: str) -> bool:
    return any(frag in filename for frag in EXEMPT_FRAGMENTS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Per-file coverage percentage (default: {DEFAULT_THRESHOLD})",
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

    tree = ET.parse(args.xml)
    root = tree.getroot()

    offenders: list[tuple[str, float, int]] = []
    checked = 0

    for cls in root.iter("class"):
        filename = cls.get("filename", "")
        if not filename or _is_exempt(filename):
            continue

        # Skip files with no measurable lines
        lines = cls.find("lines")
        if lines is None:
            continue
        n_lines = sum(1 for _ in lines.iter("line"))
        if n_lines == 0:
            continue

        # Cobertura reports line-rate as a 0..1 decimal
        rate = float(cls.get("line-rate", "0.0")) * 100.0
        checked += 1
        if rate < args.threshold:
            offenders.append((filename, rate, n_lines))

    if not offenders:
        print(
            f"per-file coverage gate: PASS "
            f"({checked} files checked, all >= {args.threshold:.0f}%)"
        )
        return 0

    print(
        f"per-file coverage gate: FAIL "
        f"({len(offenders)}/{checked} file(s) below {args.threshold:.0f}%)",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    print(f"  {'File':<60} {'Coverage':>10} {'Lines':>8}", file=sys.stderr)
    print(f"  {'-' * 60} {'-' * 10} {'-' * 8}", file=sys.stderr)
    for filename, rate, n_lines in sorted(offenders, key=lambda x: x[1]):
        print(f"  {filename:<60} {rate:>9.2f}% {n_lines:>8}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        "Each component must stand on its own at "
        f"{args.threshold:.0f}%+. Add tests or document the exemption.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
