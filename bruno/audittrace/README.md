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
| `POST /v1/chat/completions` | chat/02-chat-completions.bru |
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
