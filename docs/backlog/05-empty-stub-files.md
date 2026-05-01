---
title: "chore(src): delete or fill 5 empty forward-declared stub files"
labels: ["tech-debt", "cleanup"]
priority: P4
---

## Context

`src/audittrace/` contains five files that exist only as empty
placeholders, forward-declared during Phase 0:

- `auth.py` (NOTE: distinct from `routes/auth.py` if applicable)
- `backend.py`
- `chain.py`
- `memory.py`
- `tracing.py`

They appear in coverage reports as `0 stmts, 100% covered` (since there's
nothing to cover) but cumulatively they:

- Drag the perceived module count up
- Confuse new readers ("what does `chain.py` do?" → nothing)
- Show up in import-search results with no content
- Get touched by ruff/mypy on every run

The intent (per yesterday's session memory) was to forward-declare them for
Phase 1/2. Phase 1 is now substantially landed and these files are still
empty.

## Fix

Either:
- **Delete** all five files. If a future phase needs them, recreate at that
  point with real content. Git history preserves the intent.
- **Fill** them with at least a one-line module docstring describing the
  intended responsibility, plus a `raise NotImplementedError(...)` stub
  function to make the module's purpose grep-able.

Recommendation: delete. YAGNI applies to scaffolding too.

## Acceptance criteria

- All five files either deleted or contain real content
- No imports break (`pytest` + `ruff check` clean)
- `python -c "import audittrace.auth"` etc. either works or fails
  with a clear `ModuleNotFoundError`
