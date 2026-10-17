# Evidence — ADR-063 Phase 2 Track A, write/curation tools over MCP, local capture (2026-08-24)

**Scope of this evidence file.** This documents the LOCAL, non-deployed verification
performed while building `src/audittrace/tools/mcp_write_registry.py` +
`src/audittrace/tools/mcp_write_handlers.py` (the `write_decision`/`write_skill`
handlers) + `src/audittrace/services/mcp_write_bridge.py` (authorize → execute →
record → return) + the Track A extension of `src/audittrace/routes/mcp.py` (write
dispatch routing, manifest merge). It satisfies ADR-049 Rule 1 (Verification) in
full and gives a reconstructible Rule-3-shaped capture of the request/response/
audit-row cycle running through the REAL FastAPI `create_app()` object (not
mocked), with a genuinely signed RS256 JWT decoded through the REAL
`require_user` (no `dependency_overrides` bypass). **It explicitly does NOT
satisfy Rule 2 (Validation through a deployed image + public API + scoped JWT)**
— that is DEFERRED to the v1.24.8 candidate deploy, per the spec's own
instruction (`2026-08-23-SPEC-mcp-phase2-tool-broker-and-write-tools.md`,
"Verification / Reconstruction (Rules 2 & 3) — ADR-049 heavy gate"), mirroring
the Track B precedent
(`evidence/mcp-phase2-track-b-broker-local-capture-2026-08-23.md`) and the #290
pattern. This file is the builder-side artefact that stands in for the PR-body
Validation/Reconstruction sections until a real PR exists (the builder role
never pushes/opens a PR — SDLC-ADR-000 §invariants).

## 1. Full test-suite run (Rule 1 — Verification)

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.84%
3583 passed, 3 warnings in 178.06s (0:02:58)
per-file coverage gate: PASS (88 files checked, lines >= 90%, branches >= 90% on
79 file(s) with branches)
[no-skip-check] No skipped tests in junit.xml. Good.
```

Zero skips (junit.xml-verified by the Makefile's own no-skip-check, not a grep
heuristic). The 3 warnings are pre-existing (`StarletteDeprecationWarning`
httpx/testclient + 2 `RuntimeWarning: coroutine ... was never awaited` in
`tests/test_memory_routes.py::TestPdfFlushManifest*`), unrelated to this change
(confirmed via `git diff --stat` — those files are untouched by this build).

New/touched-file coverage (line + branch), from the same run:

```
src/audittrace/tools/mcp_write_registry.py       17      0      2      0   100%
src/audittrace/tools/mcp_write_handlers.py       47      0      8      0   100%
src/audittrace/services/mcp_write_bridge.py      57      0     14      0   100%
src/audittrace/routes/mcp.py                     95      0     22      0   100%
```

`ruff check`, `ruff format --check`, and `mypy src/` are all clean on every file
this build touched. The one remaining repo-wide `mypy` finding
(`trust_store.py:610`) is pre-existing and unrelated — reproduced identically on
a `git stash`-clean tree before this build's changes were applied.

## 2. Non-vacuous neuter→RED proofs (falsifiability, Rule 1)

Neutered `services/mcp_write_bridge.py::call_write_tool`'s scope check
(`if tool.required_scope not in user_context.scopes:` → `if False:`), reran the
Track A suites, restored, reran again:

```
# NEUTERED (scope check disabled):
$ pytest tests/test_mcp_write_tools_routes.py tests/test_mcp_write_bridge.py -q
FAILED tests/test_mcp_write_tools_routes.py::TestScopeDeniedNoMutation::test_missing_scope_denied_no_mutation_no_data_audit_row
FAILED tests/test_mcp_write_tools_routes.py::TestScopeDeniedNoMutation::test_decisions_scope_does_not_authorize_write_skill
FAILED tests/test_mcp_write_tools_routes.py::TestSentinelUnaffected::test_default_sentinel_call_of_write_tool_is_denied
FAILED tests/test_mcp_write_bridge.py::TestCallWriteTool::test_missing_scope_denied_no_execution
4 failed, 19 passed

