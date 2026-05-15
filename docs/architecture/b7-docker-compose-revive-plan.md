# B7 — docker-compose revival plan (GHA-runnable, LLM-swappable)

> Status: **PLAN ONLY** — drafted 2026-05-15 on branch
> `feat/b7-compose-revive-plan`. No code, no workflow files, no
> existing compose files touched. Awaiting Luis sign-off on the
> open questions in §10 before implementation begins.
>
> Anchor memory: `feedback_docker_compose_retained` — compose is
> a **parallel runtime by design** (offline dev, non-k8s demos,
> fallback). The k8s/Helm chart in `charts/audittrace/` remains
> the canonical production path. This work brings compose back
> into binary-parity alignment with that production path so it
> stays useful, not so it competes with it.

## 1. Current state

### What exists today

| File | State | Notes |
|---|---|---|
| `docker-compose.yml` | partially aligned | core stack: memory-server, postgres, chromadb, keycloak, redis, minio, traefik. AUDITTRACE_* env prefix already in use (the post-rename pass). |
| `docker-compose.dev.yml` | aligned | small overlay: bind-mounts `src/` for uvicorn hot-reload. Will keep as-is. |
| `docker-compose.data-compat.yml` | **out of scope for B7** | B1 harness; isolated, runs independently against PVC snapshots. Reference for style only. |
| `.github/workflows/e2e.yml` | partially working | existing compose-based E2E job; currently asserts `/v1/chat/completions` returns 502 (no LLM). Expects a Traefik-TLS-fronted memory-server. |

### What's broken / drifted vs k8s prod

1. **Postgres pin**: compose uses `postgres:16-alpine` (vanilla upstream). Prod runs `ghcr.io/lfdesousa/audittrace-postgresql:18.3-bitnami-frozen-apr17` (B1.5 frozen image, PG 18.3 binary). No binary parity — compose stack will silently test against the wrong major version.
2. **Redis pin**: compose uses `redis:7-alpine`. Prod runs `ghcr.io/lfdesousa/audittrace-redis:8.6.2-bitnami-frozen-apr17` (Redis 8.6.2 binary, RDB v13). Same drift class.
3. **RabbitMQ missing**: ADR-057 scan-control broker is a hard dependency of memory-server since v1.0.7 (lifespan calls `scan_amqp_client.ensure_connected()`). Compose has no rabbitmq service. memory-server will crash-loop on startup against the current compose file.
4. **No observability stack**: chart deploys otel-collector + tempo + loki + grafana + langfuse + promtail. Compose has none of these. Trace + log evidence (ADR-049 Rule 3) is not reproducible from compose today.
5. **No content-control sibling**: the cc-chart deploys alongside audittrace in prod and kind. Compose has no cc service. ADR-048 scan pipeline cannot be exercised end-to-end from compose.
6. **No Vault**: prod uses HashiCorp Vault for secret injection. Compose uses literal env vars (acceptable as a dev-bypass posture, but should be documented).
7. **LLM endpoint**: `AUDITTRACE_LLAMA_URL=http://host.docker.internal:11435/v1` — assumes a host-running llama-server. Works on Luis's GPU box; **does not work in GHA** (no GPU, no host llama-server).
8. **Image build**: `build: .` forces a fresh image build on every `docker compose up`. Works locally but slows GHA significantly (and runs without ghcr-published image binary parity).
9. **MinIO IAM split**: prod uses an IAM-split user (`audittrace_app_user` / `content_control_user` per ADR-048 PR-B7). Compose uses MinIO root creds. Drift the chart-side hardened away.

### What's still correctly aligned

- `AUDITTRACE_*` env prefix throughout (post-rename pass already done in compose).
- ChromaDB pin (`chromadb/chroma:1.5.8`) matches chart.
- Keycloak pin (`quay.io/keycloak/keycloak:24.0`) matches chart's keycloak subchart.
- MinIO core service shape (SSE-S3 KMS, console on 9001).
- Postgres init script wiring (`init-audittrace-app-role.sh` for the non-superuser role).
- Traefik labels are operator-friendly for local Device-Flow / cert-bypass demos.

