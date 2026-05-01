# ADR-018: Port 4-Layer Memory Architecture to audittrace-server

Date: 2026-04-10

## Status

Accepted

## Context

The audittrace-server was a Phase 0 skeleton with clean architecture
(DI container, factory pattern, @log_call observability, 90% test coverage)
but only implemented Layer 4 (ChromaDB semantic search) of the advertised
4-tier memory architecture. The `/v1/chat/completions` endpoint returned a
stub response. The working 4-layer implementation existed in
the predecessor project's LangChain backend and was validated in production
with all layers confirmed in Langfuse traces (ADR-017).

The port needed to preserve the existing architectural patterns:
- Factory/DI pattern for all services
- `@log_call` on every method for observability
- LangChain as the foundation (`Document`, `ChatMessageHistory`,
  `Chroma`, `OpenAIEmbeddings`)
- 90% test coverage threshold enforced
- OpenAI API compatibility on `/v1/chat/completions`

## Decision

Port all 4 memory layers into audittrace-server as separate service
classes following the existing ABC + implementation + mock pattern:

**Layer 1 — Episodic:** `FileEpisodicService` reads `ADR-*.md` files from
the filesystem. Query-driven keyword filtering, no arbitrary caps.

**Layer 2 — Procedural:** `FileProceduralService` reads `SKILL-*.md` files.
Same keyword matching pattern against skill names and content.

**Layer 3 — Conversational:** `SQLiteConversationalService` stores and
retrieves session summaries from SQLite. Provides continuity across
conversations. Table schema: `(id, project, date, summary, key_points, model)`.

**Layer 4 — Semantic:** `ChromaSemanticService` wraps the existing ChromaDB
infrastructure. Searches across multiple collections (`decisions`, `skills`).

**Aggregator:** `DefaultContextBuilder` receives all 4 services via
constructor injection. `build_system_context()` assembles a structured
markdown context string from all layers. Exception isolation ensures one
layer's failure does not break the others.

**Proxy:** `/v1/chat/completions` extracts the query from the last user
message (or `context_query` field), builds memory context, augments the
system message, and proxies the enriched request to llama-server via `httpx`.
The response is passed through unchanged — full OpenAI API compatibility.

**DI wiring:** All services registered in `DependencyContainer` via
`register_default_dependencies()`. Mock versions registered via
`create_test_container()` for testing.

Configuration paths (`adr_dir`, `skill_dir`, `sessions_db`,
`llama_proxy_timeout`) added to `Settings` with `AUDITTRACE_` prefix.

## Consequences

### Positive

- All 4 memory layers operational — the server does what it advertises
- 146 tests, 91.65% coverage (above 90% threshold)
- Every method `@log_call` decorated — full observability
- DI at the LangChain layer — all services mockable and testable in isolation
- No arbitrary caps — query-driven retrieval
- OpenAI API compatibility preserved — transparent augmentation proxy
- Existing Phase 0 tests updated without breakage

### Negative

- `httpx` synchronous call in chat proxy — should be `httpx.AsyncClient`
  for production streaming support. Deferred to a streaming-focused ADR.
- Episodic and procedural layers use keyword matching, not semantic search.
  A future improvement would route all retrieval through ChromaDB embeddings.

## Files Changed

### New (12 files)
- `src/audittrace/services/episodic.py`
- `src/audittrace/services/procedural.py`
- `src/audittrace/services/conversational.py`
- `src/audittrace/services/semantic.py`
- `src/audittrace/services/context_builder.py`
- `tests/test_episodic_service.py`
- `tests/test_procedural_service.py`
- `tests/test_conversational_service.py`
- `tests/test_semantic_service.py`
- `tests/test_context_builder.py`
- `tests/test_chat_proxy.py`
- `docs/ADR-018-four-layer-memory-port.md`

### Modified (5 files)
- `src/audittrace/config.py` — 4-layer path settings
- `src/audittrace/models.py` — `ContextBuildResponse`, `project` on `ChatRequest`
- `src/audittrace/dependencies.py` — register all layer services + getters
- `src/audittrace/routes/chat.py` — memory augmentation + httpx proxy
- `src/audittrace/routes/context.py` — wire ContextBuilderService

## References

- ADR-017 (predecessor project): 4-layer memory activation + cap removal
- ADR-016 (predecessor project): Memory bus bandwidth optimisation
- ADR-014.2: Logging and DI pattern (audittrace-server)
- ADR-014.4: Observability logging + OTel (audittrace-server)
