---
title: "chore(src): delete or fill 5 empty forward-declared stub files"
labels: ["tech-debt", "cleanup"]
priority: P4
status: closed
closed: 2026-05-04
---

## Closure note (2026-05-04, PR B)

Audit during PR B turned up that this item was effectively already
resolved through normal Phase 1/2 work:

- `src/audittrace/auth.py` — **exists, 321 lines**, fully-implemented:
  multi-issuer JWT validation, JWKS cache, `require_user`,
  `require_scope`, JIT user-context binding for RLS. Phase 1+2 used
  this file as designed; the original "empty stub" framing is stale.
- `src/audittrace/backend.py` — **never created**. YAGNI applied.
- `src/audittrace/chain.py` — **never created**. YAGNI applied.
- `src/audittrace/memory.py` — **never created**. YAGNI applied.
- `src/audittrace/tracing.py` — **never created**. YAGNI applied.

Acceptance criteria from the original ticket re-evaluated:

> All five files either deleted or contain real content

Met: `auth.py` has real content; the four others were never committed
(`git log -- src/audittrace/{backend,chain,memory,tracing}.py` returns
no commits), so deletion is moot. Item closed without code change.

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
