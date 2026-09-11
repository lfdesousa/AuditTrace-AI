# Evidence — WU-presets console-presets store, local capture (2026-09-11)

**Scope of this evidence file.** WU-presets of the MongoDB-elimination EPIC per
its ratified spec (`2026-09-11-SPEC-mongo-repl-wu-presets-store.md`, sha256
`d17dfcd7f0b45e8d714765e7166d75d7d9baa14a77e94fa53c5684f5647e45ad`) — explicit
"Acceptance (Rules 2 & 3 — live, deferred): The presets fork shim (later WU) +
deploy exercise this live." This file satisfies ADR-049 Rule 1 (Verification)
in full and gives a reconstructible Rule-3-shaped capture (neuter-proof of the
RLS/isolation guard, through the real FastAPI `create_app()` object via
`TestClient`, not mocked). It does **NOT** satisfy Rule 2 (Validation through
a deployed image + public API + scoped JWT) — that rides the later fork
preset-shim WU, mirroring the `wu1-console-conversations-store` precedent.

## 1. Full test-suite run (Rule 1 — Verification)

```
$ .venv/bin/python -m pytest -q
...
Required test coverage of 90% reached. Total coverage: 98.31%
FAILED tests/test_release_bump_files_ssot.py::test_make_release_dirties_exactly_the_ssot_set
1 failed, 4568 passed, 2 warnings in 640.87s (0:10:40)
```

The one failure (`test_make_release_dirties_exactly_the_ssot_set`) is the SAME
pre-existing transient documented in WU-1's own evidence capture
(`evidence/wu1-console-conversations-store-local-capture-2026-09-11.md` §1):
that test spawns a throwaway nested `git worktree` at `HEAD` but symlinks
THIS worktree's editable-installed `.venv`, so the nested worktree's `make
release` run imports the CURRENT (pre-commit) `src/audittrace` tree instead
of its own freshly-checked-out copy, producing a spurious extra OpenAPI-regen
diff. Confirmed pre-existing (not caused by this change) by running the same
test against the unmodified `origin/main` checkout at `4b2f9ba` — passes
there — and re-confirmed green in THIS worktree once the WU-presets commit
landed (HEAD == working tree, no uncommitted diff for the nested worktree's
shared venv to leak):

```
$ .venv/bin/python -m pytest tests/test_release_bump_files_ssot.py -q --no-cov
2 passed
```

(See §6 for the exact post-commit re-run this claim rests on.)

New-file coverage (line + branch), isolated run:

```
$ .venv/bin/python -m pytest tests/test_console_presets_service.py \
    tests/test_console_presets_routes.py \
    --cov=src/audittrace/services/console_presets \
    --cov=src/audittrace/routes/console_presets --cov-report=term-missing -q
src/audittrace/routes/console_presets.py     46    0    4    0   100%
src/audittrace/services/console_presets.py  163    0   32    0   100%
54 passed in 19.16s
```

100% lines AND branches on both new files — the abstract-interface + Mock +
Postgres CRUD/isolation/update-existing surface is fully covered by the 35
Mock/Postgres unit tests in `tests/test_console_presets_service.py` plus the
19 HTTP-route tests in `tests/test_console_presets_routes.py`.

`ruff check`, `ruff format --check`, and `make helm-lint` all green:

```
$ .venv/bin/ruff check .
All checks passed!
$ .venv/bin/ruff format --check bff/ src/audittrace/ tests/
315 files already formatted

$ make helm-lint
🪖  helm lint (vault.enabled=false)...
1 chart(s) linted, 0 chart(s) failed
🪖  helm template (vault.enabled=true) + vaultSecretFileGuard count...
✅ vaultSecretFileGuard present in 3 workloads
```

`mypy` clean on every new/touched file:

```
$ .venv/bin/mypy src/audittrace/services/console_presets.py \
    src/audittrace/routes/console_presets.py \
    src/audittrace/migrations/versions/024_create_console_presets.py \
    bff/console_presets_proxy.py bff/console_presets_scopes.py \
    src/audittrace/auth.py src/audittrace/dependencies.py \
    src/audittrace/models.py src/audittrace/server.py \
    src/audittrace/db/models.py bff/app.py bff/config.py
Success: no issues found in 12 source files
```

## 2. OpenAPI drift gate — additive-only confirmation

```
$ git diff --stat docs/reference/audittrace/openapi.yaml tests/fixtures/openapi.snapshot.yaml
 docs/reference/audittrace/openapi.yaml | 226 +++++++++++++++++++++++++++++++
 tests/fixtures/openapi.snapshot.yaml   | 226 +++++++++++++++++++++++++++++++
 2 files changed, 452 insertions(+)

$ git diff docs/reference/audittrace/openapi.yaml | grep -E "^[-+]\s+/v1"
(no output — /v1/chat/completions untouched)
```

226 pure insertions, zero deletions in each file — the new `/console/presets*`
paths + `ConsolePreset*` schemas + the two new `memory:presets:*` OAuth2 scope
entries. `/v1/chat/completions` is byte-for-byte unchanged.

## 3. Neuter-proof — the RLS/isolation guard fails RED when broken, restored to GREEN

