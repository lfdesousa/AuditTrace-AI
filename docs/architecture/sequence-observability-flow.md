# Sequence Diagram: Observability Data Flow (ADR-028)

Three parallel telemetry paths: metrics via OTel Collector to Prometheus,
logs via Promtail to Loki, traces via Langfuse SDK (unchanged from ADR-021.2).

## Metrics Path — OTel Collector to Prometheus

```mermaid
sequenceDiagram
    participant App as memory-server
    participant Collector as OTel Collector
    participant Prom as Prometheus

    App->>Collector: OTLP/HTTP (:4318)<br/>sovereign.operation.duration histogram<br/>sovereign.operation.errors counter

    Collector->>Prom: Remote write (:9090/api/v1/write)

    Note over Prom: Stores time-series<br/>15-day retention
```

## Infrastructure Scraping — Prometheus pulls from native endpoints

```mermaid
sequenceDiagram
    participant Prom as Prometheus
    participant Istio Gateway as Istio Gateway (:8080)
    participant Llama as llama-server (:11435)
    participant MinIO as MinIO (:9000)

    loop Every 15s
        Prom->>Istio Gateway: GET /metrics
        Istio Gateway-->>Prom: request rate, latency, status codes

        Prom->>Llama: GET /metrics
        Llama-->>Prom: tokens/sec, prompt eval, KV cache

        Prom->>MinIO: GET /minio/v2/metrics/cluster
        MinIO-->>Prom: bucket ops, disk usage, API latency
    end
```

## Log Aggregation — Promtail to Loki

```mermaid
sequenceDiagram
    participant Containers as Docker containers
    participant Promtail as Promtail
    participant Loki as Loki

    Promtail->>Containers: Docker socket (/var/run/docker.sock)<br/>discover containers on audittrace-net

    loop Continuous
        Containers-->>Promtail: stdout/stderr log lines

        Note over Promtail: Extract labels:<br/>container, compose_service<br/>Parse JSON (level, logger, trace_id)

        Promtail->>Loki: POST /loki/api/v1/push
    end
```

## Trace Path — Langfuse SDK (unchanged)

```mermaid
sequenceDiagram
    participant App as memory-server
    participant LF as Langfuse (:3000)

    Note over App: Langfuse SDK initialised<br/>in telemetry.py

    App->>LF: start_as_current_observation()
    App->>LF: update_current_span(input, output)

    Note over LF: Trace graph view<br/>langgraph_node + langgraph_step
```

## Grafana — Unified query layer

```mermaid
sequenceDiagram
    participant User as Operator
    participant Grafana as Grafana (:3001)
    participant Prom as Prometheus
    participant Loki as Loki

    User->>Grafana: Open Sovereign AI Operations dashboard

    Grafana->>Prom: PromQL: histogram_quantile(0.95, sovereign_operation_duration)
    Prom-->>Grafana: P95 latency time-series

    Grafana->>Loki: LogQL: {compose_service=~".+"} |= "ERROR"
    Loki-->>Grafana: Error log lines with timestamps

    Grafana-->>User: Unified dashboard:<br/>latency + errors + logs
```

## Full Data Flow Summary

```
memory-server
  ├── OTLP/HTTP ──► OTel Collector ──► Prometheus (metrics)
  ├── Langfuse SDK ──► Langfuse (traces) [ADR-021.2]
  └── stdout ──► Promtail ──► Loki (logs)

Prometheus ◄── scrape ── Istio Gateway, llama-server, MinIO

Grafana
  ├── PromQL ──► Prometheus
  └── LogQL ──► Loki
```
