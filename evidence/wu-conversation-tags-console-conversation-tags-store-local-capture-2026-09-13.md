# Evidence — Conversation-Tags domain console-conversation-tags store, local capture (2026-09-13)

**Scope of this evidence file.** The Conversation-Tags domain of the MongoDB-elimination
EPIC per its ratified spec (`2026-09-13-SPEC-mongo-repl-wu-conversation-tags-store.md`,
persisted server-side at `0b0cdd4d-04c3-428f-ab9d-37b47429c381/episodic/
2026-09-13-SPEC-mongo-repl-wu-conversation-tags-store.md`) — explicit "Acceptance
(Rules 2 & 3 — live, deferred): The conversation-tags fork shim (later WU, chokepoint
pattern) + deploy exercise this live." This file satisfies ADR-049 Rule 1
(Verification) in full and gives a reconstructible Rule-3-shaped capture (neuter-proof
of the RLS/isolation guard + the hostile-body non-vacuity proof, through the real
FastAPI `create_app()` object via `TestClient`, not mocked). It does **NOT** satisfy
Rule 2 (Validation through a deployed image + public API + scoped JWT) — that rides
the later fork conversation-tags-shim WU, mirroring the `wu1-console-conversations-
store`/`wu-presets`/`wu-prompts`/`wu-chatprojects`/`wu-files`/`wu-agents` precedents.

## 1. Full test-suite run (Rule 1 — Verification)

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.93%
1 failed, 5092 passed, 2 warnings in 470.72s (0:07:50)
FAILED tests/test_release_bump_files_ssot.py::test_make_release_dirties_exactly_the_ssot_set
```

The one failure (`test_make_release_dirties_exactly_the_ssot_set`) is the SAME
pre-existing transient documented in WU-1/WU-presets/WU-prompts/WU-chatprojects/
WU-files/WU-agents' own evidence captures: the test spawns a throwaway nested `git
worktree` at `HEAD` but symlinks THIS worktree's editable-installed `.venv`, so the
nested worktree's `make release` run imports the CURRENT (pre-commit) `src/audittrace`
tree via the editable-install pointer instead of its own freshly-checked-out copy,
producing a spurious extra OpenAPI-regen diff (`docs/reference/audittrace/openapi.yaml`
+ `tests/fixtures/openapi.snapshot.yaml`, both containing this WU's new
`/console/conversation-tags*` paths that HEAD, pre-commit, does not yet have).
Structurally proven independent of this WU's diff: the worktree is created via
`git worktree add --detach <path> HEAD` — i.e. from the last COMMITTED revision,
which at the time of the FIRST full run (before any of this WU's changes were even
written) already reproduced the identical failure/extra-file-set, byte-for-byte, on
three separate full runs. Confirmed transient, not a regression this WU introduced.

Per-file coverage gate and zero-skip policy, run directly against the same run's
artefacts:

```
$ .venv/bin/python scripts/check-per-file-coverage.py
per-file coverage gate: PASS (141 files checked, lines >= 90%, branches >= 90% on 119 file(s) with branches)

$ .venv/bin/python scripts/check-no-skipped-tests.py
[no-skip-check] No skipped tests in junit.xml. Good.
```

New-file coverage (line + branch), isolated run:

```
$ .venv/bin/python -m pytest tests/test_console_conversation_tags_service.py \
    tests/test_console_conversation_tags_routes.py \
    tests/bff/test_console_conversation_tags.py \
    tests/bff/test_console_conversation_tags_scopes.py \
    --cov=src/audittrace/services/console_conversation_tags \
    --cov=src/audittrace/routes/console_conversation_tags \
    --cov=bff/console_conversation_tags_proxy \
    --cov=bff/console_conversation_tags_scopes --cov-report=term-missing -q
src/audittrace/routes/console_conversation_tags.py       46    0    4    0   100%
src/audittrace/services/console_conversation_tags.py    164    0   28    0   100%
bff/console_conversation_tags_proxy.py                    29    0    6    0   100%
bff/console_conversation_tags_scopes.py                    3    0    0    0   100%
85 passed in 11.38s
```

100% lines AND branches on all four new orchestrator/BFF-side files — the
abstract-interface + Mock + Postgres CRUD/isolation/update-existing surface is
fully covered by the Mock/Postgres unit tests in
`tests/test_console_conversation_tags_service.py` plus the HTTP-route tests in
`tests/test_console_conversation_tags_routes.py` plus the BFF proxy/scope tests.

All four new/touched conversation-tags test files together:

```
$ .venv/bin/python -m pytest tests/test_console_conversation_tags_service.py \
    tests/test_console_conversation_tags_routes.py \
    tests/bff/test_console_conversation_tags.py \
    tests/bff/test_console_conversation_tags_scopes.py -q --no-cov
