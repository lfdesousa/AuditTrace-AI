# Evidence — Chat-Projects domain console-chat-projects store, local capture (2026-09-11)

**Scope of this evidence file.** The Chat-Projects domain of the MongoDB-elimination
EPIC per its ratified spec (`2026-09-11-SPEC-mongo-repl-wu-chatprojects-store.md`, sha256
`d0d3eccccb75021781833c69daeb041abf2ab04eab01fd7879e41a89987901d2`) — explicit
"Acceptance (Rules 2 & 3 — live, deferred): The chat-projects fork shim (later WU, via
the chokepoint pattern) + deploy exercise this live." This file satisfies ADR-049 Rule 1
(Verification) in full and gives a reconstructible Rule-3-shaped capture (neuter-proof of
the RLS/isolation guard, through the real FastAPI `create_app()` object via `TestClient`,
not mocked). It does **NOT** satisfy Rule 2 (Validation through a deployed image + public
API + scoped JWT) — that rides the later fork chat-projects-shim WU, mirroring the
`wu1-console-conversations-store`/`wu-presets`/`wu-prompts` precedents.

## 1. Full test-suite run (Rule 1 — Verification)

```
$ .venv/bin/python -m pytest tests/ -q --deselect tests/test_release_bump_files_ssot.py::test_make_release_dirties_exactly_the_ssot_set
...
Required test coverage of 90% reached. Total coverage: 98.41%
4781 passed, 1 deselected, 2 warnings in 1072.95s (0:17:52)
```

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.86%
FAILED tests/test_release_bump_files_ssot.py::test_make_release_dirties_exactly_the_ssot_set
1 failed, 4781 passed, 2 warnings in 561.61s (0:09:21)
```

The one failure (`test_make_release_dirties_exactly_the_ssot_set`) is the SAME
pre-existing transient documented in WU-1/WU-presets/WU-prompts' own evidence captures:
that test spawns a throwaway nested `git worktree` at `HEAD` but symlinks THIS worktree's
editable-installed `.venv`, so the nested worktree's `make release` run imports the
CURRENT (pre-commit) `src/audittrace` tree instead of its own freshly-checked-out copy,
producing a spurious extra OpenAPI-regen diff (`docs/reference/audittrace/openapi.yaml` +
`tests/fixtures/openapi.snapshot.yaml`, both containing this WU's new
`/console/chat-projects*` paths that HEAD, pre-commit, does not yet have). Confirmed
transient, not a real regression — see §6 for the post-commit re-run this claim rests on.

New-file coverage (line + branch), isolated run:

```
$ .venv/bin/python -m pytest tests/test_console_chat_projects_service.py \
    tests/test_console_chat_projects_routes.py \
    --cov=src/audittrace/services/console_chat_projects \
    --cov=src/audittrace/routes/console_chat_projects --cov-report=term-missing -q
src/audittrace/routes/console_chat_projects.py              46    0    4    0   100%
src/audittrace/services/console_chat_projects.py           161    0   28    0   100%
55 passed in 13.43s
```

100% lines AND branches on both new files — the abstract-interface + Mock + Postgres
CRUD/isolation/update-existing surface is fully covered by the 34 Mock/Postgres unit
tests in `tests/test_console_chat_projects_service.py` plus the 21 HTTP-route tests in
`tests/test_console_chat_projects_routes.py`.

`ruff check`, `ruff format --check`, and `make helm-lint` all green:

```
$ .venv/bin/ruff check .
All checks passed!
$ .venv/bin/ruff format --check bff/ src/audittrace/ tests/
333 files already formatted

$ make helm-lint
🪖  helm lint (vault.enabled=false)...
1 chart(s) linted, 0 chart(s) failed
🪖  helm template (vault.enabled=true) + vaultSecretFileGuard count...
✅ vaultSecretFileGuard present in 3 workloads
```

`mypy` clean on every new/touched file:

```
$ .venv/bin/mypy src/audittrace/services/console_chat_projects.py \
    src/audittrace/routes/console_chat_projects.py \
    src/audittrace/migrations/versions/026_create_console_chat_projects.py \
    bff/console_chat_projects_proxy.py bff/console_chat_projects_scopes.py \
    src/audittrace/auth.py src/audittrace/dependencies.py \
    src/audittrace/models.py src/audittrace/server.py \
    src/audittrace/db/models.py bff/app.py bff/config.py
