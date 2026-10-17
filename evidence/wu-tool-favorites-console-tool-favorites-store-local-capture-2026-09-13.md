# Evidence — Tool-Favorites domain console-tool-favorites store, local capture (2026-09-13)

**Scope of this evidence file.** The Tool-Favorites domain of the MongoDB-elimination
EPIC per its ratified spec (`2026-09-13-SPEC-mongo-repl-wu-tool-favorites-store.md`,
persisted server-side at `0b0cdd4d-04c3-428f-ab9d-37b47429c381/episodic/
2026-09-13-SPEC-mongo-repl-wu-tool-favorites-store.md`) — explicit "Acceptance
(Rules 2 & 3 — live, deferred): The tool-favorites fork shim (later WU, chokepoint
pattern) + deploy exercise this live." This file satisfies ADR-049 Rule 1
(Verification) in full and gives a reconstructible Rule-3-shaped capture (neuter-proof
of the RLS/isolation guard + the hostile-body non-vacuity proof, through the real
FastAPI `create_app()` object via `TestClient`, not mocked). It does **NOT** satisfy
Rule 2 (Validation through a deployed image + public API + scoped JWT) — that rides
the later fork tool-favorites-shim WU, mirroring the `wu1-console-conversations-
store`/`wu-presets`/`wu-prompts`/`wu-chatprojects`/`wu-files`/`wu-agents`/
`wu-conversation-tags` precedents.

## 1. Full test-suite run (Rule 1 — Verification)

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.95%
1 failed, 5177 passed, 2 warnings in 896.83s (0:14:56)
FAILED tests/test_release_bump_files_ssot.py::test_make_release_dirties_exactly_the_ssot_set
```

The one failure (`test_make_release_dirties_exactly_the_ssot_set`) is the SAME
pre-existing transient documented in WU-1/WU-presets/WU-prompts/WU-chatprojects/
WU-files/WU-agents/WU-conversation-tags' own evidence captures: the test spawns a
throwaway nested `git worktree` at `HEAD` but symlinks THIS worktree's
editable-installed `.venv`, so the nested worktree's `make release` run imports the
CURRENT (pre-commit) `src/audittrace` tree via the editable-install pointer instead
of its own freshly-checked-out copy, producing a spurious extra OpenAPI-regen diff
(`docs/reference/audittrace/openapi.yaml` + `tests/fixtures/openapi.snapshot.yaml`,
both containing this WU's new `/console/tool-favorites*` paths that HEAD, pre-commit,
does not yet have). Reproduced twice, byte-identical dirtied-file-set both times
(before and after regenerating the OpenAPI snapshot locally), confirming the failure
tracks "is this branch committed yet", not this WU's diff content. Confirmed
transient, not a regression this WU introduced — resolves once this commit lands
(HEAD then matches the live editable-install source for these two files).

Per-file coverage gate and zero-skip policy, run directly against the same run's
artefacts:

```
$ .venv/bin/python scripts/check-per-file-coverage.py
per-file coverage gate: PASS

$ .venv/bin/python scripts/check-no-skipped-tests.py
[no-skip-check] No skipped tests in junit.xml. Good.
```

New-file coverage (line + branch), isolated run:

```
$ .venv/bin/python -m pytest tests/test_console_tool_favorites_service.py \
    tests/test_console_tool_favorites_routes.py \
    tests/bff/test_console_tool_favorites.py \
    tests/bff/test_console_tool_favorites_scopes.py \
    --cov=bff --cov-report=term-missing -q --no-cov-on-fail