## 2. Target state

Single `docker-compose.yml` (extended by overlays in §3) renders the following stack, with image pins matching prod **binary** parity. Service names match chart conventions so AUDITTRACE_* env defaults compose cleanly.

| Service | Image | Purpose | depends_on | Key env (AUDITTRACE_*) | Volumes | Ports | Healthcheck |
|---|---|---|---|---|---|---|---|
| `memory-server` | local build (Dockerfile runtime stage) or `docker.io/lfds/audittrace-memory-server:1.0.22` | FastAPI app | postgres, chromadb, redis, rabbitmq, minio, keycloak | full AUDITTRACE_* set (LLAMA_URL / EMBED_URL / SUMMARIZER_URL parameterised, see §5) | none (12-factor) | `8765` (via traefik) | `GET /health` |
| `postgres` | `ghcr.io/lfdesousa/audittrace-postgresql:18.3-bitnami-frozen-apr17` | PG 18.3 binary parity | — | POSTGRES_DB/USER/PASSWORD, AUDITTRACE_APP_PASSWORD | `postgres_data`, two init scripts | `15432:5432` | `pg_isready` |
| `redis` | `ghcr.io/lfdesousa/audittrace-redis:8.6.2-bitnami-frozen-apr17` | TokenCache + ToolResultCache (RDB v13 parity) | — | AUDITTRACE_REDIS_PASSWORD | `audittrace_redis_data` | none | `redis-cli ping` |
| `rabbitmq` | `bitnamilegacy/rabbitmq:<chart-pin>` (TBD §10 Q4 — chart default) | ADR-057 scan-control broker | — | RABBITMQ_DEFAULT_USER/PASS/VHOST | `rabbitmq_data` | `15672` (mgmt) | `rabbitmq-diagnostics check_running` |
| `chromadb` | `chromadb/chroma:1.5.8` | semantic memory store | — | CHROMA_SERVER_AUTHN_* | `chroma_data` | `18000:8000` | `/api/v2/heartbeat` |
| `minio` | `minio/minio:RELEASE.2025-XX` (pin chart-side digest) | S3 object storage | — | MINIO_ROOT_USER/PASSWORD, MINIO_KMS_SECRET_KEY | `minio_data` | `19000:9000`, `19001:9001` | `/minio/health/live` |
| `keycloak` | `quay.io/keycloak/keycloak:24.0` | IdP for JWT issuance | postgres | KC_DB_*, KEYCLOAK_ADMIN_*, KC_HOSTNAME_URL, KC_PROXY | `realm-audittrace.json` mount | via traefik | `/health/ready` |
| `traefik` | `traefik:v3.6` | TLS termination + path routing for local Device-Flow | — | DOCKER_API_VERSION | `traefik.yml`, `dynamic.yml`, `./certs` | `443`, `8080` | container only |
| `vault` (optional, profile `vault`) | `hashicorp/vault:1.18` (dev mode) | secret injection parity | — | VAULT_DEV_ROOT_TOKEN_ID | `vault_data` | `8200:8200` | `/v1/sys/health` |
| `otel-collector` (optional, profile `obs`) | `otel/opentelemetry-collector-contrib:0.111.0` | OTLP fanout: Tempo + Langfuse | — | collector config mount | config mount | `4318:4318` | container only |
| `tempo` (optional, profile `obs`) | `grafana/tempo:2.6.1` | traces backend | — | tempo.yaml | `tempo_data`, config mount | `3200:3200` | `/ready` |
| `loki` (optional, profile `obs`) | `grafana/loki:3.2.0` | logs backend | — | loki.yaml | `loki_data`, config mount | `3100:3100` | `/ready` |
| `promtail` (optional, profile `obs`) | `grafana/promtail:3.2.0` | log shipper → loki | loki | promtail.yaml | docker socket (ro), config mount | none | container only |
| `grafana` (optional, profile `obs`) | `grafana/grafana:11.3.0` | dashboards | tempo, loki | GF_AUTH_* | dashboards mount | `3001:3000` | `/api/health` |
| `langfuse-web` (optional, profile `langfuse`) | `langfuse/langfuse:3.0.0` | LLM-specific trace store | postgres-langfuse | LANGFUSE_* | — | `3000:3000` | `/api/public/health` |
| `mock-llm` (profile `mock-llm`) | `python:3.12-slim` + inline server (see §3) | OpenAI-shape `/v1/chat/completions` + `/v1/embeddings` + `/v1/models` | — | none | configmap-equivalent mount | none | `/health` |
| `cc-control-plane` (optional, profile `content-control`) | `docker.io/lfds/audittrace-content-control:0.0.9` | ADR-048 scan worker | rabbitmq, minio | AUDITTRACE_SCAN_*, RABBIT_*, MINIO_* | — | none | container only |
| `cc-clamd` (optional, profile `content-control`) | `clamav/clamav:1.4` | virus engine sidecar | — | CLAMD_* | signature dir mount | none | `clamdscan --ping` |

