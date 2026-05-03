# Memory-layer CRUD backoffice (operator guide)

> Operator-facing REST API for managing the three non-conversational
> memory layers — episodic (ADRs), procedural (SKILL files), semantic
> (RAG vectors). Shipped in v1.0.3. Per-layer write scopes; soft-delete
> with full audit trail (created_at_ms / modified_at_ms / deleted_at_ms +
> user_id for each).

## When to use this

You're an operator (admin) managing memory content for a deployment.
You want to add an ADR through HTTP rather than `kubectl exec`-ing
into a pod and running `seed-memory.py`. Same for SKILL files and
direct semantic upserts.

For end-user uploads via LibreChat / future UIs, the upstream surface
is the same endpoints — what changes is the JWT scope (defaults vs
optional, see "Scopes" below).

## Endpoint matrix

All paths mounted under the existing `/memory/` router. JSON request
bodies; JSON responses returning `ManifestEntry` (or a list / read
shape).

### Episodic + Procedural (S3-backed)

| Method | Path | Required scope | Notes |
|--------|------|----------------|-------|
| POST | `/memory/episodic` | `memory:episodic:write` | Body: `{filename, content, title?}`. Idempotent — re-creating a soft-deleted item revives it. |
| GET | `/memory/episodic` | `memory:episodic:read` | List manifest rows. `?include_deleted=true` shows soft-deleted. |
| GET | `/memory/episodic/{filename}` | `memory:episodic:read` | Returns `{content, metadata, manifest}`. |
| PUT | `/memory/episodic/{filename}` | `memory:episodic:write` | Body: `{content, title?}`. Bumps `modified_at_ms`. |
| DELETE | `/memory/episodic/{filename}` | `memory:episodic:write` | Soft-delete by default. `?hard=true` also purges S3 (additionally requires `audittrace:admin`). |

Same shape for `/memory/procedural/` — substitute `procedural` for
`episodic` everywhere.

### Semantic (ChromaDB)

Items keyed by `<collection>/<document_id>`:

| Method | Path | Required scope | Notes |
|--------|------|----------------|-------|
| POST | `/memory/semantic` | `memory:semantic:write` | Body: `{collection, document_id, text, metadata?, title?}`. Upsert into ChromaDB. |
| GET | `/memory/semantic` | `memory:semantic:read` | List. `?collection=<name>` to filter by collection. |
| GET | `/memory/semantic/{collection}/{document_id}` | `memory:semantic:read` | Read. |
| PUT | `/memory/semantic/{collection}/{document_id}` | `memory:semantic:write` | Replace text + metadata. |
| DELETE | `/memory/semantic/{collection}/{document_id}` | `memory:semantic:write` | Soft-delete; `?hard=true` also removes from ChromaDB. |

## Manifest entry shape

Every write/list/delete response returns this dict:

```json
{
  "id": "uuid",
  "layer": "episodic|procedural|semantic",
  "key": "ADR-007.md",
  "title": "ADR-007: …",
  "size_bytes": 4096,
  "created_at_ms": 1714742400000,
  "modified_at_ms": 1714742400000,
  "created_by_user_id": "9e7a8d0f-3b5c-…",
  "modified_by_user_id": "9e7a8d0f-3b5c-…",
  "deleted_at_ms": null,
  "deleted_by_user_id": null
}
```

**Timestamps are Unix epoch milliseconds UTC.** Match `Date.now()` in
JS / `int(time.time() * 1000)` in Python. Use `new Date(ms)` to render
in the browser.

## Scopes

Three new Keycloak scopes, declared in
`charts/audittrace/files/realm-audittrace.json`:

- `memory:episodic:write`
- `memory:procedural:write`
- `memory:semantic:write`

Granted by default to:
- **`admin-client`** (service-account flow) — for backoffice
  scripts running with elevated identity.

Granted as optionalClientScopes to:
- **`audittrace-webui`** — browser PKCE flows. Operators request the
  scopes during the auth-code redirect when they need them.
- **`audittrace-opencode`** — Device Flow for CLI agents. Same
  pattern.

The existing `memory:<layer>:read` scopes (already present from
ADR-025) cover GET endpoints.

## Soft-delete vs hard-delete

By default, DELETE soft-deletes: the manifest row's `deleted_at_ms`
is set, the underlying S3 object / ChromaDB doc stays. Subsequent
GETs return 404 (row hidden); audit can still see it via
`?include_deleted=true` on LIST.

Soft-deletes are reversible — POSTing the same key revives the
manifest row (`deleted_at_ms` cleared, `modified_at_ms` bumped, new
`modified_by_user_id`).

Hard-delete (`?hard=true`) additionally removes the storage-side
content. Required scope: the per-layer `memory:<layer>:write` PLUS
`audittrace:admin`. Not reversible.

## Cache invalidation

The `S3EpisodicService` and `S3ProceduralService` keep an in-process
cache of all loaded objects (warmed on first `load()`). Pre-v1.0.3
this only refreshed on pod restart — uploads via `/memory/upload`
weren't visible to the model until the next deploy.

The CRUD endpoints invalidate the cache on every successful
write/delete. Subsequent `recall_decisions` / `read_decision` /
`recall_skills` / `read_skill` tool calls see the new state without
a pod restart.

## Operator workflow examples

### Add an ADR

