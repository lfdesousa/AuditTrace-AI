# Evidence — WU-4 promote session->durable ("keep this"), local capture (2026-09-05)

**Scope of this evidence file.** This is a **LOCAL-only** work unit per its
ratified spec (`2026-09-05-SPEC-wu4-promote-session-to-durable.md`,
sha256 `810a40705ec84b0d889a9bb916294f2c85c40daa438bc1bb6cfd5b3a8563523b`) —
`through_the_loop: audittrace-builder -> audittrace-reviewer (LOCAL gates
only; NO deploy)` and explicit "Out of scope: ... Retention/GC of ephemeral
session docs + live front-door E2E (WU-6) — where WU-1..4 get live
evidence." This file satisfies ADR-049 Rule 1 (Verification) in full and
gives a reconstructible Rule-3-shaped capture (neuter-proof of every new
guard, through the real FastAPI `create_app()` object, not mocked). It
explicitly does **NOT** satisfy Rule 2 (Validation through a deployed
image + public API + scoped JWT) — that is deferred to WU-6 per the spec,
mirroring the `wu1-session-layer-narrow-ingest-scope-local-capture-
2026-09-04.md` precedent.

## 1. Full test-suite run (Rule 1 — Verification)

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.70%
4216 passed, 2 warnings in 449.55s (0:07:29)
🔒 Enforcing per-file coverage gate (each component >= 90%)...
per-file coverage gate: PASS (104 files checked, lines >= 90%, branches >= 90% on 93 file(s) with branches)
🚫 Enforcing zero-skip policy...
[no-skip-check] No skipped tests in junit.xml. Good.
✅ Tests passed
```

New-file coverage (line + branch), from the isolated run:

```
src/audittrace/routes/memory_promote.py    106      0     16      0   100%
bff/console_promote_scopes.py                6      0      2      0   100%
bff/app.py                                 135      0      6      0   100%
bff/config.py                               42      0      2      0   100%
```

`make lint` (ruff check + ruff format + offline semgrep security-lint) and
`make helm-lint` both green:

```
$ make lint
...
Ran 2 rules on 158 files: 0 findings.
✅ Security-lint passed
All checks passed!
✅ Linting passed
287 files already formatted
✅ Formatting passed

$ make helm-lint
🪖  helm lint (vault.enabled=false)...
1 chart(s) linted, 0 chart(s) failed
🪖  helm template (vault.enabled=true) + vaultSecretFileGuard count...
✅ vaultSecretFileGuard present in 3 workloads
```

(No chart diff — expected; the route ships inside the existing
orchestrator + BFF images, per the spec's "NO realm change ... NO deploy".)

`mypy` clean on every new/touched file:

```
$ .venv/bin/mypy src/audittrace/routes/memory_promote.py src/audittrace/routes/memory.py \
    src/audittrace/server.py bff/console_promote_scopes.py bff/config.py bff/app.py \
    tests/test_memory_promote_route.py tests/bff/test_console_files_promote.py \
    tests/bff/test_console_promote_scopes.py tests/bff/test_config.py
Success: no issues found in 10 source files
```

## 2. OpenAPI drift gate — additive-only confirmation

```
$ git diff --stat docs/reference/audittrace/openapi.yaml tests/fixtures/openapi.snapshot.yaml
 docs/reference/audittrace/openapi.yaml | 71 ++++++++++++++++++++++++++++++++++
 tests/fixtures/openapi.snapshot.yaml   | 71 ++++++++++++++++++++++++++++++++++
 2 files changed, 142 insertions(+)
```

Only one new block added: `/memory/promote` (POST), with the OR-scope
security requirement (`memory:episodic:write` | `memory:semantic:write` |
`audittrace:admin`). `/v1/chat/completions` is byte-for-byte unchanged —
confirmed via `git diff docs/reference/audittrace/openapi.yaml | grep -iE
"chat|completions|/v1"` returning no output.

## 3. Neuter-proof — every new guard fails RED when broken, restored to GREEN

Builder-side falsifiability pass (the independent reviewer re-runs this
mandate-to-fail check itself; this is the builder's own verification that
each guard is real before handing off). Every neuter below was applied
directly to the working file, confirmed RED, then restored from a
byte-identical backup (`diff <backup> <file>` → no output, confirmed after
each restore) before the final `make test` run (§1) was captured.

### 3a. Durable scope gate — session-only token MUST 403

Guard: `_require_durable_write_scope` in `src/audittrace/routes/
memory_promote.py`. Neutered to also accept `memory:session:write`.

```
$ pytest tests/test_memory_promote_route.py::TestPromoteScopeGate::test_session_only_token_cannot_promote -q --no-cov
FAILED ... assert 200 == 403
1 failed in 0.21s

