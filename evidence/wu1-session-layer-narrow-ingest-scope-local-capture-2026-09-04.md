# Evidence — WU-1 session memory layer + narrow ingest scope, local capture (2026-09-04)

**Scope of this evidence file.** This is a **LOCAL-only** work unit per its
ratified spec (`2026-09-03-SPEC-wu1-session-layer-narrow-ingest-scope.md`,
sha256 `f4500b0844dac3ecfe00d6aa50c7a4a0f196cdb745bafed48928c74c6f362838`) —
explicit "Out of scope: Any deploy / live E2E (gated; WU-6 + operator go)".
This file satisfies ADR-049 Rule 1 (Verification) in full and gives a
reconstructible Rule-3-shaped capture (neuter-proof of every new guard,
through the real FastAPI `create_app()` object, not mocked). It explicitly
does **NOT** satisfy Rule 2 (Validation through a deployed image + public API
+ scoped JWT) — that is deferred to a later WU per the spec, mirroring the
`mcp-phase1-local-capture-2026-08-23.md` precedent.

## 1. Full test-suite run (Rule 1 — Verification)

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.69%
4080 passed, 2 warnings in 326.05s (0:05:26)
🔒 Enforcing per-file coverage gate (each component >= 90%)...
per-file coverage gate: PASS (99 files checked, lines >= 90%, branches >= 90% on 88 file(s) with branches)
🚫 Enforcing zero-skip policy...
[no-skip-check] No skipped tests in junit.xml. Good.
✅ Tests passed
```

New-file coverage (line + branch):

```
src/audittrace/services/session_memory.py                   75      0     12      0   100%
src/audittrace/db/models.py                                 106      0      0      0   100%
```

`make lint` (ruff check + ruff format + offline semgrep security-lint) and
`make helm-lint` both green:

```
$ make lint
...
Ran 2 rules on 155 files: 0 findings.
✅ Security-lint passed
All checks passed!
✅ Linting passed
278 files already formatted
✅ Formatting passed

$ make helm-lint
🪖  helm lint (vault.enabled=false)...
1 chart(s) linted, 0 chart(s) failed
🪖  helm template (vault.enabled=true) + vaultSecretFileGuard count...
✅ vaultSecretFileGuard present in 3 workloads
```

`mypy` clean on every new/touched file:

```
$ .venv/bin/mypy src/audittrace/routes/memory.py src/audittrace/dependencies.py \
    src/audittrace/services/session_memory.py src/audittrace/db/models.py \
    src/audittrace/auth.py src/audittrace/migrations/versions/022_create_session_memory_items.py \
    tests/test_session_memory_service.py
Success: no issues found in 7 source files
```

## 2. OpenAPI drift gate — additive-only confirmation

```
$ git diff --stat docs/reference/audittrace/openapi.yaml tests/fixtures/openapi.snapshot.yaml
 docs/reference/audittrace/openapi.yaml | 25 +++++++++++++++++++++++--
 tests/fixtures/openapi.snapshot.yaml   | 25 +++++++++++++++++++++++--
```

New `memory:session:write` entry in the OAuth2 security-scheme scope map, a
new `session` member in the `MemoryLayer` schema enum, and the extended
`upload_memory_file` docstring. `/v1/chat/completions` is byte-for-byte
unchanged — confirmed via `git diff docs/reference/audittrace/openapi.yaml`
showing zero touched lines under the `/v1/chat/completions` path block.

## 3. Neuter-proof — every new guard fails RED when broken, restored to GREEN

Builder-side falsifiability pass (the independent reviewer re-runs this
mandate-to-fail check itself; this is the builder's own verification that
each guard is real before handing off).

### 3a. RLS isolation (acceptance test d)

Guard: `PostgresSessionMemoryService.read_own`'s explicit
`.filter(SessionMemoryItem.user_id == user_context.user_id)` clause.

```
$ python3 - <<'EOF'   # drop the user_id filter
... (see src/audittrace/services/session_memory.py:read_own)
EOF
$ .venv/bin/python -m pytest tests/test_session_memory_service.py::TestPostgresSessionMemoryService::test_cross_user_isolation_denies_read -q --no-cov
FAILED tests/test_session_memory_service.py::TestPostgresSessionMemoryService::test_cross_user_isolation_denies_read
1 failed, 11 passed, 346 deselected