```sh
TOKEN=$(scripts/audittrace-login --show)
curl -X POST https://<your-host>:30952/memory/episodic \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "filename": "ADR-100-memory-backoffice.md",
    "content": "# ADR-100: Memory backoffice\n\nStatus: Accepted\n…",
    "title": "ADR-100: Memory backoffice"
  }'
```

### Update an ADR

```sh
curl -X PUT https://<your-host>:30952/memory/episodic/ADR-100-memory-backoffice.md \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ "content": "# ADR-100\n\nStatus: Accepted (revised)\n…" }'
```

### List with deleted

```sh
curl "https://<your-host>:30952/memory/episodic?include_deleted=true" \
  -H "Authorization: Bearer $TOKEN" | jq '.items[] | {key, deleted_at_ms}'
```

### Hard-delete (full purge)

```sh
curl -X DELETE "https://<your-host>:30952/memory/episodic/ADR-100.md?hard=true" \
  -H "Authorization: Bearer $TOKEN"
```

### Upsert a semantic doc

```sh
curl -X POST https://<your-host>:30952/memory/semantic \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "collection": "decisions",
    "document_id": "adr-100-summary",
    "text": "Operator-driven memory CRUD backoffice with…",
    "metadata": {"source": "ADR-100", "project": "AuditTrace"}
  }'
```

## Migration 009 — `memory_items` table

Schema (from `src/audittrace/migrations/versions/009_create_memory_items.py`):

```sql
CREATE TABLE memory_items (
  id VARCHAR(36) PRIMARY KEY,
  layer VARCHAR(16) NOT NULL,
  key VARCHAR(255) NOT NULL,
  title VARCHAR(255),
  size_bytes INTEGER,
  created_at_ms BIGINT NOT NULL,
  modified_at_ms BIGINT NOT NULL,
  created_by_user_id VARCHAR(36) NOT NULL,
  modified_by_user_id VARCHAR(36) NOT NULL,
  deleted_at_ms BIGINT,
  deleted_by_user_id VARCHAR(36),
  CONSTRAINT uq_memory_items_layer_key UNIQUE (layer, key)
);
CREATE INDEX ix_memory_items_layer_deleted_at
  ON memory_items (layer, deleted_at_ms);
```

No RLS — the manifest is operator-global (the items themselves are
shared across users). Per-layer write scope at the route gate is the
authority boundary.

## Telemetry / OpenTelemetry coverage

Per the user directive (2026-05-03 evening) +
`feedback_traceability_requirement`: every CRUD operation MUST be
visible in OTel + Langfuse + Loki. Coverage is structural, not
optional:

* **HTTP-level spans** — FastAPI auto-instrumentation
  (`FastAPIInstrumentor.instrument_app(app)` in `server.py`) wraps
  every `/memory/<layer>` request. The span carries
  `http.method`, `http.target`, `http.status_code`, plus
  `langfuse.user.id` once the route's `require_user` resolves it.
* **Service-method spans** — every `MemoryManifestService` method
  (`record_create`, `record_update`, `record_delete`,
  `list_for_layer`, `get`) carries the project's `@log_call`
  decorator, which emits a Tempo+Langfuse span tagged with the
  operation name + extracted `user_id`. Same for the new
  `EpisodicService.write/.delete/.invalidate_cache`,
  `ProceduralService.*`, `SemanticService.upsert/.delete_document/.get_document`.
* **DB query spans** — `SQLAlchemyInstrumentor` wraps every
  manifest INSERT/UPDATE/SELECT.
* **Storage backend spans** — `urllib3` instrumentation captures
  outbound HTTP to MinIO + ChromaDB.
* **Regression guard** — `tests/test_services_memory_manifest.py::TestTelemetryCoverage`
  asserts `@log_call` is present on every public method of every
  CRUD-related service class. A future refactor that strips the
  decorator fails the test instead of silently breaking the trace.

To verify in production:
1. Hit a CRUD endpoint with a valid JWT.
2. Open Tempo → search by `service.name=audittrace-server` →
   recent traces. The trace should show:
   - root `POST /memory/episodic` span (FastAPI)
   - child `MemoryManifestService.record_create` span
   - grandchild `INSERT INTO memory_items …` span (SQLAlchemy)
3. Open Langfuse → filter by `user_id` (the JWT `sub`). The
   manifest spans appear under "user observations".
4. Open Loki → query `{namespace="audittrace"} |= "memory_items"`
   to see structured `[INPUT]` / `[OUTPUT]` log lines tagged with
   `user_id`, `operation`, `call_args`.

If any of those is empty for a CRUD call you just made, the trace
was lost. The decorator is missing OR a downstream sidecar
(otel-collector, Tempo, Langfuse, Loki) is sick — investigate
before assuming the new code is the cause.

## Cross-references

- ADR-025 — Memory layers as LLM-callable tools (read paths).
- ADR-027 — MinIO single-gateway (read + write).
- ADR-018 — 4-layer memory architecture (the four layers + their
  storage backends).
- `feedback_storage_always_s3` — Layers 1+2 are S3-only; FS fallback
  removed in v1.0.1.
- `feedback_no_shortcuts` — Why the manifest is a separate table
  rather than custom S3 metadata.
- `feedback_traceability_requirement` — Mandatory OTel coverage on
  every new feature (the rationale behind the regression guard).
- `docs/guides/idp-federation-setup.md` — How to grant the new
  write scopes to a fresh IdP-federated user.
