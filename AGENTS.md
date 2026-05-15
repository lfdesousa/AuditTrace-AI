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

### Docker images — two Dockerfiles, never mixed (Chart-A, 2026-05-13)

- **`Dockerfile`** — produces the runtime image. `runtime` is the LAST
  (and final) stage, so plain `docker build .` always yields the right
  image. Use `make docker-build` for local development, or `make
  k8s-build TAG=...` to push to the in-cluster registry.
- **`Dockerfile.tests`** — produces the helm-test image (chart's RLS
  integration suite). Built `FROM` the runtime image, so you must
  build the runtime image first (`make test-integration` chains this).
  Override `TESTS_BASE_IMAGE` via `--build-arg` to validate a specific
  published runtime tag.
- **Never** add an `AS tests` stage back to `Dockerfile`. When that
  used to be the last stage, `docker build .` (no `--target`) silently
  produced an image whose ENTRYPOINT was pytest — kubelet ran it once,
  exited 0, CrashLoopBackOff'd with `Reason: Completed`. PR-B10 CI
  trip-wired this on its third run.

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
- **Kind integration (`.github/workflows/integration-content-control.yml`):**
  smoke test that spins up a one-node kind cluster on every PR + push,
  installs both `charts/audittrace` (local) and `audittrace-content-control`
  (OCI v0.0.7), and verifies memory-server reaches Ready alongside the
  subcharts (Postgres / Redis / RabbitMQ + in-chart MinIO / ChromaDB).
  Catches deploy-time bugs that unit tests + helm-lint can't see (image
  pulls, chart-rendering against a live cluster, cross-chart wiring).
  Values override: `tests/integration/fixtures/values-ci.yaml` —
  `vault.enabled=false`, `istio.enabled=false`, `keycloak.enabled=false`,
  observability off, `AUDITTRACE_AUTH_ENABLED=false`. Full upload-flow
  end-to-end is PR-B11 (next).
  Reproduce locally:
  ```
  kind create cluster --name integration-cc
  helm repo add bitnami https://charts.bitnami.com/bitnami
  helm dependency build charts/audittrace
  docker build -t audittrace-memory-server:ci .
  kind load docker-image audittrace-memory-server:ci --name integration-cc
  helm install audittrace charts/audittrace \
    -f tests/integration/fixtures/values-ci.yaml --wait --timeout=12m
  ```

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

## Test + Evidence Gate (HARD — see [ADR-049](docs/ADR-049-test-evidence-and-reconstructibility-gate.md))

Every change is gated by a four-rule **Test, Evidence, and
Reconstructibility Gate**, mapping to V&V (verification +
validation) and the **Sovereignty-Reconstructibility Gap**
formalised in `main_signed.pdf` §7. **No exceptions.**

| Rule | Discipline | Required artefact |
|---|---|---|
| 1 — Unit tests + ≥90% per-file coverage | **Verification** | `make test` green output, `check-per-file-coverage.py` PASS |
| 2 — End-to-end through public API + scoped JWT, against a deployed image | **Validation** | API request/response, image tag, `verify-deploy` summary |
| 3 — Reconstruction artefacts captured + referenced from commit + PR body | **Reconstruction** | trace ID, audit-row ID, ChromaDB / MinIO / Postgres query result |
| 4 — No override | **Discipline** | fix the friction (cluster, scope, dependency); never bypass |

**No bypass paths** — `kubectl exec` into backend pods to do what
the API should do, MinIO root credentials, scope-skipping,
mocked auth at the edge: all forbidden. If `/memory/upload`
requires `memory:episodic:write` and your token lacks it, the
answer is `AUDITTRACE_EXTRA_SCOPES="memory:episodic:write"
scripts/audittrace-login`, not a workaround.

**Mechanical enforcement** (so the rule survives a hard week):

