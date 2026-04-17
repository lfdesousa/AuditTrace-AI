# ADR-033: Three-Audience Error Envelope with OpenAI Strict-Superset Compliance

**Status:** Accepted
**Date:** 2026-04-16
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-024 (proxy pass-through), ADR-029 (audit trail completeness),
ADR-032 (OAuth2 Device Flow)

## Context

A forensic investigation on 2026-04-16 revealed that 10 HTTP 500 events on
`POST /v1/chat/completions` across the preceding 48 hours left zero rows in
the `interactions` table. The root cause was structural: `_persist_interaction`
ran only after the streaming generator completed or the tool loop returned;
an upstream `httpx.ReadTimeout` escaped both paths and bubbled to Uvicorn's
default 500 handler — which returned a bare text body with no audit trail.

Three problems needed solving simultaneously:

1. **Audit gap.** Failures that most need reconstruction under EU AI Act
   Article 12 were exactly the ones the system could not reconstruct. Every
   failed request must produce an `interactions` row with a classified
   failure reason.

2. **Error shape divergence.** The existing error responses used ad-hoc JSON
   shapes (`{"detail": "..."}` from FastAPI, bare text from Uvicorn, and
   inconsistent fields in SSE error frames). Clients like OpenCode that
   depend on the OpenAI `Error` schema received unparseable responses on
   failure paths.

3. **Audience mismatch.** A single error message cannot serve the end user
   ("something went wrong, try again"), the operator on call ("check Loki
   for trace `abc123`"), and the engineer debugging ("504 proxy_timeout on
   upstream read after 300s"). The error payload must address all three
   without leaking internal detail to the wrong audience.

Two alternatives were considered:

- **A. Status quo + logging.** Keep FastAPI's default `{"detail": "..."}`
  shape and rely on Loki for post-hoc investigation. Rejected because the
  client receives no actionable information and the audit row is still
  missing.

- **B. RFC 7807 Problem Details.** Standard `application/problem+json`
  envelope. Rejected because it breaks OpenAI client compatibility — the
  `/v1/chat/completions` contract requires the `{error: {type, message,
  param, code}}` shape, and RFC 7807's `{type, title, status, detail}`
  is structurally incompatible.

## Decision

We adopt a three-audience error envelope that is a **strict superset of
the OpenAI `Error` schema**. The four OpenAI-required keys (`type`,
`message`, `param`, `code`) are always present and semantically correct.
AuditTrace-specific extensions are additive — any OpenAI-compatible client
can parse the response by ignoring unknown keys, per standard JSON
tolerance.

### Envelope shape

```json
{
  "error": {
    "message":              "Upstream model did not respond within the configured timeout.",
    "type":                 "api_error",
    "param":                null,
    "code":                 "proxy_timeout",
    "status":               504,
    "operator_hint":        "Check Loki for trace abc123def; review SOVEREIGN_LLAMA_PROXY_TIMEOUT (currently 300s).",
    "trace_id":             "abc123def456789",
    "user_facing_message":  "The model is taking longer than expected. Please try again."
  }
}
```

### Audience mapping

| Field | Audience | Purpose |
|-------|----------|---------|
| `user_facing_message` | End user | Safe to display in UI; no internal detail |
| `operator_hint`, `trace_id` | Operator on call | Pivot to Loki, Langfuse, or Grafana |
| `message`, `type`, `code`, `status` | Engineer | Full diagnostic context for debugging |
| `type`, `message`, `param`, `code` | OpenAI-compatible client | Parse without modification |

### Failure taxonomy

Four failure classes, each mapped to an HTTP status and persisted in the
`interactions` table via migration 007:

| Constant | Value | HTTP | Trigger |
|----------|-------|------|---------|
| `FAILURE_PROXY_TIMEOUT` | `proxy_timeout` | 504 | `httpx.ReadTimeout` — upstream did not respond within configured timeout |
| `FAILURE_UPSTREAM_UNREACHABLE` | `upstream_unreachable` | 502 | `httpx.ConnectError` — TCP connection to upstream refused or timed out |
| `FAILURE_UPSTREAM_ERROR` | `upstream_error` | proxied | `httpx.HTTPStatusError` — upstream returned a 4xx or 5xx status |
| `FAILURE_INTERNAL_ERROR` | `internal_error` | 500 | Any other exception on the memory-server side |

Reserved for future use: `validation_error`, `client_abort`, `auth_denied`.

### Audit persistence

Every request path — streaming, non-streaming, and tools mode — is wrapped
in try/except blocks that call `_persist_interaction` with `status="failed"`,
the appropriate `failure_class`, and `error_detail` before returning the
error response. The `interactions` table (migration 007) carries `status`
(default `"success"`), `failure_class`, `error_detail`, and `duration_ms`
columns with an index on `status` for failure queries.

### SSE error frames

For streaming responses where HTTP headers have already been sent, the error
envelope is delivered as a final SSE `data:` frame before the `data: [DONE]`
sentinel. The shape is identical to the JSON body envelope, ensuring clients
that parse SSE frames can handle failures without special-casing.

### Implementation

The envelope is constructed by the `_openai_error_body()` helper in
`routes/chat.py`, which accepts the four OpenAI-required keys plus the
AuditTrace extensions and returns the wrapped `{"error": {...}}` dict. A
global exception handler in `server.py` catches unhandled exceptions on
non-streaming routes and returns the same envelope shape.

## Consequences

**What becomes easier:**

- Clients receive structured, parseable error responses on every failure
  path. OpenCode and other OpenAI-compatible tools work without
  modification.
- Operators can pivot directly from an error response to the relevant Loki
  query or Langfuse trace using the embedded `trace_id`.
- Every failure is auditable: the `interactions` table has a row for every
  request regardless of outcome, closing the EU AI Act Article 12 gap.
- The failure taxonomy enables Prometheus metrics by class (deferred but
  now possible without schema changes).

**What becomes harder:**

- The envelope shape is now a contract. Removing or renaming any of the
  four OpenAI keys, or changing `code` values that clients may match on,
  is a breaking change requiring a new ADR.
- SSE error frames add complexity to client parsers that previously only
  handled `data: [DONE]` as a terminal signal. Clients must now check for
  `"error"` key in parsed SSE JSON.

**Known gap:**

FastAPI's built-in `HTTPException` still returns `{"detail": "..."}` on
routes other than `/v1/chat/completions`. A dedicated
`add_exception_handler(HTTPException)` that produces the OpenAI envelope
for all routes is deferred as low-priority — the chat completions path
is the only one with an OpenAI compatibility requirement.

**Validation:**

- 6 compatibility guard tests in `test_openai_compatibility.py` lock the
  success, streaming, and error shapes.
- 10 regression tests in `test_chat_failure_audit.py` verify audit
  persistence on every failure path.
- 539 tests pass, 94.86% coverage, per-file gate ≥ 90%.
- Live smoke: three rows captured (IDs 249–251) confirming non-streaming
  timeout, tools-mode timeout, and streaming success with correct
  failure_class and duration_ms values.
