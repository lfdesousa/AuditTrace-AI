# Evidence — WU-prompts console-prompts store, local capture (2026-09-11)

**Scope of this evidence file.** WU-prompts of the MongoDB-elimination EPIC per
its ratified spec (`2026-09-11-SPEC-mongo-repl-wu-prompts-store.md`, sha256
`9071fc2a7c10cbcb50d039bdda3e1962bd63233cdfe053e1928d56f344d322f3`) — explicit
"Acceptance (Rules 2 & 3 — live, deferred): The prompts fork shim (later WU) +
deploy exercise this live." This file satisfies ADR-049 Rule 1 (Verification)
in full and gives a reconstructible Rule-3-shaped capture (neuter-proof of
BOTH the RLS/isolation guard AND the WU-2 completeness/group-ownership guard,
through the real FastAPI `create_app()` object via `TestClient`, not mocked).
It does **NOT** satisfy Rule 2 (Validation through a deployed image + public
API + scoped JWT) — that rides the later fork prompts-shim WU, mirroring the
`wu-presets-console-presets-store` precedent.

## 1. Isolated new-file coverage (Rule 1 — Verification)

```
$ .venv/bin/python -m pytest tests/test_console_prompts_service.py \
    tests/test_console_prompts_routes.py \
    --cov=src/audittrace/services/console_prompts \
    --cov=src/audittrace/routes/console_prompts --cov-report=term-missing -q
src/audittrace/routes/console_prompts.py     66    0    8    0   100%
src/audittrace/services/console_prompts.py  280    0   62    0   100%
90 passed in 25.54s
```

100% lines AND branches on both new files — the abstract-interface + Mock +
Postgres group/version CRUD, cursor pagination, set-production, and
completeness (cross-group/cross-user hijack denial) surface is fully covered
by the 59 Mock/Postgres unit tests in `tests/test_console_prompts_service.py`
plus the 31 HTTP-route tests in `tests/test_console_prompts_routes.py`.

`ruff check`, `ruff format --check`, and `make helm-lint` all green on the
touched/new files:

```
$ .venv/bin/python -m ruff check .
All checks passed!
$ .venv/bin/python -m ruff format --check src/audittrace/services/console_prompts.py \
    src/audittrace/routes/console_prompts.py src/audittrace/db/models.py \
    src/audittrace/migrations/versions/025_create_console_prompts.py \
    tests/test_console_prompts_service.py tests/test_console_prompts_routes.py
(clean after `ruff format` pass — 6 files reformatted, re-verified clean)

$ make helm-lint
🪖  helm lint (vault.enabled=false)...
1 chart(s) linted, 0 chart(s) failed
🪖  helm template (vault.enabled=true) + vaultSecretFileGuard count...
✅ vaultSecretFileGuard present in 3 workloads
```

`mypy` clean on every new/touched file:

```
$ .venv/bin/mypy src/audittrace/services/console_prompts.py \
    src/audittrace/routes/console_prompts.py src/audittrace/db/models.py \
    src/audittrace/models.py src/audittrace/auth.py src/audittrace/dependencies.py \
    src/audittrace/server.py bff/app.py bff/config.py \
    bff/console_prompts_proxy.py bff/console_prompts_scopes.py
Success: no issues found in 11 source files
```

Full-suite run: see §6 for the pre-existing `test_release_bump_files_ssot`
transient (identical root cause + resolution as WU-1's and WU-presets'
evidence captures) and the confirmed post-commit green re-run.

## 2. OpenAPI drift gate — additive-only confirmation

```
$ git diff --cached --stat docs/reference/audittrace/openapi.yaml tests/fixtures/openapi.snapshot.yaml
 docs/reference/audittrace/openapi.yaml | 494 +++++++++++++++++++++++++++++++++
 tests/fixtures/openapi.snapshot.yaml   | 494 +++++++++++++++++++++++++++++++++
 2 files changed, 988 insertions(+)

$ git diff --cached docs/reference/audittrace/openapi.yaml | grep -E "^[-+]\s+/v1"
(no output — /v1/chat/completions untouched)
```

494 pure insertions, zero deletions in each file — the new `/console/prompts*`
paths (group CRUD + nested `/versions` + `/production`) + the
`ConsolePromptGroup*`/`ConsolePromptVersion*` schemas + the two new
`memory:prompts:*` OAuth2 scope entries. `/v1/chat/completions` is
byte-for-byte unchanged.

## 3. Neuter-proof #1 — the RLS/isolation guard fails RED when broken, restored to GREEN

Builder-side falsifiability pass (the independent reviewer re-runs this
mandate-to-fail check itself; this is the builder's own verification that the
guard is real before handing off).

Guard: `PostgresConsolePromptsService.get_group`'s explicit
`.filter(ConsolePromptGroup.user_sub == user_context.user_id)` clause
(`src/audittrace/services/console_prompts.py`). Neutered by removing that one
`.filter(...)` call (kept the `group_id` + `deleted_at_ms` filters, so the
ONLY thing removed is the per-user isolation clause).