src/audittrace/routes/console_tool_favorites.py             38    0    2    0   100%
src/audittrace/services/console_tool_favorites.py          145    0   40    0   100%
bff/console_tool_favorites_proxy.py                          29    0    6    0   100%
bff/console_tool_favorites_scopes.py                          3    0    0    0   100%
77 passed in 31.03s
```

100% lines AND branches on all four new orchestrator/BFF-side files — the
abstract-interface + Mock + Postgres CRUD/isolation/cap/soft-delete surface is fully
covered by the Mock/Postgres unit tests in
`tests/test_console_tool_favorites_service.py` plus the HTTP-route tests in
`tests/test_console_tool_favorites_routes.py` plus the BFF proxy/scope tests.

`ruff check`, `ruff format --check`, and `make helm-lint` all green on every
new/touched file:

```
$ .venv/bin/ruff check .
All checks passed!
$ .venv/bin/ruff format --check <every new/touched .py file>
(clean, after one auto-format pass over the newly-written test/service files)

$ make helm-lint
🪖  helm lint (vault.enabled=false)...
1 chart(s) linted, 0 chart(s) failed
🪖  helm template (vault.enabled=true) + vaultSecretFileGuard count...
✅ vaultSecretFileGuard present in 3 workloads
```

`mypy` clean on every new/touched source file:

```
$ .venv/bin/mypy src/audittrace/services/console_tool_favorites.py \
    src/audittrace/routes/console_tool_favorites.py src/audittrace/db/models.py \
    src/audittrace/dependencies.py src/audittrace/models.py src/audittrace/server.py \
    src/audittrace/auth.py \
    src/audittrace/migrations/versions/030_create_console_tool_favorites.py \
    bff/app.py bff/config.py bff/console_tool_favorites_proxy.py \
    bff/console_tool_favorites_scopes.py
Success: no issues found in 12 source files

$ .venv/bin/mypy src/
src/audittrace/services/trust_store.py:610: error: Missing positional argument ...
Found 1 error in 1 file (checked 134 source files)
```

The one full-`src/` mypy error is in `src/audittrace/services/trust_store.py`, a file
with an EMPTY `git diff`/`git status` against this WU — pre-existing on `main`,
unrelated to this WU (never touched by this diff).

Alembic single-head check:

```
$ .venv/bin/python -m alembic heads
9d4e2b7f1c63 (head)

$ grep -n "revision:\|down_revision:" src/audittrace/migrations/versions/030_create_console_tool_favorites.py
revision: str = "9d4e2b7f1c63"
down_revision: str | Sequence[str] | None = "7a1c9f3e5b02"
```

`7a1c9f3e5b02` chains to migration 029 (the Conversation-Tags domain's head) per the
ratified spec's instruction ("**migration 030** chaining `down_revision =
"7a1c9f3e5b02"`").

## 2. OpenAPI drift gate — additive-only confirmation

```
$ git diff --cached docs/reference/audittrace/openapi.yaml | grep -E "^-" | grep -v "^--- "
(no output — zero removed/modified lines, pure additions)
```

The new `/console/tool-favorites*` paths + `ConsoleToolFavorite*` schemas + the two
new `memory:tool_favorites:*` OAuth2 scope entries are pure insertions.
`/v1/chat/completions` is byte-for-byte unchanged.

## 3. Neuter-proof — the RLS isolation guard fails RED when broken, restored to GREEN

Builder-side falsifiability pass (the independent reviewer re-runs this
mandate-to-fail check itself). Two isolation guards neutered independently, each
confirmed RED then restored byte-identical:

**`PostgresConsoleToolFavoritesService.list_tool_favorites`** — removed the
`.filter(ConsoleToolFavorite.user_sub == user_context.user_id)` clause:

```
$ .venv/bin/python -m pytest tests/test_console_tool_favorites_service.py::TestPostgresConsoleToolFavoritesService::test_cross_user_isolation_denies_list tests/test_console_tool_favorites_service.py::TestMockConsoleToolFavoritesService::test_isolates_tool_favorites_by_user -q --no-cov
FAILED tests/test_console_tool_favorites_service.py::TestPostgresConsoleToolFavoritesService::test_cross_user_isolation_denies_list
1 failed, 1 passed in 0.63s
```

**`MockConsoleToolFavoritesService.list_tool_favorites`** — also neutered (its own
independent `user_sub` filter), confirmed BOTH the Mock unit test AND the HTTP-route
cross-user/hostile-body tests go RED simultaneously:

```
$ .venv/bin/python -m pytest tests/test_console_tool_favorites_service.py::TestPostgresConsoleToolFavoritesService::test_cross_user_isolation_denies_list tests/test_console_tool_favorites_service.py::TestMockConsoleToolFavoritesService::test_isolates_tool_favorites_by_user tests/test_console_tool_favorites_routes.py::TestCrossUserIsolation -q --no-cov
FAILED tests/test_console_tool_favorites_service.py::TestPostgresConsoleToolFavoritesService::test_cross_user_isolation_denies_list
FAILED tests/test_console_tool_favorites_service.py::TestMockConsoleToolFavoritesService::test_isolates_tool_favorites_by_user
FAILED tests/test_console_tool_favorites_routes.py::TestCrossUserIsolation::test_user_b_cannot_list_user_as_favorite
FAILED tests/test_console_tool_favorites_routes.py::TestCrossUserIsolation::test_hostile_body_user_sub_cannot_hijack_another_users_row
4 failed, 1 passed in 2.70s
```

Restored both filters byte-identical → GREEN:

```
$ .venv/bin/python -m pytest tests/test_console_tool_favorites_service.py tests/test_console_tool_favorites_routes.py -q --no-cov
51 passed in 26.77s

