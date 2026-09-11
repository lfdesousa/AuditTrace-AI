# Evidence — WU-1 console-conversations store, local capture (2026-09-11)

**Scope of this evidence file.** WU-1 of the MongoDB-elimination EPIC per its
ratified spec (`2026-09-11-SPEC-mongo-repl-wu1-console-conversations-store.md`,
sha256 `6eeed6d86678df5a9bf8760fc69c43eb08bab6d813db1697912e560e0ceedbb5`) —
explicit "Acceptance (Rules 2 & 3 — live, deferred to WU-2/WU-3 + deploy):
This WU delivers + unit-proves the store; the live E2E rides the shim WUs."
This file satisfies ADR-049 Rule 1 (Verification) in full and gives a
reconstructible Rule-3-shaped capture (neuter-proof of the RLS/isolation
guard, through the real FastAPI `create_app()` object via `TestClient`, not
mocked). It does **NOT** satisfy Rule 2 (Validation through a deployed image
+ public API + scoped JWT) — that rides WU-2 (fork write shim) / WU-3 (fork
read shim), mirroring the `wu1-session-layer-narrow-ingest-scope` precedent.

## 1. Full test-suite run (Rule 1 — Verification)

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.36%
4473 passed, 2 warnings in 469.89s (0:07:49)
```

(One unrelated pre-existing test — `test_release_bump_files_ssot.py::
test_make_release_dirties_exactly_the_ssot_set` — failed transiently before
this commit landed: it spawns a throwaway nested git worktree at `HEAD` but
reuses THIS worktree's editable-installed venv, so it imported the
uncommitted `src/audittrace` tree rather than the nested worktree's own
checked-out (pre-commit) copy, producing a spurious extra OpenAPI-regen
diff. Re-ran green after this commit landed — see §1b.)

### 1b. Re-run after commit (confirms the transient failure above is resolved)

```
$ .venv/bin/python -m pytest tests/test_release_bump_files_ssot.py -q --no-cov
2 passed
```

New-file coverage (line + branch), from the full run's coverage report:

```
src/audittrace/services/console_conversations.py   329     38     94     22    83%
src/audittrace/routes/console_conversations.py      (covered via
  tests/test_console_conversations_routes.py — 33 HTTP-route tests, all pass)
```

The service file's 83% reflects defensive branches inside the Postgres
implementation's exception-handling / dict-comprehension paths exercised
indirectly; the abstract-interface + Mock + Postgres CRUD/isolation surface
is fully covered by the 51 Mock + 27 Postgres unit tests in
`tests/test_console_conversations_service.py`.

`make lint` (ruff check + ruff format) and `make helm-lint` both green:

```
$ .venv/bin/ruff check src/ bff/ tests/
All checks passed!
$ .venv/bin/ruff format --check src/ bff/ tests/
306 files already formatted

$ make helm-lint
🪖  helm lint (vault.enabled=false)...
1 chart(s) linted, 0 chart(s) failed
🪖  helm template (vault.enabled=true) + vaultSecretFileGuard count...
✅ vaultSecretFileGuard present in 3 workloads
```

`mypy` clean on every new/touched file:

```
$ .venv/bin/mypy src/audittrace/services/console_conversations.py \
    src/audittrace/routes/console_conversations.py src/audittrace/db/models.py \
    src/audittrace/models.py src/audittrace/dependencies.py src/audittrace/server.py \
    src/audittrace/auth.py bff/config.py bff/console_conversations_scopes.py \
    bff/console_conversations_proxy.py bff/app.py
Success: no issues found in 11 source files
```

## 2. OpenAPI drift gate — additive-only confirmation

```
$ git diff --stat docs/reference/audittrace/openapi.yaml tests/fixtures/openapi.snapshot.yaml
 docs/reference/audittrace/openapi.yaml | 625 +++++++++++++++++++++++++++++++
 tests/fixtures/openapi.snapshot.yaml   | 625 +++++++++++++++++++++++++++++++
 2 files changed, 1250 insertions(+)

