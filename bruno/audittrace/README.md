# Bruno collection — AuditTrace-AI

Public-API smoke + walkthrough collection. Mirrors every route in `docs/reference/audittrace/openapi.yaml` so a Bruno run against a deployed image is the live-evidence pivot — no curl-by-hand.

## Layout

```
bruno/audittrace/
├── audit/          — /interactions, /sessions
├── auth/           — Device Flow init/poll/refresh (RFC 8628, ADR-032)
├── chat/           — /v1/models, /v1/chat/completions
├── context/        — /context (memory bundle for a query)
├── environments/   — cluster + local
├── health/         — /health, /metrics
├── mcp/            — POST /mcp (JSON-RPC, ADR-063 Phase 1+2 A/B/C)
├── memory/         — per-layer CRUD + index/upload (5 ops × 4 layers)
│   ├── conversational/
│   ├── episodic/
│   ├── procedural/
│   └── semantic/
├── session/        — /session/save, /session/summary
└── system/         — /system/trust-store (GET + POST refresh, ADR-052)
```

## Getting started

1. Install Bruno (CLI or desktop): https://www.usebruno.com/.
2. Open this collection in Bruno.
3. Pick an environment:
   - **`local`** — `http://localhost:8765`, Keycloak on `localhost:8081`. Used with the docker-compose dev stack.
   - **`cluster`** — `https://audittrace.allaboutdata.eu:30952`. Used against the k3s deploy.
4. Mint an access token via the Device Flow chain (`auth/01-device-flow-init` → open the URL in a browser → `auth/02-device-flow-poll`). The polled response writes `accessToken` into the env's secret vars.
5. Run any request. The bearer auth on each .bru pulls `{{accessToken}}` from the env automatically.

## Scopes per request

The default env scope string includes every routine + admin scope:

```
openid memory:episodic:read memory:procedural:read memory:semantic:read
memory:conversational:read-own memory:episodic:write memory:procedural:write
memory:semantic:write audittrace:query audittrace:context audittrace:audit
audittrace:admin
```

`audittrace:admin` was added 2026-05-09 alongside the new
`/system/trust-store/*` admin endpoints (ADR-052 §5). If you don't
need admin paths, you can trim the scope to keep token surface
minimal.

## What's covered (post-2026-05-09 update)

| Route | Coverage |
|---|---|
| `GET /health` | health/health.bru |
| `GET /metrics` | health/metrics.bru |
| `POST /v1/chat/completions` | chat/02-chat-completions.bru, chat/03-chat-completions-param.bru, chat/04-chat-untrusted-traceparent.bru |
| `GET /v1/models` | chat/01-list-models.bru |
| `POST /context` | context/01-context.bru ← **NEW 2026-05-09** |
| `GET /interactions` | audit/01-list-interactions.bru |
| `POST /interactions` | audit/03-create-interaction.bru ← **NEW 2026-05-09** |
| `GET /sessions` | audit/02-list-sessions.bru |
| `POST /session/save` | session/01-save-session.bru ← **NEW 2026-05-09** |
| `POST /session/summary` | session/02-summary-session.bru ← **NEW 2026-05-09** |
| `POST /memory/upload` | memory/upload-legacy.bru |
| `POST /memory/index` | memory/index-single-file.bru, memory/index-legacy.bru |
| `GET/POST/DELETE/PUT /memory/{layer}/{...}` | memory/{conversational,episodic,procedural,semantic}/01..05 |
| `GET /system/trust-store` | system/01-trust-store-get.bru ← **NEW 2026-05-09** |
| `POST /system/trust-store/refresh` | system/02-trust-store-refresh.bru ← **NEW 2026-05-09** |
| `POST /mcp` (JSON-RPC) | mcp/01-tools-list.bru, mcp/02-tools-call-read.bru, mcp/03-tools-call-write.bru, mcp/04-tools-call-broker-template.bru, mcp/05-no-auth-401.bru ← **NEW 2026-08-24 (ADR-063 Phase 2 Track C, folds read+write+broker+negative)** |

## Chat recall + TRACEPARENT-TRUST chain (BRUNO-CHAT-PARAM)

Closes the v1.30.0 candidate verify gaps F2 (no parameterisable chat
request that can drive a recall turn), F3 (corroboration tests passing
vacuously as separate runs) and F5 (no shipped request sends a
`traceparent`).

