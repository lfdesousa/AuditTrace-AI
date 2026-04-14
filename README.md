# AuditTrace-AI

![Sovereign Architecture Overview](./docs/sovereign-architecture-overview.png)

Production-grade sovereign AI memory server with 4-layer memory architecture, Docker Compose deployment, TLS, and full observability.

## Research & Governance

This implementation is based on the technical and regulatory framework defined in:
**"Sovereign Local AI: Why On-Device LLM Inference on Unified Memory Hardware Outperforms Commercial API Stacks for Regulated Industries"** (March 2026).

> [!IMPORTANT]
> **[Read the Full Technical Analysis (PDF)](./main_signed.pdf)**
> Digitally signed via SwissSign to certify authenticity and research priority.

This work formalises the *Sovereignty-Reconstructibility Gap* and provides an auditable architecture compliant with **EU AI Act Article 12** and **GDPR Article 44**. The PDF is digitally signed, providing authorship non-repudiation, temporal anchoring, and tamper-evidence -- consistent with the reconstructibility principles defined in Section 7 of the paper.

> **Academic trajectory:** This research is under discussion with the University of Liverpool Department of Computer Science as a potential PhD programme, building on the author's MSc in Big Data Analytics (Liverpool, 2022).

[![CI](https://github.com/lfdesousa/AuditTrace-AI/actions/workflows/ci.yml/badge.svg)](https://github.com/lfdesousa/AuditTrace-AI/actions/workflows/ci.yml)

## Features

- 🧠 **4-Layer Memory Architecture** -- Episodic (ADRs), procedural (skills), conversational (PostgreSQL), semantic (ChromaDB)
- 🛠️ **Memory-as-Tools** -- LLM calls `recall_decisions`, `recall_skills`, `recall_recent_sessions`, `recall_semantic` on demand instead of paying the 4-layer cost on every prompt (ADR-025). Dynamic registry, Redis-backed result cache, configurable iteration cap.
- 👥 **Multi-user Identity** -- Keycloak-delegated OAuth2 + per-user `UserContext` plumbing + Postgres Row-Level Security + ChromaDB scoped wrapper. Non-superuser `sovereign_app` role means RLS actually bites at the DB layer (ADR-026).
- 🗄️ **Server-Mode Databases** -- PostgreSQL 16 + ChromaDB HTTP server + Redis 7, all with authentication (ADR-019, ADR-020)
- 🔒 **TLS Everywhere** -- Traefik v3 reverse proxy with mkcert certificates (ADR-021)
- 🔍 **Reconstructible by Design** -- Every interaction + memory tool call traced via Langfuse + OpenTelemetry; one `tool_calls` audit row per memory invocation with `interaction_id` FK
- 📊 **Full-Stack Observability** -- `@log_call` aspect emits logs, OTel spans, and histogram metrics from a single decorator; stdout-only logging (12-factor)
- 🔄 **Transparent LLM Proxy** -- Raw dict pass-through for OpenAI tool-calling protocol (ADR-024); memory context injected into the system message without stripping `tools`, `tool_calls`, `tool_call_id`
- 🇪🇺 **GDPR-Compliant** -- Data never leaves your infrastructure
- 🔌 **OpenAI-Compatible** -- `/v1/chat/completions` API
- 🐳 **Docker Compose** -- One command deployment: memory-server, PostgreSQL, ChromaDB, Redis, Keycloak, Traefik
- ✅ **Comprehensive Test Suite** -- 90% project-wide + 90% per-file coverage gates enforced in CI ([latest run](https://github.com/lfdesousa/AuditTrace-AI/actions/workflows/ci.yml))

## Quick Start

### Deploy with Docker Compose

```bash
# Clone repository
git clone https://github.com/lfdesousa/AuditTrace-AI
cd AuditTrace-AI

# Generate secrets (postgres password + chroma token)
./scripts/setup-secrets.sh

# Generate TLS certificates (requires mkcert)
./certs/generate-certs.sh

# Create shared Docker network
docker network create sovereign-ai-net

# Configure environment
cp .env.example .env
# Edit .env with the secrets from setup-secrets.sh output

# Deploy (or use the full stack script which includes Langfuse)
./scripts/start-full-stack.sh
```

The stack exposes:
- **https://localhost** -- memory-server API (TLS via Traefik)
- **http://localhost:8080** -- Traefik dashboard
- **http://localhost:3000** -- Langfuse (if set up)

### Optional: Langfuse Observability

```bash
# Bootstrap Langfuse as sibling compose stack (ADR-021.2)
./scripts/setup-langfuse.sh
cd ../langfuse && docker compose up -d
cd ../AuditTrace-AI

# 1. Open http://localhost:3000 in your browser
# 2. Sign up (first user becomes admin), create a project
# 3. Settings -> API Keys -> Create new key. Copy public + secret keys.

# Set in your .env (replace pk-lf-... and sk-lf-... with your real keys):
SOVEREIGN_LANGFUSE_ENABLED=true
SOVEREIGN_LANGFUSE_HOST=http://host.docker.internal:3000
SOVEREIGN_LANGFUSE_PUBLIC_KEY=pk-lf-your-public-key
SOVEREIGN_LANGFUSE_SECRET_KEY=sk-lf-your-secret-key
# OTLP endpoint with basic auth -- Langfuse v3 native ingest
SOVEREIGN_OTLP_ENDPOINT=http://pk-lf-your-public-key:sk-lf-your-secret-key@host.docker.internal:3000/api/public/otel/v1/traces
# Langfuse only accepts OTLP traces, not metrics -- disable to silence 400 errors
SOVEREIGN_METRICS_ENABLED=false

# Restart to pick up changes
docker compose up -d --force-recreate memory-server

# Verify: traces should appear at http://localhost:3000 after the next request
curl -sk https://localhost/health
```

**Note:** We use `host.docker.internal` instead of the Langfuse container name
because Langfuse's web container binds only to its own network interface, and
mounting cross-network in Docker is fragile. Routing through the host's
published `:3000` port is simpler and just as fast on localhost.

### Local Development

```bash
# Setup isolated development environment
./scripts/setup.sh
# or
make install

# Run tests (90% coverage required)
make test

# Run linting + formatting
make lint
make format

# Run development server (without Docker)
source .venv/bin/activate
uvicorn sovereign_memory.server:app --reload
```

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI-compatible chat with memory augmentation |
| `/context` | POST | 4-layer memory retrieval |
| `/interactions` | GET | Human-facing audit browser — filters `project`, `user_id`, `session_id`, `source`, `since`; pagination via `limit` (≤1000) + `offset`. RLS scopes every caller to their own rows. See [Auditing conversations](#auditing-conversations-interactions). |
| `/session/save` | POST | Persist session data |
| `/health` | GET | Health check |
| `/metrics` | GET | Monitoring metrics |

## Architecture

### Docker Compose Topology

```
Client (HTTPS :443, Bearer JWT)
    |
    v
+---------+     +------------------+     +--------------+
| Traefik |---->| memory-server    |---->| PostgreSQL 16|
| (TLS)   |     | (FastAPI +       |     | (RLS: sover- |
+---------+     |  require_user +  |     |  eign_app    |
      ^         |  tool-call loop) |     |  non-super)  |
      |         +-----+----+------+      +--------------+
      |               |    |
      |               |    v
+---------+           |  +--------------+     +--------------+
| Keycloak|<----------+  | ChromaDB     |---->| embed-server |
| (JWKS)  |              | (token auth  |     | (nomic, CPU) |
+---------+              |  + scoped    |     +--------------+
                         |  wrapper)    |            ^
                         +--------------+     host.docker.internal
                                ^                    |
                                |             +--------------+
                         +--------------+     | llama-server |
                         | Redis 7      |     | (Qwen, ROCm) |
                         |  sovereign:  |     +--------------+
                         |  token:*     |
                         |  tool-result:*|
                         +--------------+
```

Langfuse runs as a **sibling compose stack** on the shared `sovereign-ai-net` network (ADR-021.2). Redis serves both the `TokenCache` (JWT hot path) and the `ToolResultCache` (memory-as-tools result cache) under disjoint key prefixes.

### 4-Layer Memory

| Layer | Service | Storage | Purpose |
|---|---|---|---|
| 1. Episodic | `FileEpisodicService` | ADR-*.md files | Architecture decisions |
| 2. Procedural | `FileProceduralService` | SKILL-*.md files | Reusable skill documents |
| 3. Conversational | `PostgresConversationalService` | PostgreSQL 16 | Session history + continuity |
| 4. Semantic | `ChromaSemanticService` | ChromaDB server | Vector search / RAG |

All services follow the ABC + implementation + mock pattern with `@log_call` observability.

### Memory Access Modes (ADR-025)

The 4-layer memory can be exposed to the LLM in two different ways. The choice is a runtime flag (`SOVEREIGN_MEMORY_MODE`) and does not change the underlying storage layout.

| Mode | What happens on every `/v1/chat/completions` | Trade-off |
|---|---|---|
| **`tools`** *(live default — `.env` ships with this set)* | The proxy advertises four recall tools to the LLM and runs a tool-call loop. The model decides which layer it needs and calls `recall_decisions`, `recall_skills`, `recall_recent_sessions`, or `recall_semantic` on demand — at most once per question in practice. Results are cached in Redis (TTL 900s) and audit-logged to `tool_calls`. | Pay memory cost only when the model asks. Extra round-trips to llama-server (one per tool iteration), bounded by `SOVEREIGN_MEMORY_TOOL_LOOP_MAX_ITERATIONS`. |
| **`inject`** *(legacy — v0.2.x default)* | The proxy retrieves all 4 layers up front and injects them into the system message. The model sees a preassembled context block and never issues tool calls for memory. | Zero tool-loop latency but every prompt pays the full 4-layer retrieval + token cost, even when the model would have ignored memory. |

The four tools in `tools` mode each map to one layer and carry their own Keycloak scope (`memory:episodic:read`, `memory:procedural:read`, `memory:conversational:read-own`, `memory:semantic:read`). Per-user isolation, RLS, and the ChromaDB scoped wrapper apply unchanged to both modes — the difference is purely *when* the layers are queried, not *what* the user sees.

Switch modes by setting `SOVEREIGN_MEMORY_MODE=inject|tools` in `.env` and recreating the `memory-server` container. See [ADR-025](docs/ADR-025-memory-as-tools.md) for the full design and the acceptance evidence.

### Security

- **OAuth2** -- Keycloak-delegated identity (ADR-022, ADR-023, ADR-026). Every request is a JWT validated against the Keycloak JWKS endpoint. Hot path hits a Redis-backed `TokenCache` for sub-millisecond lookups.
- **Per-user isolation, defense in depth:**
  1. **Service layer** -- every memory service method takes a `UserContext` first arg; conversational `load_sessions` applies unconditional `WHERE user_id =`
  2. **Postgres RLS** -- `interactions`, `sessions`, `tool_calls` all carry `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY` + one policy per table gated on `current_setting('app.current_user_id', true)`
  3. **Non-superuser app role** -- memory-server connects as `sovereign_app` (NOSUPERUSER + NOBYPASSRLS). Postgres superusers always bypass RLS, so this is not optional for real enforcement.
  4. **ChromaDB wrapper** -- `UserScopedSemanticService` binds a `UserContext` at construction and overrides any per-call context; isolation is true by construction
  5. **Session-id uniqueness** -- `_compute_session_id` includes `user_id` in the sha256 so two users with the same first message never produce a colliding session id
- **TLS** -- Traefik terminates HTTPS on :443 with mkcert certificates
- **ChromaDB** -- Token-based authentication via `Authorization: Bearer` header
- **Network isolation** -- Database services on Docker internal network only
- **Secrets** -- Credentials via environment variables, never in code

### Authentication (ADR-022, ADR-023, ADR-026)

All endpoints except `/health` require a valid Keycloak JWT. `SOVEREIGN_AUTH_REQUIRED=true` is the docker-compose default -- every request needs `Authorization: Bearer <JWT>`.

**Scopes exposed by the `sovereign-ai` realm:**

| Scope | Endpoints / Tools |
|---|---|
| `sovereign-ai:query` | `/v1/chat/completions`, `/session/save` |
| `sovereign-ai:context` | `/context` |
| `sovereign-ai:audit` | `/interactions` |
| `sovereign-ai:admin` | `/metrics` |
| `memory:episodic:read` | `recall_decisions` tool (ADR-025) |
| `memory:procedural:read` | `recall_skills` tool |
| `memory:conversational:read-own` | `recall_recent_sessions` tool |
| `memory:semantic:read` | `recall_semantic` tool |

### Roadmap

| Priority | Item | Description |
|---|---|---|
| ✅ Done | **Intelligent Memory Tool Routing** | Solved via system prompt guidance — ambient context instructs the LLM to pick ONE tool per question. Validated with Qwen3.5-35B-A3B. Quantitative measurement pending (needs observability dashboards). ADR-025 Accepted. |
| ✅ Done | **MinIO Object Storage** | Stateless 12-factor containers — host filesystem mounts replaced with MinIO S3 (ADR-027). Two-tier buckets: `memory-shared` (ADRs, skills) + `memory-private` (per-user, JWT sub prefix). SSE-S3 encryption at rest. |
| ✅ Done | **Observability Aggregation Stack** | Prometheus + Grafana + Loki + OTel Collector as sibling compose stack (ADR-028). Scrapes Traefik, llama-server, MinIO. Promtail for container log aggregation. Pre-provisioned Grafana dashboard. |
| ✅ Done | **mypy --strict Clean** | 0 errors across 41 source files. Pre-commit pipeline passes without SKIP=mypy. types-redis stubs installed. |
| ✅ Done | **Audit Trail Completeness** | Project tagging via X-Project header (ADR-029) + urllib3 instrumentation + Tempo service-graph clean host-name edges. `/interactions` endpoint live with RLS-scoped pagination. |
| 🔴 Next | **Session Summarizer** | Hybrid `recall_recent_sessions` (sessions table first, interactions fallback) + background summarizer loop writing `SessionRecord` rows. ADR-030. |
| 🔴 Next | **Full Package Rename** | Rename Python package from `sovereign_memory` to `audittrace`, env prefix from `SOVEREIGN_` to `AUDITTRACE_`, all container/service/network names. Dedicated PR. |
| 🟡 Planned | **OAuth2 Device Flow** | Human authentication beyond the current `client_credentials` dev client. Dedicated Keycloak public client for OpenCode. |
| 🟡 Planned | **Tool Routing Measurement** | Quantitative comparison of inject vs tools mode: latency, token efficiency, context utilisation. `scripts/eval-memory-modes.py` harness ready; baseline run pending. |
| 🔵 Future | **Async Persistence** | Non-blocking audit row writes for `_persist_interaction` and `_flush_pending_tool_calls`. Prerequisite satisfied by ADR-029 (payload now carries project tag). |
| 🔵 Future | **Kubernetes** | K3s + Istio mTLS + SPIFFE/SVID identity. |

## Observability Stack (ADR-028)

Sibling Docker Compose stack following the Langfuse pattern (ADR-021.2). Provides metrics aggregation, log search, and dashboards.

```bash
# Start the observability stack
./scripts/setup-observability.sh

# Stop
./scripts/setup-observability.sh --down
```

| Service | URL | Purpose |
|---|---|---|
| Grafana | `http://localhost:3001` (admin / sovereign) | Dashboards: latency percentiles, error rates, infra metrics, logs |
| Prometheus | `http://localhost:19090` | Metrics storage + PromQL queries |
| Loki | `http://localhost:3100` | Log aggregation + LogQL queries |
| OTel Collector | `http://localhost:4318` | OTLP receiver (memory-server exports here) |

**Data flow:**
- Memory-server sends OTLP metrics/logs to OTel Collector, which fans out to Prometheus + Loki
- Prometheus scrapes Traefik, llama-server, and MinIO natively
- Promtail auto-discovers Docker containers and pushes logs to Loki
- Langfuse retains exclusive ownership of LLM traces (SDK path, unchanged)
- Grafana queries both Prometheus and Loki for the unified operations dashboard

**Pre-provisioned dashboard:** "Sovereign AI Operations" — P50/P95/P99 latency, error rates by type, llama-server tokens/sec, KV cache usage, MinIO API requests, container error logs.

## Memory Seeding (ADR-027)

After a fresh clone or when knowledge changes:

```bash
# Generate secrets (includes MinIO KMS key)
./scripts/setup-secrets.sh

# Upload to MinIO + index into ChromaDB
python scripts/seed-memory.py --user-id <keycloak-sub-claim>

# Index only (skip MinIO upload)
python scripts/index-chromadb.py --user-id <keycloak-sub-claim>

# Selective re-index
python scripts/index-chromadb.py --collections decisions skills

# Preview without writing
python scripts/index-chromadb.py --dry-run --user-id <sub>
```

| Collection | Source | Content |
|---|---|---|
| `decisions` | `docs/ADR-*.md` | Architecture Decision Records |
| `skills` | `~/work/claude-config/skills/` | Domain knowledge skill files |
| `ai_research` | `~/work/ai-knowledge/` | Research papers (PDF + text) |
| `scm_coursework` | `~/work/scm-knowledge/` | MIT SCM coursework (transcripts, notes, slides) |

## Auditing conversations (`/interactions`)

The `/interactions` endpoint is the **human-facing** view over the audit trail — distinct from the **model-facing** `recall_*` tools. The LLM never calls it; it's what you (or a dashboard) curl when you want to browse or filter history.

```bash
# Every row the caller is allowed to see, newest first
curl -sk -H "Authorization: Bearer $TOKEN" \
  "https://localhost/interactions?limit=20"

# Scoped to one project
curl -sk -H "Authorization: Bearer $TOKEN" \
  "https://localhost/interactions?project=AuditTrace-AI&limit=50"

# Everything in one session
curl -sk -H "Authorization: Bearer $TOKEN" \
  "https://localhost/interactions?session_id=opencode-2026-04-14-d1c6f4a0f67f4f67"

# Rolling window since a point in time
curl -sk -H "Authorization: Bearer $TOKEN" \
  "https://localhost/interactions?since=2026-04-14T00:00:00&limit=100"
```

Filters: `project`, `user_id`, `session_id`, `source`, `since` (ISO-8601).
Pagination: `limit` (1-1000, default 100) + `offset`.
Response: `{interactions: [...], total, limit, offset}` — `total` reflects
the filter match count *for the caller*.

**Per-user isolation is automatic.** Postgres RLS sets `app.current_user_id` from the caller's Keycloak `sub` on every transaction (ADR-026). You cannot accidentally see another user's rows, even with a forged `user_id` query parameter — the RLS policy intersects `WHERE user_id = current_setting(...)`. Scope required on the token: `sovereign-ai:audit`.

**Project tagging contract** (ADR-029). Every interaction carries a `project` tag resolved on entry: `X-Project` header → `body.metadata.project` → `body.project` → `"default"`. Configure the header per-project once via `scripts/configure-project.py <name>`.

## Configuration

### Database (ADR-020)

| Variable | Default | Description |
|---|---|---|
| `SOVEREIGN_POSTGRES_URL` | -- | Full PostgreSQL URL. Production connects as the non-superuser `sovereign_app` role so RLS policies actually apply |
| `SOVEREIGN_POSTGRES_PASSWORD` | -- | Database password (required) |
| `SOVEREIGN_APP_PASSWORD` | fallback to `SOVEREIGN_POSTGRES_PASSWORD` | Dedicated password for the `sovereign_app` role |
| `SOVEREIGN_CHROMA_URL` | `http://chromadb:8000` | ChromaDB server URL |
| `SOVEREIGN_CHROMA_TOKEN` | -- | ChromaDB auth token |
| `SOVEREIGN_REDIS_URL` | `redis://redis:6379/0` | Redis URL for TokenCache + ToolResultCache |
| `SOVEREIGN_REDIS_PASSWORD` | -- | Required for the TokenCache hot path |
| `SOVEREIGN_TOKEN_CACHE_TTL_SECONDS` | `300` | JWT TTL in TokenCache |

### Auth (ADR-022, ADR-023, ADR-026)

| Variable | Default | Description |
|---|---|---|
| `SOVEREIGN_AUTH_REQUIRED` | `true` (docker-compose default) | When true, every request needs a valid Keycloak JWT |
| `SOVEREIGN_KEYCLOAK_ISSUER` | `http://keycloak:8080/realms/sovereign-ai` | Must match the JWT `iss` claim |
| `SOVEREIGN_KEYCLOAK_JWKS_URL` | `http://keycloak:8080/realms/sovereign-ai/protocol/openid-connect/certs` | JWKS endpoint for RS256 validation (cached 5 min) |
| `SOVEREIGN_JWT_AUDIENCE` | `sovereign-memory-server` | Must match the JWT `aud` claim |

### Memory-as-Tools (ADR-025)

| Variable | Default | Description |
|---|---|---|
| `SOVEREIGN_MEMORY_MODE` | `tools` (shipped `.env`) / `inject` (compose fallback) | `tools` = proxy-internal tool-call loop (current default); `inject` = legacy 4-layer dump into system message. See [Memory Access Modes](#memory-access-modes-adr-025). |
| `SOVEREIGN_MEMORY_TOOL_LOOP_MAX_ITERATIONS` | `5` | Hard cap on tool-call round-trips per request |
| `SOVEREIGN_MEMORY_TOOL_CACHE_TTL_SECONDS` | `900` | Redis tool result cache TTL; `0` disables caching |
| `SOVEREIGN_TOOLS_CONFIG_PATH` | `tools.toml` | Optional TOML overlay to disable/rename/retune tools |

### Observability (ADR-014.4)

| Variable | Default | Description |
|---|---|---|
| `SOVEREIGN_LOG_LEVEL` | `INFO` | Log level |
| `SOVEREIGN_OTLP_ENDPOINT` | `""` (no export) | OTLP/HTTP collector URL |
| `SOVEREIGN_OTEL_SERVICE_NAME` | `sovereign-memory-server` | OTel service name |
| `SOVEREIGN_TRACING_ENABLED` | `true` | Enable OTel tracing |
| `SOVEREIGN_METRICS_ENABLED` | `true` | Enable OTel metrics |

### Langfuse (ADR-021.2)

| Variable | Default | Description |
|---|---|---|
| `SOVEREIGN_LANGFUSE_HOST` | `http://langfuse-web:3000` | Langfuse server URL |
| `SOVEREIGN_LANGFUSE_PUBLIC_KEY` | -- | Langfuse public key |
| `SOVEREIGN_LANGFUSE_SECRET_KEY` | -- | Langfuse secret key |
| `SOVEREIGN_LANGFUSE_ENABLED` | `false` | Enable Langfuse integration |

## Documentation

### Architecture Decision Records

- [ADR-014: Python Package Structure](docs/ADR-014-python-package-structure.md)
- [ADR-014.2: Logging & Dependency Injection](docs/ADR-014.2-logging-dependency-injection.md)
- [ADR-014.3: Makefile + Virtual Environment](docs/ADR-014.3-makefile-venv.md)
- [ADR-014.4: Observability -- Logging + OpenTelemetry](docs/ADR-014.4-observability-logging-otel.md)
- [ADR-018: 4-Layer Memory Port](docs/ADR-018-four-layer-memory-port.md)
- [ADR-019: ChromaDB Server Mode](docs/ADR-019-chromadb-server-mode.md)
- [ADR-020: PostgreSQL + Server-Mode Databases](docs/ADR-020-postgresql-server-databases.md)
- [ADR-021: TLS with mkcert + Traefik](docs/ADR-021-tls-mkcert-traefik.md)
- [ADR-021.2: Langfuse as Sibling Compose Stack](docs/ADR-021.2-langfuse-sibling-stack.md)
- [ADR-022: Keycloak Realm Configuration](docs/ADR-022-keycloak-realm.md)
- [ADR-023: JWT Validation + JWKS Caching](docs/ADR-023-jwt-validation-jwks-caching.md)
- [ADR-024: Chat Proxy Pass-Through + Langfuse Trace Decoupling](docs/ADR-024-proxy-passthrough-and-langfuse-trace-decoupling.md)
- [ADR-025: Memory-as-Tools](docs/ADR-025-memory-as-tools.md)
- [ADR-026: Multi-user Identity, Scopes, and Cross-user Isolation](docs/ADR-026-multi-user-identity.md)
- [ADR-027: MinIO Object Storage](docs/ADR-027-minio-object-storage.md)
- [ADR-028: Observability Aggregation Stack](docs/ADR-028-observability-aggregation-stack.md)
- [ADR-029: End-to-End Audit Trail — Project Tagging & HTTP Telemetry Refinements](docs/ADR-029-audit-trail-completeness.md)

### Architecture Diagrams

- [C4 Workspace (Structurizr DSL)](docs/architecture/workspace.dsl)
- [Sequence -- chat completions (inject mode)](docs/architecture/sequence-chat-completions.md)
- [Sequence -- memory tool-call loop (tools mode)](docs/architecture/sequence-memory-tool-call.md)
- [Sequence -- OAuth2 flow](docs/architecture/sequence-oauth2-flow.md)

### Operator Playbooks

- [Langfuse Dashboards -- Recipes](docs/langfuse-dashboards.md)
- [Agent Configuration (OpenCode, Continue, Roo Code)](docs/agent-configuration.md)
- Dev JWT minting: `scripts/mint-dev-jwt.sh`
- App role init: `scripts/init-sovereign-app-role.sh` (creates the non-superuser Postgres role for RLS)

## Legal Disclaimer & Copyright

Copyright &copy; 2026 Luis Filipe de Sousa ([allaboutdata.eu](https://allaboutdata.eu)). All rights reserved.

- **Independent Research:** This project, including the "Sovereign Local AI Stack" framework and the AuditTrace-AI implementation, is the result of independent research conducted by the author in a private capacity.
- **Resource Separation:** No employer data, confidential information, or corporate infrastructure was used in the development of this work.
- **Institutional Independence:** The views, architectures, and code presented here do not represent the positions, strategies, or opinions of any current or former employer.
- **Licensing:** While the core engine is licensed under [AGPL v3](./LICENSE) to support Open Science, the underlying theoretical framework and specific architectural designs (ADRs) remain the intellectual property of allaboutdata.eu.

---

*"The software artefact that separates compliant deployments from merely well-architected ones."*