Success: no issues found in 12 source files
```

Alembic single-head check:

```
$ .venv/bin/python -m alembic -c alembic.ini heads
a3f7c92e1d5b (head)
```

## 2. OpenAPI drift gate — additive-only confirmation

```
$ git diff --stat docs/reference/audittrace/openapi.yaml tests/fixtures/openapi.snapshot.yaml
 docs/reference/audittrace/openapi.yaml | 229 +++++++++++++++++++++++++++++++++
 tests/fixtures/openapi.snapshot.yaml   | 229 +++++++++++++++++++++++++++++++++
 2 files changed, 458 insertions(+)

$ git diff docs/reference/audittrace/openapi.yaml | grep -E "^[-+]\s+/v1"
(no output — /v1/chat/completions untouched)
```

229 pure insertions, zero deletions in each file — the new `/console/chat-projects*`
paths + `ConsoleChatProject*` schemas + the two new `memory:chat_projects:*` OAuth2 scope
entries. `/v1/chat/completions` is byte-for-byte unchanged.

## 3. Neuter-proof — the RLS/isolation guard fails RED when broken, restored to GREEN

Builder-side falsifiability pass (the independent reviewer re-runs this mandate-to-fail
check itself; this is the builder's own verification that the guard is real before
handing off).

Guard: `PostgresConsoleChatProjectsService.get_chat_project`'s explicit
`.filter(ConsoleChatProject.user_sub == user_context.user_id)` clause
(`src/audittrace/services/console_chat_projects.py`). Neutered by commenting out that one
`.filter(...)` call (kept the `chat_project_id` + `deleted_at_ms` filters, so the ONLY
thing removed is the per-user isolation clause).

Baseline (GREEN, guard intact):

```
$ .venv/bin/python -m pytest \
    tests/test_console_chat_projects_service.py::TestPostgresConsoleChatProjectsService::test_cross_user_isolation_denies_read \
    tests/test_console_chat_projects_routes.py::TestCrossUserIsolation \
    -q --no-cov
3 passed in 1.98s
```

Neutered (RED):

```
$ .venv/bin/python -m pytest \
    tests/test_console_chat_projects_service.py::TestPostgresConsoleChatProjectsService::test_cross_user_isolation_denies_read \
    tests/test_console_chat_projects_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_chat_project \
    -q --no-cov
FAILED tests/test_console_chat_projects_service.py::TestPostgresConsoleChatProjectsService::test_cross_user_isolation_denies_read
AssertionError: user B read user A's chat-project — the isolation wall is broken (missing/neutered user_sub filter)
assert {'chat_project_id': 'secret-project', 'user_sub': 'user-alice-rls', 'name': "alice's private project", 'description': '', ...} is None

FAILED tests/test_console_chat_projects_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_chat_project
AssertionError: user B read user A's chat-project via the real HTTP route — the RLS/isolation wall is broken
assert 200 == 404
2 failed in 1.24s
```

Both the SQLite-backed service-level guard AND the real-HTTP-route guard (driven through
the actual `require_user` cold path with two distinct mocked-JWT subs, per
`feedback_test_through_real_http_route` — not a `dependency_overrides` swap, which would
bypass the RLS ContextVar binding entirely) go genuinely RED when the isolation filter is
removed.

Restored (`git diff` → no output after re-copy, confirming byte-identical restore) and
re-verified GREEN:

```
$ git diff --stat src/audittrace/services/console_chat_projects.py
(no output — byte-identical to the pre-neuter version)

$ .venv/bin/python -m pytest \
    tests/test_console_chat_projects_service.py tests/test_console_chat_projects_routes.py \
    -q --no-cov
55 passed in 14.56s
```

The same isolation discipline (explicit `user_sub` filter on every query) is repeated
identically across every service method (`list_chat_projects`, `upsert_chat_project`,
`delete_chat_project`), each with its own dedicated cross-user non-vacuity test in
`tests/test_console_chat_projects_service.py` and
`tests/test_console_chat_projects_routes.py::TestCrossUserIsolation` — this neuter-proof
exercises one representative guard end-to-end; the reviewer's own independent neuter pass
covers the rest.

## 4. Regression-safety — audittrace-opencode scope set unchanged

```
$ .venv/bin/python -m pytest tests/test_chart_drift_guards.py \
    -k "ChatProjects or LibrechatConsoleClient or Opencode" -q --no-cov
