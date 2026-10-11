#!/usr/bin/env python3
"""Fail if pytest's junit.xml records any skipped tests.

Enforces the zero-skip policy added 2026-04-18: a skipped test is a
missing assertion in disguise. If a test depends on an environmental
resource, the runner provisions it (e.g. the GitHub Actions Postgres
service in .github/workflows/ci.yml). Otherwise the test is either
broken — fix it — or not needed — delete it.

Reads a junit XML file (default ``junit.xml`` at the repo root, written by
``scripts/ci/run-tests.sh``; pass a path to check a different one, e.g. a
subset run's own junit). Prints each skipped test with its skip reason so
a human can act on the message, then exits non-zero.

ADDENDUM A1-5 (2026-10-11, review REJECT of f72cdd7, N4): reads BOTH
signals — the per-testcase ``<skipped>`` child (what this script always
read) AND the ``<testsuite skipped="N">`` roll-up attribute the OLD CI
inline heredoc also summed. A junit file can in principle carry a
testsuite-level ``skipped`` count without a matching per-testcase
``<skipped>`` element (e.g. a pytest plugin or a hand-crafted/non-pytest
junit writer that only maintains the roll-up); reading only the per-
testcase shape would miss that. Either signal being non-zero fails.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    junit_path = (
        Path(argv[0]) if argv else Path(__file__).resolve().parent.parent / "junit.xml"
    )
    if not junit_path.exists():
        print(
            f"[no-skip-check] {junit_path} not found. "
            "Run pytest with --junit-xml=<path> first.",
            file=sys.stderr,
        )
        return 1
    root = ET.parse(junit_path).getroot()
    skipped: list[tuple[str, str]] = []
    for tc in root.iter("testcase"):
        for skip in tc.findall("skipped"):
            skipped.append(
                (
                    f"{tc.get('classname')}.{tc.get('name')}",
                    skip.get("message", "<no reason>"),
                )
            )
    # A1-5 / N4: the suite-level roll-up attribute, read independently of
    # the per-testcase scan above — a junit file can set one without the
    # other, and either being non-zero must fail.
    suite_level_skipped = 0
    for ts in root.iter("testsuite"):
        try:
            suite_level_skipped += int(ts.get("skipped", "0") or "0")
        except ValueError:
            suite_level_skipped += 0
    if skipped or suite_level_skipped:
        print(
            f"[no-skip-check] {len(skipped)} skipped testcase(s) "
            f"(testsuite roll-up reports {suite_level_skipped}) in "
            f"{junit_path} — skips are forbidden; either provision the "
            "dependency or remove the test.",
            file=sys.stderr,
        )
        for name, reason in skipped:
            print(f"  - {name}\n      {reason}", file=sys.stderr)
        if suite_level_skipped and not skipped:
            print(
                "  - (testsuite roll-up is non-zero but no per-testcase "
                "<skipped> element was found — failing closed on the "
                "roll-up alone)",
                file=sys.stderr,
            )
        return 1
    print(f"[no-skip-check] No skipped tests in {junit_path}. Good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
