# Evidence — Files-metadata domain console-files store, local capture (2026-09-11)

**Scope of this evidence file.** The Files-metadata domain of the MongoDB-elimination
EPIC per its ratified spec (`2026-09-11-SPEC-mongo-repl-wu-files-metadata-store.md`, sha256
`f9ebba97a5468d23df142dc0bc10c1f073473cdf6fa40871f4810eb0c48b118`) — explicit
"Acceptance (Rules 2 & 3 — live, deferred): The files fork shim (later WU, chokepoint
pattern) + deploy exercise this live." This file satisfies ADR-049 Rule 1 (Verification)
in full and gives a reconstructible Rule-3-shaped capture (neuter-proof of the
RLS/isolation guard, through the real FastAPI `create_app()` object via `TestClient`, not
mocked). It does **NOT** satisfy Rule 2 (Validation through a deployed image + public API
+ scoped JWT) — that rides the later fork files-shim WU, mirroring the
`wu1-console-conversations-store`/`wu-presets`/`wu-prompts`/`wu-chatprojects` precedents.

## 1. Full test-suite run (Rule 1 — Verification)

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.89%
1 failed, 4889 passed, 2 warnings in 724.72s (0:12:04)
FAILED tests/test_release_bump_files_ssot.py::test_make_release_dirties_exactly_the_ssot_set
```

The one failure (`test_make_release_dirties_exactly_the_ssot_set`) is the SAME
pre-existing transient documented in WU-1/WU-presets/WU-prompts/WU-chatprojects' own
evidence captures: that test spawns a throwaway nested `git worktree` at `HEAD` but
symlinks THIS worktree's editable-installed `.venv`, so the nested worktree's
`make release` run imports the CURRENT (pre-commit) `src/audittrace` tree instead of its
own freshly-checked-out copy, producing a spurious extra OpenAPI-regen diff
(`docs/reference/audittrace/openapi.yaml` + `tests/fixtures/openapi.snapshot.yaml`, both
containing this WU's new `/console/files*` paths that HEAD, pre-commit, does not yet
have). Confirmed transient, not a real regression — see §6 for the post-commit re-run
this claim rests on.

Per-file coverage gate and zero-skip policy, run directly (the Makefile recipe aborted
before reaching them because the one pytest failure above returned non-zero):

```
$ .venv/bin/python scripts/check-per-file-coverage.py
per-file coverage gate: PASS (133 files checked, lines >= 90%, branches >= 90% on 113 file(s) with branches)

$ .venv/bin/python scripts/check-no-skipped-tests.py
[no-skip-check] No skipped tests in junit.xml. Good.
```

New-file coverage (line + branch), isolated run:

```
$ .venv/bin/python -m pytest tests/test_console_files_service.py \
    tests/test_console_files_routes.py \
    --cov=src/audittrace/services/console_files \
    --cov=src/audittrace/routes/console_files --cov-report=term-missing -q
src/audittrace/routes/console_files.py       52    0    4    0   100%
src/audittrace/services/console_files.py    211    0   54    0   100%
72 passed in 19.04s
```

100% lines AND branches on both new orchestrator-side files — the abstract-interface +
Mock + Postgres CRUD/isolation/update-existing/batch-get surface is fully covered by the
Mock/Postgres unit tests in `tests/test_console_files_service.py` plus the HTTP-route
tests in `tests/test_console_files_routes.py` (82 tests total across both files).

BFF-side new files, isolated run:

```
$ .venv/bin/python -m pytest tests/bff/test_console_file_records.py \
    tests/bff/test_console_file_records_scopes.py tests/bff/test_console_files.py \
    tests/bff/test_console_files_promote.py tests/bff/test_console_files_scopes.py -q
80 passed in 7.07s
```

`ruff check`, `ruff format --check`, and `make helm-lint` all green on every new/touched
file:

```
$ .venv/bin/ruff check <every new/touched file>
All checks passed!
$ .venv/bin/ruff format --check <every new/touched file>
(after one reformat pass) all files already formatted

$ make helm-lint
🪖  helm lint (vault.enabled=false)...
1 chart(s) linted, 0 chart(s) failed
🪖  helm template (vault.enabled=true) + vaultSecretFileGuard count...
✅ vaultSecretFileGuard present in 3 workloads
```

`mypy` clean on every new/touched source file:

```
$ .venv/bin/mypy src/audittrace/routes/console_files.py \
    src/audittrace/services/console_files.py src/audittrace/db/models.py \
    src/audittrace/models.py src/audittrace/dependencies.py src/audittrace/server.py \
    src/audittrace/auth.py bff/console_file_records_proxy.py \
    bff/console_file_records_scopes.py bff/app.py bff/config.py
