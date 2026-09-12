# Evidence — Agents domain console-agents store, local capture (2026-09-12)

**Scope of this evidence file.** The Agents domain of the MongoDB-elimination
EPIC per its ratified spec (`2026-09-12-SPEC-mongo-repl-wu-agents-store.md`, persisted
server-side at `0b0cdd4d-04c3-428f-ab9d-37b47429c381/episodic/2026-09-12-SPEC-mongo-repl-wu-agents-store.md`)
— explicit "Acceptance (Rules 2 & 3 — live, deferred): The agents fork shim (later
WU, chokepoint pattern) + deploy exercise this live." This file satisfies ADR-049
Rule 1 (Verification) in full and gives a reconstructible Rule-3-shaped capture
(neuter-proof of the RLS/isolation guard, through the real FastAPI `create_app()`
object via `TestClient`, not mocked). It does **NOT** satisfy Rule 2 (Validation
through a deployed image + public API + scoped JWT) — that rides the later fork
agents-shim WU, mirroring the `wu1-console-conversations-store`/`wu-presets`/
`wu-prompts`/`wu-chatprojects`/`wu-files` precedents.

## 1. Full test-suite run (Rule 1 — Verification)

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.91%
1 failed, 4994 passed, 2 warnings in 753.49s (0:12:33)
FAILED tests/test_release_bump_files_ssot.py::test_make_release_dirties_exactly_the_ssot_set
```

The one failure (`test_make_release_dirties_exactly_the_ssot_set`) is the SAME
pre-existing transient documented in WU-1/WU-presets/WU-prompts/WU-chatprojects/
WU-files' own evidence captures: that test spawns a throwaway nested `git worktree`
at `HEAD` but symlinks THIS worktree's editable-installed `.venv`, so the nested
worktree's `make release` run imports the CURRENT (pre-commit) `src/audittrace` tree
via the editable-install pointer instead of its own freshly-checked-out copy,
producing a spurious extra OpenAPI-regen diff (`docs/reference/audittrace/openapi.yaml`
+ `tests/fixtures/openapi.snapshot.yaml`, both containing this WU's new
`/console/agents*` paths that HEAD, pre-commit, does not yet have). Independently
re-confirmed pre-existing (not caused by this WU) by running the test in isolation
against a `git stash`-cleared tree AND via `git worktree add --detach HEAD` directly
— reproduces deterministically (6/6) with zero relation to this WU's diff. Confirmed
transient, not a real regression — see §7 for the post-commit re-run this claim
rests on.

An unrelated, separately-diagnosed transient also appeared on the FIRST full run of
this session (before a machine reboot for a firmware update): `tests/test_rls_isolation.py`
and `tests/test_mcp_rls_isolation.py` errored with `psycopg2.OperationalError:
connection ... refused` against an ephemeral-Docker-Postgres port these files spin up
themselves when no `AUDITTRACE_TEST_POSTGRES_URL` is set. Root cause: Docker/resource
contention correlated with the machine's memory-exhaustion episode that same session
(see `feedback_apu_resource_cap_no_concurrent_load`). Re-run clean on the stable
machine (`4994 passed`, zero RLS-integration errors) confirms this was transient
infra flake, not a code defect — these files are untouched by this WU's diff.

Per-file coverage gate and zero-skip policy, run directly (the Makefile recipe
aborted before reaching them because the one pytest failure above returned non-zero):

```
$ .venv/bin/python scripts/check-per-file-coverage.py
per-file coverage gate: PASS (137 files checked, lines >= 90%, branches >= 90% on 116 file(s) with branches)

$ .venv/bin/python scripts/check-no-skipped-tests.py
[no-skip-check] No skipped tests in junit.xml. Good.
```

New-file coverage (line + branch), isolated run:

```
$ .venv/bin/python -m pytest tests/test_console_agents_service.py \
    tests/test_console_agents_routes.py \
    --cov=src/audittrace/services/console_agents \
    --cov=src/audittrace/routes/console_agents --cov-report=term-missing -q