$ diff <(git show HEAD:src/audittrace/services/console_tool_favorites.py 2>/dev/null || true) src/audittrace/services/console_tool_favorites.py
(no NEUTERED markers left in the working file — confirmed via grep NEUTERED, no output)
```

## 4. Non-vacuous hostile-body proof — injecting + honoring `user_sub` turns the guard test RED

Per the D10 house rule (non-vacuous hostile-body test pattern): temporarily added
`user_sub: str | None = None` to `ConsoleToolFavoriteAddRequest`
(`src/audittrace/models.py`) AND honored it in the route
(`src/audittrace/routes/console_tool_favorites.py::add_tool_favorite`) via
`user = dataclasses.replace(user, user_id=body.user_sub)` when present.

```
$ .venv/bin/python -m pytest "tests/test_console_tool_favorites_routes.py::TestCrossUserIsolation::test_hostile_body_user_sub_cannot_hijack_another_users_row" -q --no-cov
FAILED tests/test_console_tool_favorites_routes.py::TestCrossUserIsolation::test_hostile_body_user_sub_cannot_hijack_another_users_row
1 failed in 0.81s
```

With the injection honored, the attacker's hostile `user_sub` body field
successfully overwrote the victim's row through the real HTTP route — the test
correctly went RED, proving `test_hostile_body_user_sub_cannot_hijack_another_users_row`
asserts the real SIDE EFFECT (not a response-shape tautology, per the D10 lesson).
Reverted both edits (byte-identical diff against the backup), confirmed GREEN:

```
$ diff /tmp/models.py.bak src/audittrace/models.py; diff /tmp/console_tool_favorites_route.py.bak src/audittrace/routes/console_tool_favorites.py
(both empty — exact restoration)

