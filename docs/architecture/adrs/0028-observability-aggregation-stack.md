# ADR-028 — Observability aggregation stack (Prometheus + Grafana + Loki + Tempo)

**Status:** Accepted

Canonical decision record: [`docs/ADR-028-observability-aggregation-stack.md`](../../ADR-028-observability-aggregation-stack.md)
(this file is a naming-convention pointer only, kept for the Structurizr `!adrs` browser —
`NNNN-title.md`, four-digit — never a duplicate; edit the canonical file, not this one).

The 2026-08-22 per-signal retention amendment is the load-bearing update for this diagram
refresh: obs signals (traces/logs/metrics) are the **ephemeral corroborating witness**
(Tempo traces 7d, Loki logs 30d, Prometheus metrics 30d); the memory layers (MinIO,
ChromaDB) and the Postgres audit rows (`interactions`/`tool_calls`) are the **durable
record** (no-expiry, EU AI Act Art 12). Past a 30-day horizon, the memory layers + audit
rows are the entire reconstruction story.