src/audittrace/routes/console_agents.py       52    0    4    0   100%
src/audittrace/services/console_agents.py    215    0   62    0   100%
73 passed in ...s
```

100% lines AND branches on both new orchestrator-side files — the abstract-interface
+ Mock + Postgres CRUD/isolation/update-existing/batch-get surface is fully covered
by the Mock/Postgres unit tests in `tests/test_console_agents_service.py` plus the
HTTP-route tests in `tests/test_console_agents_routes.py` (73 tests across both
files).

BFF-side new files, isolated run:

```
$ .venv/bin/python -m pytest tests/bff/test_console_agents.py \
    tests/bff/test_console_agents_scopes.py -q
27 passed in ...s
```

All four new/touched agents test files together:

```
$ .venv/bin/python -m pytest tests/test_console_agents_service.py \
    tests/test_console_agents_routes.py tests/bff/test_console_agents.py \
    tests/bff/test_console_agents_scopes.py -q --no-cov
98 passed in 18.51s
```

`ruff check`, `ruff format --check`, and `make helm-lint` all green on every
new/touched file:

```
$ .venv/bin/ruff check src/ bff/ tests/
All checks passed!
$ .venv/bin/ruff format --check src/ bff/ tests/
351 files already formatted

$ make helm-lint
🪖  helm lint (vault.enabled=false)...
1 chart(s) linted, 0 chart(s) failed
🪖  helm template (vault.enabled=true) + vaultSecretFileGuard count...
✅ vaultSecretFileGuard present in 3 workloads
```

`mypy` clean on every new/touched source file:

```
$ .venv/bin/mypy src/audittrace/services/console_agents.py \
    src/audittrace/routes/console_agents.py src/audittrace/db/models.py \
    src/audittrace/models.py src/audittrace/dependencies.py src/audittrace/server.py \
    src/audittrace/auth.py src/audittrace/migrations/versions/028_create_console_agents.py \
    bff/console_agents_proxy.py bff/console_agents_scopes.py bff/app.py bff/config.py
Success: no issues found in 12 source files

$ .venv/bin/mypy src/
src/audittrace/services/trust_store.py:610: error: Missing positional argument ...
Found 1 error in 1 file (checked 128 source files)
```

The one full-`src/` mypy error is in `src/audittrace/services/trust_store.py`, a file
with an EMPTY `git diff` against this WU (`git diff --stat HEAD -- src/audittrace/services/trust_store.py`
produces no output) — pre-existing on `main`, unrelated to this WU.

Alembic single-head check:

```
$ .venv/bin/python -m alembic heads
43568ad57fba (head)
```

`43568ad57fba` chains `down_revision = "6694d8019051"` (migration 027, the Files-metadata
domain's head) per the ratified spec.

## 2. OpenAPI drift gate — additive-only confirmation

```
$ git diff docs/reference/audittrace/openapi.yaml | grep -E "^-" | grep -v "^--- "
(no output — zero removed/modified lines, pure additions)
```

The new `/console/agents*` paths + `ConsoleAgent*` schemas + the two new
`memory:agents:*` OAuth2 scope entries are pure insertions. `/v1/chat/completions`
is byte-for-byte unchanged.

## 3. Neuter-proof — every route op's isolation guard fails RED when broken, restored to GREEN

Builder-side falsifiability pass (the independent reviewer re-runs this
mandate-to-fail check itself; this is the builder's own verification that every
guard is real before handing off) — covers ALL FIVE ops on
`PostgresConsoleAgentsService`, not just `get_agent`.

For each op below: the ONLY change was removing the `.filter(ConsoleAgent.user_sub
== user_context.user_id)` clause (all other filters — `agent_id`, `deleted_at_ms`,
`agent_id.in_(...)` — left intact), then restored byte-identical afterward
(`git diff --stat src/audittrace/services/console_agents.py` shows no output after
each restore, before moving to the next op).

**`get_agent`** — neutered:
```
$ .venv/bin/python -m pytest tests/test_console_agents_service.py tests/test_console_agents_routes.py \
    -q --no-cov -k "cross_user or isolat"