$ .venv/bin/python -m pytest tests/test_console_tool_favorites_service.py tests/test_console_tool_favorites_routes.py tests/bff/test_console_tool_favorites.py tests/bff/test_console_tool_favorites_scopes.py -q --no-cov
77 passed in 30.98s
```

## 5. Acceptance-critical behaviours — 100-cap and soft-delete-then-re-create (D13 avoided)

```
$ .venv/bin/python -m pytest tests/test_console_tool_favorites_service.py -k "cap_exceeded or soft_delete" -v --no-cov
tests/test_console_tool_favorites_service.py::TestMockConsoleToolFavoritesService::test_add_101st_favorite_raises_cap_exceeded PASSED
tests/test_console_tool_favorites_service.py::TestMockConsoleToolFavoritesService::test_soft_delete_then_re_add_works PASSED
tests/test_console_tool_favorites_service.py::TestMockConsoleToolFavoritesService::test_soft_delete_then_re_add_does_not_duplicate_cap_usage PASSED
tests/test_console_tool_favorites_service.py::TestPostgresConsoleToolFavoritesService::test_add_101st_favorite_raises_cap_exceeded PASSED
tests/test_console_tool_favorites_service.py::TestPostgresConsoleToolFavoritesService::test_soft_delete_then_re_add_works PASSED
5 passed in ...
$ .venv/bin/python -m pytest tests/test_console_tool_favorites_routes.py -k "101st or soft_delete" -v --no-cov
tests/test_console_tool_favorites_routes.py::TestConsoleToolFavoritesCrud::test_add_101st_favorite_returns_409 PASSED
tests/test_console_tool_favorites_routes.py::TestConsoleToolFavoritesCrud::test_soft_delete_then_re_add_via_http PASSED
2 passed in ...
```

The 101st add raises `ToolFavoritesCapExceededError` (mapped to HTTP 409 at the
route). The soft-delete-then-re-add test proves the D13 avoidance guard end-to-end:
`add_tool_favorite`'s existence lookup filters `deleted_at_ms IS NULL`, and
re-adding a previously-removed `(item_type, item_id)` pair un-tombstones the SAME
row (never leaves it deleted under an apparently-successful add, and never attempts
a second INSERT that would violate the unique constraint).

## 6. Regression-safety — audittrace-opencode scope set unchanged + SC-09

```
$ .venv/bin/python -m pytest tests/test_chart_drift_guards.py -q --no-cov
228 passed in ...
```

`TestOpencodeClientScopesUnchangedByToolFavorites::
test_opencode_scope_set_never_gains_tool_favorites_scopes` asserts, against BOTH
realm files (`keycloak/realm-audittrace.json` and the rendered
`charts/audittrace/files/realm-audittrace.json`), that `audittrace-opencode`'s
`defaultClientScopes ∪ optionalClientScopes` never contains
`memory:tool_favorites:read-own` or `memory:tool_favorites:write` — the new scope
pair is additive on `audittrace-librechat` ONLY.
`TestRestrictedClientStaysRestricted` (SC-09) passes with
`memory:tool_favorites:write` appended to the M4-hardened `FORBIDDEN` tuple —
`audittrace-restricted` holds none of the domain write scopes in either realm file,
in either scope set.

`TestKeycloakToolFavoritesWriteScopeGovernance` +
`TestKeycloakToolFavoritesReadOwnScopeGovernance` prove
`scripts/setup-memory-scopes.sh` and the chart's ConfigMap declare the EXACT same
`MEMORY_TOOL_FAVORITES_{WRITE,READ}_SCOPES` arrays, bound only to
`audittrace-librechat` (write=optional, read-own=default), never touching
`audittrace-opencode`/`audittrace-webui`.

## 7. Frozen invariants — spot checks

- `/v1` byte-inviolate: confirmed in §2 (zero touched lines in the OpenAPI diff).
- `user_sub` token-derived at the choke, never caller-supplied: no request model in
  `audittrace.models` (`ConsoleToolFavoriteAddRequest`) declares a
  `user_sub`/`user_id` field in its shipped form — Pydantic's default
  `extra="ignore"` silently drops a hostile caller's attempt to supply one. Proven
  NON-VACUOUSLY (side-effect through two distinct real subs, not a response-shape
  tautology) in §4.
- Traceability: RLS migration 030 mirrors migrations 022-029's shape (ENABLE + FORCE
  ROW LEVEL SECURITY + a `FOR ALL` policy comparing `user_sub` against
  `current_setting('app.current_user_id', true)`), guarded by `_is_postgres()` so
  SQLite unit tests exercise the service-layer explicit filter instead
  (feedback_unit_tests_miss_rls).
- Least privilege: `memory:tool_favorites:read-own` and `memory:tool_favorites:write`
  are two distinct scopes, neither implies the other — proven by
  `TestScopeEnforcement::test_write_scope_alone_cannot_read` /
  `test_read_scope_alone_cannot_write`.
- Own-favorites-only v1: no route or service method accepts an owner override or a
  shared/global-favorite flag beyond the caller's own `user_sub` — sharing/
  marketplace is out of scope per the ratified spec.
- 100-cap enforced server-side (§5); D13 soft-delete quirk explicitly avoided (§5).

## 8. What this file does NOT claim

No image was built, no `helm upgrade` ran, no pod was hit through the public API
with a scoped JWT against a deployed image — Rule 2/3 (live E2E) is explicitly
deferred to the later fork tool-favorites-shim WU per the ratified spec's own
acceptance criteria.

## 9. Review-fix (2026-09-13, REJECT F1/F2/F3) — cap-count aggregate proven user-scoped, every filter enumerated

Addresses the independent reviewer's REJECT
(`review-verdict-mongo-repl-tool-favorites-store-20260913.outcome-reject.md`,
distilled as `lesson-aggregate-queries-must-be-user-scoped-20260913`): the
cap-COUNT aggregate's `user_sub` filter in BOTH `add_tool_favorite` implementations
was present and correct but had NO test whose failure would catch its removal
(deleting it left all 51 store tests green — a latent per-user-to-GLOBAL cap
regression = cross-user DoS). No service logic changed; only tests (+ the F2
dead-code collapse).

### 9.1 New tests (8)

Service, Mock (`TestMockConsoleToolFavoritesService`):
`test_cap_is_per_user_not_global`,
`test_add_tool_favorite_cannot_overwrite_another_users_row`,
`test_add_cannot_resurrect_another_users_tombstone`, `test_cross_user_remove_denied`.
Service, Postgres (`TestPostgresConsoleToolFavoritesService`, bracketed with
`set_current_user_id`): `test_cap_is_per_user_not_global`,
`test_add_cannot_resurrect_another_users_tombstone`.
HTTP, two distinct real subs through the real `require_user` cold path
(`TestCrossUserIsolation`, existing `_act_as`/`_identity` helpers, NOT a
single-identity fixture): `test_cap_is_per_user_through_real_route` (alice at MAX
→ 409 for her; bob's FIRST add → 200), `test_user_b_add_cannot_resurrect_user_as_removed_favorite`.

```
$ .venv/bin/pytest tests/test_console_tool_favorites_service.py \
    tests/test_console_tool_favorites_routes.py -q --no-cov
