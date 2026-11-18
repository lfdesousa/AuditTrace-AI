#!/usr/bin/env python3
"""D6 gate: a parallel run must not silently run fewer tests than are collected.

``scripts/ci/run-tests.sh`` collects the test ids once, serially
(``pytest --collect-only -q``) and hands that listing plus the run's
``junit.xml`` to this script. It fails closed when

* the collection holds no test id (a gate that ran on zero items is a failure),
* ``junit.xml`` is missing or holds no testcase, or
* the number of junit testcases differs from the number of collected ids.

On a green run (the only run that reaches this check) every collected id yields
exactly one junit testcase, serial or under pytest-xdist, so equality is the
invariant. The counts are printed either way, so a log states how much ran.
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# A ``pytest --collect-only -q`` node id line: ``<file>.py::<name>[...]``.
_ID_LINE = re.compile(r"^[^\s:]+\.py::")


def count_collected(listing: str) -> int:
    """Number of node-id lines in a ``--collect-only -q`` listing."""
    return sum(1 for line in listing.splitlines() if _ID_LINE.match(line))


def count_junit_testcases(junit_path: Path) -> int:
    """Number of ``<testcase>`` elements in a junit file."""
    return sum(1 for _ in ET.parse(junit_path).getroot().iter("testcase"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collected_ids", type=Path)
    parser.add_argument("junit", type=Path)
    parser.add_argument("--workers", default="?", help="label only, for the log")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    for path in (args.collected_ids, args.junit):
        if not path.exists():
            print(f"[collected-vs-junit] {path} not found.", file=sys.stderr)
            return 1

    collected = count_collected(args.collected_ids.read_text())
    ran = count_junit_testcases(args.junit)
    print(
        f"[collected-vs-junit] collected={collected} junit_testcases={ran} "
        f"workers={args.workers}"
    )
    if collected == 0:
        print("[collected-vs-junit] no test ids were collected.", file=sys.stderr)
        return 1
    if ran != collected:
        print(
            f"[collected-vs-junit] collected {collected} ids but junit holds "
            f"{ran} testcases: the run did not execute what was collected.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