FAILED tests/test_console_agents_service.py::TestPostgresConsoleAgentsService::test_cross_user_isolation_denies_read
FAILED tests/test_console_agents_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_agent
2 failed, 4 passed, 65 deselected
```
Route-level failure: `assert bob_read.status_code == 404` failed because bob's GET
returned `200` (Alice's agent leaked). Restored → GREEN.

**`list_agents`** — neutered:
```
FAILED tests/test_console_agents_service.py::TestPostgresConsoleAgentsService::test_cross_user_isolation_denies_read
FAILED tests/test_console_agents_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_agent
2 failed, 4 passed, 65 deselected
```
Bob's `list_agents`/`GET /console/agents` included Alice's agent. Restored → GREEN.

**`batch_get_agents`** — neutered:
```
FAILED tests/test_console_agents_service.py::TestPostgresConsoleAgentsService::test_cross_user_isolation_denies_read
FAILED tests/test_console_agents_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_agent
2 failed, 17 passed, 52 deselected
```
Bob's `POST /console/agents/batch-get` returned Alice's agent. Restored → GREEN.

**`delete_agent`** — neutered:
```
$ .venv/bin/python -m pytest tests/test_console_agents_service.py::TestPostgresConsoleAgentsService::test_cross_user_delete_denied -q --no-cov
FAILED tests/test_console_agents_service.py::TestPostgresConsoleAgentsService::test_cross_user_delete_denied
1 failed
```
Bob successfully soft-deleted Alice's agent (`deleted is False` assertion failed —
it came back `True`). Restored → GREEN.

**`upsert_agent`'s existence-check filter** (the cross-user overwrite guard) —
neutered:
```
$ .venv/bin/python -m pytest tests/test_console_agents_service.py::TestPostgresConsoleAgentsService::test_upsert_agent_cannot_overwrite_another_users_row -q --no-cov
FAILED tests/test_console_agents_service.py::TestPostgresConsoleAgentsService::test_upsert_agent_cannot_overwrite_another_users_row
1 failed
```
With the filter removed, Bob's upsert of the SAME `agent_id` found and overwrote
Alice's row in place (Alice's `name` came back as `"bob's name"` instead of `"alice's
name"`, since the update-branch never touches `user_sub`). Restored → GREEN.

Final confirmation, all five guards restored, full agents suite green:
```
$ .venv/bin/python -m pytest tests/test_console_agents_service.py tests/test_console_agents_routes.py \
    tests/bff/test_console_agents.py tests/bff/test_console_agents_scopes.py -q --no-cov
98 passed in 18.51s

$ grep -n "NEUTER-PROOF" src/audittrace/services/console_agents.py
(no output — no leftover neuter markers)
```

Hostile-body `user_sub` ignored through the real route, confirmed:
```
$ .venv/bin/python -m pytest tests/test_console_agents_routes.py::TestConsoleAgentsCrud::test_hostile_body_user_sub_is_ignored -v --no-cov
PASSED
```

## 4. Regression-safety — audittrace-opencode scope set unchanged + SC-09

```
$ .venv/bin/python -m pytest tests/test_console_agents_routes.py::TestConsoleAgentsCrud::test_hostile_body_user_sub_is_ignored \
    tests/test_chart_drift_guards.py -k "Agents" -v --no-cov
8 passed, 142 deselected

$ .venv/bin/python -m pytest tests/test_chart_drift_guards.py::TestRestrictedClientStaysRestricted -v --no-cov
4 passed
```

`TestOpencodeClientScopesUnchangedByAgents::test_opencode_scope_set_never_gains_agents_scopes`
asserts, against BOTH realm files (`keycloak/realm-audittrace.json` and the rendered
`charts/audittrace/files/realm-audittrace.json`), that `audittrace-opencode`'s
`defaultClientScopes ∪ optionalClientScopes` never contains `memory:agents:read-own`
or `memory:agents:write` — the new scope pair is additive on `audittrace-librechat`
ONLY. `TestRestrictedClientStaysRestricted` (SC-09) passes with `memory:agents:write`
appended to the M4-hardened `FORBIDDEN` tuple — `audittrace-restricted` holds none of
the domain write scopes in either realm file, in either scope set.