### Network

Single user-defined bridge network `audittrace-net`. Drop `external: true` (current compose requires `docker network create audittrace-net` as a pre-step — friction that adds nothing) and let compose own the network lifecycle.

### Volumes

Named volumes for every stateful service. CI compose runs MUST blow them away on teardown (`docker compose down -v`); local dev preserves them between runs.

## 3. LLM swappability — the key design decision

Luis's new requirement: ONE compose file must support **mock LLM in CI** and **real LLM in dev with a GPU** without forking. Four options evaluated.

### Option (a) — Compose profiles

```yaml
services:
  mock-llm:
    profiles: ["mock-llm"]
    image: python:3.12-slim
    # ...
  real-llm:
    profiles: ["real-llm"]
    image: ghcr.io/ggerganov/llama.cpp:server
    # ...
```

Usage:
- CI: `docker compose --profile mock-llm up -d`
- Dev (GPU): `docker compose --profile real-llm up -d` OR run llama-server natively + omit the profile (treat the external host LLM as the default).

**Pros**: native compose feature; one file; switching is one CLI flag; both services declared so the file documents both modes; no `extends` indirection.

**Cons**: real-LLM-in-container requires GPU runtime (`gpus: all`) which not every dev box has; the natural prod-like default is "no LLM service in compose, use the host one" — that mode is what (a) makes awkward (you'd add a third "no-llm" profile or just omit `--profile` and accept the missing service).

### Option (b) — Env-var-driven service replacement

Single `llm-chat` service whose `image` is `${LLM_IMAGE:-python:3.12-slim}` and whose entrypoint flips on `LLM_MODE`. **Rejected**: brittle (one service can't be both a Python FastAPI mock AND a CUDA-bound llama-server depending on env), opacity (operators can't `docker compose config` to see what's running), no profile-level toggles.

### Option (c) — Override-file pattern

`docker-compose.yml` defines the stack with NO LLM service. Operator layers in one of:
- `docker-compose.mock-llm.yaml` (committed) → adds `mock-llm` service + sets `AUDITTRACE_LLAMA_URL=http://mock-llm:11435/v1` via env override
- `docker-compose.real-llm.yaml` (committed but pointing at host) → sets `AUDITTRACE_LLAMA_URL=http://host.docker.internal:11435/v1`, no additional service

```bash
# CI
docker compose -f docker-compose.yml -f docker-compose.mock-llm.yaml up -d
# Dev (host llama-server)
docker compose -f docker-compose.yml -f docker-compose.real-llm.yaml up -d
```

**Pros**: explicit; mirrors `docker-compose.dev.yml` pattern already in the repo; each file is small and reviewable; matches how the kind workflow keeps `mock-llm-*.yaml` as separate apply-able fixtures (§3 ↔ kind parity).

**Cons**: two extra files to maintain; an operator who forgets the `-f` ends up with no LLM at all and gets a runtime 502.

### Option (d) — External LLM reference with env override

Compose declares no LLM service. memory-server's `AUDITTRACE_LLAMA_URL` defaults to a value that **assumes** an external host-running llama-server. The CI workflow runs a sidecar compose project (or a docker-run command) for the mock and points `AUDITTRACE_LLAMA_URL` at it.

**Pros**: cleanest separation of concerns; production-shaped (real LLM IS external in prod too — k8s headless Service pointing at host).

**Cons**: workflow has to orchestrate two `docker compose` projects (or one project + one `docker run`); not a single-command-up; loses the "this compose file describes the whole testable stack" property.

### Recommendation: **Option (a) — compose profiles** with a default-empty LLM and three profiles

Concretely:

```yaml
services:
  mock-llm:
    profiles: ["mock-llm", "ci"]
    image: python:3.12-slim
    command: ["/bin/sh", "-c", "pip install ... && uvicorn server:app ..."]
    volumes:
      - ./tests/integration/fixtures/compose/mock-llm/server.py:/app/server.py:ro
    healthcheck: ...

  llama-chat:
    profiles: ["real-llm"]
    image: ghcr.io/ggerganov/llama.cpp:server-cuda
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    # ...

  memory-server:
    environment:
      # Default points at the mock service name; the real-llm profile
      # also creates a service called `llama-chat` so swap is just the
      # service-name resolution flipping.
      AUDITTRACE_LLAMA_URL: ${AUDITTRACE_LLAMA_URL:-http://mock-llm:11435/v1}
    # ...
```

**Why (a) wins**:

1. **Mirrors the kind-CI pattern by structure**: in kind, the mock LLM is three k8s fixtures whose Service name (`audittrace-llm-chat`) matches the chart's hardcoded `AUDITTRACE_LLAMA_URL`. In compose, the mock-llm profile's service name plays the same role. Operators who already understand the kind path will recognise the compose path.
2. **Single command**: `docker compose --profile mock-llm up -d` is one invocation. No `-f` chaining, no separate `docker run`.
3. **Both modes documented in-file**: `docker compose config --profile mock-llm` and `docker compose config --profile real-llm` both render valid stacks. New contributors don't have to know about an override file.
4. **No coupling to a third file**: keeps the maintenance surface to one `docker-compose.yml`. The mock LLM script lives at `tests/integration/fixtures/compose/mock-llm/server.py` (mirrors the kind fixture path; can be the same script, copy-via-symlink-or-include).
5. **GHA-friendly**: the workflow just adds `--profile mock-llm` and is done.

**Caveat for Luis**: GPU + compose-real-llm is fiddly. Most local-dev posture already runs llama-server natively (Caddy proxy etc., per `feedback_bun_fetch_ignores_extra_ca_certs`). Operators with that posture should set `AUDITTRACE_LLAMA_URL=http://host.docker.internal:11435/v1` in their `.env` and skip the `real-llm` profile entirely. That's still option (a) — just with the profile unused. See open Q1.

## 4. GitHub Actions integration — feasibility + shape

**Is it possible?** Yes. Confirmed against current GHA capabilities.

### GHA runner capability check

- `ubuntu-latest` (currently `ubuntu-24.04`) ships with Docker Engine + Compose v2 preinstalled. `docker compose up` works without `setup-docker` (compose v2 is the bundled `docker-compose-plugin`). Reference: github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md.
- Resources on public runners: 4 vCPU, 16 GB RAM, 14 GB disk free. Existing `e2e.yml` already proves the core compose stack fits.
- Existing `integration-content-control.yml` (kind-based) already runs the in-cluster mock LLM same shape — the compose mock is a port of that pattern.

### RAM audit — what fits

Total ceiling: ~14 GB usable (16 GB minus runner overhead). Rough requests (conservative — `resources.requests` analog):

| Service | RAM | Notes |
|---|---|---|
| memory-server | 512 MB | FastAPI + sqlalchemy |
| postgres | 256 MB | Bitnami PG 18.3 |
| redis | 128 MB | maxmemory 256M |
| rabbitmq | 512 MB | Erlang VM floor |
| chromadb | 512 MB | Python + SQLite + duckdb |
| minio | 256 MB | |
| keycloak | 1024 MB | Java; biggest single service |
| traefik | 128 MB | |
| mock-llm | 256 MB | Python + fastapi + uvicorn |
| **Core subtotal** | **~3.6 GB** | Fits comfortably |
| vault (optional) | 256 MB | |
| otel-collector | 256 MB | |
| tempo | 512 MB | |
| loki | 512 MB | |
| promtail | 128 MB | |
| grafana | 512 MB | |
| langfuse-web | 1024 MB | Node.js + workers; plus a langfuse postgres ~256MB |
| cc-control-plane | 512 MB | |
| cc-clamd | 1024 MB | clamd OOMKilled at 768 MB in PR-A11 kind test — bumped to ≥1 GB |
| **Full obs + content-control** | **~9 GB** | Tight but fits |

**Verdict**: core stack + mock-llm (~3.6 GB) is comfortable. Adding the full obs stack pushes to ~7 GB which is OK. Adding content-control AND clamd brings us to ~9 GB which is the edge — clamd's 1 GB RAM floor (per `project_session_20260512`) is the biggest single risk class. **Recommended CI default**: core + mock-llm only. Obs and content-control gated behind separate workflows or `workflow_dispatch` inputs (see §6 + §9).

### Workflow shape — `e2e-compose.yml` (new, NOT replacing `e2e.yml`)

Per `feedback_docker_compose_retained`: keep `e2e.yml` as-is, add a sibling. The new workflow is the "fully wired stack with mock LLM" path; the existing `e2e.yml` is the "minimal core + auth-bypass" path that already passes.

**Decision: write a NEW file** `.github/workflows/e2e-compose.yml`. Don't extend `e2e.yml` — its current shape (502 expected, no mock LLM, auth bypass) is a different contract.

Workflow outline:

```yaml
name: E2E (compose, mock LLM)
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
permissions:
  contents: read
  packages: read  # ghcr image pulls

jobs:
  compose-e2e:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v6
      - name: Login to ghcr.io for frozen pg+redis
        run: echo "${{ secrets.GITHUB_TOKEN }}" | docker login ghcr.io -u "${{ github.actor }}" --password-stdin
      - name: Render .env.ci → .env
        run: cp .env.ci .env
      - name: docker compose up
        run: docker compose --profile mock-llm up -d --wait --wait-timeout 300
      - name: Wait for /health
        run: ./scripts/wait-for-health.sh https://localhost/health 180
      - name: E2E — POST /memory/upload (clean + eicar)
        run: bash tests/integration/compose/test-upload-clean-and-eicar.sh
      - name: E2E — POST /v1/chat/completions via mock-llm
        run: bash tests/integration/compose/test-chat-completions.sh
      - name: Capture logs (always)
        if: always()
        run: docker compose logs --tail=300 > /tmp/compose-logs.txt
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: compose-logs
          path: /tmp/compose-logs.txt
      - name: Tear down
        if: always()
        run: docker compose down -v --remove-orphans
```

Key decisions:
- `--wait --wait-timeout 300`: lets compose handle the healthcheck convergence rather than hand-rolling a polling loop.
- `packages: read`: required to pull `ghcr.io/lfdesousa/audittrace-{postgresql,redis}` (same auth pattern as the kind workflow).
- Test scripts under `tests/integration/compose/` mirror the inline bash in `integration-content-control.yml` — easier to share via local re-run.

## 5. Parameterisation surface

Goal: the SAME `docker-compose.yml` runs in three environments. Differences captured in `.env` files.

### Env vars that flip CI → dev

| Variable | `.env.ci` value | `.env.dev-real-llm` value | Note |
|---|---|---|---|
| `AUDITTRACE_LLAMA_URL` | `http://mock-llm:11435/v1` | `http://host.docker.internal:11435/v1` | Resolves to mock service in CI, host llama-server in dev |
| `AUDITTRACE_EMBED_URL` | `http://mock-llm:11435/v1` | `http://host.docker.internal:11436/v1` | Same mock serves embeddings on the same port in CI |
| `AUDITTRACE_SUMMARIZER_URL` | `http://mock-llm:11435/v1` | `http://host.docker.internal:11437/v1` | |
| `AUDITTRACE_SUMMARIZER_ENABLED` | `false` | `true` | CI doesn't exercise the background loop |
| `AUDITTRACE_AUTH_ENABLED` | `false` | `true` | CI bypasses Keycloak |
| `AUDITTRACE_AUTH_REQUIRED` | `false` | `true` | CI bypasses Keycloak |
| `AUDITTRACE_LANGFUSE_ENABLED` | `false` | `true` (if `--profile langfuse`) | |
| `AUDITTRACE_OTLP_ENDPOINT` | empty (no obs) | `http://otel-collector:4318` (if `--profile obs`) | |
| `AUDITTRACE_LOG_LEVEL` | `INFO` | `DEBUG` (dev) | |
| `COMPOSE_PROFILES` | `mock-llm` | `` (or `real-llm,obs,content-control`) | Drives profile selection without `--profile` CLI flag |
| `AUDITTRACE_POSTGRES_PASSWORD` | `test-postgres-password` | from dev `.env` | CI uses literal; dev sources from operator |
| `AUDITTRACE_REDIS_PASSWORD` | `test-redis-password` | from dev `.env` | |
| `AUDITTRACE_CHROMA_TOKEN` | `test-chroma-token` | from dev `.env` | |
| `AUDITTRACE_MINIO_SECRET_KEY` | `test-minio-secret-key-32chars!!` | from dev `.env` | |
| `AUDITTRACE_MINIO_KMS_KEY` | `<32-byte base64>` | from dev `.env` | |
| `KEYCLOAK_ADMIN_PASSWORD` | `admin` | from dev `.env` | |

### Two committed `.env` snippets

**`.env.ci`** (committed under repo root, used by GHA):

```bash
COMPOSE_PROFILES=mock-llm
AUDITTRACE_LLAMA_URL=http://mock-llm:11435/v1
AUDITTRACE_EMBED_URL=http://mock-llm:11435/v1
AUDITTRACE_SUMMARIZER_URL=http://mock-llm:11435/v1
AUDITTRACE_SUMMARIZER_ENABLED=false
AUDITTRACE_AUTH_ENABLED=false
AUDITTRACE_AUTH_REQUIRED=false
AUDITTRACE_LANGFUSE_ENABLED=false
AUDITTRACE_LOG_LEVEL=INFO
AUDITTRACE_POSTGRES_PASSWORD=test-postgres-password
AUDITTRACE_REDIS_PASSWORD=test-redis-password
AUDITTRACE_CHROMA_TOKEN=test-chroma-token
AUDITTRACE_MINIO_ACCESS_KEY=minioadmin
AUDITTRACE_MINIO_SECRET_KEY=test-minio-secret-key-32chars!!
AUDITTRACE_MINIO_KMS_KEY=YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY=
KEYCLOAK_ADMIN_PASSWORD=admin
```

**`.env.dev-real-llm.example`** (committed; operator copies to `.env`):

```bash
# COMPOSE_PROFILES left blank → no LLM service in compose;
# memory-server's AUDITTRACE_LLAMA_URL points at host llama-server.
# Add `obs` and/or `content-control` as profiles when you want
# the full observability stack or the cc scan pipeline.
COMPOSE_PROFILES=
AUDITTRACE_LLAMA_URL=http://host.docker.internal:11435/v1
AUDITTRACE_EMBED_URL=http://host.docker.internal:11436/v1
AUDITTRACE_SUMMARIZER_URL=http://host.docker.internal:11437/v1
AUDITTRACE_SUMMARIZER_ENABLED=true
AUDITTRACE_AUTH_ENABLED=true
AUDITTRACE_AUTH_REQUIRED=true
AUDITTRACE_LANGFUSE_ENABLED=false
AUDITTRACE_LOG_LEVEL=DEBUG
# Real secrets sourced from operator's private store
AUDITTRACE_POSTGRES_PASSWORD=
AUDITTRACE_REDIS_PASSWORD=
# ... etc
```

`.env` is already gitignored. `.env.ci` and `.env.dev-real-llm.example` are committed templates.

## 6. Telemetry plumbing

Three modes:

| Mode | Services | When |
|---|---|---|
| **off** | none | default in CI; lowest RAM |
| **minimal** | otel-collector (stdout exporter only) | dev with `AUDITTRACE_OTLP_ENDPOINT` set; no tempo/loki/grafana — collector logs spans to stdout for `docker compose logs otel-collector` inspection |
| **full** | otel-collector + tempo + loki + promtail + grafana + langfuse-web (+ langfuse-postgres) | dev/demo when you want the live Grafana dashboards from prod |

Gating: `COMPOSE_PROFILES=obs` → minimal; `COMPOSE_PROFILES=obs,langfuse` → full.

### Recommendation for CI

**Off by default.** Rationale:
1. CI's job is API-shape validation, not dashboard recreation. Tempo + Loki traces are reconstructible in prod and kind already.
2. Adding ~3 GB RAM (tempo + loki + grafana + langfuse + langfuse-postgres) to every PR run for a property already tested elsewhere is wasteful.
3. The compose workflow's failure-mode diagnostics rely on `docker compose logs` and uploaded log artefacts, which work without an obs stack.

### Recommendation for dev

**Minimal** is the default-on profile when an operator adds `obs` to `COMPOSE_PROFILES`. The full stack is opt-in (`obs,langfuse`) because langfuse is heavyweight and not always wanted.

### Config files

`config/compose/` (new directory) holds the otel-collector, tempo, loki, promtail, grafana provisioning, and langfuse env-template files. Mirrors the k8s ConfigMaps in `charts/audittrace/templates/observability/`. Where possible, the SAME config file feeds both paths (single source of truth; reduces drift surface).

## 7. Step-by-step implementation order

Each step is small enough to commit-and-test on its own. ADR-049 evidence captured at every step.

1. **Refresh image pins + add rabbitmq + drop external network** — minimal change to current `docker-compose.yml`: ghcr pg+redis pins, bitnamilegacy/rabbitmq service, drop `external: true` on the network. Validate: `docker compose up -d` locally, memory-server reaches /health 200.
2. **Add mock-llm service under the `mock-llm` profile** — port `tests/integration/fixtures/mock-llm-configmap.yaml`'s Python script to a compose-side file at `tests/integration/fixtures/compose/mock-llm/server.py`. Symlink or include from the kind path so the two stay in sync (or accept controlled duplication and add a drift-guard test). Validate: `docker compose --profile mock-llm up -d` produces a /health 200 from the mock.
3. **Commit `.env.ci` + `.env.dev-real-llm.example`** — codify the parameterisation surface from §5.
4. **Add `.github/workflows/e2e-compose.yml`** — the new GHA workflow. Validate: workflow goes green on a draft PR.
5. **Migrate test scripts** — move the inline bash from `integration-content-control.yml`'s upload/chat steps into shared `tests/integration/compose/*.sh` files. Validate: local re-run works (`bash tests/integration/compose/test-upload-clean-and-eicar.sh`).
6. **Add vault dev-mode service under `vault` profile** — optional; for parity demos. Validate: `docker compose --profile vault up -d` + `vault status` returns initialised + unsealed.
7. **Add obs stack (otel-collector + tempo + loki + grafana + promtail) under `obs` profile** — config files under `config/compose/` mirroring chart templates. Validate: traces visible in local Grafana when `AUDITTRACE_OTLP_ENDPOINT=http://otel-collector:4318`.
8. **Add langfuse under `langfuse` profile** — depends on a sibling langfuse-postgres service. Validate: Langfuse UI shows traces from a sample request.
9. **Add content-control sibling under `content-control` profile** — cc-control-plane + cc-clamd. Validate: POST /memory/upload of an eicar PDF produces a `rejected_malware` terminal status via the compose stack.
10. **AGENTS.md operator caveat** — document compose-vs-helm posture, profile combinations, the parallel-maintenance discipline anchor.
11. **(Optional, deferred)** drift-guard test: parse compose service image pins + chart subchart pins and assert binary-version match (PG / Redis / RabbitMQ).

## 8. Validation checklist (ADR-049)

### Verification (unit-level)

- [ ] `docker compose config` passes for every profile combination: `(no-profile)`, `mock-llm`, `real-llm`, `obs`, `obs,langfuse`, `content-control`, and `mock-llm,obs,content-control`.
- [ ] YAML lint on `docker-compose.yml`, `docker-compose.dev.yml`, `.env.ci`, `.env.dev-real-llm.example`.
- [ ] `tests/test_compose_drift.py` (new) asserts the compose image pins for `postgres` / `redis` / `rabbitmq` match `charts/audittrace/values.yaml` defaults.

### Validation (live)

- [ ] `docker compose --profile mock-llm up -d --wait` locally produces /health 200 from memory-server within 180 s.
- [ ] POST /memory/upload (clean + eicar PDF) reaches terminal status from compose stack.
- [ ] POST /v1/chat/completions returns OpenAI-shape JSON with `choices[0].message.content="bruno"` via mock-llm.
- [ ] `.github/workflows/e2e-compose.yml` goes green on a PR (full run captured).

### Reconstruction

- [ ] Workflow run URL + run ID referenced from the PR body.
- [ ] Compose-logs artefact attached to the workflow run.
- [ ] Local `docker compose ps` + `docker compose logs --tail=50` captured to evidence dir under `~/work/audittrace-evidence/<date>-b7-compose/`.

## 9. Out of scope

Explicitly NOT in B7's scope:

- **Full obs stack in CI by default**. Opt-in via `COMPOSE_PROFILES=obs`.
- **Real-LLM CI runs**. GHA has no GPU; the `real-llm` profile exists for dev but is never exercised in CI.
- **HA / multi-replica posture**. Compose stack is single-instance; HA is k8s territory (see `project_session_20260503_close`).
- **Production cutover**. Compose remains a parallel runtime per `feedback_docker_compose_retained`; the k8s chart is canonical for prod.
- **Vault auto-unseal** in compose. Compose uses Vault dev mode (auto-unsealed at start); the systemd auto-unseal unit applies only to the k3s deployment.
- **Subchart drift forensic tooling**. The data-compat harness (`docker-compose.data-compat.yml`) stays separate; B7 doesn't merge it into the main stack.
- **Bruno collection refresh for compose endpoints**. Bruno is wired to the Istio Gateway URL; compose endpoints are different. Deferred to the existing "Bruno collection completeness" backlog item.
- **Trust store / SwissSign 2020-2 root** in compose. Backlog #13 territory.
- **Cluster-side recall_semantic of compose-generated artefacts**. Out of scope; compose is a local runtime, not a memory-shared sink.

## 10. Open questions for Luis

Implementation should not start until these are answered.

1. **§3 LLM-swap option** — Confirm option (a) profiles is the right call vs option (c) override files. The recommendation rests on the structural parallel with the kind-CI mock-LLM pattern.
2. **§2 RabbitMQ image pin** — chart's bitnamilegacy/rabbitmq tag is the wrong-binary-but-frozen pre-sunset version (per the 2026-05-13 forensic). Should compose use the same `bitnamilegacy/rabbitmq:<chart-pin>` for binary parity even though the tag is mislabelled? Or pin to `ghcr.io/lfdesousa/audittrace-rabbitmq:<frozen-tag>` once an equivalent frozen mirror is built (B1.5-style)? **No such mirror exists today** — building it would be a B7 dependency or a follow-up.
3. **§4 workflow naming** — `e2e-compose.yml` vs replacing the existing `e2e.yml`? Recommendation is sibling (per `feedback_docker_compose_retained`). Confirm.
4. **§6 obs stack** — accept that compose CI runs with obs OFF, and obs is dev-opt-in only?
5. **§7 step 9** — is exercising the full cc scan pipeline through compose in B7's scope, or is that a follow-up after compose core lands? The PR-A11 kind test already covers that path; B7 may not need to.
6. **mock-llm script duplication** — accept controlled duplication (a separate `tests/integration/fixtures/compose/mock-llm/server.py` mirroring the kind ConfigMap script), or symlink the same file into both consumers? Symlink is portable on Linux but cross-platform friction; duplication + drift-guard test is more verbose but explicit.