24 passed, 111 deselected
```

`TestOpencodeClientScopesUnchangedByChatProjects::test_opencode_scope_set_never_gains_chat_projects_scopes`
asserts, against BOTH realm files (`keycloak/realm-audittrace.json` and the rendered
`charts/audittrace/files/realm-audittrace.json`), that `audittrace-opencode`'s
`defaultClientScopes ∪ optionalClientScopes` never contains
`memory:chat_projects:read-own` or `memory:chat_projects:write` — the new scope pair is
additive on `audittrace-librechat` ONLY. `audittrace-opencode`'s client block in both
realm files is untouched by this change (`git diff` shows zero lines changed inside that
client's JSON object in either file). SC-09 (`audittrace-restricted`) is also untouched —
no edit in this WU touches that client's block in either realm file.

## 5. Frozen invariants — spot checks

- `/v1` byte-inviolate: confirmed in §2 (zero touched lines under any
  `/v1/chat/completions` path).
- `user_sub` token-derived at the choke, never caller-supplied: no request model in
  `audittrace.models` (`ConsoleChatProjectUpsertRequest`) declares a `user_sub`/`user_id`
  field — Pydantic's default `extra="ignore"` silently drops a hostile caller's attempt to
  supply one. Proven by
  `tests/test_console_chat_projects_routes.py::TestConsoleChatProjectsCrud::test_hostile_body_user_sub_is_ignored`.
- Traceability: RLS migration 026 mirrors migrations 022/023/024/025's shape (ENABLE +
  FORCE ROW LEVEL SECURITY + a `FOR ALL` policy comparing `user_sub` against
  `current_setting('app.current_user_id', true)`), guarded by `_is_postgres()` so SQLite
  unit tests exercise the service-layer explicit filter instead
  (feedback_unit_tests_miss_rls).
- Least privilege: `memory:chat_projects:read-own` and `memory:chat_projects:write` are
  two distinct scopes, neither implies the other — proven by
  `TestScopeEnforcement::test_write_scope_alone_cannot_read` /
  `test_read_scope_alone_cannot_write`.
- The existing `project` audit-namespace field in `src/audittrace/models.py` is untouched
  by this change (`git diff -- src/audittrace/models.py` shows only additive
  `ConsoleChatProject*` model insertions after the existing `ConsolePromptGroupListResponse`
  block — zero lines touched anywhere near the pre-existing `project` field).

## 6. Post-commit re-run (the release-bump-files transient resolves)

Run immediately after the WU-chat-projects commit (`c95d46e`) landed on this branch —
HEAD now equals the working tree, so the nested throwaway worktree the
`test_release_bump_files_ssot` test spawns checks out the SAME (committed) source the
shared editable `.venv` resolves to, and the spurious extra OpenAPI-regen diff
disappears (same mechanism confirmed at WU-1/WU-presets/WU-prompts):

```
$ .venv/bin/python -m pytest tests/test_release_bump_files_ssot.py -q --no-cov
2 passed in 4.75s
```

**Confirmed final run — full suite + per-file gate, post-commit (`c95d46e`), zero
failures:**

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.86%
4782 passed, 2 warnings in 653.83s (0:10:53)
🔒 Enforcing per-file coverage gate (each component >= 90%)...
per-file coverage gate: PASS (129 files checked, lines >= 90%, branches >= 90% on 110 file(s) with branches)
🚫 Enforcing zero-skip policy...
[no-skip-check] No skipped tests in junit.xml. Good.
✅ Tests passed
```

(The 2 warnings are pre-existing, unrelated `RuntimeWarning`s about an un-awaited
`_flush_pdf_manifest` coroutine in two PDF-manifest tests — present before this change,
not introduced by it.)

## 7. What this file does NOT claim

No image was built, no `helm upgrade` ran, no pod was hit through the public API with a
scoped JWT against a deployed image — Rule 2/3 (live E2E) is explicitly deferred to the
later fork chat-projects-shim WU per the ratified spec's own acceptance criteria.
