# ADR-028: Observability Aggregation Stack — Prometheus + Grafana + Loki

**Status:** Accepted
**Date:** 2026-04-13
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-014.4 (OTel logging + tracing), ADR-021.2 (Langfuse sibling stack)

## Context

ADR-014.4 wired `@log_call` across the entire codebase — every decorated call
emits a DEBUG log line, an OTel span, and a histogram metric. But there is no
backend to receive the metrics. Langfuse (ADR-021.2) captures LLM traces but
explicitly rejects metrics (OTLP metric export returns 400 Bad Request). Logs
go to stdout only — no aggregation, no search, no correlation with metrics.

The result: we have instrumented code but no dashboards, no alerting, no way to
answer "what was the P95 latency of memory context builds last hour?" or "did
Keycloak restart overnight?".

## Decision

Deploy **Prometheus** (metrics), **Grafana** (dashboards), **Loki** (logs), and
an **OpenTelemetry Collector** (bridge) as a sibling Docker Compose stack.
Follow the ADR-021.2 pattern: separate directory, shared `audittrace-net`
network, independent lifecycle from the application stack.

### §1. Architecture — OTel Collector as central hub

```
memory-server (OTLP/HTTP)
        │
        ▼
OTel Collector (:4318)
   ├──▶ Prometheus (:9090)    ← metrics via remote write
   └──▶ Loki (:3100)         ← logs via OTLP
                                        ▲
Promtail ───── Docker socket ───────────┘  (container stdout/stderr)

Grafana (:3001)
   ├──▶ Prometheus (metrics queries + service graph)
   ├──▶ Loki (log queries, trace-ID derived-field → Tempo)
   └──▶ Tempo (trace queries, service map + node graph)      ← added 2026-04-14

memory-server ──▶ Langfuse (:3000)  ← LLM traces via LangfuseSpanProcessor
                                       attached to the same TracerProvider
                                       (see ADR-014.4 amendment 2026-04-14)
```

The OTel Collector decouples the memory-server's export format (OTLP) from the
storage backends. New exporters are collector config changes, not application
changes.

**Traces now fan out to both Tempo and Langfuse** — Tempo captures the full
cross-service call chain (HTTP → auth → DB → llama-server → ChromaDB → MinIO),
Langfuse continues to surface LLM-specific observations with its own
first-class Input/Output panels. Both signals flow through the Langfuse-installed
global ``TracerProvider`` via dual SpanProcessors — see ADR-014.4 §Amendment 2026-04-14.

**Collector self-telemetry** is exposed on ``:8888/metrics`` via the 0.149
``service.telemetry.metrics.readers.pull`` config block; Prometheus scrapes
it for the "OTel Collector — Throughput / Queue Saturation" dashboard panels.

### §2. Sibling stack structure

```
../observability-stack/
├── docker-compose.yml
├── otel-collector/
│   └── otel-collector-config.yml
├── prometheus/
│   └── prometheus.yml
├── loki/
│   └── loki-config.yml
├── promtail/
│   └── promtail-config.yml
└── grafana/
    ├── provisioning/
    │   ├── datasources/datasources.yml
    │   └── dashboards/dashboards.yml
    └── dashboards/
        └── sovereign-overview.json
```

### §3. Scrape targets

Prometheus scrapes infrastructure services that expose native `/metrics`
endpoints. Application metrics arrive via the OTel Collector (OTLP → Prometheus
remote write).

| Target | Endpoint | Metrics |
|---|---|---|
| Traefik | `traefik:8080/metrics` | Request rate, latency, status codes |
| llama-server | `host.docker.internal:11435/metrics` | Tokens/sec, prompt eval time, KV cache usage |
| MinIO | `minio:9000/minio/v2/metrics/cluster` | Bucket operations, disk usage, API latency |
| OTel Collector | `otel-collector:8888/metrics` | Pipeline health, drop counts |
| memory-server | Via OTel Collector (OTLP) | `sovereign.operation.duration`, `sovereign.operation.errors` |