- **Pre-commit hook** (`scripts/check-commit-evidence.sh` via
  `.pre-commit-config.yaml`) — refuses commits to
  `src/audittrace/routes/**` or `src/audittrace/services/**`
  whose commit message lacks an evidence reference (PR draft URL,
  evidence-file path, or trace-ID-shaped string).
- **CI gate** (`evidence-check` job in
  `.github/workflows/ci.yml`) — parses the PR body via `gh api`;
  fails the check if the **Verification** / **Validation** /
  **Reconstruction** sections are missing or empty. Branch
  protection enforces it for merges to `main`.
- **PR template** (`.github/pull_request_template.md`) —
  pre-populates every new PR with the three required sections so
  the requirement is visible from the moment the PR opens.

Out-of-scope (path-skip): pure docs (`docs/**.md`), pure tests
(`tests/**`), and `*.md` top-level files skip the pre-commit hook.
The CI gate still requires the PR body sections.

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
  Operator seed files live in `~/work/audittrace-private/secrets/` (mode
  600, never in-repo). Override the source dir with `SECRETS_DIR=… make
  k8s-bootstrap-secrets`. The default in the Makefile target points at
  the private dir.
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
- **Bitnami tag mislabel (operator-caveat, 2026-05-13):** The chart's
  Bitnami subcharts (`postgresql ~16`, `redis ~19`) declare image tags
  `17.6.0-debian-12-r4` / `7.2.5-debian-12-r4` — but the pre-sunset
  Bitnami Docker Hub images at those tags actually shipped
  **PostgreSQL 18.3** + **Redis 8.6.2**. Post-Aug-2025 Bitnami
  republished to `bitnamilegacy/*` with the LITERAL versions the tags
  promised (PG 17.6.0 + Redis 7.2.5). Existing clusters bootstrapped
  pre-Aug-2025 have PG 18 / Redis 8 data on disk; bitnamilegacy/*
  binaries refuse to read it.
  **Permanent fix (2026-05-14, B1.5):** We extracted the pre-sunset
  PG 18.3 + Redis 8.6.2 images from the cluster's containerd cache
  and republished them privately to `ghcr.io/lfdesousa/audittrace-{postgresql,redis}`
  with explicit `*-bitnami-frozen-apr17` tags. Both prod
  (`values-local.yaml`) and kind CI (`tests/integration/fixtures/values-ci.yaml`)
  pull from the SAME ghcr-hosted image — single source of truth, no
  more cache-dependency fragility, no CI-vs-prod binary drift.
  Prod-side authentication via a long-lived `ghcr-pull-secret` k8s
  Secret referenced through `global.imagePullSecrets`; CI side via
  the workflow's `GITHUB_TOKEN` + `packages: read` permission.
  **Operator runbook for PAT lifecycle + Secret rotation:**
  `~/work/audittrace-private/runbooks/12-ghcr-pull-secret.md` (private).
  Full forensic at memory `project_bitnami_systemic_tag_mislabel`.
- **RabbitMQ ghcr-frozen mirror (operator-caveat, 2026-05-15, B1.6):**
  Same posture as B1.5 extended to the third subchart. The chart's
  `rabbitmq ~14` subchart pulls `bitnamilegacy/rabbitmq:3.13.7-debian-12-r2`
  by default. Unlike PG/Redis, the rabbitmq tag is NOT mislabelled
  (verified: `rabbitmqctl version` inside the running container
  returns 3.13.7, matching the tag claim). But the same single-source-
  of-truth discipline applies — control the registry, freeze the
  binary moment-in-time, mirror into kind CI so compose B7 + kind
  + prod all exercise the same bytes. Frozen image at
  `ghcr.io/lfdesousa/audittrace-rabbitmq:3.13.7-debian-12-r2-bitnami-frozen-may15`.
  Both `values-local.yaml` and `tests/integration/fixtures/values-ci.yaml`
  pin it. Same auth posture as B1.5 (ghcr-pull-secret prod-side,
  GITHUB_TOKEN CI-side). Anchor memory:
  `project_pickup_20260515_b7` — Luis: "use the exact same images
  we are using today for all the components".
- **`global.security.allowInsecureImages: true` (operator-caveat, 2026-05-14):**
  Set in both `charts/audittrace/values-local.yaml` and
  `tests/integration/fixtures/values-ci.yaml`. The name is misleading
  — it does **NOT** disable any actual security control. It only
  suppresses Bitnami subchart 16.7.27+'s built-in `verify-images`
  template, which refuses to render any postgres/redis image ref
  that doesn't sit under `bitnami/*` or `bitnamilegacy/*`. Bitnami's
  documented escape hatch for downstream operators republishing images
  to their own registry: github.com/bitnami/charts/issues/30850.
  We need it because our frozen PG 18.3 + Redis 8.6.2 images live at
  `ghcr.io/lfdesousa/audittrace-{postgresql,redis}` (see the bullet
  above). The actual image content is unchanged — same OCI digest
  as the pre-sunset Bitnami images — so "insecure" here is a Bitnami
  vendor-convention check, not a CVE-class one. **Do not** set this
  flag as a blanket default; scope it to values files that override
  postgres/redis image references. The chart drift guard
  (`tests/test_chart_drift_guards.py`) catches AP/role coverage drift
  but does not flag a missing `allowInsecureImages` — if a fresh
  override is added and helm template aborts with "Unrecognized
  images", this is the flag to set in the same values file.

### docker-compose stack — B7 step 1 rebuild (2026-05-15)

`docker-compose.yml` is the **parallel dev runtime** per
`feedback_docker_compose_retained` — NOT a competing production
path; the Helm chart in `charts/audittrace/` remains canonical.

After B7 step 1 the compose stack pulls the SAME images Helm
deploys in prod: `ghcr.io/lfdesousa/audittrace-{postgresql,redis,
rabbitmq}` (B1.5 / B1.6 frozen Bitnami), `chromadb/chroma:1.5.7`
(chart pin), `quay.io/keycloak/keycloak:24.0`, `minio/minio:latest`,
`docker.io/lfds/audittrace-memory-server:1.0.22` (published).
Configuration follows Bitnami conventions (`POSTGRESQL_*`,
`REDIS_*`, `RABBITMQ_*` env vars; `/bitnami/{postgresql,redis,
rabbitmq}` volume paths) so the runtime contract matches the
Bitnami subcharts the Helm chart deploys.

**Operator caveat — first up after B7 step 1 wipes data.** The
previous compose used vanilla `postgres:16-alpine` (PG 16 on disk
at `/var/lib/postgresql/data`) and `redis:7-alpine` with cmdline
auth. The new images write PG 18.3 / Redis 8.6.2 to `/bitnami/*`.
The OLD named volumes are incompatible; do this BEFORE first up:

```
docker compose down -v --remove-orphans
```

Clears `postgres_data` / `audittrace_redis_data` / `chroma_data` /
`minio_data` so the next `docker compose up` initialises against
the new images cleanly. Acceptable because compose is dev-only.

**Dev hot-reload** still works via the dev overlay:

```
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d memory-server
```

The overlay re-enables `build: .` and tags the result as
`docker.io/lfds/audittrace-memory-server:dev` so it doesn't shadow
the base's `:1.0.22` pin in other operators' shells.

**Network is compose-managed** (was `external: true` pre-B7). An
operator who needs to bridge into an existing network can override
with a sibling overlay (`docker-compose.network-external.yaml`).

Subsequent B7 steps (mock-LLM profile, GHA workflow, obs stack,
content-control sibling) are tracked in
`docs/architecture/b7-docker-compose-revive-plan.md`.

**RabbitMQ deviation (operator caveat, 2026-05-15, B7 step 1 round 7):**
Compose's rabbitmq service runs `rabbitmq:3.13-management-alpine`
(upstream image), NOT the ghcr-frozen Bitnami mirror the chart uses
(`ghcr.io/lfdesousa/audittrace-rabbitmq:3.13.7-debian-12-r2-bitnami-frozen-may15`).
**Binary parity preserved** — both images ship RabbitMQ 3.13.7
(verified `rabbitmqctl version`). **Wrapper differs** because the
Bitnami image applies `loopback_users.<RABBITMQ_USERNAME> = true`
by default in its auto-generated rabbitmq.conf, blocking cross-
container PLAIN auth. Six rounds of env-var + RABBITMQ_EXTRA_CONF
+ mounted-conf-file overrides all failed (the bitnami libfile
re-writes the conf after our overrides). Pragmatic compromise:
compose deviates to upstream; chart's prod path keeps bitnami
unchanged. The application's AMQP behaviour is identical against
either wrapper — `aio_pika.connect_robust` doesn't care about
loopback policy as long as the user can authenticate.

**Compose profiles (B7 steps 2 / 6-9, 2026-05-15):** the compose
stack now has 5 opt-in profiles for heavyweight services. Default
`docker compose up` brings up the 8 core services + `mock-llm`
(via `.env.ci`'s `COMPOSE_PROFILES=mock-llm`). The 11 remaining
services are dormant unless explicitly activated.

| Profile | Services added | Use case |
|---|---|---|
| `mock-llm` | mock-llm (1) | CI + dev-without-host-llama |
| `vault` | vault (1) | Local-dev secret-rotation rehearsal |
| `obs` | otel-collector, tempo, loki, grafana (4) | Local trace+log dashboards |
| `langfuse` | langfuse-web, langfuse-postgres (2) | LLM observability — heavy |
| `content-control` | cc-control-plane, cc-clamd (2) | Local scan-pipeline E2E — clamd ≥1 GB RAM |

Profiles compose: `COMPOSE_PROFILES=obs,langfuse docker compose up -d`
activates both. CI uses ONLY `mock-llm` (CI doesn't need the rest;
chart+kind covers them). All profile services have explicit
`profiles: [...]` lists asserted by
`tests/test_compose_drift.py::TestComposeProfileGating`.

**Operator workflow examples:**

```bash
# Minimal CI-shaped stack (mock LLM, no obs, no cc)
docker compose --env-file .env.ci up -d --wait

# Dev with host llama-server + local Grafana/Tempo dashboards
cp .env.dev-real-llm.example .env  # edit, fill ###CHANGE###
COMPOSE_PROFILES=obs docker compose up -d --wait

# Full operator-rehearsal: every profile active
COMPOSE_PROFILES=mock-llm,vault,obs,langfuse,content-control \
  docker compose up -d --wait
```

**Shared test scripts** in `tests/integration/compose/`:
`test-health.sh`, `test-chat-completion.sh`, `test-models.sh` are
runnable locally against any compose-up state.
`.github/workflows/e2e-compose.yml` calls the same scripts so CI
and local dev exercise identical contracts.

### Data-compat harness — test before any subchart image swap

Before any helm change that touches `postgresql.image.*`,
`redis.image.*`, or the Chart.lock pin for either, validate the
candidate binary can read the existing PVC data **offline**:

```bash
scripts/snapshot-pvc.sh postgres ~/work/audittrace-private/data-snapshots/$(date +%Y-%m-%d)
scripts/test-image-compat.sh postgres <candidate-image:tag>
# exit 0 = PASS (safe to deploy), 1 = FAIL (binary rejects on-disk data)
```

ADR-049 rule: any PR touching those values MUST cite a recent
`test-image-compat.sh` PASS in its `## Validation` section. Without
this harness, Chart-A's image-repo flip looked clean in CI (fresh
PVCs) but exploded on prod's existing data — see
`tests/integration/data-compat/README.md` for the full story.

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