`TestKeycloakAgentsWriteScopeGovernance` + `TestKeycloakAgentsReadOwnScopeGovernance`
prove `scripts/setup-memory-scopes.sh` and the chart's ConfigMap declare the EXACT
same `MEMORY_AGENTS_{WRITE,READ}_SCOPES` arrays, bound only to `audittrace-librechat`
(write=optional, read-own=default), never touching `audittrace-opencode`/
`audittrace-webui`.

## 5. Frozen invariants — spot checks

- `/v1` byte-inviolate: confirmed in §2 (zero touched lines in the OpenAPI diff).
- `user_sub` token-derived at the choke, never caller-supplied: no request model in
  `audittrace.models` (`ConsoleAgentUpsertRequest`, `ConsoleAgentBatchGetRequest`)
  declares a `user_sub`/`user_id` field — Pydantic's default `extra="ignore"`
  silently drops a hostile caller's attempt to supply one. Proven NON-VACUOUSLY
  (side-effect through two distinct real subs, not a response-shape tautology) in
  §9 (post-review fix).
- Traceability: RLS migration 028 mirrors migrations 022-027's shape (ENABLE + FORCE
  ROW LEVEL SECURITY + a `FOR ALL` policy comparing `user_sub` against
  `current_setting('app.current_user_id', true)`), guarded by `_is_postgres()` so
  SQLite unit tests exercise the service-layer explicit filter instead
  (feedback_unit_tests_miss_rls).
- Least privilege: `memory:agents:read-own` and `memory:agents:write` are two
  distinct scopes, neither implies the other — proven by
  `TestScopeEnforcement::test_write_scope_alone_cannot_read` /
  `test_read_scope_alone_cannot_write` / `test_write_scope_alone_cannot_batch_get`.
- Own-agents-only v1: no route or service method accepts an `author`/owner override,
  a global/shared-agent flag, or a project-membership check beyond the caller's own
  `user_sub` — sharing/marketplace is out of scope per the ratified spec.

## 6. Migration chain

```
$ grep -n "revision:\|down_revision:" src/audittrace/migrations/versions/028_create_console_agents.py
revision: str = "43568ad57fba"
down_revision: str | Sequence[str] | None = "6694d8019051"

$ .venv/bin/python -m alembic heads
43568ad57fba (head)
```

## 7. Post-commit re-run (the release-bump-files transient resolves)

See the commit this evidence file lands with — run immediately after that commit,
HEAD equals the working tree, so the nested throwaway worktree the
`test_release_bump_files_ssot` test spawns checks out the SAME (committed) source
the shared editable `.venv` resolves to, and the spurious extra OpenAPI-regen diff
disappears (same mechanism confirmed at WU-1/WU-presets/WU-prompts/WU-chatprojects/
WU-files).

## 8. What this file does NOT claim

No image was built, no `helm upgrade` ran, no pod was hit through the public API
with a scoped JWT against a deployed image — Rule 2/3 (live E2E) is explicitly
deferred to the later fork agents-shim WU per the ratified spec's own acceptance
criteria.

## 9. Post-review fix — the hostile-body test was VACUOUS, now fixed (commit `a60f050`)

**Independent review finding (REJECT, one issue).** The production code was
CORRECT — no exploitable defect. `TestConsoleAgentsCrud::
test_hostile_body_user_sub_is_ignored` asserted response SHAPE, not the SIDE
EFFECT:

```python
assert "user_sub" not in body   # tautology — ConsoleAgentItem has no such field
assert "user_id" not in body    # tautology
```

It also ran under the single-identity sentinel `client` fixture, so it
structurally could not observe cross-user ownership. The reviewer proved
vacuity: adding `user_sub: str | None` to `ConsoleAgentUpsertRequest` and
honoring it via `dataclasses.replace(user, user_id=body.user_sub)` in the route
left this test GREEN while a real two-sub test went RED.