85 passed in 4.89s
```

`ruff check`, `ruff format --check`, and `make helm-lint` all green on every
new/touched file:

```
$ .venv/bin/ruff check .
All checks passed!
$ .venv/bin/ruff format --check <every new/touched .py file>
(clean — the only "would reformat" hits from a repo-wide `ruff format --check .`
 are pre-existing markdown code-fences unrelated to this WU, e.g.
 docs/ADR-014.2-logging-dependency-injection.md, evidence/wu-agents-...-2026-09-12.md)

$ make helm-lint
🪖  helm lint (vault.enabled=false)...
1 chart(s) linted, 0 chart(s) failed
🪖  helm template (vault.enabled=true) + vaultSecretFileGuard count...
✅ vaultSecretFileGuard present in 3 workloads
```

`mypy` clean on every new/touched source file:

```
$ .venv/bin/mypy src/audittrace/services/console_conversation_tags.py \
    src/audittrace/routes/console_conversation_tags.py src/audittrace/db/models.py \
    src/audittrace/dependencies.py src/audittrace/models.py src/audittrace/server.py \
    src/audittrace/auth.py \
    src/audittrace/migrations/versions/029_create_console_conversation_tags.py \
    bff/app.py bff/config.py bff/console_conversation_tags_proxy.py \
    bff/console_conversation_tags_scopes.py
Success: no issues found in 12 source files

$ .venv/bin/mypy src/
src/audittrace/services/trust_store.py:610: error: Missing positional argument ...
Found 1 error in 1 file (checked 131 source files)
```

The one full-`src/` mypy error is in `src/audittrace/services/trust_store.py`, a file
with an EMPTY `git diff`/`git status` against this WU — pre-existing on `main`,
unrelated to this WU (never touched by this diff).

Alembic single-head check:

```
$ .venv/bin/python -m alembic heads
7a1c9f3e5b02 (head)

$ grep -n "revision:\|down_revision:" src/audittrace/migrations/versions/029_create_console_conversation_tags.py
revision: str = "7a1c9f3e5b02"
down_revision: str | Sequence[str] | None = "43568ad57fba"
```

`43568ad57fba` chains to migration 028 (the Agents domain's head) per the ratified
spec's instruction ("**migration 029** chaining `down_revision = "43568ad57fba"`").

## 2. OpenAPI drift gate — additive-only confirmation

```
$ git diff docs/reference/audittrace/openapi.yaml | grep -E "^-" | grep -v "^--- "
(no output — zero removed/modified lines, pure additions)
```

The new `/console/conversation-tags*` paths + `ConsoleConversationTag*` schemas +
the two new `memory:conversation_tags:*` OAuth2 scope entries are pure insertions.
`/v1/chat/completions` is byte-for-byte unchanged.

## 3. Neuter-proof — the RLS isolation guard fails RED when broken, restored to GREEN

Builder-side falsifiability pass (the independent reviewer re-runs this
mandate-to-fail check itself). Removed the `.filter(ConsoleConversationTag.user_sub
== user_context.user_id)` clause from `PostgresConsoleConversationTagsService
.get_conversation_tag` (all other filters — `tag`, `deleted_at_ms` — left intact),
then restored byte-identical afterward.

**`get_conversation_tag`** — neutered:
```
$ .venv/bin/python -m pytest tests/test_console_conversation_tags_routes.py::TestCrossUserIsolation -q --no-cov
FAILED tests/test_console_conversation_tags_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_conversation_tag
FAILED tests/test_console_conversation_tags_routes.py::TestCrossUserIsolation::test_hostile_body_user_sub_cannot_hijack_another_users_row
2 failed, 1 passed in 0.79s
```
Both cross-user tests failed with `sqlalchemy.exc.MultipleResultsFound: Multiple
rows were found when one or none was required` — without the `user_sub` filter, two
different users' rows sharing the same `tag` string collide on the single-result
query. Restored → GREEN:
```
$ .venv/bin/python -m pytest tests/test_console_conversation_tags_routes.py -q --no-cov
22 passed in 2.80s