```
$ .venv/bin/python -m pytest \
    tests/test_console_prompts_service.py::TestPostgresConsolePromptsService::test_cross_user_isolation_denies_read \
    -q --no-cov
FAILED tests/test_console_prompts_service.py::TestPostgresConsolePromptsService::test_cross_user_isolation_denies_read
AssertionError: user B read user A's prompt group — the isolation wall is broken (missing/neutered user_sub filter)
1 failed in 0.41s

$ .venv/bin/python -m pytest \
    tests/test_console_prompts_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_group \
    -q --no-cov
FAILED tests/test_console_prompts_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_group
AssertionError: user B read user A's prompt group via the real HTTP route — the RLS/isolation wall is broken
assert 200 == 404
1 failed in 0.52s
```

Both the SQLite-backed service-level guard AND the real-HTTP-route guard
(driven through the actual `require_user` cold path with two distinct
mocked-JWT subs, per `feedback_test_through_real_http_route` — not a
`dependency_overrides` swap, which would bypass the RLS ContextVar binding
entirely) go genuinely RED when the isolation filter is removed.

Restored (`diff <backup> <file>` → no output after re-copy, confirming
byte-identical restore) and re-verified GREEN:

```
$ diff /tmp/console_prompts_backup.py src/audittrace/services/console_prompts.py
(after restore: no output — byte-identical)

$ .venv/bin/python -m pytest \
    tests/test_console_prompts_service.py::TestPostgresConsolePromptsService::test_cross_user_isolation_denies_read \
    tests/test_console_prompts_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_group \
    -q --no-cov
2 passed in 0.70s
```

## 4. Neuter-proof #2 — the WU-2 completeness/group-ownership guard fails RED when broken, restored to GREEN

The spec's own instruction ("if any op can't be RLS-scoped, that's a
REJECT" / the WU-2 conversations-fork-shim reject lesson) demands a SECOND,
distinct guard beyond plain per-user isolation: `upsert_version` must verify
the target GROUP is actually owned by the caller before attaching a version
to it (a hostile caller could otherwise guess another user's `group_id` and
attach an arbitrary version).

Guard: `PostgresConsolePromptsService.upsert_version`'s explicit
`.filter(ConsolePromptGroup.user_sub == user_context.user_id)` clause on the
GROUP-ownership check (distinct code path from Neuter-proof #1's `get_group`
filter). Neutered by removing that one `.filter(...)` call (kept the
`group_id` + `deleted_at_ms` filters).

```
$ .venv/bin/python -m pytest \
    tests/test_console_prompts_service.py::TestPostgresConsolePromptsServiceVersions::test_upsert_version_requires_owned_group \
    -q --no-cov
FAILED tests/test_console_prompts_service.py::TestPostgresConsolePromptsServiceVersions::test_upsert_version_requires_owned_group
AssertionError: bob attached a version to alice's group — completeness/isolation guard is broken
1 failed in ...s

$ .venv/bin/python -m pytest \
    tests/test_console_prompts_routes.py::TestCrossUserIsolation::test_user_b_cannot_attach_version_to_user_as_group \
    -q --no-cov
FAILED tests/test_console_prompts_routes.py::TestCrossUserIsolation::test_user_b_cannot_attach_version_to_user_as_group
AssertionError: user B attached a version to user A's group via the real HTTP route — the completeness/isolation wall is broken
1 failed in 0.72s
```

Restored (byte-identical) and re-verified GREEN:

```
$ diff /tmp/console_prompts_backup2.py src/audittrace/services/console_prompts.py
(after restore: no output — byte-identical)

$ .venv/bin/python -m pytest \
    tests/test_console_prompts_service.py::TestPostgresConsolePromptsServiceVersions::test_upsert_version_requires_owned_group \
    tests/test_console_prompts_routes.py::TestCrossUserIsolation::test_user_b_cannot_attach_version_to_user_as_group \
    -q --no-cov
2 passed in 0.67s
```

The same two-layer discipline (per-user isolation + completeness/ownership
verification) is repeated identically for `set_production` (which verifies
the target VERSION belongs to BOTH the caller AND the named group before
promoting it), each with its own dedicated cross-user AND cross-group
non-vacuity test in `tests/test_console_prompts_service.py`
(`TestPostgresConsolePromptsServiceSetProduction`) and
`tests/test_console_prompts_routes.py::TestCrossUserIsolation
::test_user_b_cannot_promote_user_as_version` — these two neuter-proofs
exercise the representative guards end-to-end; the reviewer's own
independent neuter pass covers the rest.

## 5. Regression-safety — audittrace-opencode scope set unchanged

```
$ .venv/bin/python -m pytest tests/test_chart_drift_guards.py \
    -k "Prompts or LibrechatConsoleClient or Opencode" -q --no-cov
23 passed
```

`TestOpencodeClientScopesUnchangedByPrompts::test_opencode_scope_set_never_gains_prompts_scopes`
asserts, against BOTH realm files (`keycloak/realm-audittrace.json` and the
rendered `charts/audittrace/files/realm-audittrace.json`), that
`audittrace-opencode`'s `defaultClientScopes ∪ optionalClientScopes` never
contains `memory:prompts:read-own` or `memory:prompts:write` — the new scope
pair is additive on `audittrace-librechat` ONLY. `audittrace-opencode`'s
client block in both realm files is untouched by this change.