Success: no issues found in 11 source files
```

Alembic single-head check:

```
$ .venv/bin/python -m alembic heads
6694d8019051 (head)
```

## 2. OpenAPI drift gate — additive-only confirmation

```
$ git diff --stat docs/reference/audittrace/openapi.yaml tests/fixtures/openapi.snapshot.yaml
 docs/reference/audittrace/openapi.yaml | 391 +++++++++++++++++++++++++++++++
 tests/fixtures/openapi.snapshot.yaml   | 391 +++++++++++++++++++++++++++++++
 2 files changed, 782 insertions(+)

$ git diff docs/reference/audittrace/openapi.yaml | grep -E "^[-+]\s+/v1"
(no output — /v1/chat/completions untouched)
```

391 pure insertions, zero deletions in each file — the new `/console/files*` paths +
`ConsoleFile*` schemas + the two new `memory:files:*` OAuth2 scope entries.
`/v1/chat/completions` is byte-for-byte unchanged.

## 3. Neuter-proof — the RLS/isolation guard fails RED when broken, restored to GREEN

Builder-side falsifiability pass (the independent reviewer re-runs this mandate-to-fail
check itself; this is the builder's own verification that the guard is real before
handing off).

Guard: `PostgresConsoleFilesService.get_file`'s explicit
`.filter(ConsoleFile.user_sub == user_context.user_id)` clause
(`src/audittrace/services/console_files.py`). Neutered by commenting out that one
`.filter(...)` call (kept the `file_id` + `deleted_at_ms` filters, so the ONLY thing
removed is the per-user isolation clause).

Baseline (GREEN, guard intact):

```
$ .venv/bin/python -m pytest \
    tests/test_console_files_service.py::TestPostgresConsoleFilesService::test_cross_user_isolation_denies_read \
    tests/test_console_files_routes.py::TestCrossUserIsolation \
    -q --no-cov
3 passed in ...s
```

Neutered (RED):

```
$ .venv/bin/python -m pytest \
    tests/test_console_files_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_file \
    tests/test_console_files_service.py::TestPostgresConsoleFilesService::test_cross_user_isolation_denies_read \
    -q --no-cov
FAILED tests/test_console_files_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_file
FAILED tests/test_console_files_service.py::TestPostgresConsoleFilesService::test_cross_user_isolation_denies_read
2 failed in 0.87s
```

Both the SQLite-backed service-level guard AND the real-HTTP-route guard (driven through
the actual `require_user` cold path with two distinct mocked-JWT subs, per
`feedback_test_through_real_http_route` — not a `dependency_overrides` swap, which would
bypass the RLS ContextVar binding entirely) go genuinely RED when the isolation filter is
removed: the route-level failure is `assert bob_read.status_code == 404` failing because
bob's GET returned `200` (user A's file leaked); the service-level failure is
`assert bob_read is None` failing because Alice's row came back for Bob's lookup.

Restored (`git diff` → no output after re-copy, confirming byte-identical restore) and
re-verified GREEN:

```
$ git diff --stat src/audittrace/services/console_files.py
(no output — byte-identical to the pre-neuter version, other than the unrelated
 batch_get_files ORM-session-scope fix captured in §4 below)

$ .venv/bin/python -m pytest \
    tests/test_console_files_service.py tests/test_console_files_routes.py \
    tests/test_session_scope_discipline.py -q --no-cov