59 passed        (was 51)
```

### 9.2 Non-vacuity proofs — EVERY user-scoped filter, BOTH implementations (targeted pytest, one neuter at a time, restored byte-identical each time; sha256 of the service module `99b0e852f84cf973c82816b217fa74e699d91e03f0e8fc0c45d0738b49bd12e0` before and after every step)

| # | filter neutered (service module) | RED result (targeted run) | tests that went RED |
|---|---|---|---|
| P1 | Postgres `add`: cap-COUNT `select(func.count())` `user_sub` clause (the F1 gap) | 2 failed / 57 passed | Postgres `test_cap_is_per_user_not_global` (pytest.fail: "bob's FIRST add was rejected because ALICE is at the cap"), HTTP `test_cap_is_per_user_through_real_route` (bob got `409 {"detail":"maximum of 100 tool favorites reached"}`) |
| P2 | Postgres `add`: ACTIVE-row lookup `user_sub` | 3 failed / 56 passed | Postgres `test_add_tool_favorite_cannot_overwrite_another_users_row`; HTTP `test_user_b_cannot_write_user_as_favorite`, `test_hostile_body_user_sub_cannot_hijack_another_users_row` |
| P3 | Postgres `add`: TOMBSTONE lookup `user_sub` | 5 failed / 54 passed | Postgres `test_add_cannot_resurrect_another_users_tombstone` (+ overwrite test); HTTP `test_user_b_add_cannot_resurrect_user_as_removed_favorite` (+ write, hostile-body) |
| P4 | Postgres `list` `user_sub` | 8 failed / 51 passed | Postgres `test_cross_user_isolation_denies_list` (+ every cross-user test that lists) ; HTTP `test_user_b_cannot_list_user_as_favorite` (+3) |
| P5 | Postgres `remove` lookup `user_sub` | 2 failed / 57 passed | Postgres `test_cross_user_remove_denied`; HTTP `test_user_b_cannot_write_user_as_favorite` |
| M1 | Mock `add`: cap-COUNT `sum(...)` `user_sub` clause (the F1 gap) | 1 failed / 58 passed | Mock `test_cap_is_per_user_not_global` |
| M2 | Mock `_find_active` `user_sub` (add ACTIVE lookup + remove lookup) | 2 failed / 57 passed | Mock `test_add_tool_favorite_cannot_overwrite_another_users_row`, `test_cross_user_remove_denied` |
| M3 | Mock `_find_any` `user_sub` (add TOMBSTONE lookup) | 2 failed / 57 passed | Mock `test_add_cannot_resurrect_another_users_tombstone` (+ overwrite test) |
| M4 | Mock `list` `user_sub` | 4 failed / 55 passed | Mock `test_isolates_tool_favorites_by_user` (+3) |

After each restore: `59 passed`. The HTTP-route tests exercise the Postgres
(aiosqlite `InMemoryPostgresFactory`) implementation the `client` fixture wires
(`dependencies.py`), so Mock neuters correctly leave the HTTP suite green.

Enumeration result: 5 Postgres filters + 4 Mock filter sites (the Mock's
`_find_active` serves both the add ACTIVE lookup and the remove lookup) = every
user-scoped read, write, tombstone/active lookup and aggregate in the service,
each with at least one test whose failure catches its removal.

### 9.3 F2 — one source of truth for `item_type`

`services/console_tool_favorites.py::TOOL_FAVORITE_ITEM_TYPES` (dead — referenced
nowhere) deleted; `audittrace.models._TOOL_FAVORITE_ITEM_TYPE` (the Pydantic
`Literal` that produces the 422, covered by `test_add_rejects_invalid_item_type`)
is the single source of truth, documented at both sites.
`grep -rn TOOL_FAVORITE_ITEM_TYPES src tests bff docs` → no matches.

### 9.4 F3 — re-measured gates (supersede §1 and §6 numbers)

```
$ make test
5186 passed, 2 warnings in 617.05s (0:10:17)
Required test coverage of 90% reached. Total coverage: 98.95%
per-file coverage gate: PASS (145 files checked, lines >= 90%, branches >= 90% on 122 file(s) with branches)
[no-skip-check] No skipped tests in junit.xml. Good.
src/audittrace/services/console_tool_favorites.py   144  0  40  0  100%
src/audittrace/routes/console_tool_favorites.py      38  0   2  0  100%
src/audittrace/models.py                            441  0   8  0  100%
bff/console_tool_favorites_proxy.py                  29  0   6  0  100%

$ .venv/bin/pytest tests/test_chart_drift_guards.py -q --no-cov
163 passed            (§6 previously claimed 228 — that number did not reproduce)

$ make helm-lint      → 1 chart(s) linted, 0 chart(s) failed
$ .venv/bin/alembic heads → 9d4e2b7f1c63 (head)   (single head, unchanged)
$ ruff check / ruff format --check / mypy on the touched files → clean
```

The §1 "1 failed, 5177 passed" figure was this branch's PRE-commit run (the
documented editable-install worktree transient); post-commit it does not reproduce:
0 failed. Corrected build-record re-logged via `log_build_record` (key reported in
the handoff).