| Request | Proves |
|---|---|
| `chat/03-chat-completions-param.bru` | `POST /v1/chat/completions` with the prompt from env `chatPrompt` (default asks for the ratified memory-mode decision, which triggers a recall in `memory_mode=tools`), optional `X-Session-Id` from `chatSessionId`, `stream:false`. 200 + OpenAI shape (id/object/created/model/choices/usage), additive-only key check, and with `expectSourcesTrailer=true` the LC-6 trailer `\n\n**Sources consultées**\n- <source_ref>...` at the END of `choices[0].message.content` (label: `routes/_sources_trailer.py:47`; appended by `routes/chat.py:1890-1896` when `AUDITTRACE_RESPONSE_SOURCES=trailer`). |
| `chat/04-chat-untrusted-traceparent.bru` | Same call with a valid W3C `traceparent` (env `inboundTraceparent`, default carries the obviously synthetic trace id `0123456789abcdef0123456789abcdef`) and a unique `X-Session-Id`. |
| `chat/05-audit-session-server-trace.bru` | Finds the 04 interaction by session: exactly one row, 32-hex non-zero `trace_id` that is NOT the inbound id. Stashes the server trace id + interaction id. |
| `chat/06-audit-inbound-trace-zero-rows.bru` | `GET /interactions?trace_id=<inbound>` returns **0 rows**: the server never adopted the caller's trace context (`telemetry.py:90` `ServerMintedTraceContext`, `:145` `inbound_trace_context_hook`). Fails if the server adopted it. |
| `chat/07-audit-server-trace-one-row.bru` | Positive control: the same filter with the server id returns exactly the 04 interaction, so the 0 in 06 is not a broken filter. |
| `chat/08-audit-tool-calls-of-traceparent-turn.bru` | The `tool_calls` audit rows (ids, tools, scopes) of that interaction; with `expectSourcesTrailer=true` at least one recall row is required. |

**These must run in ONE `bru run`.** 05-08 read runtime vars (`bru.setVar`)
set by 04 and carry precondition tests that FAIL (not pass vacuously)
when the earlier requests did not run in the same invocation. Run
sequence is the `seq` order:

```bash
cd bruno/audittrace
bru run chat --env cluster --env-var expectSourcesTrailer=true \
  --output ../../evidence/chat-param-run.json     # folder form: 01..08
# or only the new chain (explicit paths, one invocation):
bru run chat/03-chat-completions-param.bru chat/04-chat-untrusted-traceparent.bru \
  chat/05-audit-session-server-trace.bru chat/06-audit-inbound-trace-zero-rows.bru \
  chat/07-audit-server-trace-one-row.bru chat/08-audit-tool-calls-of-traceparent-turn.bru \
  --env cluster --env-var expectSourcesTrailer=true --output run.json
```

Knobs: `chatPrompt`, `chatSessionId` (empty = generated), `expectSourcesTrailer`
(`true` only against a deploy with `AUDITTRACE_RESPONSE_SOURCES=trailer`),
`inboundTraceparent`. A recall turn on the local model can take minutes.
`--output` JSON masks the bearer token; secret-scan it before sharing.

## Live-validation — 2026-05-09

All 4 newly-added requests smoke-tested via curl against the v1.0.16 cluster:

- `POST /context` — returns `{context_string, layer_stats, query, project, retrieved_at}`.
- `POST /session/save` — accepts `{project, interactions[]}`, returns `{status: "ok", project, interactions_saved, metadata}`.
- `POST /session/summary` — accepts `{project, summary, key_points[], session_id}`, returns `{status: "ok", session_id, project}`.
- `GET /system/trust-store` — returns 200 with `{sha256, builder_id, cert_count, source_url, built_at}` when the bundle is provisioned, 404 otherwise.
- `POST /system/trust-store/refresh` — returns 200 with the new bundle's metadata after walking EU LOTL + Swiss TSL (~25-35 s).
- `POST /interactions` — returns 200 with the persisted record's id.

## Pre-M5 demo walkthrough

For the M5 off-LAN rehearsal (HARD 2026-05-15) the recommended Bruno run order:

1. `health/health.bru` — confirm version + components.
2. `auth/01-device-flow-init.bru` → `auth/02-device-flow-poll.bru` — mint a token (interactive, opens browser).
3. `system/02-trust-store-refresh.bru` — provision the trust store (one-time per fresh install).
4. `system/01-trust-store-get.bru` — verify metadata.
5. `memory/upload-legacy.bru` — upload a fixture PDF.
6. `memory/index-single-file.bru` — index it.
7. `memory/episodic/01-list.bru` — confirm signature_status flipped correctly.
8. `chat/02-chat-completions.bru` — exercise the augmented chat path.
9. `audit/01-list-interactions.bru` — show the audit row appeared.
