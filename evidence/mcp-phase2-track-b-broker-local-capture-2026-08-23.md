# Evidence — ADR-063 Phase 2 Track B, tool-broker gateway, local capture (2026-08-23)

**Scope of this evidence file.** This documents the LOCAL, non-deployed verification
performed while building `src/audittrace/services/mcp_broker.py` + the Track B
extension of `src/audittrace/routes/mcp.py` (broker dispatch + merged manifest) +
migration 021 (`tool_calls` broker-provenance columns). It satisfies ADR-049 Rule 1
(Verification) in full and gives a reconstructible Rule-3-shaped capture of the
request/response/audit-row cycle running through the REAL FastAPI `create_app()`
object (not mocked), against a STUB downstream MCP server (`httpx.MockTransport` —
no real network process, per the spec's "fake/stub downstream MCP server in tests"
instruction). **It explicitly does NOT satisfy Rule 2 (Validation through a deployed
image + public API + scoped JWT against a real downstream MCP server)** — that is
DEFERRED to the next candidate deploy, per the spec's own instruction
(`2026-08-23-SPEC-mcp-phase2-tool-broker-and-write-tools.md`, "Verification /
Reconstruction (Rules 2 & 3) — ADR-049 heavy gate": *"Live E2E (candidate deploy):
stand up a stub downstream MCP server; an MCP client with a scoped JWT calls a
brokered tool through the front door... Loop the memory server."*), mirroring the
Phase 1 precedent (`evidence/mcp-phase1-local-capture-2026-08-23.md`) and the #290
pattern. This file is the builder-side artefact that stands in for the PR-body
Validation/Reconstruction sections until a real PR exists (the builder role never
pushes/opens a PR — SDLC-ADR-000 §invariants).

## 1. Full test-suite run (Rule 1 — Verification)

```
$ PYTHONPATH=src .venv/bin/pytest tests/ -q
...
Required test coverage of 90% reached. Total coverage: 98.33%
3560 passed, 3 warnings in 172.82s (0:02:52)
```

Zero skips (confirmed — the one "skip" hit in a full-text grep of the run is a test
NAME containing the substring "skips", not a SKIPPED outcome). The 3 warnings are
pre-existing (`StarletteDeprecationWarning` httpx/testclient + 2
`RuntimeWarning: coroutine ... was never awaited` in
`tests/test_memory_routes.py::TestPdfFlushManifest*`), unrelated to this change
(confirmed via `git diff --stat` — those files are untouched by this build).

New/touched-file coverage (line + branch), from the same run:

```
src/audittrace/services/mcp_broker.py                      141      0     28      0   100%
src/audittrace/routes/mcp.py                                 88      0     20      0   100%
src/audittrace/routes/_memory_tool_loop.py                  276      3     86      0    99%
src/audittrace/routes/chat.py                                728     10    208      0    99%
src/audittrace/config.py                                     163      1     16      1    99%
src/audittrace/db/models.py                                   97      0      0      0   100%
```

(`_memory_tool_loop.py` / `chat.py` residual misses are pre-existing, unrelated
lines — confirmed via `git diff` showing only additive edits to `PendingToolCall`
and `_flush_pending_tool_calls` in those two files.)

Per-file coverage gate:

```
$ PYTHONPATH=src .venv/bin/python scripts/check-per-file-coverage.py
per-file coverage gate: PASS (72 files checked, lines >= 90%, branches >= 90% on
63 file(s) with branches)
```

`ruff check`, `ruff format --check`, and `mypy src/` are all clean on every file
this build touched (the one remaining `mypy` finding, `trust_store.py:610`, is
pre-existing and unrelated — confirmed via `git diff --stat`, that file is not
part of this change).

## 2. OpenAPI drift gate — zero drift confirmed

```
$ git diff --stat docs/reference/audittrace/openapi.yaml tests/fixtures/openapi.snapshot.yaml
(no output — zero-byte diff)
```

The `/mcp` route already existed (Phase 1); Track B extends its INTERNAL dispatch
logic only — no new route, no new Pydantic response model, no change to the
route's declared signature — so the generated OpenAPI schema is byte-identical.
`/v1/chat/completions` is untouched. `tests/test_openapi_drift.py` — 4/4 passed.

## 3. Local end-to-end capture through the real FastAPI app (Rule 3 shape)

Run against `audittrace.server.create_app()` (the actual production factory) via
`fastapi.testclient.TestClient`, with `dependencies.container =
create_test_container()` (in-memory SQLite `InMemoryPostgresFactory` — real
SQLAlchemy `InteractionRecord`/`ToolCall` rows, not mocked). One downstream server
("weather") configured via `AUDITTRACE_MCP_BROKER_SERVERS`; the broker's outbound
`httpx.AsyncClient` is pointed at an `httpx.MockTransport` stub standing in for the
downstream MCP server (no real network). `AUDITTRACE_AUTH_REQUIRED` unset →
bypass-mode admin sentinel identity (`00000000-0000-0000-0000-000000000001`).

### 3a. Merged manifest — `tools/list`

```
POST /mcp {"jsonrpc":"2.0","id":1,"method":"tools/list"}
→ 200 tools include the 6 Phase 1 own tools (recall_decisions, recall_skills,
  recall_recent_sessions, recall_semantic, read_decision, read_skill) PLUS:
  {"name":"broker:weather:forecast","description":"Get a weather forecast",
   "inputSchema":{"type":"object","properties":{"city":{"type":"string"}}}}
```

One audited surface — the namespaced brokered tool appears alongside, not instead
of, Phase 1's own tools.

### 3b. Successful brokered call — TWO tamper-evident rows, one interaction/trace

```
POST /mcp {"jsonrpc":"2.0","id":1,"method":"tools/call",
  "params":{"name":"broker:weather:forecast","arguments":{"city":"Zurich"}}}
→ 200 {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text",
  "text":"sunny, 21C"}],"structuredContent":{"forecast":"sunny","tempC":21},
  "isError":false}}

  Server logs for this call (unedited):
  INFO audittrace.services.mcp_broker — mcp broker tools/call ok
       tool=broker:weather:forecast user=00000000-0000-0000-0000-000000000001
       duration_ms=0
  INFO audittrace.routes.chat — interaction persisted id=1
       user=00000000-0000-0000-0000-000000000001 status=success
       project=default model=None duration_ms=0
  INFO audittrace.routes.chat — tool_calls persisted count=2 interaction_id=1
```

Raw `tool_calls` rows for `interaction_id=1` (queried directly via the ORM — the
public `GET /interactions/{id}/tool-calls` endpoint deliberately does NOT expose
the new provenance columns, an explicit additive-only choice; see §5):

```json
{"interaction_id": 1, "tool_name": "broker:weather:forecast", "provenance": "brokered",
 "phase": "request", "downstream_server": "weather", "downstream_tool": "forecast",
 "args_digest": "0b715fb1deeebde62c3458e8b0479385e9a92effc8e30f2e10bbb934728bd155",
 "result_digest": null, "error": null, "granted_scope": "audittrace:broker:weather"}
{"interaction_id": 1, "tool_name": "broker:weather:forecast", "provenance": "brokered",
 "phase": "result", "downstream_server": "weather", "downstream_tool": "forecast",
 "args_digest": "0b715fb1deeebde62c3458e8b0479385e9a92effc8e30f2e10bbb934728bd155",
 "result_digest": "0986dc3f431be04aa95d9eab6afaedc35253121a3d38c4dd735679266b49293e",
 "error": null, "granted_scope": "audittrace:broker:weather"}
```

Both rows share `interaction_id=1` (one trace); `args_digest` matches between the
request and result row (confirming they describe the same call); the request row
has `result_digest=null` (not known yet when written); the result row's
`result_digest` is populated. This is the "TWO tamper-evident audit events (request
+ result) tagged brokered with the downstream identity" the spec requires,
reconstructed from the persisted rows themselves — not asserted from memory.