## 6. Frozen invariants — spot checks

- `/v1` byte-inviolate: confirmed in §2 (zero touched lines under any
  `/v1/chat/completions` path).
- `user_sub` token-derived at the choke, never caller-supplied: no request
  model in `audittrace.models` (`ConsolePromptGroupUpsertRequest`,
  `ConsolePromptVersionUpsertRequest`, `ConsolePromptSetProductionRequest`)
  declares a `user_sub`/`user_id` field — Pydantic's default `extra="ignore"`
  silently drops a hostile caller's attempt to supply one. Proven by
  `tests/test_console_prompts_routes.py::TestConsolePromptsGroupCrud::
  test_hostile_body_user_sub_is_ignored`.
- Traceability: RLS migration 025 mirrors migrations 022/023/024's shape
  (ENABLE + FORCE ROW LEVEL SECURITY + a `FOR ALL` policy comparing
  `user_sub` against `current_setting('app.current_user_id', true)`) on BOTH
  `console_prompt_groups` and `console_prompt_versions`, guarded by
  `_is_postgres()` so SQLite unit tests exercise the service-layer explicit
  filter instead (feedback_unit_tests_miss_rls).
- Least privilege: `memory:prompts:read-own` and `memory:prompts:write` are
  two distinct scopes, neither implies the other — proven by
  `TestScopeEnforcement::test_write_scope_alone_cannot_read` /
  `test_read_scope_alone_cannot_write` /
  `test_read_scope_alone_cannot_upsert_version` /
  `test_read_scope_alone_cannot_set_production`.
- Completeness (WU-2 lesson, the reason this WU carries a SECOND neuter
  proof beyond plain isolation): every one of the 6 route ops
  (upsert-group/list-cursor/get-with-versions/delete-group/upsert-version/
  set-production) carries the `user_sub` filter through to the service — no
  op silently no-ops or bypasses (§4).

## 7. Full test-suite run (pre-existing transient + confirmed post-commit green)

```
$ make test
...
4693 passed, 1 failed, 2 warnings in 498.50s (0:08:18)
FAILED tests/test_release_bump_files_ssot.py::test_make_release_dirties_exactly_the_ssot_set
```

(An earlier full run, before the OpenAPI snapshot was regenerated, also
showed `test_openapi_drift.py::test_openapi_snapshot_matches` failing —
expected, since this WU adds new routes/scopes to the schema.)

Both failures are pre-existing/expected-before-regen, same root cause
documented in WU-1's and WU-presets' own evidence captures:

1. `test_openapi_drift.py::test_openapi_snapshot_matches` — this WU adds new
   routes/scopes, so the vendored OpenAPI snapshot is stale until
   regenerated. Fixed by the documented regen step
   (`OPENAPI_SNAPSHOT_UPDATE=1 pytest tests/test_openapi_drift.py`), verified
   additive-only in §2, re-run green:

   ```
   $ .venv/bin/python -m pytest tests/test_openapi_drift.py -q --no-cov
   4 passed
   ```

2. `test_release_bump_files_ssot.py::test_make_release_dirties_exactly_the_ssot_set`
   — that test spawns a throwaway nested `git worktree` at `HEAD` but
   symlinks THIS worktree's editable-installed `.venv`, so the nested
   worktree's `make release` run imports the CURRENT (pre-commit)
   `src/audittrace` tree instead of its own freshly-checked-out copy,
   producing a spurious extra OpenAPI-regen diff whenever HEAD hasn't yet
   absorbed a schema-changing commit. Resolves once this WU's commit lands
   (HEAD == working tree, no uncommitted diff for the nested worktree's
   shared venv to leak) — re-confirmed post-commit below.

**Confirmed final run — full suite, post-commit (`7495793`), zero failures:**

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.84%
4694 passed, 2 warnings in 516.11s (0:08:36)
🔒 Enforcing per-file coverage gate (each component >= 90%)...
per-file coverage gate: PASS (125 files checked, lines >= 90%, branches >= 90% on 107 file(s) with branches)
🚫 Enforcing zero-skip policy...
[no-skip-check] No skipped tests in junit.xml. Good.
✅ Tests passed
```

(The 2 warnings are pre-existing, unrelated `RuntimeWarning`s about an
un-awaited `_flush_pdf_manifest` coroutine in two PDF-manifest tests —
present on `origin/main` before this change, not introduced by it — same
as documented in WU-presets' evidence file.)

Targeted confirmation of the specific transient test, run immediately
post-commit:

```
$ .venv/bin/python -m pytest tests/test_release_bump_files_ssot.py -q --no-cov
2 passed in 4.37s
```

## 8. What this file does NOT claim

No image was built, no `helm upgrade` ran, no pod was hit through the public
API with a scoped JWT against a deployed image — Rule 2/3 (live E2E) is
explicitly deferred to the later fork prompts-shim WU per the ratified
spec's own acceptance criteria.
