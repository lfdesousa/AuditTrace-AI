#!/usr/bin/env python3
"""Fail if pytest's junit.xml records any skipped tests.

Enforces the zero-skip policy added 2026-04-18: a skipped test is a
missing assertion in disguise. If a test depends on an environmental
resource, the runner provisions it (e.g. the GitHub Actions Postgres
service in .github/workflows/ci.yml). Otherwise the test is either
broken — fix it — or not needed — delete it.

Reads ``junit.xml`` at the repo root (written by ``make test`` and by
the CI step). Prints each skipped test with its skip reason so a
human can act on the message, then exits non-zero.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main() -> int:
    junit_path = Path(__file__).resolve().parent.parent / "junit.xml"
    if not junit_path.exists():
        print(
            f"[no-skip-check] junit.xml not found at {junit_path}. "
            "Run pytest with --junit-xml=junit.xml first.",
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
    if skipped:
        print(
            f"[no-skip-check] {len(skipped)} skipped test(s) — "
            "skips are forbidden; either provision the dependency or "
            "remove the test.",
            file=sys.stderr,
        )
        for name, reason in skipped:
            print(f"  - {name}\n      {reason}", file=sys.stderr)
        return 1
    print(f"[no-skip-check] No skipped tests in {junit_path.name}. Good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