# RESTORED (scope check live):
$ pytest tests/test_mcp_write_tools_routes.py tests/test_mcp_write_bridge.py -q
23 passed
```

Four independent tests go RED under the neuter and none of the remaining 19 mask
it — the exact falsifiability shape ADR-059/`feedback_vacuous_neuter_test_antipattern`
requires: real signed JWT through the real `require_user`, no
`dependency_overrides`, and the assertion is a real observable side effect (the
mock semantic service's `_docs` dict staying empty, and an audit-DENIED
`ToolCall` row with `error` populated), not a mocked call-through.

## 3. OpenAPI drift gate — zero drift confirmed

```
$ pytest tests/test_openapi_drift.py -q
4 passed
```

The `/mcp` route already existed (Phase 1 + Track B); Track A extends its
INTERNAL dispatch logic and merged manifest only — no new route, no new Pydantic
response model, no change to the route's declared signature — so the generated
OpenAPI schema is byte-identical. `/v1/chat/completions` is untouched (Track A
never registers into `audittrace.tools.MEMORY_TOOL_REGISTRY`, the dict
`tools_visible_to` — the function the chat tool loop reads — iterates; see the
module docstrings on `tools/mcp_write_handlers.py` and
`tools/mcp_write_registry.py` for why that separation is structural, not
conventional).

## 4. Local end-to-end capture through the real FastAPI app (Rule 3 shape)

Run against `audittrace.server.create_app()` (the actual production factory) via
`fastapi.testclient.TestClient`, with `dependencies.container =
create_test_container()` (in-memory SQLite `InMemoryPostgresFactory` — real
SQLAlchemy `InteractionRecord`/`ToolCall` rows, not mocked). `AUDITTRACE_AUTH_REQUIRED=true`
+ a genuinely signed RS256 JWT (throwaway keypair, fakeredis-backed token cache)
decoded through the REAL `require_user` cold path — no `dependency_overrides`
anywhere in this capture.

### 4a. `tools/list` — write tools appear ONLY for the scoped caller, never via admin bypass

```
Caller: sub=alice, scope="" (no write scope, not admin)
POST /mcp {"jsonrpc":"2.0","id":1,"method":"tools/list"}
→ tool names: [] for write tools (only the 6 Phase 1 read tools present, omitted here)

Caller: sub=curator-evidence, scope="memory:decisions:write"
POST /mcp {"jsonrpc":"2.0","id":1,"method":"tools/list"}
→ tool names include: "write_decision"
→ tool names do NOT include: "write_skill" (per-tool, not per-tier)
```

### 4b. Valid-scope `tools/call write_decision` — real mutation + tamper-evident audit

```
POST /mcp {"jsonrpc":"2.0","id":1,"method":"tools/call",
  "params":{"name":"write_decision","arguments":{
    "document_id":"adr-evidence-1",
    "text":"Track A ships write/curation tools over MCP.",
    "title":"ADR-063 Phase 2 Track A"}}}
→ 200 {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text",
  "text":"{\"document_id\": \"adr-evidence-1\", \"collection\": \"decisions\",
  \"tier\": \"private\", \"manifest_id\": \"05077d5e-3f58-49d9-9cfa-e3e07a98c8c8\",
  \"created_at_ms\": 1787559773043, \"modified_at_ms\": 1787559773043}"}],
  "structuredContent":{"document_id":"adr-evidence-1","collection":"decisions",
  "tier":"private","manifest_id":"05077d5e-3f58-49d9-9cfa-e3e07a98c8c8",
  "created_at_ms":1787559773043,"modified_at_ms":1787559773043},"isError":false}}
```

Raw `tool_calls` row (queried directly via the ORM):

```json
{"tool_name": "write_decision", "user_id": "curator-evidence", "error": null,
 "granted_scope": "memory:decisions:write", "interaction_id": 2}
```

`InteractionRecord` rows produced (`source="mcp"`, the MCP call itself, id=2,
status=success) AND (`source="memory-audit"`, the SAME
`services.memory_audit.emit_memory_audit_event` write path
`routes/memory.py::create_semantic` uses, id=1) — the write is indistinguishable,
downstream, from one made through the REST API:

```json
{"id": 1, "question": "op=write layer=semantic key=adr-evidence-1", "user_id": "curator-evidence"}
```

The document landed in the semantic service at the PRIVATE tier, and a manifest
row was created attributing authorship to the token `sub` (never a caller-
supplied value):

```json
semantic doc metadata: {"collection": "decisions", "document_id": "adr-evidence-1", "tier": "private"}
manifest row: {"id": "05077d5e-3f58-49d9-9cfa-e3e07a98c8c8", "layer": "semantic",
 "key": "decisions/adr-evidence-1", "title": "ADR-063 Phase 2 Track A",
 "created_by_user_id": "curator-evidence", "modified_by_user_id": "curator-evidence",
 "tier": "private"}
