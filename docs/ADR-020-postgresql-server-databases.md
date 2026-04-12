# ADR-020: PostgreSQL + Server-Mode Databases

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Luis Filipe de Sousa  
**Related:** ADR-018 (4-layer memory port), ADR-014 (package structure)

## Context

Phase 0 used file-based databases: SQLite for conversational session history
(Layer 3) and ChromaDB in embedded/persistent mode for semantic search (Layer 4).
This worked for local development but is incompatible with a Docker Compose
deployment where services must be stateless containers connecting to shared
database servers.

File-based databases also lack:
- Connection pooling
- Concurrent access safety
- Authentication and authorization
- Schema migration management
- Backup and restore workflows

## Decision

**Eliminate all file-based databases.** From Phase 1 forward:

1. **PostgreSQL 16** replaces SQLite for all relational data (sessions, audit trail).
2. **ChromaDB server mode** replaces embedded ChromaDB for vector search.
3. Both databases are network services with authentication, managed via Docker Compose.
4. **Alembic** manages PostgreSQL schema migrations in code.
5. **SQLAlchemy ORM** provides connection pooling and type-safe queries.

### Security Model

**PostgreSQL:**
- Dedicated `sovereign_app` role with least-privilege grants.
- Permissions: `CONNECT`, `USAGE` on schema, `SELECT/INSERT/UPDATE/DELETE` on tables.
- No `CREATE`, `DROP`, `ALTER`, or superuser privileges for the application user.
- Password via `SOVEREIGN_POSTGRES_PASSWORD` environment variable.
- Listens only on Docker internal network in production.

**ChromaDB:**
- Token-based authentication via `CHROMA_SERVER_AUTHN_CREDENTIALS`.
- Token provided via `SOVEREIGN_CHROMA_TOKEN` environment variable.
- `Authorization: Bearer <token>` header on all HTTP requests.
- Listens only on Docker internal network in production.

### Factory Pattern

Follows the established ABC + implementation + mock pattern from Phase 0:

```
PostgresFactory (ABC)
  +-- URLPostgresFactory     (production: real PostgreSQL via URL)
  +-- InMemoryPostgresFactory (tests: SQLite in-memory via SQLAlchemy)
  +-- MockPostgresFactory     (tests: call tracking, no real connections)
```

### What Was Removed

- `SQLiteConversationalService` class (not deprecated, deleted)
- `SQLiteChromaDBFactory` class (deleted)
- `sessions_db` config field (deleted)
- `chroma_persist_dir` config field (deleted)
- All `sqlite3` imports from service code

### What Was Added

- `PostgresConversationalService` (same `ConversationalService` ABC)
- `PostgresFactory` ABC with 3 implementations
- `SessionRecord` SQLAlchemy ORM model
- Alembic migrations directory with initial `sessions` table migration
- `chroma_token` config field for ChromaDB authentication
- `database_url` property updated with `postgresql+psycopg2://` driver prefix

## Consequences

### Positive
- Production-ready database layer with connection pooling and auth
- Schema migrations are versioned and auditable
- No file permissions or disk space issues from DB files
- Clear separation: application code never touches raw SQL
- Test isolation via `InMemoryPostgresFactory` — no real PG needed for CI

### Negative
- Docker Compose becomes a hard dependency for running the full stack
- Credential management required (env vars, Docker secrets)
- Slightly more complex local development setup

### Neutral
- Test count increased from 146 to 180+
- Coverage maintained above 90% floor
- Existing `ConversationalService` ABC unchanged — consumers unaffected
