# AuditTrace-AI — Agent Instructions

## Identity
- **Python 3.12+** FastAPI service
- **Author:** Luis Filipe de Sousa
- **Entry point:** `src/audittrace/server.py` → `app = create_app()`
- **CLI:** `audittrace` entry point calls `main()` → `uvicorn.run()`

## Setup & Workflow
```bash
# Install deps + pre-commit
make install

# Run tests (90% per-file coverage gate enforced)
make test

# Full check sequence
make lint && make format --check && make test

# Format code
make format

# Run server (dev, without Docker)
source .venv/bin/activate
uvicorn audittrace.server:app --reload
```

## Test Strategy
- **Coverage:** 90% **per-file** gate (every file stands on its own, not the
  average). Enforced via `scripts/check-per-file-coverage.py` in `make test`.
- **421 tests** across 25+ files. Session-scoped `configure_observability`
  sets DEBUG logging + no-op OTel. Per-test `_reset_global_container`
  autouse fixture isolates DI state.
- **Env isolation:** `tests/conftest.py` clears every `AUDITTRACE_*` env var
  at collection time and sets `AUDITTRACE_ENV=test` so the suite runs in
  bypass mode with the sentinel `UserContext`, regardless of the docker-
  compose default (which is now `AUDITTRACE_AUTH_REQUIRED=true`).
- **Cross-user isolation:** `tests/test_cross_user_isolation.py` runs
  alice + bob across every memory layer in one authoritative file.
- **RLS integration:** `tests/test_rls_isolation.py` connects to the
  running sovereign-postgres container as a non-superuser test role and
  verifies the migration-005 RLS policies bite. Skips-if-unreachable.

## Architecture
- **Package:** `audittrace/` under `src/` (Julien Danjou style)
- **Factory pattern:** DB factory in `dependencies.py` with
  `create_test_container()` for tests
- **Dependency injection:** Global `container` module-level variable,
  reset between tests. `get_context_builder(user)` is **per-request**:
  wraps the shared semantic service in `UserScopedSemanticService` bound
  to the caller's `UserContext` (ADR-026 Phase 4 follow-up).
- **Observability:** `@log_call` decorator (ADR-014.4) emits logs, OTel
  spans, histograms to stdout only. Defensive `record_exception` so
  `LangfuseSpan` instances don't crash the request path on
  `HTTPException`.
- **OTel:** No-op by default; set `AUDITTRACE_OTLP_ENDPOINT` to enable export
- **Routes:** `/v1/chat/completions`, `/v1/models`, `/context`,
  `/interactions`, `/session/save`, `/session/summary`, `/health`,
  `/metrics`

## Identity + multi-user (ADR-026 §15, §16)
- **Keycloak-delegated** — no local `users` table. `require_user`
  validates a JWT against JWKS (cached 5 min), hot path hits a
  **Redis-backed `TokenCache`** keyed on `sha256(token)`.
- **`UserContext`** (frozen dataclass) threads through every memory
  service method as the first positional argument.
- **Postgres RLS** — migration 005 enables + forces RLS on
  `interactions`, `sessions`, `tool_calls`. Non-superuser
  `audittrace_app` role is what the memory-server connects as (created
  via `scripts/init-sovereign-app-role.sh`).
- **`db/rls.py`** — ContextVar + SQLAlchemy `after_begin` listener
  emits `set_config('app.current_user_id', :uid, true)` per
  transaction. SQLite-safe (listener no-ops on non-Postgres dialects).
- **`UserScopedSemanticService`** — thin wrapper binds a `UserContext`
  at construction; ignores any per-call argument. Isolation by
  construction for the ChromaDB seam.

## Memory-as-tools (ADR-025)
- **Kill switch:** `AUDITTRACE_MEMORY_MODE={inject|tools}`. Default is
  `inject` in process code; docker-compose can override.
- **Registry:** `src/audittrace/tools/__init__.py` — dynamic,
  decorator-based, optional TOML overlay for per-tool config.
- **Handlers:** `src/audittrace/tools/memory_handlers.py` —
  `recall_decisions`, `recall_skills`, `recall_recent_sessions`,
  `recall_semantic`. Each wraps an existing service and normalises
  results to `{matches, total, truncated}`.
- **Cache:** `src/audittrace/tools/cache.py` — `ToolResultCache`,
  Redis under `sovereign:tool-result:*` (disjoint from
  `sovereign:token:*`). Cache hits skip the `ToolCall` audit row
  (§Decision.8).
- **Loop:** `src/audittrace/routes/_memory_tool_loop.py` —
  proxy-internal non-streaming round-trip, bounded by
  `AUDITTRACE_MEMORY_TOOL_LOOP_MAX_ITERATIONS` (default 5). Defensive
  scope re-check at dispatch time so stale `tool_calls` after a scope
  revocation still get rejected.
- **No Langchain.** `langchain` and `langchain-community` deps were
  dropped in ADR-025 Phase 0. Only `langchain-core` is retained for
  the passive `Document` dataclass import.

## Environment Variables (subset — see README for the full tables)