Builder-side falsifiability pass (the independent reviewer re-runs this
mandate-to-fail check itself; this is the builder's own verification that the
guard is real before handing off).

Guard: `PostgresConsolePresetsService.get_preset`'s explicit
`.filter(ConsolePreset.user_sub == user_context.user_id)` clause
(`src/audittrace/services/console_presets.py`). Neutered by removing that one
`.filter(...)` call (kept the `preset_id` + `deleted_at_ms` filters, so the
ONLY thing removed is the per-user isolation clause).

```
$ .venv/bin/python -m pytest \
    tests/test_console_presets_service.py::TestPostgresConsolePresetsService::test_cross_user_isolation_denies_read \
    -q --no-cov
FAILED tests/test_console_presets_service.py::TestPostgresConsolePresetsService::test_cross_user_isolation_denies_read
AssertionError: user B read user A's preset — the isolation wall is broken (missing/neutered user_sub filter)
assert {'preset_id': 'secret-preset', 'user_sub': 'user-alice-rls', 'title': "alice's private preset", 'data': {}, ...} is None
1 failed in 0.48s

$ .venv/bin/python -m pytest \
    tests/test_console_presets_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_preset \
    -q --no-cov
FAILED tests/test_console_presets_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_preset
AssertionError: user B read user A's preset via the real HTTP route — the RLS/isolation wall is broken
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
$ diff /tmp/console_presets_backup.py src/audittrace/services/console_presets.py
(after restore: no output — byte-identical)

$ .venv/bin/python -m pytest \
    tests/test_console_presets_service.py::TestPostgresConsolePresetsService::test_cross_user_isolation_denies_read \
    tests/test_console_presets_routes.py::TestCrossUserIsolation::test_user_b_cannot_read_user_as_preset \
    -q --no-cov
2 passed in 6.31s
```

The same isolation discipline (explicit `user_sub` filter on every query) is
repeated identically across every service method (`list_presets`,
`upsert_preset`, `delete_preset`), each with its own dedicated cross-user
non-vacuity test in `tests/test_console_presets_service.py` and
`tests/test_console_presets_routes.py::TestCrossUserIsolation` — this
neuter-proof exercises one representative guard end-to-end; the reviewer's
own independent neuter pass covers the rest.

## 4. Regression-safety — audittrace-opencode scope set unchanged

```
$ .venv/bin/python -m pytest tests/test_chart_drift_guards.py \
    -k "Presets or LibrechatConsoleClient or Opencode" -q --no-cov
22 passed
```

`TestOpencodeClientScopesUnchangedByPresets::test_opencode_scope_set_never_gains_presets_scopes`
asserts, against BOTH realm files (`keycloak/realm-audittrace.json` and the
rendered `charts/audittrace/files/realm-audittrace.json`), that
`audittrace-opencode`'s `defaultClientScopes ∪ optionalClientScopes` never
contains `memory:presets:read-own` or `memory:presets:write` — the new scope
pair is additive on `audittrace-librechat` ONLY. `audittrace-opencode`'s
client block in both realm files is untouched by this change (`git diff`
shows zero lines changed inside that client's JSON object in either file).

## 5. Frozen invariants — spot checks

- `/v1` byte-inviolate: confirmed in §2 (zero touched lines under any
  `/v1/chat/completions` path).
- `user_sub` token-derived at the choke, never caller-supplied: no request
  model in `audittrace.models` (`ConsolePresetUpsertRequest`) declares a
  `user_sub`/`user_id` field — Pydantic's default `extra="ignore"` silently
  drops a hostile caller's attempt to supply one. Proven by
  `tests/test_console_presets_routes.py::TestConsolePresetsCrud::
  test_hostile_body_user_sub_is_ignored`.
- Traceability: RLS migration 024 mirrors migrations 022/023's shape (ENABLE +
  FORCE ROW LEVEL SECURITY + a `FOR ALL` policy comparing `user_sub` against
  `current_setting('app.current_user_id', true)`), guarded by `_is_postgres()`
  so SQLite unit tests exercise the service-layer explicit filter instead
  (feedback_unit_tests_miss_rls).
- Least privilege: `memory:presets:read-own` and `memory:presets:write` are
  two distinct scopes, neither implies the other — proven by
  `TestScopeEnforcement::test_write_scope_alone_cannot_read` /
  `test_read_scope_alone_cannot_write`.

## 6. Post-commit re-run (the release-bump-files transient resolves)

Run immediately after the WU-presets commit landed on this branch — HEAD now
equals the working tree, so the nested throwaway worktree the
`test_release_bump_files_ssot` test spawns checks out the SAME (committed)
source the shared editable `.venv` resolves to, and the spurious extra
OpenAPI-regen diff disappears:

```
$ .venv/bin/python -m pytest tests/test_release_bump_files_ssot.py -q --no-cov
2 passed
```

## 7. What this file does NOT claim

No image was built, no `helm upgrade` ran, no pod was hit through the public
API with a scoped JWT against a deployed image — Rule 2/3 (live E2E) is
explicitly deferred to the later fork preset-shim WU per the ratified spec's
own acceptance criteria.
