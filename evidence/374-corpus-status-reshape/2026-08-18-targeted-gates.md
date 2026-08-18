# SPEC #374 RESHAPE — targeted gate evidence (2026-08-18)

Branch: fix/374-corpus-status-reshape (local, not pushed)
Spec: audittrace-private/specs/2026-08-18-SPEC-374-corpus-status-reshape.md

## Rule reshaped

corpus_status attaches iff summary.matched is non-empty (a query-matched
un-indexed doc is accessible), independent of the recall result.
BEHAVIOR CHANGE: empty recall + only UNRELATED pending -> now SILENT.

## Gate 1 — targeted tests

\`\`\`
Coverage XML written to file coverage.xml
FAIL Required test coverage of 90% not reached. Total coverage: 26.47%
================= 9 passed, 80 deselected, 1 warning in 2.59s ==================
```

Note: the "Required test coverage of 90%" line is an artefact of running a
9-test subset of the 89-test file (per-file coverage is measured over the
FULL suite). All 9 selected tests PASS; the full-suite coverage gate is
executed by the orchestrator's `make test` run, as scoped by the task.

## Gate 2 — lint (make lint: ruff check + ruff format + security-lint)

\`\`\`
🔍 Running linter...
All checks passed!
✅ Linting passed
📝 Running formatter check...
225 files already formatted
✅ Formatting passed
\`\`\`

## Gate 3 — mypy --strict on changed source files

\`\`\`
Success: no issues found in 2 source files
\`\`\`