# restored:
$ .venv/bin/python -m pytest tests/test_session_memory_service.py::TestPostgresSessionMemoryService::test_cross_user_isolation_denies_read -q --no-cov
1 passed in 0.28s
```

### 3b. Least-privilege wall (acceptance tests b/c)

Guard: `_require_layer_write` in `src/audittrace/routes/memory.py`.
Neutered to an unconditional pass-through (`if True: return`).

```
$ .venv/bin/python -m pytest tests/test_memory_routes.py -k "Session or session" -q --no-cov
FAILED tests/test_memory_routes.py::TestUploadSessionLayerAuth::test_session_write_scope_cannot_write_episodic
FAILED tests/test_memory_routes.py::TestUploadSessionLayerAuth::test_no_scope_token_cannot_write_session
FAILED tests/test_memory_routes.py::TestUploadSessionLayerAuth::test_session_write_scope_cannot_write_procedural
3 failed, 8 passed, 346 deselected

# restored:
$ .venv/bin/python -m pytest tests/test_memory_routes.py -k "Session or session" -q --no-cov
11 passed, 346 deselected
```

### 3c. PDF-to-durable-pipeline refusal (the "config-flag-is-not-an-enforced-control" finding)

Guard: the `layer == MemoryLayer.session` early-400 inside the
`is_pdf_upload(...)` branch of `upload_memory_file` — without it, a
`memory:session:write`-only token could upload a PDF that the quarantine →
verdict → promotion pipeline lands DURABLY in `episodic/papers/` regardless
of the declared `layer=`, defeating the wall. Neutered to `if False:`.

```
$ .venv/bin/python -m pytest tests/test_memory_routes.py::TestUploadSessionLayerAuth::test_session_pdf_upload_refused -q --no-cov
FAILED tests/test_memory_routes.py::TestUploadSessionLayerAuth::test_session_pdf_upload_refused
(falls through to 503 "scan pipeline disabled" instead of 400 — proves the
 request WOULD have reached the durable quarantine pipeline)

# restored:
$ .venv/bin/python -m pytest tests/test_memory_routes.py::TestUploadSessionLayerAuth -q --no-cov
6 passed
```

### 3d. Realm/Job scope-binding drift guards (deliverable e)

Guard: the `MEMORY_SESSION_WRITE_SCOPES` array in
`charts/audittrace/templates/keycloak/configmap-memory-scopes-script.yaml`.
Emptied.

```
$ .venv/bin/python -m pytest tests/test_chart_drift_guards.py -k "SessionWrite" -q --no-cov
FAILED tests/test_chart_drift_guards.py::TestKeycloakSessionWriteScopeGovernance::test_provisioner_arrays_match_and_exact
FAILED tests/test_chart_drift_guards.py::TestSessionWriteScopeJobRenderedBinding::test_rendered_job_binds_exactly_the_session_write_scope
2 failed, 4 passed, 89 deselected

# restored:
$ .venv/bin/python -m pytest tests/test_chart_drift_guards.py -k "SessionWrite" -q --no-cov
6 passed, 89 deselected
```

All four neuter/restore cycles verified working-tree byte-identical to
pre-neuter state after restore (`diff <backup> <file>` → no output) before
the final `make test` run (§1) was captured.

## 4. Frozen invariants — spot checks

- `/v1` byte-inviolate: `git diff` touches zero lines under any
  `/v1/chat/completions` path; the OpenAPI diff (§2) is scoped to the
  `MemoryLayer` schema, the OAuth2 scope map, and `/memory/upload`'s
  docstring only.
- Least privilege: §3b/3c above.
- Traceability: `upload_memory_file`'s session branch calls
  `_emit_write_audit(user=user, op="write", layer="session", ...)`
  inline + awaited (fail-closed per `services/memory_audit.py`'s module
  docstring) — the row carries `user.user_id` (token-derived `sub`) and
  `trace_id` via `_current_trace_id_hex()`.
- Identity token-derived at the choke: `user.user_id` throughout comes
  from `require_user` (JWT `sub`); no caller-supplied identity field is
  ever read in the new code paths.

## 5. What this file does NOT claim

No image was built, no `helm upgrade` ran, no pod was hit through the
public API with a scoped JWT against a deployed image — this WU is
LOCAL-only by ratified spec. Live E2E is explicitly deferred (WU-2's BFF
narrow exchange + a later operator-gated deploy).
