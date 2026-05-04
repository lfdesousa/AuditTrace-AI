# AuditTrace-AI — Agent Instructions

## Identity
- **Python 3.12+** FastAPI service
- **Author:** Luis Filipe de Sousa
- **Entry point:** `src/audittrace/server.py` → `app = create_app()`
- **CLI:** `audittrace` entry point calls `main()` → `uvicorn.run()`
- **Deployment:** k3s + Istio + Helm; `charts/audittrace/` is the sole
  deployable unit (ADR-035 amendment, 2026-04-17). Sibling stacks
  (Langfuse, observability) and llama.cpp run as separate processes
  on the host (ADR-045).

## Setup & Workflow
```bash
# Install deps + pre-commit
make install

# Run tests (90% per-file coverage gate enforced; zero-skip policy)
make test

# Full check sequence
make lint && make format --check && make test

# Format code
make format

# Run server (dev, without k8s)
source .venv/bin/activate
uvicorn audittrace.server:app --reload
```

For cluster-deploy work see `docs/guides/deployment-runbook.md` and
`make help`.

## Test Strategy
- **Coverage:** 90% **per-file** gate (every file stands on its own, not the
  average). Enforced via `scripts/check-per-file-coverage.py` in `make test`.
- **850+ tests** across the suite (current count via `make test`; pinned
  numbers drift, so cite the script). Session-scoped
  `configure_observability` sets DEBUG logging + no-op OTel. Per-test
  `_reset_global_container` autouse fixture isolates DI state.
- **Zero-skip policy:** `scripts/check-no-skips.py` rejects any pytest skip.
  Required deps (e.g. test Postgres) are spun up explicitly, not skipped.
- **Env isolation:** `tests/conftest.py` clears every `AUDITTRACE_*` env var
  at collection time and sets `AUDITTRACE_ENV=test` so the suite runs in
  bypass mode with the sentinel `UserContext`.
- **Cross-user isolation:** `tests/test_cross_user_isolation.py` runs
  alice + bob across every memory layer in one authoritative file.
- **RLS integration:** `tests/test_rls_isolation.py` connects to the
  cluster's PostgreSQL via port-forward
  (`kubectl -n audittrace port-forward svc/audittrace-postgresql 15432:5432`)
  and verifies the migration-005 RLS policies bite. Never skipped — set
  `AUDITTRACE_TEST_POSTGRES_URL` or fail.
- **Doc drift guard:** `tests/test_docs_drift.py` pins forbidden stale
  terms in AGENTS.md (e.g. old `sovereign-*` names, replaced components).

## Architecture
- **Package:** `src/audittrace/` (Julien Danjou style)
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
- **OTel:** No-op by default; set `AUDITTRACE_OTLP_ENDPOINT` to enable export.
  OTel attribute prefix is `sovereign.component` / `sovereign.operation.*`
  — retained as-is per ADR-035 amendment for backwards-compat with existing
  dashboards.
- **Routes:** `/v1/chat/completions`, `/v1/models`, `/context`,
  `/interactions`, `/session/save`, `/session/summary`, `/memory/*`,
  `/health`, `/metrics`

## Identity + multi-user (ADR-026 §15, §16; ADR-032; ADR-044)
- **Keycloak-delegated** — no local `users` table. `require_user`
  validates a JWT against JWKS (cached 5 min), hot path hits a
  **Redis-backed `TokenCache`** keyed on `sha256(token)`.
- **Multi-issuer** — Keycloak's audittrace realm accepts tokens from
  external IdPs (Google Workspace live since 2026-05-02 per ADR-044).
- **`UserContext`** (frozen dataclass) threads through every memory
  service method as the first positional argument.
- **Postgres RLS** — migration 005 enables + forces RLS on
  `interactions`, `sessions`, `tool_calls`. Non-superuser
  `audittrace_app` role is what the memory-server connects as (created
  via `scripts/init-audittrace-app-role.sh`).
- **`db/rls.py`** — ContextVar + SQLAlchemy `after_begin` listener
  emits `set_config('app.current_user_id', :uid, true)` per
  transaction. SQLite-safe (listener no-ops on non-Postgres dialects).
- **`UserScopedSemanticService`** — thin wrapper binds a `UserContext`
  at construction; ignores any per-call argument. Isolation by
  construction for the ChromaDB seam.

## Memory-as-tools (ADR-025)
- **Kill switch:** `AUDITTRACE_MEMORY_MODE={inject|tools}`. Default is
  `tools` since 2026-04-14 (ADR-025 §Decision validated live).
- **Registry:** `src/audittrace/tools/__init__.py` — dynamic,
  decorator-based, optional TOML overlay for per-tool config.
- **Handlers:** `src/audittrace/tools/memory_handlers.py` —
  `recall_decisions`, `recall_skills`, `recall_recent_sessions`,
  `recall_semantic`. Each wraps an existing service and normalises
  results to `{matches, total, truncated}`.
