# Sequence Diagram: Observability Data Flow (ADR-028)

Three parallel telemetry paths: metrics via OTel Collector to Prometheus,
logs via Promtail to Loki, traces via Langfuse SDK (unchanged from ADR-021.2).

## Metrics Path — OTel Collector to Prometheus

```mermaid
sequenceDiagram
    participant App as memory-server
    participant Collector as OTel Collector
    participant Prom as Prometheus

    App->>Collector: OTLP/HTTP (:4318)<br/>audittrace.operation.duration histogram<br/>audittrace.operation.errors counter

    Collector->>Prom: Remote write (:9090/api/v1/write)

    Note over Prom: Stores time-series<br/>15-day retention
```

## Infrastructure Scraping — Prometheus pulls from native endpoints

```mermaid
sequenceDiagram
    participant Prom as Prometheus
    participant Istio as Istio IngressGateway (:15090)
    participant Llama as llama-server (:11435)
    participant MinIO as MinIO (:9000)

    loop Every 15s
        Prom->>Istio: GET /stats/prometheus
        Istio-->>Prom: request rate, latency, status codes (Envoy metrics)

        Prom->>Llama: GET /metrics
        Llama-->>Prom: tokens/sec, prompt eval, KV cache

        Prom->>MinIO: GET /minio/v2/metrics/cluster
        MinIO-->>Prom: bucket ops, disk usage, API latency
    end
```

## Log Aggregation — Promtail to Loki

```mermaid
sequenceDiagram
    participant Pods as k8s Pods (audittrace namespace)
    participant Promtail as Promtail (DaemonSet)
    participant Loki as Loki

    Promtail->>Pods: kubelet API / node log directory<br/>discover pods in audittrace namespace

    loop Continuous
        Pods-->>Promtail: stdout/stderr log lines

        Note over Promtail: Extract labels:<br/>pod, namespace, container<br/>Parse JSON (level, logger, trace_id)

        Promtail->>Loki: POST /loki/api/v1/push
    end
```

**OTel Collector** runs as a DaemonSet inside the `audittrace` namespace,
receiving OTLP from the audittrace-server sidecar (mesh-local traffic).
Envoy sidecars on every pod also emit their own metrics to Prometheus
via the `/stats/prometheus` scrape endpoint.

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

    User->>Grafana: Open AuditTrace Operations dashboard

    Grafana->>Prom: PromQL: histogram_quantile(0.95, audittrace_operation_duration)
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

Prometheus ◄── scrape ── Istio IngressGateway, Envoy sidecars, llama-server, MinIO

Grafana
  ├── PromQL ──► Prometheus
  └── LogQL ──► Loki
```