# restored:
$ pytest tests/test_memory_promote_route.py::TestPromoteScopeGate -q --no-cov
5 passed in 0.37s
```

### 3b. Ownership (cross-user / nonexistent doc) — 404

Guard: the `if doc is None: raise HTTPException(404, ...)` branch in
`promote_session_to_durable`. Neutered to fabricate a placeholder
`Document` instead of raising.

```
$ pytest tests/test_memory_promote_route.py::TestPromoteOwnership -q --no-cov
FAILED test_promote_nonexistent_filename_404 - assert 200 == 404
FAILED test_cross_user_promote_404 - assert 200 == 404
2 failed, 1 passed in 0.32s

# restored:
$ pytest tests/test_memory_promote_route.py::TestPromoteOwnership -q --no-cov
3 passed in 0.27s
```

### 3c. Provenance token-derived, never caller-supplied

Guard: `promoted_by = user.user_id` in `promote_session_to_durable`.
Neutered to `payload.get("promoted_by") or user.user_id`.

```
$ pytest tests/test_memory_promote_route.py::TestPromoteProvenance -q --no-cov
FAILED test_provenance_from_token_not_caller_field - AssertionError: assert 'mallory' == 'alice'
1 failed, 1 passed in 0.26s

# restored:
$ pytest tests/test_memory_promote_route.py::TestPromoteProvenance -q --no-cov
2 passed in 0.21s
```

### 3d. Copy, not move — the session row survives promote

Guard: the ABSENCE of any delete/GC call on the session row. Neutered by
inserting a raw `DELETE FROM session_memory_items WHERE user_id=... AND
filename=...` immediately after `read_own` (simulating an accidental
move-semantics regression against the real `PostgresSessionMemoryService`
backing, not the mock — the mock has no shared `_items` state visible
across the read-back call, so the neuter targets the actual Postgres path
the DI container wires in tests, same as production).

```
$ pytest tests/test_memory_promote_route.py::TestPromoteCopyNotMove -q --no-cov
FAILED test_session_doc_still_exists_after_promote - assert None is not None
1 failed in 0.19s

# restored:
$ pytest tests/test_memory_promote_route.py -q --no-cov
31 passed in 2.30s
```

### 3e. target_layer validation — durable set only

Guard: `_DURABLE_PROMOTE_LAYERS = frozenset({"episodic", "semantic"})`.
Widened to include `"session"` and `"conversational"`.

```
$ pytest tests/test_memory_promote_route.py::TestPromoteTargetLayerValidation -q --no-cov
FAILED test_target_layer_session_rejected_422 - assert 200 == 422
FAILED test_target_layer_conversational_rejected_422 - assert 404 == 422
2 failed, 3 passed in 0.41s

# restored:
$ pytest tests/test_memory_promote_route.py -q --no-cov
31 passed in 2.20s
```

### 3f. BFF exact durable scope (never session, never the broad set)

Guard: `requested_scope = promote_scope_string_for_layer(settings.
console_promote_default_layer)` in `bff/app.py::console_files_promote`.
Neutered to `requested_scope = INGEST_SCOPE_STRING` (the WU-2 session
scope).

```
$ pytest tests/bff/test_console_files_promote.py::TestExactDurableScope -q --no-cov
FAILED test_exchange_requests_exactly_the_durable_scope - assert ['memory:session:write'] == ['memory:episodic:write']
FAILED test_promote_scope_is_never_session_or_broad
FAILED test_configured_target_layer_changes_the_requested_scope
3 failed in 0.28s

# restored:
$ pytest tests/bff/test_console_files_promote.py -q --no-cov
20 passed in 1.32s
```

### 3g. BFF fail-closed relay — orchestrator 4xx never manufactured into 200

Guard: the byte-faithful status-code relay in `console_files_promote`.
Neutered by force-setting `resp.status_code = 200` after a successful
`proxy_memory_request` call, regardless of the orchestrator's real status.

```
$ pytest "tests/bff/test_console_files_promote.py::TestFailClosed::test_orchestrator_denial_forwarded_verbatim_not_manufactured" -q --no-cov
FAILED [403] - assert 200 == 403
FAILED [404] - assert 200 == 404
FAILED [422] - assert 200 == 422
3 failed in 0.27s