82 passed in 23.32s
```

The same isolation discipline (explicit `user_sub` filter on every query) is repeated
identically across every service method (`list_files`, `upsert_file`, `delete_file`,
`batch_get_files`), each with its own dedicated cross-user non-vacuity test in
`tests/test_console_files_service.py` and
`tests/test_console_files_routes.py::TestCrossUserIsolation` — this neuter-proof
exercises one representative guard end-to-end; the reviewer's own independent neuter pass
covers the rest.

## 4. Bug caught by the builder's own non-vacuity pass — ORM instance outliving its session

The FIRST `make test` run (pre-fix) surfaced a genuine defect, not a test artefact:

```
FAILED tests/test_session_scope_discipline.py::TestNoOrmEscapesSessionScope::test_no_orm_instance_outlives_its_session
AssertionError: ORM instance(s) read after their session closed (#364).
  at-wt-files/src/audittrace/services/console_files.py:364 batch_get_files() -> rows
```

`PostgresConsoleFilesService.batch_get_files` built its `by_id = {r.file_id:
_file_to_dict(r) for r in rows}` dict-comprehension OUTSIDE the `async with
self._session_factory() as session:` block — the ORM rows were still being read
(`r.file_id`, and inside `_file_to_dict`, every other column) after the session that
loaded them had already closed. Fixed by moving the comprehension inside the `async
with` block (mirrors `list_files`' existing, correct pattern). Re-run confirms GREEN:

```
$ .venv/bin/python -m pytest tests/test_session_scope_discipline.py -q --no-cov
10 passed in ...s
$ .venv/bin/python -m pytest tests/test_console_files_service.py::TestPostgresConsoleFilesServiceBatchGet -q --no-cov
4 passed in ...s
```

## 5. Regression-safety — audittrace-opencode scope set unchanged

```
$ .venv/bin/python -m pytest tests/test_chart_drift_guards.py \
    -k "Files or ChatProjects or Restricted or LibrechatConsoleClient" -q --no-cov
30 passed, 112 deselected
```

`TestOpencodeClientScopesUnchangedByFiles::test_opencode_scope_set_never_gains_files_scopes`
asserts, against BOTH realm files (`keycloak/realm-audittrace.json` and the rendered
`charts/audittrace/files/realm-audittrace.json`), that `audittrace-opencode`'s
`defaultClientScopes ∪ optionalClientScopes` never contains `memory:files:read-own` or
`memory:files:write` — the new scope pair is additive on `audittrace-librechat` ONLY.
`audittrace-opencode`'s client block in both realm files is untouched by this change.

`TestRestrictedClientStaysRestricted` (SC-09) now also passes with the M4-hardened
`FORBIDDEN` tuple — `memory:{conversations,presets,prompts,chat_projects,files}:write`
were added alongside the pre-existing four memory-layer write scopes, and
`audittrace-restricted` is confirmed to hold none of them in either realm file, in
either scope set.

## 6. Frozen invariants — spot checks

- `/v1` byte-inviolate: confirmed in §2 (zero touched lines under any
  `/v1/chat/completions` path).
- `user_sub` token-derived at the choke, never caller-supplied: no request model in
  `audittrace.models` (`ConsoleFileUpsertRequest`, `ConsoleFileBatchGetRequest`) declares
  a `user_sub`/`user_id` field — Pydantic's default `extra="ignore"` silently drops a
  hostile caller's attempt to supply one. Proven by
  `tests/test_console_files_routes.py::TestConsoleFilesCrud::test_hostile_body_user_sub_is_ignored`.
- Traceability: RLS migration 027 mirrors migrations 022-026's shape (ENABLE + FORCE ROW
  LEVEL SECURITY + a `FOR ALL` policy comparing `user_sub` against
  `current_setting('app.current_user_id', true)`), guarded by `_is_postgres()` so SQLite
  unit tests exercise the service-layer explicit filter instead
  (feedback_unit_tests_miss_rls).
- Least privilege: `memory:files:read-own` and `memory:files:write` are two distinct
  scopes, neither implies the other — proven by
  `TestScopeEnforcement::test_write_scope_alone_cannot_read` /
  `test_read_scope_alone_cannot_write` / `test_write_scope_alone_cannot_batch_get`.
- Metadata-only / bytes stay in object storage: no route or service method in this WU
  accepts or returns a byte payload — `ConsoleFileUpsertRequest`/`ConsoleFileItem` carry
  only `object_key` (a pointer), never content; grep confirms no `bytes`/`UploadFile`
  request-body handling anywhere in `routes/console_files.py`.
- Pre-existing ephemeral file-ingest route (`POST /console/files` on the BFF,
  `/memory/upload` on the orchestrator) is untouched — proven by
  `tests/bff/test_console_file_records.py::TestIngestRouteUntouched` and the unmodified
  passing state of `tests/bff/test_console_files.py`/`test_console_files_promote.py`.

## 7. Post-commit re-run (the release-bump-files transient resolves)

See the commit this evidence file lands with — run immediately after that commit, HEAD
equals the working tree, so the nested throwaway worktree the
`test_release_bump_files_ssot` test spawns checks out the SAME (committed) source the
shared editable `.venv` resolves to, and the spurious extra OpenAPI-regen diff
disappears (same mechanism confirmed at WU-1/WU-presets/WU-prompts/WU-chatprojects).

## 8. What this file does NOT claim

No image was built, no `helm upgrade` ran, no pod was hit through the public API with a
scoped JWT against a deployed image — Rule 2/3 (live E2E) is explicitly deferred to the
later fork files-shim WU per the ratified spec's own acceptance criteria.
