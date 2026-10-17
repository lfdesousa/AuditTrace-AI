# ADR-057 — Scan-control broker = RabbitMQ

**Status:** Accepted

Canonical decision record: [`docs/ADR-057-scan-control-broker-rabbitmq.md`](../../ADR-057-scan-control-broker-rabbitmq.md)
(this file is a naming-convention pointer only, kept for the Structurizr `!adrs` browser —
`NNNN-title.md`, four-digit — never a duplicate; edit the canonical file, not this one).

RabbitMQ carries the scan-control plane between the memory-server (producer) and
`audittrace-content-control` (consumer): `audittrace.scan` (requests, topic exchange),
`audittrace.scan.verdicts` + `audittrace.scan.audit` (results, topic exchanges), and
`audittrace.scan.dlx` → `audittrace.scan.requests.dlq` (the dead-letter path, quorum queue,
`x-delivery-limit=5`). The 2026-08-23 amendment adds the operator recovery lever
(`scripts/audittrace-scan-dlq`: `inspect` / `replay` / `drain --confirm`) — the sibling of
the Redis async-persist DLQ tool and the Postgres index dead-letter replay script.
