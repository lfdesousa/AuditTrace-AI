# AuditTrace-AI

![Sovereign Architecture Overview](./docs/sovereign-architecture-overview.png)

Production-grade sovereign AI memory server with 4-layer memory architecture, full observability, and Zero Trust deployment on Kubernetes (Istio mTLS, SPIFFE/SVID workload identity, deny-all AuthorizationPolicies) or Docker Compose for local development.

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
- 👥 **Multi-user Identity** -- Keycloak-delegated OAuth2 + per-user `UserContext` plumbing + Postgres Row-Level Security + ChromaDB scoped wrapper. Non-superuser `audittrace_app` role means RLS actually bites at the DB layer (ADR-026).
- 🗄️ **Server-Mode Databases** -- PostgreSQL 16 + ChromaDB HTTP server + Redis 7, all with authentication (ADR-019, ADR-020)
- 🔒 **TLS Everywhere** -- Traefik v3 reverse proxy with mkcert certificates (ADR-021)
- 🔍 **Reconstructible by Design** -- Every interaction + memory tool call traced via Langfuse + OpenTelemetry; one `tool_calls` audit row per memory invocation with `interaction_id` FK
- 📊 **Full-Stack Observability** -- `@log_call` aspect emits logs, OTel spans, and histogram metrics from a single decorator; stdout-only logging (12-factor)
- 🔄 **Transparent LLM Proxy** -- Raw dict pass-through for OpenAI tool-calling protocol (ADR-024); memory context injected into the system message without stripping `tools`, `tool_calls`, `tool_call_id`
- 🇪🇺 **GDPR-Compliant** -- Data never leaves your infrastructure
- 🔌 **OpenAI-Compatible** -- `/v1/chat/completions` API
- 🐳 **Docker Compose + Kubernetes** -- Docker Compose for local dev; k3s + Istio + Helm chart for production ZTA (mTLS, SPIFFE/SVID, deny-all AuthorizationPolicies)
- ✅ **Comprehensive Test Suite** -- 558 tests, 94.88% coverage, 90% per-file gate enforced in CI ([latest run](https://github.com/lfdesousa/AuditTrace-AI/actions/workflows/ci.yml))

## Quick Start

### Deployment Options

| Mode | Infrastructure | Use case |
|------|---------------|----------|
| Docker Compose | Traefik + docker-compose.yml | Local development |
| Kubernetes | k3s + Istio + Helm chart | Production / ZTA |

### Deploy with Docker Compose (local dev)

```bash
# Clone repository
git clone https://github.com/lfdesousa/AuditTrace-AI
cd AuditTrace-AI

# Generate secrets (postgres password + chroma token)
./scripts/setup-secrets.sh

# Generate TLS certificates (requires mkcert)
./certs/generate-certs.sh

# Create shared Docker network
docker network create audittrace-net

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

### Deploy with Kubernetes (k3s + Istio)

```bash
# Build container images
make k8s-build

# Install (Helm chart + Istio resources)
make k8s-install
```

See [docs/guides/deployment-runbook.md](docs/guides/deployment-runbook.md) for full instructions including Istio setup, SPIFFE/SVID workload identity, deny-all AuthorizationPolicies, and secret provisioning.

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
AUDITTRACE_LANGFUSE_ENABLED=true
AUDITTRACE_LANGFUSE_HOST=http://host.docker.internal:3000
AUDITTRACE_LANGFUSE_PUBLIC_KEY=pk-lf-your-public-key
AUDITTRACE_LANGFUSE_SECRET_KEY=sk-lf-your-secret-key
# OTLP endpoint with basic auth -- Langfuse v3 native ingest
AUDITTRACE_OTLP_ENDPOINT=http://pk-lf-your-public-key:sk-lf-your-secret-key@host.docker.internal:3000/api/public/otel/v1/traces
# Langfuse only accepts OTLP traces, not metrics -- disable to silence 400 errors
AUDITTRACE_METRICS_ENABLED=false

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
uvicorn audittrace.server:app --reload
```

## Drop-in OpenAI compatibility

**Strict OpenAI `/v1/chat/completions` compatibility is
AuditTrace-AI's North Star.** Point any OpenAI SDK
(`openai-python`, `@ai-sdk/openai-compatible`, `langchain`,
`llamaindex`) or IDE integration (OpenCode, Continue, Cursor, Zed) at
`https://<your-audittrace-host>/v1` and it works. Every request field
the SDK sends passes through unchanged (ADR-024 dict pass-through);
every response shape — success, streaming chunk, error — is a strict
superset of what the OpenAI OpenAPI spec defines.

All AuditTrace-specific features are **additive and opt-in**:

| Extension | Where | Opt-in mechanism |
|---|---|---|
| Project tagging | request | `X-Project: …` header (ADR-029) |
| Memory-mode routing | request | `X-Memory-Mode: inject \| tools` header (ADR-031, scoped) |
| Depth-of-thinking | request | `X-Thinking: deep \| fast \| auto` header (ADR-034, scoped) |
| Audit-row forensics | response error body | net-new keys `status`, `operator_hint`, `trace_id`, `user_facing_message` alongside OpenAI's `{message,type,param,code}` |
| Async job pattern | request | `X-Async: true` header OR separate endpoint — default POST stays strictly OpenAI-shaped (ADR-035, scoped) |

No custom header is ever *required* for the default path to work.
The guardrail is codified in
[`docs/reference/openai/`](docs/reference/openai/) — we vendor the
current upstream OpenAI OpenAPI spec and compare our response shapes
against it directly. Regression tests in
`tests/test_openai_compatibility.py` lock the key contracts
(ChatCompletion, ChatCompletionChunk, Error/ErrorResponse) so any
future change that would break compatibility fails CI before it
reaches main.

Refresh the vendored spec with `./scripts/refresh-openai-spec.sh`
and review the diff like any other dependency bump.

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

### Service Topology (Kubernetes)

```
Client (HTTPS :30952, Bearer JWT)
    |
    v
+---------------------+
| Istio Gateway       |  istio-ingress · TLS (SIMPLE) · NodePort 30952
| + VirtualService    |  hosts: "*"  (audittrace namespace)
+----------+----------+
           |
===========|=== namespace: audittrace ====================================
           |    PeerAuthentication  mTLS STRICT
           |    Workload identity   SPIFFE/SVID
           |    AuthorizationPolicy deny-all + 6 per-flow allow rules
           v
+---------------------+    JWKS    +------------------+
| memory-server       +----------->| Keycloak         |
| FastAPI +           |            | OAuth2 Device    |
| require_user +      |            | Flow (ADR-032)   |
| tool-call loop      |            +------------------+
+-+--+--+--+--+--+----+
  |  |  |  |  |  |
  |  |  |  |  |  v
  |  |  |  |  |  +----------------+
  |  |  |  |  |  | PostgreSQL 16  |  RLS + audittrace_app
  |  |  |  |  |  +----------------+  (NOSUPERUSER + NOBYPASSRLS)
  |  |  |  |  v
  |  |  |  |  +----------------+
  |  |  |  |  | ChromaDB       |  token auth + UserScopedSemanticService
  |  |  |  |  +----------------+
  |  |  |  v
  |  |  |  +----------------+
  |  |  |  | Redis 7        |  token:* + tool-result:* (disjoint prefixes)
  |  |  |  +----------------+
  |  |  v
  |  |  +----------------+
  |  |  | MinIO          |  SSE-S3 KMS · episodic + procedural (ADR-027)
  |  |  +----------------+
  |  |
  |  |  OTLP :4318
  |  v
  |  +--------------------+   traces  --> Tempo  (egress)
  |  | OTel Collector     |
  |  | (DaemonSet)        |   metrics --> Prometheus scrape :8889
  |  +--------------------+
  |
  |  Langfuse SDK (LLM traces, separate from OTLP path)
  +---> Langfuse                                            (egress)

=== mesh egress: ServiceEntry + no-mTLS DestinationRule ==================

External LLMs (host-resident, systemd-managed):
  qwen-chat-llm      :11435   Qwen 3.6-35B-A3B     chat + tools
  nomic-embed-text   :11436   nomic v1.5           embeddings
  mistral-summariser :11437   Mistral 7B v0.3      ADR-030 summariser

Observability sinks (sibling repo AiSovereignObservability, off-mesh):
  Tempo (OTLP :14318)  ·  Prometheus  ·  Loki  ·  Grafana  ·  Langfuse
```

The Istio Gateway terminates TLS on NodePort 30952 and fans out via a VirtualService. Inside the mesh, PeerAuthentication enforces STRICT mTLS and every flow has an explicit SPIFFE-identity allow in its AuthorizationPolicy — everything else is deny-all. External egress (LLMs + observability sinks) traverses a ServiceEntry paired with a no-mTLS DestinationRule, since the upstreams are systemd processes on the host, not mesh workloads. The OTel Collector runs in-mesh as a DaemonSet: it receives OTLP from memory-server and fans out traces to Tempo and metrics to a Prometheus scrape endpoint on :8889. Langfuse receives LLM traces via the Langfuse SDK (separate from OTLP). Redis serves both the `TokenCache` (JWT hot path) and the `ToolResultCache` (memory-as-tools result cache) under disjoint key prefixes.

**Local development** uses the Docker Compose stack (`docker-compose.yml` + Traefik + sibling Langfuse stack). See [docs/guides/deployment-runbook.md](docs/guides/deployment-runbook.md) for the dev-mode topology and the production k8s bring-up.

### 4-Layer Memory

| Layer | Service | Storage | Purpose |
|---|---|---|---|
| 1. Episodic | `FileEpisodicService` | ADR-*.md files | Architecture decisions |
| 2. Procedural | `FileProceduralService` | SKILL-*.md files | Reusable skill documents |
| 3. Conversational | `PostgresConversationalService` | PostgreSQL 16 | Session history + continuity |
| 4. Semantic | `ChromaSemanticService` | ChromaDB server | Vector search / RAG |

All services follow the ABC + implementation + mock pattern with `@log_call` observability.

### Memory Access Modes (ADR-025)

The 4-layer memory can be exposed to the LLM in two different ways. The choice is a runtime flag (`AUDITTRACE_MEMORY_MODE`) and does not change the underlying storage layout.

| Mode | What happens on every `/v1/chat/completions` | Trade-off |
|---|---|---|
| **`tools`** *(live default — `.env` ships with this set)* | The proxy advertises four recall tools to the LLM and runs a tool-call loop. The model decides which layer it needs and calls `recall_decisions`, `recall_skills`, `recall_recent_sessions`, or `recall_semantic` on demand — at most once per question in practice. Results are cached in Redis (TTL 900s) and audit-logged to `tool_calls`. | Pay memory cost only when the model asks. Extra round-trips to llama-server (one per tool iteration), bounded by `AUDITTRACE_MEMORY_TOOL_LOOP_MAX_ITERATIONS`. |
| **`inject`** *(legacy — v0.2.x default)* | The proxy retrieves all 4 layers up front and injects them into the system message. The model sees a preassembled context block and never issues tool calls for memory. | Zero tool-loop latency but every prompt pays the full 4-layer retrieval + token cost, even when the model would have ignored memory. |

The four tools in `tools` mode each map to one layer and carry their own Keycloak scope (`memory:episodic:read`, `memory:procedural:read`, `memory:conversational:read-own`, `memory:semantic:read`). Per-user isolation, RLS, and the ChromaDB scoped wrapper apply unchanged to both modes — the difference is purely *when* the layers are queried, not *what* the user sees.

Switch modes by setting `AUDITTRACE_MEMORY_MODE=inject|tools` in `.env` and recreating the `memory-server` container. See [ADR-025](docs/ADR-025-memory-as-tools.md) for the full design and the acceptance evidence.

### Security

- **OAuth2** -- Keycloak-delegated identity (ADR-022, ADR-023, ADR-026). Every request is a JWT validated against the Keycloak JWKS endpoint. Hot path hits a Redis-backed `TokenCache` for sub-millisecond lookups.
- **Per-user isolation, defense in depth:**
  1. **Service layer** -- every memory service method takes a `UserContext` first arg; conversational `load_sessions` applies unconditional `WHERE user_id =`
  2. **Postgres RLS** -- `interactions`, `sessions`, `tool_calls` all carry `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY` + one policy per table gated on `current_setting('app.current_user_id', true)`
  3. **Non-superuser app role** -- memory-server connects as `audittrace_app` (NOSUPERUSER + NOBYPASSRLS). Postgres superusers always bypass RLS, so this is not optional for real enforcement.
  4. **ChromaDB wrapper** -- `UserScopedSemanticService` binds a `UserContext` at construction and overrides any per-call context; isolation is true by construction
  5. **Session-id uniqueness** -- `_compute_session_id` includes `user_id` in the sha256 so two users with the same first message never produce a colliding session id
- **TLS** -- Traefik terminates HTTPS on :443 with mkcert certificates
- **ChromaDB** -- Token-based authentication via `Authorization: Bearer` header
- **Network isolation** -- Database services on Docker internal network only
- **Secrets** -- Credentials via environment variables, never in code

### Authentication (ADR-022, ADR-023, ADR-026, ADR-032)

All endpoints except `/health` require a valid Keycloak JWT. `AUDITTRACE_AUTH_REQUIRED=true` is the docker-compose default -- every request needs `Authorization: Bearer <JWT>`.

**Human agents (OpenCode / Continue / Roo Code) — OAuth2 Device Flow (ADR-032):**

```bash
# One-time on a fresh Keycloak (realm import handles it): nothing to do.
# On an already-running Keycloak: provision the public client + realm user once.
KEYCLOAK_ADMIN_PASSWORD=admin scripts/setup-human-user.sh

# Interactive login (opens a browser login on any device):
scripts/audittrace-login
#   → prints a short verification URL + user code; log in as `luis`,
#     tokens land in ~/.config/audittrace/tokens.json (mode 0600).

# Launch OpenCode with a fresh token wired into ~/.config/opencode/config.json:
scripts/opencode-wrapper.sh

# In scripts / ad-hoc curl calls:
BEARER=$(scripts/audittrace-login --show)   # auto-refreshes if near expiry
curl -H "Authorization: Bearer $BEARER" https://localhost/v1/chat/completions ...
```

Silent refresh is automatic within the 30-day SSO session lifetime. After that, `scripts/audittrace-login` re-logs in interactively.

**Service accounts (CI, smoke tests) -- `client_credentials`:**
The `audittrace-dev` client is the legacy path. Still the right choice for non-interactive scripts; see `scripts/mint-dev-jwt.sh`.

**Scopes exposed by the `audittrace` realm:**

| Scope | Endpoints / Tools |
|---|---|
| `audittrace:query` | `/v1/chat/completions`, `/session/save` |
| `audittrace:context` | `/context` |
| `audittrace:audit` | `/interactions` |
| `audittrace:admin` | `/metrics` |
| `memory:episodic:read` | `recall_decisions` tool (ADR-025) |
| `memory:procedural:read` | `recall_skills` tool |
| `memory:conversational:read-own` | `recall_recent_sessions` tool |
| `memory:semantic:read` | `recall_semantic` tool |

### Roadmap

> **See [`docs/roadmap.md`](docs/roadmap.md) for the full dated master roadmap** — phased through 2026-10-31, with honest risk renegotiation policy. The table below captures the completed-to-date subset; the master doc covers Phases 1–4 + research track.

| Priority | Item | Description |
|---|---|---|
| ✅ Done | **Intelligent Memory Tool Routing** | Ambient context instructs the LLM to pick ONE tool per question. Validated with Qwen 3.6-35B-A3B. ADR-025 Accepted. |
| ✅ Done | **MinIO Object Storage** | Stateless 12-factor containers — host filesystem mounts replaced with MinIO S3 (ADR-027). SSE-S3 encryption at rest. |
| ✅ Done | **Observability Aggregation Stack** | Prometheus + Grafana + Loki + OTel Collector as sibling compose stack (ADR-028). Per-LLM `peer.service` edges on Tempo service graph. |
| ✅ Done | **mypy --strict Clean** | 0 errors across all source files. |
| ✅ Done | **Audit Trail Completeness** | Project tagging via X-Project header (ADR-029). `/interactions` endpoint with RLS-scoped pagination. |
| ✅ Done | **Session Summariser** | Background asyncio loop via Mistral 7B Instruct v0.3 (ADR-030). Three-model topology. |
| ✅ Done | **OAuth2 Device Flow** | RFC 8628 Device Flow for human agents (ADR-032). Multi-issuer JWT validation. Per-user RLS bites at user granularity. |
| ✅ Done | **Three-Audience Error Envelope** | Classify + persist chat-path failures (ADR-033). OpenAI strict-superset error shape. Migration 007. |
| ✅ Done | **Long-Running Generation** | Per-chunk idle timeout, SSE keep-alive, X-Thinking header (ADR-034). Qwen `<think>` reasoning runs indefinitely as long as tokens flow. |
| ✅ Done | **Qwen 3.6-35B-A3B Upgrade** | DeltaNet linear attention, ~2x throughput vs 3.5, native thinking mode on/off. |
| ✅ Done | **Auth Fully Enabled** | Both `AUDITTRACE_AUTH_ENABLED` and `AUDITTRACE_AUTH_REQUIRED` active. Per-route scope enforcement. |
| ✅ Done | **Backlog Cleared** | All 7 tech-debt items resolved: streaming generator decomposed, step counter scoped per-trace, session ID hash widened, empty stubs deleted, dev compose overlay added. |
| ✅ Done | **Package Rename** | `sovereign_memory` → `audittrace`, `SOVEREIGN_*` → `AUDITTRACE_*`, all containers/DB/realm/scopes aligned (ADR-035). |
| ✅ Done | **N=100 Eval Sweep** | Full 100 probes × 2 modes with 30-min client timeout. Validates Qwen 3.6 + ADR-034 per-chunk idle timeout. |
| ✅ Done | **Kubernetes + Istio ZTA** | k3s + Istio mTLS + SPIFFE/SVID workload identity. Helm chart + deny-all AuthorizationPolicies. The v1.0 milestone. |
| ⏸ On hold | **ADR-031 Per-Request Memory-Mode Routing** | N=100 eval (2026-04-17) showed tools wins every category including ambiguous. Routing complexity not justified. Revisit only if a future model reintroduces a category gap. |
| 🟡 Planned | **Async Persistence** | Non-blocking audit row writes. k8s prerequisite. |
| 🟡 Planned | **External IdP** | Keycloak brokering to Google/Okta/EntraID for multi-user SSO. |

## Observability Stack (ADR-028)

Sibling Docker Compose stack following the Langfuse pattern (ADR-021.2). Provides metrics aggregation, log search, and dashboards. Lives in its own repository at [lfdesousa/AiSovereignObservability](https://github.com/lfdesousa/AiSovereignObservability) — clone alongside this repo for the full observability surface.

```bash
# Start the observability stack
./scripts/setup-observability.sh

# Stop
./scripts/setup-observability.sh --down
```

| Service | URL | Purpose |
|---|---|---|
| Grafana | `http://localhost:3001` (admin / audittrace) | Dashboards: latency percentiles, error rates, infra metrics, logs |
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

**Per-user isolation is automatic.** Postgres RLS sets `app.current_user_id` from the caller's Keycloak `sub` on every transaction (ADR-026). You cannot accidentally see another user's rows, even with a forged `user_id` query parameter — the RLS policy intersects `WHERE user_id = current_setting(...)`. Scope required on the token: `audittrace:audit`.

**Project tagging contract** (ADR-029). Every interaction carries a `project` tag resolved on entry: `X-Project` header → `body.metadata.project` → `body.project` → `"default"`. Configure the header per-project once via `scripts/configure-project.py <name>`.

**Failure rows are audited too** (migration 007 / ADR-033 seed, 2026-04-16). Every upstream failure on `/v1/chat/completions` — proxy timeout, llama-server error, connect failure, unexpected exception — produces an interaction row with `status='failed'` and a controlled-vocabulary `failure_class`. Pre-migration, silent streaming/tools-mode timeouts left zero audit trail: the 10 HTTP 500s observed on 2026-04-14/15 were reconstructible only from Loki logs. Query `SELECT * FROM interactions WHERE status='failed' ORDER BY timestamp DESC` to enumerate the failure history. 5xx responses on non-streaming paths now carry a 3-audience envelope (`code`, `status`, `message`, `operator_hint`, `trace_id`, `user_facing_message`) — the operator pivots from the `trace_id` into Loki / Langfuse / Grafana.

## Configuration

### Database (ADR-020)

| Variable | Default | Description |
|---|---|---|
| `AUDITTRACE_POSTGRES_URL` | -- | Full PostgreSQL URL. Production connects as the non-superuser `audittrace_app` role so RLS policies actually apply |
| `AUDITTRACE_POSTGRES_PASSWORD` | -- | Database password (required) |
| `AUDITTRACE_APP_PASSWORD` | fallback to `AUDITTRACE_POSTGRES_PASSWORD` | Dedicated password for the `audittrace_app` role |
| `AUDITTRACE_CHROMA_URL` | `http://chromadb:8000` | ChromaDB server URL |
| `AUDITTRACE_CHROMA_TOKEN` | -- | ChromaDB auth token |
| `AUDITTRACE_REDIS_URL` | `redis://redis:6379/0` | Redis URL for TokenCache + ToolResultCache |
| `AUDITTRACE_REDIS_PASSWORD` | -- | Required for the TokenCache hot path |
| `AUDITTRACE_TOKEN_CACHE_TTL_SECONDS` | `300` | JWT TTL in TokenCache |

### Auth (ADR-022, ADR-023, ADR-026)

| Variable | Default | Description |
|---|---|---|
| `AUDITTRACE_AUTH_REQUIRED` | `true` (docker-compose default) | When true, every request needs a valid Keycloak JWT |
| `AUDITTRACE_KEYCLOAK_ISSUER` | `http://keycloak:8080/realms/audittrace` | Must match the JWT `iss` claim |
| `AUDITTRACE_KEYCLOAK_JWKS_URL` | `http://keycloak:8080/realms/audittrace/protocol/openid-connect/certs` | JWKS endpoint for RS256 validation (cached 5 min) |
| `AUDITTRACE_JWT_AUDIENCE` | `audittrace-server` | Must match the JWT `aud` claim |

### Memory-as-Tools (ADR-025)

| Variable | Default | Description |
|---|---|---|
| `AUDITTRACE_MEMORY_MODE` | `tools` (shipped `.env`) / `inject` (compose fallback) | `tools` = proxy-internal tool-call loop (current default); `inject` = legacy 4-layer dump into system message. See [Memory Access Modes](#memory-access-modes-adr-025). |
| `AUDITTRACE_MEMORY_TOOL_LOOP_MAX_ITERATIONS` | `5` | Hard cap on tool-call round-trips per request |
| `AUDITTRACE_MEMORY_TOOL_CACHE_TTL_SECONDS` | `900` | Redis tool result cache TTL; `0` disables caching |
| `AUDITTRACE_TOOLS_CONFIG_PATH` | `tools.toml` | Optional TOML overlay to disable/rename/retune tools |

### Observability (ADR-014.4)

| Variable | Default | Description |
|---|---|---|
| `AUDITTRACE_LOG_LEVEL` | `INFO` | Log level |
| `AUDITTRACE_OTLP_ENDPOINT` | `""` (no export) | OTLP/HTTP collector URL |
| `AUDITTRACE_OTEL_SERVICE_NAME` | `audittrace-server` | OTel service name |
| `AUDITTRACE_TRACING_ENABLED` | `true` | Enable OTel tracing |
| `AUDITTRACE_METRICS_ENABLED` | `true` | Enable OTel metrics |

### Langfuse (ADR-021.2)

| Variable | Default | Description |
|---|---|---|
| `AUDITTRACE_LANGFUSE_HOST` | `http://langfuse-web:3000` | Langfuse server URL |
| `AUDITTRACE_LANGFUSE_PUBLIC_KEY` | -- | Langfuse public key |
| `AUDITTRACE_LANGFUSE_SECRET_KEY` | -- | Langfuse secret key |
| `AUDITTRACE_LANGFUSE_ENABLED` | `false` | Enable Langfuse integration |

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
- [ADR-030: Session Summariser](docs/ADR-030-session-summarizer.md)
- [ADR-032: OAuth2 Device Flow](docs/ADR-032-oauth2-device-flow.md)
- [ADR-033: Three-Audience Error Envelope](docs/ADR-033-three-audience-error-envelope.md)
- [ADR-034: Long-Running Generation](docs/ADR-034-long-running-generation.md)
- [ADR-035: Package Rename](docs/ADR-035-package-rename.md)
- [ADR-037: Agent Tool Audit Boundary](docs/ADR-037-agent-tool-audit-boundary.md)
- [ADR-038: Cross-Device Memory Sync Protocol](docs/ADR-038-memory-sync-protocol.md) *(placeholder — joint draft with Otopoetic)*
- [ADR-041: Product Boundary and Dependencies](docs/ADR-041-product-boundary-and-dependencies.md)

### Architecture Diagrams

- [**Product boundary & dependencies**](docs/architecture/product-and-dependencies.md) — what AuditTrace-AI is (memory-server) and the eight market-standard dependencies it integrates with. Formal decision in ADR-041.
- [C4 Workspace (Structurizr DSL)](docs/architecture/workspace.dsl)
- [Sequence -- chat completions (inject mode)](docs/architecture/sequence-chat-completions.md)
- [Sequence -- memory tool-call loop (tools mode)](docs/architecture/sequence-memory-tool-call.md)
- [Sequence -- OAuth2 flow](docs/architecture/sequence-oauth2-flow.md)

### Operator Playbooks

- [Langfuse Dashboards -- Recipes](docs/langfuse-dashboards.md)
- [Agent Configuration (OpenCode, Continue, Roo Code)](docs/agent-configuration.md)
- Dev JWT minting: `scripts/mint-dev-jwt.sh`
- App role init: `scripts/init-audittrace-app-role.sh` (creates the non-superuser Postgres role for RLS)
- Project tagging: `scripts/configure-project.py <name>` (ADR-029)

### Evaluation Reports

- [Memory Access Modes — 2026-04-14 smoke](docs/eval-memory-modes-20260414.md) — inject vs tools baseline; tools wins on latency, reliability, and tool-selection accuracy. Follow-ups: LangGraph-style exit conditions + ADR-030 before re-measuring at full N=100.

## Legal Disclaimer & Copyright

Copyright &copy; 2026 Luis Filipe de Sousa ([allaboutdata.eu](https://allaboutdata.eu)). All rights reserved.

- **Independent Research:** This project, including the "Sovereign Local AI Stack" framework and the AuditTrace-AI implementation, is the result of independent research conducted by the author in a private capacity.
- **Resource Separation:** No employer data, confidential information, or corporate infrastructure was used in the development of this work.
- **Institutional Independence:** The views, architectures, and code presented here do not represent the positions, strategies, or opinions of any current or former employer.
- **Licensing:** While the core engine is licensed under [AGPL v3](./LICENSE) to support Open Science, the underlying theoretical framework and specific architectural designs (ADRs) remain the intellectual property of allaboutdata.eu.

---

*"The software artefact that separates compliant deployments from merely well-architected ones."*