### 3c. Downstream timeout — the failure is ALSO recorded (no silent gap)

Same session, downstream mock reconfigured to raise `httpx.ConnectTimeout`:

```
POST /mcp {"jsonrpc":"2.0","id":3,"method":"tools/call",
  "params":{"name":"broker:weather:forecast","arguments":{"city":"Geneva"}}}
→ 200 {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text",
  "text":"{\"error\": \"timed out\"}"}],"isError":true}}
```

Raw `tool_calls` rows for the new `interaction_id=2`:

```json
{"interaction_id": 2, "tool_name": "broker:weather:forecast", "provenance": "brokered",
 "phase": "request", "downstream_server": "weather", "downstream_tool": "forecast",
 "args_digest": "52e4a2914370880b07f8ed0ded276817c836ec7ca4facf65396d1804dd17dd5e",
 "result_digest": null, "error": null, "granted_scope": "audittrace:broker:weather"}
{"interaction_id": 2, "tool_name": "broker:weather:forecast", "provenance": "brokered",
 "phase": "result", "downstream_server": "weather", "downstream_tool": "forecast",
 "args_digest": "52e4a2914370880b07f8ed0ded276817c836ec7ca4facf65396d1804dd17dd5e",
 "result_digest": "b4365c9ceea44ac8e140475814eb59cffd1a5470bff84f1958fdf47d9f6039fa",
 "error": "timed out", "granted_scope": "audittrace:broker:weather"}
```