- **Cache:** `src/audittrace/tools/cache.py` — `ToolResultCache`,
  Redis under `sovereign:tool-result:*` (key prefix retained per ADR-035;
  disjoint from `sovereign:token:*` used by `TokenCache`). Cache hits
  skip the `ToolCall` audit row (§Decision.8).
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
| `AUDITTRACE_LLAMA_URL` | `http://audittrace-llm-chat:11435/v1` | LLM endpoint (chart override sets the cluster name) |
| `AUDITTRACE_OTLP_ENDPOINT` | `` | OTLP/HTTP collector (no-op when empty) |
| `AUDITTRACE_LANGFUSE_ENABLED` | `false` | Langfuse integration |
| `AUDITTRACE_AUTH_ENABLED` | `false` | Legacy `require_scope` gate |
| `AUDITTRACE_AUTH_REQUIRED` | `true` (chart default) | Keycloak `require_user` gate; conftest wipes for tests |
| `AUDITTRACE_MEMORY_MODE` | `tools` | `inject` / `tools` — memory-as-tools kill switch |
| `AUDITTRACE_MEMORY_TOOL_LOOP_MAX_ITERATIONS` | `5` | Hard cap for the tool-call loop |
| `AUDITTRACE_MEMORY_TOOL_CACHE_TTL_SECONDS` | `900` | `0` disables the Redis tool result cache |
| `AUDITTRACE_REDIS_URL` | `redis://audittrace-redis-master:6379/0` | Shared between TokenCache + ToolResultCache |
| `AUDITTRACE_SUMMARIZER_URL` | (chart default) | Background summariser endpoint (ADR-030) |
| `AUDITTRACE_SUMMARIZER_CTX_TOKENS` | `32768` | Pre-flight ctx ceiling for summariser (backlog #10) |

## Code Style
- **Formatter:** `ruff format` (line-length 88, py312 target)
- **Linter:** `ruff check` (E/W/F/I/N/UP/YTT; ignores E501)
- **Typecheck:** `mypy --strict` (ignores missing imports)
- **pre-commit:** hooks for ruff, ruff-format, black, mypy, gitleaks,
  and the privacy gate (see below)

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

No `Co-Authored-By` tags on commits.

## Cluster operations (k3s + Istio + Helm)
- **Build + push image:** `make k8s-build TAG=<unique-tag>` (honours
  `TAG=…` since 2026-05-04; pushes to local registry on `localhost:5000`).
- **Install / upgrade:** `make k8s-install` (first time) or
  `make k8s-rolling-image TAG=<tag>` (image-only iteration).
  Both gated by `make deploy-preflight`.
- **Bootstrap secrets (post-install / post-upgrade):**
  `export VAULT_TOKEN=<root-or-operator-token> && make k8s-bootstrap-secrets`.
  Idempotent. Chains `setup-vault.sh` (Vault policies + roles + KV seeds)
  and `setup-memory-scopes.sh` (Keycloak `memory:*:write` scopes).
- **Verify gate (always run after any deploy):** `make verify-deploy`.
  9 checks including pod readiness, helm release status, `/health`,
  `/metrics`, `pg_isready`, Tempo / Loki, and a Vault drift guard
  (ConfigMap policies/roles ⊆ actual Vault state — closes the
  2026-05-03 chart↔Vault drift class).
- **Stack (Helm subcharts):** memory-server + PostgreSQL + ChromaDB +
  Redis + Keycloak + HashiCorp Vault, all behind Istio Gateway with
  mTLS via PeerAuthentication.

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
  `_flush_pending_tool_calls` are still synchronous. ADR-046 (Proposed,
  v1.0.7) designs the opt-in `X-Persist-Mode: async` header for
  callers that want lower TTFB.
- **Superuser bypass:** in dev, if you connect as the `sovereign`
  Postgres superuser role you bypass RLS entirely (role name retained
  per ADR-035 amendment). Use `audittrace_app` (created via
  `scripts/init-audittrace-app-role.sh`) for anything where isolation
  matters.

## ADRs (full list in README)
- [ADR-014](docs/ADR-014-python-package-structure.md) — Package layout
- [ADR-014.4](docs/ADR-014.4-observability-logging-otel.md) — Observability
- [ADR-018](docs/ADR-018-four-layer-memory-port.md) — 4-layer memory
- [ADR-019](docs/ADR-019-chromadb-server-mode.md) — ChromaDB server
- [ADR-020](docs/ADR-020-postgresql-server-databases.md) — PostgreSQL
- [ADR-022](docs/ADR-022-keycloak-realm.md) — Keycloak realm
- [ADR-024](docs/ADR-024-proxy-passthrough-and-langfuse-trace-decoupling.md) — Proxy pass-through
- [ADR-025](docs/ADR-025-memory-as-tools.md) — Memory-as-tools
- [ADR-026](docs/ADR-026-multi-user-identity.md) — Multi-user identity (Accepted)
- [ADR-029](docs/ADR-029-audit-trail-completeness.md) — Audit-trail completeness
- [ADR-030](docs/ADR-030-session-summarizer.md) — Background session summariser
- [ADR-032](docs/ADR-032-oauth2-device-authorization-flow.md) — OAuth2 device flow
- [ADR-033](docs/ADR-033-three-audience-error-envelope.md) — Three-audience error envelope
- [ADR-034](docs/ADR-034-long-running-generation.md) — Long-running generation
- [ADR-035](docs/ADR-035-package-rename.md) — **Package rename + retention exceptions**
- [ADR-041](docs/ADR-041-product-boundary-and-dependencies.md) — Product boundary
- [ADR-042](docs/ADR-042-oidc-authorization-code-pkce.md) — OIDC AuthCode PKCE
- [ADR-043](docs/ADR-043-vault-as-sole-secret-store.md) — Vault as sole secret store
- [ADR-044](docs/ADR-044-external-idp-federation.md) — External IdP federation (Accepted, Google live)
- [ADR-045](docs/ADR-045-laptop-first-no-lan-hardcodes.md) — Laptop-first, no LAN hardcodes
- [ADR-046](docs/ADR-046-async-chat-persistence.md) — Async chat persistence (Proposed)