| Variable | Default | Notes |
|---|---|---|
| `AUDITTRACE_HOST` | `0.0.0.0` | Bind address |
| `AUDITTRACE_PORT` | `8765` | Server port |
| `AUDITTRACE_LLAMA_URL` | `http://host.docker.internal:11435/v1` | LLM endpoint |
| `AUDITTRACE_OTLP_ENDPOINT` | `` | OTLP/HTTP collector (no-op when empty) |
| `AUDITTRACE_LANGFUSE_ENABLED` | `false` | Langfuse integration |
| `AUDITTRACE_AUTH_ENABLED` | `false` | Legacy `require_scope` gate |
| `AUDITTRACE_AUTH_REQUIRED` | `true` (docker-compose default) | Keycloak `require_user` gate; conftest wipes for tests |
| `AUDITTRACE_MEMORY_MODE` | `inject` | `inject` / `tools` — memory-as-tools kill switch |
| `AUDITTRACE_MEMORY_TOOL_LOOP_MAX_ITERATIONS` | `5` | Hard cap for the tool-call loop |
| `AUDITTRACE_MEMORY_TOOL_CACHE_TTL_SECONDS` | `900` | `0` disables the Redis tool result cache |
| `AUDITTRACE_REDIS_URL` | `redis://redis:6379/0` | Shared between TokenCache + ToolResultCache |
| `AUDITTRACE_REDIS_PASSWORD` | — | Required — generate via `scripts/setup-secrets.sh` |

## Code Style
- **Formatter:** `ruff format` (line-length 88, py312 target)
- **Linter:** `ruff check` (E/W/F/I/N/UP/YTT; ignores E501)
- **Typecheck:** `mypy --strict` (ignores missing imports)
- **pre-commit:** hooks for ruff, ruff-format, black, mypy

## Privacy: no private/legal content in the public repo
A pre-commit hook (`scripts/check-no-private-content.sh`, wired in
`.pre-commit-config.yaml`) flags forbidden patterns before they can
land. Forbidden: customer / counterparty names of non-publicly-
approaching parties, contact details (email domains tied to those
parties), and commercial pricing patterns (currency-with-figure,
billing-cadence language, spending-cap language).
Path exceptions: `docs/pitch/`, `docs/phd/`, vendored content under
`docs/reference/`, and the gate script itself.

If the gate fires: fix the content, don't bypass the hook. Move the
substance to `~/work/audittrace-private/` or `~/work/pitch-private/`
and reference it generically. Adding new private parties: extend
`FORBIDDEN_PATTERNS` in the gate script.

## Commit Messages
Conventional Commits. Types: `feat`, `fix`, `docs`, `style`, `refactor`,
`test`, `chore`, `perf`, `ci`, `build`, `revert`.

Examples:
- `feat(rls): DESIGN §16 Phase 4 — Postgres RLS + UserScopedSemanticService`
- `refactor(chat): propagate UserContext end-to-end`
- `docs(adr): Phase 6 — promote DESIGN-multi-user-identity to ADR-026 (Accepted)`

## Docker
- **Image:** Multi-stage build, non-root user
- **Network:** `audittrace-net` (external, `docker network create` at setup)
- **Health check:** `scripts/healthcheck.sh` → `curl http://localhost:8765/health`
- **Stack:** memory-server + PostgreSQL 16 + ChromaDB + Redis 7 +
  Keycloak 24 + Traefik v3

## Known Quirks
- **Logging:** All logs to stdout (12-factor). `caplog` is flaky in this
  suite for assertions on `@log_call` output — monkey-patch
  `logger.warning` directly instead (see `test_memory_tools_registry.py`
  + `test_context_builder.py` for the pattern).
- **Telemetry:** `telemetry._reset_for_tests()` must be called before tests.
- **Routes:** `/v1` prefix only on chat router; others use root paths.
- **Factory:** `create_test_container()` returns mock services + in-memory
  Postgres + MockChromaDBFactory; no real connections.
- **Async persistence:** `_persist_interaction` and
  `_flush_pending_tool_calls` are still synchronous (ADR deferred).
- **Superuser bypass:** in dev, if you connect as the `sovereign`
  superuser role you bypass RLS entirely. Use `audittrace_app` (created
  via `scripts/init-sovereign-app-role.sh`) for anything where
  isolation matters.

## ADRs (full list in README)
- [ADR-014](docs/ADR-014-python-package-structure.md) — Package layout
- [ADR-014.2](docs/ADR-014.2-logging-dependency-injection.md) — Logging + DI
- [ADR-014.3](docs/ADR-014.3-makefile-venv.md) — Makefile + venv
- [ADR-014.4](docs/ADR-014.4-observability-logging-otel.md) — Observability
- [ADR-018](docs/ADR-018-four-layer-memory-port.md) — 4-layer memory
- [ADR-019](docs/ADR-019-chromadb-server-mode.md) — ChromaDB server
- [ADR-020](docs/ADR-020-postgresql-server-databases.md) — PostgreSQL
- [ADR-021](docs/ADR-021-tls-mkcert-traefik.md) — TLS
- [ADR-022](docs/ADR-022-keycloak-realm.md) — Keycloak realm
- [ADR-023](docs/ADR-023-jwt-validation-jwks-caching.md) — JWT validation
- [ADR-024](docs/ADR-024-proxy-passthrough-and-langfuse-trace-decoupling.md) — Proxy pass-through
- [ADR-025](docs/ADR-025-memory-as-tools.md) — Memory-as-tools
- [ADR-026](docs/ADR-026-multi-user-identity.md) — **Multi-user identity (Accepted)**