# restored:
$ pytest tests/bff/test_console_files_promote.py -q --no-cov
20 passed in 0.91s
```

All seven neuter/restore cycles verified working-tree byte-identical to
pre-neuter state after restore (`diff /tmp/memory_promote.py.bak
src/audittrace/routes/memory_promote.py` and `diff /tmp/app.py.bak
bff/app.py` → no output in both cases) before the final `make test` run
(§1) was captured.

## 4. Frozen invariants — spot checks

- `/v1` byte-inviolate: `git diff docs/reference/audittrace/openapi.yaml`
  touches zero lines under any `/v1/chat/completions` path (§2);
  `git diff bff/app.py` shows only docstring rewording + new-route
  additions, zero removed functional lines from `chat_completions`,
  `memory_proxy`, or `console_files`.
- Durable scope required, session scope insufficient: §3a/3f above.
- Ownership (RLS-equivalent): §3b above.
- Provenance token-derived: §3c above.
- COPY not move: §3d above.
- Explicit-only: no code path places a durable doc from a chat upload
  without an explicit `POST /memory/promote` call carrying a
  durable-scoped token — the only other write path into episodic/
  semantic is the pre-existing `create_episodic`/`create_semantic`
  routes, unchanged by this WU.
- Traceability: `_emit_promote_audit` calls `emit_memory_audit_event`
  inline + awaited (fail-closed, mirrors `routes/memory.py::
  _emit_write_audit`'s discipline) — the row carries `user.user_id`
  (token-derived `sub`) and the standard `trace_id` plumbing.

## 5. What this file does NOT claim

No image was built, no `helm upgrade` ran, no pod was hit through the
public API with a scoped JWT against a deployed image — this WU is
LOCAL-only by ratified spec. Live E2E is explicitly deferred to WU-6 +
operator go, per the spec's "Out of scope" section.

## 6. Review pass 1 REJECTION + fix (2026-09-05, same day)

The independent reviewer REJECTED the build on a live-proven finding:
semantic promote used the raw session **filename** as the ChromaDB
`document_id` in the shared `semantic_v2` physical collection, with NO
per-user namespacing. Two users independently promoting a same-named
file (e.g. both promoting `shared.md`) silently overwrote the SAME
ChromaDB row — reproduced live against the real `/memory/promote` +
`/memory/semantic` endpoints. User A's read-back of "their own" key
returned User B's content + attribution.

### 6a. The fix

`src/audittrace/routes/memory_promote.py::_namespaced_semantic_document_id`
(new) bakes the TOKEN-derived `user.user_id` into the ChromaDB
`document_id` (`f"{user_id}/{filename}"`), mirroring the INTENT of
episodic's per-user S3 object-prefix isolation (episodic/procedural
already isolate PRIVATE-tier content by a per-user storage path; the
semantic layer's private-tier physical collection has no equivalent
per-user storage-path mechanism, so the namespace has to live inside
the id itself). `_promote_to_semantic` now returns `(durable_key,
collection)` explicitly rather than re-deriving `collection` via
`durable_key.rsplit("/", 1)` — the old single-split parse would have
silently mis-parsed the collection name once the id itself started
containing a `/`. Does NOT touch the broader, pre-existing
`authorize_write` corpus-collision primitive gap (ADR-062 §B4) — that
stays the documented separate follow-up; this closes the specific
hijack this promote path introduced.

Three existing tests updated for the new (correct) namespaced key shape
(`TestPromoteSemanticTarget::test_promote_to_semantic_default_collection`
/ `test_promote_to_semantic_custom_collection` /
`test_semantic_document_readable_after_promote`) and one existing
corpus-collision seed
(`TestPromoteManifestAuthorizationChoke::test_promote_over_existing_corpus_semantic_row_denied`)
updated to seed the namespaced key so the collision it tests still
actually collides.

### 6b. New non-vacuous regression test (the crux)

`TestPromoteSemanticCrossUserIsolation::test_two_users_same_filename_never_collide`
— two users (alice, bob) each upload a session doc named `shared.md`
with DIFFERENT content, both promote to the default semantic
collection, and the test asserts: (1) the two returned durable keys are
DIFFERENT (`semantic/alice/shared.md` vs `semantic/bob/shared.md`, never
a shared `semantic/shared.md`); (2) each user's read-back returns THEIR
OWN content, never the other's.

```
$ pytest tests/test_memory_promote_route.py::TestPromoteSemanticCrossUserIsolation -q --no-cov
1 passed in ...s
```

Neuter: `_namespaced_semantic_document_id` reverted to return the raw
*filename* (dropping the `user_id` prefix) — the exact pre-fix shape.

```
$ pytest tests/test_memory_promote_route.py::TestPromoteSemanticCrossUserIsolation -q --no-cov
FAILED test_two_users_same_filename_never_collide - AssertionError: assert 'semantic/shared.md' != 'semantic/shared.md'
1 failed in 0.32s