```

### 4c. Missing-scope `tools/call write_decision` — denied, NO mutation, audit-DENIED row

```
Caller: sub=alice, scope="" (no write scope)
POST /mcp {"jsonrpc":"2.0","id":1,"method":"tools/call",
  "params":{"name":"write_decision","arguments":{
    "document_id":"adr-evidence-DENIED","text":"must never land"}}}
→ 200 {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text",
  "text":"{\"error\": \"scope denied: memory:decisions:write not in caller
  scopes for tool write_decision\"}"}],"isError":true}}
```

No document with `document_id="adr-evidence-DENIED"` exists in the semantic
service afterwards (asserted programmatically in the capture script — the script
aborts with an `AssertionError` if it does). Raw `tool_calls` row for this call:

```json
{"tool_name": "write_decision", "user_id": "alice",
 "error": "scope denied: memory:decisions:write not in caller scopes for tool write_decision",
 "granted_scope": "memory:decisions:write", "interaction_id": 3}
```

The audit-DENIED row exists (`interaction_id=3`, `error` populated) even though
no mutation happened — "no mutation, no data, an audit-DENIED row written," the
falsifiable acceptance criterion, reconstructed from the persisted row itself.

### 4d. Cross-tool scope check — `write_skill` denied when holding only `memory:decisions:write`

```
Caller: sub=curator-evidence, scope="memory:decisions:write" (NOT memory:skills:write)
POST /mcp {"jsonrpc":"2.0","id":1,"method":"tools/call",
  "params":{"name":"write_skill","arguments":{
    "document_id":"skill-evidence-DENIED","text":"must never land either"}}}
→ 200 {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text",
  "text":"{\"error\": \"scope denied: memory:skills:write not in caller scopes
  for tool write_skill\"}"}],"isError":true}}
```

No document was written to the `skills` collection. This is the Track B review's
design input applied and locally proven: holding `memory:decisions:write` never
authorizes `write_skill` — per-TOOL scope, not a coarse operator tier.

## 5. Reconstruction chain proven locally

Every write/curation MCP call produces exactly one `InteractionRecord`
(`source="mcp"`) + exactly one `ToolCall` row — same "one call, one row"
invariant Phase 1/Track B use — PLUS, on a successful mutation, one additional
`InteractionRecord` (`source="memory-audit"`) via the SAME
`services.memory_audit.emit_memory_audit_event` path `routes/memory.py`'s REST
write endpoints already use. A denied call produces the `ToolCall`
denial row but no `memory-audit` row (no mutation occurred to audit). This
confirms Track A rides the EXISTING tamper-evident recorder + the EXISTING
memory-audit trail rather than inventing a third one.

## 6. What is explicitly NOT covered here (Rule 2 — deferred)

- No image was built or deployed for this evidence.
- No request was sent through the Istio front door with a real Keycloak-issued
  JWT.
- No trace was exported to Tempo/Langfuse (local dev has
  `AUDITTRACE_OTLP_ENDPOINT=""`, the laptop no-op default).
- Corpus-tier promotion (`memory:corpus:{decisions,skills}:write`) is
  deliberately out of scope for Track A's MCP write tools — every write lands
  private-tier only, even for an operator holding the corpus scope; a future
  spec can extend this once ratified.

These are the ADR-049 Rule 2 (Validation) artefacts the spec defers to the
v1.24.8 candidate deploy cycle (the CD-deployer + deploy-verifier agents, not
the builder). When that deploy happens, capture: an MCP client `tools/list` +
`write_decision`/`write_skill tools/call` through the front door with a scoped
JWT, the resulting `ToolCall`/manifest/memory-audit row ids, and the trace id —
per the spec's "Verification / Reconstruction (Rules 2 & 3)" section — and
supersede this file.