### §4. Log aggregation

Promtail attaches to the Docker socket and scrapes stdout/stderr from all
containers on `audittrace-net`. Labels are auto-extracted from Docker
metadata: `container_name`, `compose_service`, `compose_project`. The
memory-server's `StructuredFormatter` emits JSON lines at DEBUG level,
which Loki indexes for full-text search + structured field extraction.

### §5. Grafana dashboards

Auto-provisioned on first boot:

**Sovereign AI Operations:**
- Request latency: P50/P95/P99 from `sovereign_operation_duration` histogram
- Error rate: `sovereign_operation_errors` counter by error type
- Memory layer hits: per-layer result counts from `@log_call` spans
- Infrastructure: Traefik RPS, llama-server tokens/sec, MinIO operations
- Logs panel: Loki query for `{compose_service=~".+"} |= "ERROR"`

### §6. Port allocation

| Service | Port | Notes |
|---|---|---|
| OTel Collector | `:4318` | OTLP HTTP receiver (internal) |
| Prometheus | `:9090` | Query API + UI |
| Loki | `:3100` | Log push/query API |
| Grafana | `:3001` | Dashboards (`:3000` taken by Langfuse) |

## Consequences

### Positive
- Full observability pipeline: metrics → dashboards, logs → search, both correlated by time
- Sibling stack pattern proven by Langfuse — same operational model
- OTel Collector decouples export format from backends — future-proof
- Promtail auto-discovers containers — zero config per new service
- `AUDITTRACE_METRICS_ENABLED=true` is now safe (collector accepts metrics, unlike Langfuse)

### Negative
- 5 additional containers (~500MB RAM total at idle)
- Prometheus retention defaults to 15 days (sufficient for dev, tune for production)
- Grafana dashboard JSON is verbose (~500 lines) — maintain via Grafana UI, export to JSON

### Neutral
- Langfuse remains the trace backend — no migration, no duplication
- llama-server metrics require `host.docker.internal` bridge (same as LLM proxy calls)

## Amendment 2026-04-18 — k3s realisation

The original decision assumed a single Docker Compose runtime. The production deployment now runs memory-server on k3s + Istio with STRICT mTLS and SPIFFE/SVID workload identity (see `charts/audittrace/` and `docs/guides/deployment-runbook.md`). The observability *backends* stay where they are — sibling Docker Compose stack in the `lfdesousa/AiSovereignObservability` repository — but the *telemetry shippers* (OTel Collector + Promtail) now have a second deployment profile:

| Profile | OTel Collector | Promtail |
|---|---|---|
| Docker Compose dev | Container in observability-stack repo, Docker-socket discovery | Container in observability-stack, Docker-socket discovery |
| k3s production | DaemonSet in the AuditTrace-AI Helm chart (`templates/observability/otel-collector-daemonset.yaml`) | DaemonSet in the AuditTrace-AI Helm chart (`templates/observability/promtail-daemonset.yaml`), Kubernetes pod-log discovery |

The k3s DaemonSets egress to the same sibling backends over the cluster-external NodePort. This keeps the storage/query surface identical across profiles — the same dashboards, the same LogQL queries, the same retention — only the discovery mechanism differs because kubelet log layout and Docker socket are different substrates.

This amendment does not revise the original decision; it records that ADR-028 now applies to two runtimes, not one. The sibling stack pattern (ADR-021.2) still holds: backends are deployed and operated independently from the application stack.

### Amendment consequences
- One more DaemonSet to reason about per k3s cluster (OTel Collector + Promtail) — counted in the Helm chart resource footprint.
- Collector self-telemetry on `:8888/metrics` is scraped by the sibling Prometheus via NodePort `:30888` (added in the same change).
- Nothing in the application code path changes. The OTLP endpoint is still `http://otel-collector:4318`; the name resolves inside whichever runtime is active.