# restored (diff <backup> <file> -> no output):
$ pytest tests/test_memory_promote_route.py -q --no-cov
32 passed in 6.96s
```

### 6c. All 7 original guards re-confirmed after the fix (RED -> restore -> GREEN)

Every guard from §3 re-neutered and re-restored against the POST-FIX
code, confirming the fix did not regress any of them (the diff between
pre-fix and post-fix `memory_promote.py` is scoped entirely to
`_promote_to_semantic` + the caller's tuple-unpack — verified via
`git diff` before this pass):

1. Durable scope gate (session-only token 403) — RED then GREEN.
2. Ownership 404 (nonexistent + cross-user) — RED (2 tests) then GREEN.
3. Provenance token-derived — RED then GREEN.
4. Copy-not-move — RED then GREEN; full 32-test file re-run GREEN after
   restore.
5. `target_layer` validation (durable set only) — RED (2 tests) then
   GREEN; `diff` confirmed byte-identical restore.
6. BFF exact durable scope — RED (3 tests) then GREEN; `diff` confirmed
   byte-identical restore of `bff/app.py`.
7. BFF fail-closed relay — RED (3 tests) then GREEN; `diff` confirmed
   byte-identical restore.

### 6d. Full gate re-run after the fix

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.70%
4217 passed, 2 warnings in 380.88s (0:06:20)
🔒 Enforcing per-file coverage gate (each component >= 90%)...
per-file coverage gate: PASS (104 files checked, lines >= 90%, branches >= 90% on 93 file(s) with branches)
🚫 Enforcing zero-skip policy...
[no-skip-check] No skipped tests in junit.xml. Good.
✅ Tests passed

$ make lint  -> clean
$ make helm-lint -> 1 chart(s) linted, 0 chart(s) failed
$ mypy <10 touched/new files> -> Success: no issues found in 10 source files
```

New-file coverage: `src/audittrace/routes/memory_promote.py` 100%
(107 statements, 16 branches, 0 missed).

This section satisfies ADR-049 Rule 1 (Verification) and Rule 3
(Reconstruction — the neuter/restore transcripts above) for the fix
commit; Rule 2 (Validation) remains deferred to WU-6 as in §5.

## 7. Review pass 2 REJECTION + re-fix #2 (2026-09-05, same day)

Pass-2 review REJECTED fix commit `fa76ead1`: the namespaced id
`f"{user_id}/{filename}"` contains a `/`, and the read route
`GET/PUT/DELETE /memory/semantic/{collection}/{document_id}` compiles
`document_id` to Starlette's default converter (`[^/]+`, a single URL
segment) — it can NEVER match a slash-containing id, so EVERY promoted
semantic doc became permanently unreadable via the REST surface, for
EVERY caller (not just an attacker). The cross-user collision genuinely
was closed, but the fix traded a data-leak for a total-read-outage. The
pass-1 regression test missed this because it called
`get_semantic_service().get_document(...)` DIRECTLY, bypassing
Starlette's own path-matching — the actual failure point.

### 7a. Lessons recalled before the re-fix

`lesson-wu4-pass2-real-http-route-20260905.md` (memory server, key
`decisions/085b4a8d89f4d59f` et al.): (1) security/round-trip regression
tests MUST exercise the REAL HTTP route, never the service method
directly; (2) a security fix must not trade one bug for another —
always re-run the happy path end to end after a security fix; (3) any
id/key containing `/` needs a `:path` converter or must avoid the slash
entirely — check every route consuming that id when its shape changes;
(4) isolation has TWO halves — unique-per-user id (write) AND
read-scoping (read) — fix and test BOTH.

### 7b. The re-fix (operator-chosen approach: metadata scoping)

1. `_namespaced_semantic_document_id` changed from `f"{user_id}/
   {filename}"` to `f"{user_id}__{filename}"` (double underscore, NO
   `/`) — a single URL segment the existing route's default `[^/]+`
   converter matches, so no route change and no `:path` converter is
   needed. Collision-safety argument: Keycloak `sub` values are
   fixed-length UUIDs (36 chars); two distinct user ids always differ
   within that fixed-length prefix regardless of what an attacker's own
   (non-spoofable, token-derived) filename choice appends after it.
