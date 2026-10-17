"""The fast neuter harness (ratified SPEC, 2026-09-29, v3).

Public, in-repo SDLC tooling that lets a builder or reviewer prove every
guard in a Work Unit non-vacuous in minutes rather than hours: apply each
neuter (a small, exact-text edit that should turn a guard's test RED),
run only the mapped tests under real Postgres or the mock engine, read the
verdict from junit (never the summary line), and produce a durable,
resumable, per-guard report.

No default path, id, hash, or hostname lives in this package — product
neuter files, guard-test mappings, and evidence are always passed in by
path from the caller (see ``tests/test_neuter_no_private_content.py``).
"""

from __future__ import annotations
