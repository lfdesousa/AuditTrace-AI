# ADR-019: ChromaDB Server Mode

**Status:** Accepted
**Date:** 2026-04-10
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-020 (PostgreSQL + server-mode DBs), ADR-021.2 (Langfuse sibling stack)

## Context

Phase 0 used ChromaDB in embedded/persistent mode (`PersistentClient`), storing
vectors in a local `chroma_data/` directory. This worked for single-process
development but is incompatible with Docker Compose deployment where the
application container must be stateless.

ADR-020 eliminated `SQLiteChromaDBFactory` and added token-based authentication
to `HTTPChromaDBFactory`. This ADR completes the transition by running ChromaDB
as a dedicated Docker service.

## Decision

Run ChromaDB as a Docker Compose service in HTTP server mode with token-based
authentication.

### Configuration

- **Image:** `chromadb/chroma:latest`
- **Auth:** `CHROMA_SERVER_AUTHN_PROVIDER=chromadb.auth.token_authn.TokenAuthenticationServerProvider`
- **Token:** Via `CHROMA_SERVER_AUTHN_CREDENTIALS` environment variable
- **Storage:** Named Docker volume `chroma_data`
- **Network:** `audittrace-net` (shared with memory-server)
- **Health check:** `GET /api/v1/heartbeat`

### Client Connection

The existing `HTTPChromaDBFactory` (updated in ADR-020) connects with:
```python
HTTPChromaDBFactory(url="http://chromadb:8000", token=settings.chroma_token)
```

No application code changes needed — the factory and token auth were pre-wired.

## Consequences

### Positive
- Stateless application container — ChromaDB data persists in Docker volume
- Token-based authentication — no anonymous access
- Embedding server connection managed by ChromaDB, not the application
- Health check integration with Docker Compose `depends_on`

### Negative
- ChromaDB must be running for the application to start (hard dependency)
- Token must be distributed to all clients via environment variables

### Neutral
- Same `HTTPChromaDBFactory` code path used in Phase 0 development and Phase 1 Docker