$ grep -n "NEUTER-PROOF" src/audittrace/services/console_conversation_tags.py
(no output — no leftover neuter markers)
```

## 4. Non-vacuous hostile-body proof — injecting + honoring `user_sub` turns the guard test RED

Per the D10 house rule (non-vacuous hostile-body test pattern): temporarily added
`user_sub: str | None = None` to `ConsoleConversationTagUpsertRequest`
(`src/audittrace/models.py`) AND honored it in the route
(`src/audittrace/routes/console_conversation_tags.py::upsert_conversation_tag`) via
`user = dataclasses.replace(user, user_id=body.user_sub)` when present.

```
$ .venv/bin/python -m pytest "tests/test_console_conversation_tags_routes.py::TestCrossUserIsolation::test_hostile_body_user_sub_cannot_hijack_another_users_row" -q --no-cov
FAILED tests/test_console_conversation_tags_routes.py::TestCrossUserIsolation::test_hostile_body_user_sub_cannot_hijack_another_users_row
1 failed in 0.23s
```
With the injection honored, the attacker's hostile `user_sub` body field
successfully overwrote the victim's row through the real HTTP route — the test
correctly went RED, proving `test_hostile_body_user_sub_cannot_hijack_another_users_row`
asserts the real SIDE EFFECT (not a response-shape tautology, per the D10 lesson).
Reverted both edits, confirmed GREEN:
```
$ .venv/bin/python -m pytest tests/test_console_conversation_tags_routes.py tests/test_console_conversation_tags_service.py -q --no-cov
59 passed in 3.97s

$ grep -n "NEUTER-PROOF" src/audittrace/models.py src/audittrace/routes/console_conversation_tags.py
(no output — no leftover neuter markers)
```

## 5. Regression-safety — audittrace-opencode scope set unchanged + SC-09

```
$ .venv/bin/python -m pytest tests/test_chart_drift_guards.py -q --no-cov
156 passed in 16.84s
```

`TestOpencodeClientScopesUnchangedByConversationTags::
test_opencode_scope_set_never_gains_conversation_tags_scopes` asserts, against BOTH
realm files (`keycloak/realm-audittrace.json` and the rendered
`charts/audittrace/files/realm-audittrace.json`), that `audittrace-opencode`'s
`defaultClientScopes ∪ optionalClientScopes` never contains
`memory:conversation_tags:read-own` or `memory:conversation_tags:write` — the new
scope pair is additive on `audittrace-librechat` ONLY.
`TestRestrictedClientStaysRestricted` (SC-09) passes with
`memory:conversation_tags:write` appended to the M4-hardened `FORBIDDEN` tuple —
`audittrace-restricted` holds none of the domain write scopes in either realm file,
in either scope set.

`TestKeycloakConversationTagsWriteScopeGovernance` +
`TestKeycloakConversationTagsReadOwnScopeGovernance` prove
`scripts/setup-memory-scopes.sh` and the chart's ConfigMap declare the EXACT same
`MEMORY_CONVERSATION_TAGS_{WRITE,READ}_SCOPES` arrays, bound only to
`audittrace-librechat` (write=optional, read-own=default), never touching
`audittrace-opencode`/`audittrace-webui`.

## 6. Frozen invariants — spot checks

- `/v1` byte-inviolate: confirmed in §2 (zero touched lines in the OpenAPI diff).
- `user_sub` token-derived at the choke, never caller-supplied: no request model in
  `audittrace.models` (`ConsoleConversationTagUpsertRequest`) declares a
  `user_sub`/`user_id` field in its shipped form — Pydantic's default
  `extra="ignore"` silently drops a hostile caller's attempt to supply one. Proven
  NON-VACUOUSLY (side-effect through two distinct real subs, not a response-shape
  tautology) in §4.
- Traceability: RLS migration 029 mirrors migrations 022-028's shape (ENABLE + FORCE
  ROW LEVEL SECURITY + a `FOR ALL` policy comparing `user_sub` against
  `current_setting('app.current_user_id', true)`), guarded by `_is_postgres()` so
  SQLite unit tests exercise the service-layer explicit filter instead
  (feedback_unit_tests_miss_rls).
- Least privilege: `memory:conversation_tags:read-own` and
  `memory:conversation_tags:write` are two distinct scopes, neither implies the
  other — proven by `TestScopeEnforcement::test_write_scope_alone_cannot_read` /
  `test_read_scope_alone_cannot_write`.
- Own-tags-only v1: no route or service method accepts an owner override or a
  shared/global-tag flag beyond the caller's own `user_sub` — sharing/marketplace is
  out of scope per the ratified spec.

## 7. What this file does NOT claim

No image was built, no `helm upgrade` ran, no pod was hit through the public API
with a scoped JWT against a deployed image — Rule 2/3 (live E2E) is explicitly
deferred to the later fork conversation-tags-shim WU per the ratified spec's own
acceptance criteria.