Even though the downstream call never returned a real response, both the request
event AND a result event (status="failed", `error="timed out"`, its own
`result_digest` over the error payload) are recorded — the spec's "Failure/
timeout/deny are audited too... no silent gaps", reconstructed from a real timeout
raised by the stub transport, not a happy-path assumption.

### 3d. Scope-denied-before-forward and identity/RLS — proven by the non-vacuous test
suite, not re-derived by hand here

This script exercises the bypass-mode admin sentinel, which (by design, matching
every other scope check in this codebase) bypasses per-server scope gating — so it
cannot itself demonstrate a genuine denial. That property is proven rigorously
(spy-on-transport, real signed JWTs, no `dependency_overrides`) by:

- `tests/test_mcp_broker.py::TestBrokerCallDenied::test_scope_denied_never_forwards`
  — unit-level, spies on the httpx transport call-counter (stays at 0).
- `tests/test_mcp_broker_routes.py::TestScopeDeniedBeforeForward::test_missing_scope_denied_no_forward_no_data`
  — HTTP-level, drives a REAL signed JWT through the REAL `require_user` (no
  `dependency_overrides`), spies on the transport call-counter (stays at 0).
- `tests/test_mcp_broker_routes.py::TestIdentityBinding::test_rls_contextvar_bound_at_dispatch_time_for_broker_calls`
  — genuinely neuters `audittrace.auth.set_current_user_id` (the exact Phase 1
  reviewer-finding bug class) and confirms the captured ContextVar value goes
  RED (`None`) instead of the real caller's sub, then confirms it is GREEN again
  once restored.
- `tests/test_mcp_broker_routes.py::TestIdentityBinding::test_two_real_callers_each_own_their_audit_rows`
  — two distinct real signed callers, each own exactly their own `ToolCall` rows.

All four pass in the full suite run in §1.

## 4. Reconstruction chain proven locally

Exactly two `ToolCall` rows (request + result, both `provenance="brokered"`) land
per brokered `tools/call`, sharing the one `InteractionRecord` (`source="mcp"`)
that call produced — the SAME tamper-evident write path
(`_persist_interaction` / `_flush_pending_tool_calls`, ADR-037/058) every other
interaction in the system uses, confirming the broker rides the existing recorder
rather than a parallel one, exactly as the ADR's Track B section requires.

## 5. What is explicitly NOT covered here (Rule 2 — deferred)

- No image was built or deployed for this evidence.
- No request was sent through the Istio front door with a real Keycloak-issued
  JWT against a REAL (non-stub) downstream MCP server process.
- No trace was exported to Tempo/Langfuse (local dev has
  `AUDITTRACE_OTLP_ENDPOINT=""`, the laptop no-op default).
- The `GET /interactions/{id}/tool-calls` API response does not (yet) surface
  `provenance`/`phase`/`downstream_server`/`downstream_tool`/`args_digest`/
  `result_digest` — a deliberate, minimal-diff additive-only choice for this
  build (kept `ToolCallListItem`/`_tool_call_to_dict` byte-unchanged to hold
  OpenAPI drift at zero); the columns exist and are queryable directly, and a
  follow-up spec can extend the audit API to surface them once the shape is
  reviewer-ratified.

These are the ADR-049 Rule 2 (Validation) artefacts the spec defers to the next
candidate deploy cycle (the CD-deployer + deploy-verifier agents, not the
builder). When that deploy happens, capture: an MCP client `tools/list` +
`broker:<server>:<tool>` call through the front door with a scoped JWT against a
real stub downstream MCP server, the resulting TWO audit-row ids, and the trace
id — per the spec's "Verification / Reconstruction (Rules 2 & 3)" section — and
supersede this file.