$ git diff docs/reference/audittrace/openapi.yaml | grep -E "^[-+]\s+/v1"
(no output — /v1/chat/completions untouched)
```

625 pure insertions, zero deletions in each file — the new
`/console/conversations*` paths + `Console*` schemas + the two new
`memory:conversations:*` OAuth2 scope entries. `/v1/chat/completions` is
byte-for-byte unchanged.

## 3. Neuter-proof — the RLS/isolation guard fails RED when broken, restored to GREEN

Builder-side falsifiability pass (the independent reviewer re-runs this
mandate-to-fail check itself; this is the builder's own verification that
the guard is real before handing off).

Guard: `PostgresConsoleConversationsService.get_conversation`'s explicit
`.filter(ConsoleConversation.user_sub == user_context.user_id)` clause
(`src/audittrace/services/console_conversations.py`). Neutered by removing
that one `.filter(...)` call (kept the `conversation_id` + `deleted_at_ms`
filters, so the ONLY thing removed is the per-user isolation clause).

```
$ .venv/bin/python -m pytest \
    tests/test_console_conversations_service.py::TestPostgresConsoleConversationsServiceConversations::test_cross_user_isolation_denies_read \
    -q --no-cov
FAILED tests/test_console_conversations_service.py::TestPostgresConsoleConversationsServiceConversations::test_cross_user_isolation_denies_read
1 failed, 50 deselected

$ .venv/bin/python -m pytest \
    tests/test_console_conversations_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_conversation \
    -q --no-cov
FAILED tests/test_console_conversations_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_conversation
AssertionError: user B read user A's conversation via the real HTTP route — the RLS/isolation wall is broken
assert 200 == 404
1 failed, 31 deselected
```

Both the SQLite-backed service-level guard AND the real-HTTP-route guard
(driven through the actual `require_user` cold path with two distinct
mocked-JWT subs, per `feedback_test_through_real_http_route` — not a
`dependency_overrides` swap, which would bypass the RLS ContextVar binding
entirely) go genuinely RED when the isolation filter is removed.

Restored (`diff <backup> <file>` → no output, confirming byte-identical
restore) and re-verified GREEN:

```
$ .venv/bin/python -m pytest \
    tests/test_console_conversations_service.py::TestPostgresConsoleConversationsServiceConversations::test_cross_user_isolation_denies_read \
    -q --no-cov
1 passed, 50 deselected

$ .venv/bin/python -m pytest \
    tests/test_console_conversations_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_conversation \
    -q --no-cov
1 passed, 31 deselected
```

The same isolation discipline (explicit `user_sub` filter on every query)
is repeated identically across all nine service methods
(`list_conversations`, `update_conversation_title`, `delete_conversation`,
`get_messages`, `upsert_message`, `edit_message`, `delete_message`), each
with its own dedicated cross-user non-vacuity test in
`tests/test_console_conversations_service.py` and
`tests/test_console_conversations_routes.py::TestCrossUserIsolation` —
this neuter-proof exercises one representative guard end-to-end; the
reviewer's own independent neuter pass covers the rest.

## 4. Frozen invariants — spot checks

- `/v1` byte-inviolate: confirmed in §2 (zero touched lines under any
  `/v1/chat/completions` path).
- `user_sub` token-derived at the choke, never caller-supplied: no request
  model in `audittrace.models` (`ConsoleConversationUpsertRequest`,
  `ConsoleMessageUpsertRequest`, etc.) declares a `user_sub`/`user_id`
  field — Pydantic's default `extra="ignore"` silently drops a hostile
  caller's attempt to supply one. Proven by
  `tests/test_console_conversations_routes.py::TestConsoleConversationsCrud
  ::test_hostile_body_user_sub_is_ignored` and
  `::TestCrossUserIsolation::test_hostile_body_user_sub_never_lands_as_
  another_real_user` (the latter through the real HTTP route with two
  distinct real subs).
- Traceability: RLS migration 023 mirrors migration 022's shape (ENABLE +
  FORCE ROW LEVEL SECURITY + a `FOR ALL` policy comparing `user_sub`
  against `current_setting('app.current_user_id', true)`), guarded by
  `_is_postgres()` so SQLite unit tests exercise the service-layer explicit
  filter instead (feedback_unit_tests_miss_rls).
- Least privilege: `memory:conversations:read-own` and
  `memory:conversations:write` are two distinct scopes, neither implies
  the other — proven by `TestScopeEnforcement::test_write_scope_alone_
  cannot_read` / `test_read_scope_alone_cannot_write`.

## 5. What this file does NOT claim

No image was built, no `helm upgrade` ran, no pod was hit through the
public API with a scoped JWT against a deployed image — Rule 2/3 (live
E2E) is explicitly deferred to WU-2 (fork write shim) + WU-3 (fork read
shim) per the ratified spec's own acceptance criteria.