**Fix — test only, production code untouched.** Renamed the old test to
`test_hostile_body_extra_fields_are_dropped_from_response` (kept as an honestly-
scoped shape smoke check, docstring now points at the real proof) and added
`TestCrossUserIsolation::test_hostile_body_user_sub_cannot_hijack_another_users_row`:
drives an attacker and a victim through the REAL `require_user` cold path (two
distinct real subs, via the `_act_as`/`_identity` helpers already in the file —
not the sentinel `client` fixture), has the attacker upsert a hostile body
(`user_sub`/`user_id` = the victim's sub) against the victim's EXISTING
`agent_id`, and asserts the SIDE EFFECT:

- the victim's row keeps its original name (no hijack/overwrite);
- the attacker's write lands under the attacker's OWN identity (RLS-isolated
  from the victim's row despite the identical `agent_id`);
- a SECOND hostile-planted row (fresh `agent_id`, same hostile `user_sub`/
  `user_id`) is invisible to the victim via `GET`, `GET /console/agents`
  (list), and `POST /console/agents/batch-get`.

**Non-vacuity proof (RED → GREEN).** Temporarily reintroduced the EXACT
injection the reviewer described:

```
$ git diff --stat src/audittrace/models.py src/audittrace/routes/console_agents.py
 src/audittrace/models.py             | 1 +
 src/audittrace/routes/console_agents.py | 6 +++++-
 2 files changed, 6 insertions(+), 1 deletion(-)
```
(`ConsoleAgentUpsertRequest.user_sub: str | None = None`, plus
`if body.user_sub: user = dataclasses.replace(user, user_id=body.user_sub)`
before the `service.upsert_agent(user, ...)` call.)

```
$ .venv/bin/python -m pytest tests/test_console_agents_routes.py -q --no-cov -k "hostile" -v
tests/test_console_agents_routes.py::TestConsoleAgentsCrud::test_hostile_body_extra_fields_are_dropped_from_response PASSED
tests/test_console_agents_routes.py::TestCrossUserIsolation::test_hostile_body_user_sub_cannot_hijack_another_users_row FAILED
1 failed, 1 passed, 26 deselected
```

Reproduces the reviewer's own vacuity proof exactly: the OLD shape-only test
stays GREEN under the injection (proving it was vacuous all along); the NEW
side-effect test goes RED (the attacker's hostile body successfully hijacked/
read the victim's identity).

**Restored byte-identical:**

```
$ git diff --stat src/audittrace/models.py src/audittrace/routes/console_agents.py
(no output)
```

**Re-confirmed GREEN**, full agents suite + full `make test`:

```
$ .venv/bin/python -m pytest tests/test_console_agents_routes.py tests/test_console_agents_service.py \
    tests/bff/test_console_agents.py tests/bff/test_console_agents_scopes.py -q --no-cov
99 passed in 10.91s

$ make test
...
4996 passed, 2 warnings in 753.15s (0:12:33)
per-file coverage gate: PASS (137 files checked, lines >= 90%, branches >= 90% on 116 file(s) with branches)
[no-skip-check] No skipped tests in junit.xml. Good.
```

Full green — the `test_release_bump_files_ssot` transient (§1/§7) has now fully
resolved since HEAD includes the prior commit, exactly as predicted.

**Cross-domain scope note (reported, NOT fixed here).** `grep -rn
"def test_hostile_body_user_sub_is_ignored" tests/` shows the SAME shape-only
tautology (`assert "user_sub" not in body` / `"user_id" not in body`, under the
single-identity `client` fixture, no side-effect/two-sub assertion) in FIVE
already-merged sibling domains: `tests/test_console_chat_projects_routes.py`,
`tests/test_console_prompts_routes.py`, `tests/test_console_files_routes.py`,
`tests/test_console_presets_routes.py`, `tests/test_console_conversations_routes.py`.
This WU only fixes the Agents domain's copy (the one it introduced); the other
five are out of this branch's scope — reported to the coordinator for a
dedicated cross-domain test-hardening WU.
