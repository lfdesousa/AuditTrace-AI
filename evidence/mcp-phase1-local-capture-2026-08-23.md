# Evidence — ADR-063 Phase 1 MCP entry-interface, local capture (2026-08-23)

**Scope of this evidence file.** This documents the LOCAL, non-deployed
verification performed while building `src/audittrace/routes/mcp.py` +
`src/audittrace/services/mcp_bridge.py`. It satisfies ADR-049 Rule 1
(Verification) in full and gives a reconstructible Rule-3-shaped capture
of the request/response/audit-row cycle running through the REAL FastAPI
`create_app()` object (not mocked). **It explicitly does NOT satisfy
Rule 2 (Validation through a deployed image + public API + scoped JWT)**
— that is DEFERRED to the next candidate deploy, per the spec's own
instruction (`2026-08-23-SPEC-mcp-entry-interface-phase1.md`, "Verification
/ Reconstruction (Rules 2 & 3) — ADR-049 heavy gate": *"Live E2E
(candidate deploy)... Loop the memory server."*), mirroring the #290
pattern. This file is the builder-side artefact that stands in for the
PR-body Validation/Reconstruction sections until a real PR exists (the
builder role never pushes/opens a PR — SDLC-ADR-000 §invariants).

## 1. Full test-suite run (Rule 1 — Verification)

```
$ .venv/bin/python -m pytest -q
...
Required test coverage of 90% reached. Total coverage: 98.29%
3466 passed, 2 warnings in 216.99s (0:03:36)
```

New-file coverage (line + branch):

```
src/audittrace/routes/mcp.py                                73      0     16      0   100%
src/audittrace/services/mcp_bridge.py                       63      0     16      0   100%
```

Zero skips. The 2 warnings are pre-existing (`ResourceWarning` in
`tests/test_memory_routes.py::TestPdfFlushManifestDetailsLog`, unrelated
to this change — confirmed via `git diff --name-only`).

## 2. OpenAPI drift gate — additive-only confirmation

```
$ git diff --stat docs/reference/audittrace/openapi.yaml tests/fixtures/openapi.snapshot.yaml
 docs/reference/audittrace/openapi.yaml | 26 ++++++++++++++++++++++++++
 tests/fixtures/openapi.snapshot.yaml   | 26 ++++++++++++++++++++++++++
 2 files changed, 52 insertions(+)
```

+26/-0 in both files: one new `/mcp` path entry, one new `mcp` OpenAPI
tag. `/v1/chat/completions` is byte-for-byte unchanged.

## 3. Local end-to-end capture through the real FastAPI app (Rule 3 shape)

Run against `audittrace.server.create_app()` (the actual production
factory — same object `uvicorn` serves in the deployed image) via
`fastapi.testclient.TestClient`, with `dependencies.container =
create_test_container()` (in-memory SQLite `InMemoryPostgresFactory`,
so `InteractionRecord`/`ToolCall` rows are real SQLAlchemy rows, not
mocked). `AUDITTRACE_AUTH_REQUIRED` unset → bypass-mode admin sentinel
identity (`00000000-0000-0000-0000-000000000001`).

```
POST /mcp {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
→ 200 {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25",
  "capabilities":{"tools":{"listChanged":false}},
  "serverInfo":{"name":"audittrace-mcp","version":"1.24.6"},
  "instructions":"AuditTrace-AI MCP entry-interface, Phase 1 (ADR-063)..."}}

POST /mcp {"jsonrpc":"2.0","method":"notifications/initialized"}
→ 202 (empty body)

POST /mcp {"jsonrpc":"2.0","id":2,"method":"tools/list"}
→ 200 tools: ["recall_decisions","recall_skills","recall_recent_sessions",
  "recall_semantic","read_decision","read_skill"]   (no write tool)

POST /mcp {"jsonrpc":"2.0","id":3,"method":"tools/call",
  "params":{"name":"recall_decisions","arguments":{"query":"test"}}}
→ 200 {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text",
  "text":"{\"matches\": [], \"total\": 0, ...}"}],
  "structuredContent":{"matches":[],"total":0,...},"isError":false}}

  Server logs for this call (unedited):
  INFO audittrace.services.mcp_bridge — mcp tools/call ok tool=recall_decisions
       user=00000000-0000-0000-0000-000000000001 duration_ms=3
  INFO audittrace.routes.chat — interaction persisted id=1
       user=00000000-0000-0000-0000-000000000001 status=success
       project=default model=None duration_ms=3
  INFO audittrace.routes.chat — tool_calls persisted count=1 interaction_id=1

POST /mcp {"jsonrpc":"2.0","id":4,"method":"bogus/method"}
→ 200 {"jsonrpc":"2.0","id":4,"error":{"code":-32601,
  "message":"Method not found: bogus/method"}}

GET /interactions
→ 200 {"interactions":[{"id":1,"project":"default","source":"mcp",
  "question":"{\"tool\": \"recall_decisions\", \"arguments\": {\"query\": \"test\"}}",
  "answer":"{\"matches\": [], \"total\": 0, ...}",
  "user_id":"00000000-0000-0000-0000-000000000001","status":"success",
  "content_hash":"c84cd04626564970bf01a9c7ec883478dc1ba382ce8bcdd3b665403847f6fb0b",
  ...}],"total":1,...}
```

**Reconstruction chain proven locally:** exactly one `InteractionRecord`
(`source="mcp"`) + exactly one `ToolCall` row landed for the one
`tools/call`, both readable back through the existing public
`GET /interactions` audit endpoint, with the ADR-058 `content_hash`
computed on the persisted fields — the SAME tamper-evident shape every
other interaction in the system gets, confirming MCP rides the existing
recorder rather than a parallel one.

## 4. What is explicitly NOT covered here (Rule 2 — deferred)

- No image was built or deployed for this evidence.
- No request was sent through the Istio front door with a real
  Keycloak-issued JWT.
- No trace was exported to Tempo/Langfuse (local dev has
  `AUDITTRACE_OTLP_ENDPOINT=""`, the laptop no-op default).

These are the ADR-049 Rule 2 (Validation) artefacts the spec defers to
the next candidate deploy cycle (the CD-deployer + deploy-verifier
agents, not the builder). When that deploy happens, capture: an MCP
client `tools/list` + `recall_*` call through the front door with a
scoped JWT, the resulting audit-row id, and the trace id — per the
spec's "Verification / Reconstruction (Rules 2 & 3)" section — and
supersede this file.