2. **Metadata scoping was ALREADY the existing pattern** —
   `ChromaSemanticService.upsert` already unconditionally stamps
   `meta["user_id"] = user_context.user_id` (token-derived, direct
   assignment, WU-B5 review fix 2026-08-04) on every write, and
   `ChromaSemanticService.get_document` already applies
   `_tier_authorized` (owner-or-corpus, admin-bypass) before returning
   a fetched-by-id document — `read_semantic`
   (`routes/memory.py`) already calls `service.get_document(user, ...)`
   with the REAL caller. No service/route code change was needed for
   this half; the promote path already passed the real `user` through
   to `upsert` since the pass-1 fix. Verified by reading
   `services/semantic.py::ChromaSemanticService.upsert`/
   `get_document`/`_tier_authorized` and `routes/memory.py::
   read_semantic` directly rather than assumed.
3. Verified the semantic GET round-trip WORKS end-to-end after the
   change (§7c) — the exact regression pass-2 caught.

### 7c. New regression tests — REAL HTTP route (the crux)

`TestPromoteSemanticRealHttpRoundTrip` (new,
`tests/test_memory_promote_route.py`) — wired to a REAL
`ChromaSemanticService` (backed by the in-repo fake ChromaDB client,
`MockChromaDBFactory`) via a dedicated `real_semantic_client` fixture,
because the standard `client` fixture's `MockSemanticService.
get_document` is explicitly documented "mock: no scoping" and would
make a cross-user-read-denial assertion pass VACUOUSLY.

* `test_two_users_same_filename_full_http_round_trip` — two users
  promote the SAME filename via the real `POST /memory/promote`;
  asserts distinct keys; each reads back via the real
  `GET /memory/semantic/{collection}/{document_id}` and gets ONLY their
  OWN content (200, correct body) — the exact request shape that 404'd
  for everyone under the pass-1 id.
* `test_cross_user_read_by_known_key_returns_404` — Alice promotes;
  Bob, given Alice's EXACT key, attempts to read it — 404 (no existence
  disclosure), while Alice's own read of the same key succeeds (200,
  positive control).

```
$ pytest tests/test_memory_promote_route.py::TestPromoteSemanticRealHttpRoundTrip -q --no-cov
2 passed in 0.25s
```

### 7d. Two independent neuter proofs (both halves)

**Neuter #1 — id-uniqueness** (write half). Reverted
`_namespaced_semantic_document_id` to return the raw *filename*.

```
$ pytest tests/test_memory_promote_route.py::TestPromoteSemanticRealHttpRoundTrip::test_two_users_same_filename_full_http_round_trip -q --no-cov
FAILED ... AssertionError: assert 'semantic/shared.md' != 'semantic/shared.md'
1 failed in 0.20s

# restored (diff <backup> <file> -> no output):
$ pytest tests/test_memory_promote_route.py -q --no-cov
34 passed in 2.72s
```

**Neuter #2 — read where-filter** (read half). Broke
`ChromaSemanticService._tier_authorized` to return `True`
unconditionally (dropping the ownership check).

```
$ pytest tests/test_memory_promote_route.py::TestPromoteSemanticRealHttpRoundTrip::test_cross_user_read_by_known_key_returns_404 -q --no-cov
FAILED ... assert 200 == 404
1 failed in 0.21s

# restored (diff <backup> <file> -> no output):
$ pytest tests/test_memory_promote_route.py -q --no-cov
34 passed in 2.75s
```

### 7e. Full gate re-run after re-fix #2

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.70%
4219 passed, 2 warnings in 332.21s (0:05:32)
🔒 Enforcing per-file coverage gate (each component >= 90%)...
per-file coverage gate: PASS (104 files checked, lines >= 90%, branches >= 90% on 93 file(s) with branches)
🚫 Enforcing zero-skip policy...
[no-skip-check] No skipped tests in junit.xml. Good.
✅ Tests passed

$ make lint -> clean
$ make helm-lint -> 1 chart(s) linted, 0 chart(s) failed
$ mypy <11 touched/new files> -> Success: no issues found in 11 source files
```

`src/audittrace/routes/memory_promote.py` remains 100% covered.
`src/audittrace/services/semantic.py` untouched in the final diff
(only transiently edited during the neuter proof, restored
byte-identical — confirmed via `diff`).

This section satisfies ADR-049 Rule 1 (Verification) and Rule 3
(Reconstruction) for re-fix #2. Rule 2 (Validation through a deployed
image) remains deferred to WU-6, unchanged from §5.
