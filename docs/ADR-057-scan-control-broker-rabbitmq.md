# ADR-057 — Scan-control broker = RabbitMQ

**Status:** Accepted
**Date:** 2026-05-10
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-048 (ingestion content-control), ADR-046 (async chat
persistence — stays on Redis Streams), ADR-041 (named-dependency
taxonomy — RabbitMQ becomes the tenth named dependency).

## Context

ADR-048 introduces the content-control trust boundary: memory-server
PUTs uploads to a MinIO `quarantine/` prefix it cannot read; a
separate `audittrace-content-control` service (private repo) reads
quarantine, scans, publishes verdicts. Verdicts ride a message
broker between the two services.

The broker decision was originally locked as **Redis Streams** (PR-B1
+ PR-A1, 2026-05-10 morning), reusing ADR-046's `async_persist`
XADD/XREADGROUP pattern with the explicit goal of "no 10th named
dependency". The plan also said MinIO event notification would
publish object-created events directly to the same Redis Stream so
content-control consumes from a single source.

PR-B2 surfaced two problems:

1. **MinIO does not publish to Redis Streams.** The `notify_redis`
   event target writes to a Redis LIST (RPUSH), not a Stream (XADD).
   Streams are not a supported MinIO event-target format.

2. **The shortcut to bypass that — "memory-server XADDs directly
   from its `/memory/upload` route" — couples HTTP request shape to
   broker availability.** If Redis is briefly unavailable, the
   upload request hangs or fails. Luis rejected this as a coupling
   shortcut: *"I want a proper event-based mechanism not a shortcut
   and Julien Danjou proposes a Python Queue."*

The "proper event-based mechanism" requires **decoupling the upload
HTTP shape from the broker** through an in-process producer-consumer
queue (Danjou §3, *Scaling Python*, asyncio.Queue) backed by a
durable persistent record (Hohpe Outbox pattern — `memory_items` row
becomes the outbox). Once that is the producer pattern, the
specific broker choice can be evaluated on its merits rather than
on "what's already deployed" expedience.

## Decision

**RabbitMQ (AMQP topic exchanges + quorum queues + DLX) becomes the
scan-control broker.** Adopted via the Bitnami subchart in the
AuditTrace-AI Helm chart. RabbitMQ is the tenth named dependency in
ADR-041's taxonomy.

ADR-046 (chat-completion async persistence) **stays on Redis
Streams** — different domain, different broker is fine. The two
brokers coexist by design.

## Why RabbitMQ over the alternatives

The 2026-05-10 PM design review evaluated five alternatives. Honest
trade-offs:

| Broker | Verdict | Why |
|---|---|---|
| **Redis Streams (the original locked decision)** | **Rejected.** | Without an outbox layer the "memory-server XADDs directly" shortcut violates Luis's no-shortcuts rule. Adding the outbox layer would have made Redis Streams workable, but at that point the *queue+ack* shape RabbitMQ provides natively becomes preferable to the *append-only-log* shape Streams provides. |
| **NATS JetStream** | Rejected. | Cleaner exactly-once + consumer-group semantics than RabbitMQ, but adds a less-mature broker to the operations footprint. RabbitMQ is the canonical "I need a proper queue" answer in 2026; NATS is the canonical "I need lightweight pub/sub". This pipeline is queues-shaped (durable work, ack on success, DLX on poison). |
| **Redpanda (Kafka-compatible)** | Rejected. | Streaming-log primitive (replayable, partitioned). Overkill for the scan-request → verdict pipeline, which is fundamentally a queue with ack semantics. Replay isn't a feature this domain needs. |
| **pgmq (Postgres extension)** | Rejected, but tempting. | Strongest transactional-outbox story (queue insert atomic with manifest INSERT in the same DB transaction), zero new pod. Throughput ceiling is "Postgres-shaped" — fine for our PDF-upload rate. Rejected because (a) it ties the queue's availability to PG's, and PG is already the tightest dependency in our cluster, and (b) `aio-pika` + RabbitMQ has 10+ years of production hardening that pgmq lacks. Worth revisiting if RabbitMQ becomes operationally painful. |
| **NSQ** | Rejected. | Lightweight ops, but less mature; no replay; ephemeral by default. The "for stronger queue features just use RabbitMQ" answer matches our case. |

**RabbitMQ wins because:**

1. **Queue+ack semantics natively.** Quorum queues with `x-delivery-limit`
   + DLX-on-rejection is a single declaration. Redis Streams
   `XPENDING` + `XAUTOCLAIM` re-implements those primitives in
   application code (every consumer must.)
2. **Topic exchange decouples future routing.** v2 PII/DLP events
   and v3 sandboxed-detonation events can publish to
   `scan.classification.*` and `scan.detonation.*` routing keys
   without rebinding existing consumers.
3. **Mature operational story.** 20-year project, well-documented
   failure modes, established runbooks, well-supported Bitnami
   chart.
4. **Python ecosystem fit.** `aio-pika` is the de-facto async AMQP
   client with active maintenance and asyncio-native APIs.
5. **mTLS-friendly inside Istio.** Single TCP port (5672) for AMQP,
   simple AuthorizationPolicy.

## AMQP topology (locked)

| Element | Type | Properties |
|---|---|---|
| `audittrace.scan` | Topic exchange | durable, auto_delete=false. Producer-side: memory-server publishes scan-requests with routing keys `scan.request.*`. |
| `audittrace.scan.verdicts` | Topic exchange | Same shape; content-control publishes verdicts with `scan.verdict.*`. |
| `audittrace.scan.audit` | Topic exchange | Same; content-control publishes SECURITY audit rows with `scan.audit.*`. |
| `audittrace.scan.dlx` | Topic exchange | DLX target for the scan-requests queue. Catch-all binding to `audittrace.scan.requests.dlq`. |
| `audittrace.scan.requests` | Quorum queue | Bound to `audittrace.scan` via `scan.request.*`. `x-queue-type=quorum`, `x-dead-letter-exchange=audittrace.scan.dlx`, `x-delivery-limit=5`. Consumed by content-control. |
| `audittrace.scan.requests.dlq` | Quorum queue | Bound to `audittrace.scan.dlx` via `#`. Operator-monitored; manual remediation only. |
| `audittrace.scan.verdicts` | Quorum queue | Bound to `audittrace.scan.verdicts` via `scan.verdict.*`. Consumed by memory-server. |
| `audittrace.scan.audit` | Quorum queue | Bound to `audittrace.scan.audit` via `scan.audit.*`. Consumed by memory-server's audit consumer. |

Materialised post-install / post-upgrade by
`charts/audittrace/templates/rabbitmq/job-amqp-topology-bootstrap.yaml`.
Idempotent (every command is HTTP `PUT` against the management API,
which is upsert-shaped).

## Users + permissions

| User | Source | Permissions on default vhost |
|---|---|---|
| `audittrace` (broker admin) | Bitnami subchart `auth.username` | configure: `.*` / write: `.*` / read: `.*` |
| `content-control` | Provisioned by topology bootstrap Job | configure: `""` / write: `^audittrace\.scan\.(verdicts\|audit)$` / read: `^audittrace\.scan\.requests$` |

Content-control's user is intentionally unable to **create** new
exchanges or queues, **publish** to anything outside the verdict +
audit exchanges, or **consume** from any queue except scan-requests.
A compromised content-control identity cannot pollute the broker.

## Producer pattern (memory-server, PR-B3)

Hohpe Transactional Outbox + Danjou asyncio.Queue:

```
POST /memory/upload
   1. PUT bytes → MinIO quarantine prefix         (durable)
   2. INSERT memory_items                          (durable; scan_status=pending_scan)
                                                   published_at=NULL
   3. asyncio.Queue.put(ScanRequest)               (in-process buffer)
   4. return 202 + scan_id                         (no broker dependency)

ScanRequestPublisher (asyncio task in memory-server lifespan):
   while not stack.cancelled():
       req = await Queue.get()
       async with channel.transaction():
           await channel.basic_publish(
               exchange='audittrace.scan',
               routing_key=f'scan.request.{event_class}',
               body=ScanRequest.to_json().encode(),
               properties=BasicProperties(delivery_mode=2),  # persistent
           )
       UPDATE memory_items SET published_at = NOW()
       WHERE id = req.scan_id

PendingScanReaper (periodic asyncio task, every 30s):
   rows = SELECT * FROM memory_items
            WHERE scan_status='pending_scan'
              AND published_at IS NULL
              AND created_at < NOW() - INTERVAL '30s'
   for row in rows:
       Queue.put_nowait(ScanRequest.from_row(row))   # re-publish
```

Properties:

- **Decoupled** — the route never touches the broker.
- **Durable** — the manifest row is the source of truth; if
  memory-server crashes mid-flight (Queue not yet drained), the
  reaper finds and re-publishes the orphaned rows on next boot.
- **Fast happy path** — route → queue is microseconds; 202
  returns immediately.
- **Crash-safe** — at-least-once via the publisher; consumer-side
  dedupes by `scan_id`.

## Consumer pattern (content-control, PR-A3)

`adapters/scan_request_consumer_rabbitmq.py` implements the
`ScanRequestConsumer` port via `aio-pika`:

- `RobustConnection` for auto-reconnect.
- `basic_consume` on `audittrace.scan.requests` with manual ack.
- On success: `channel.basic_ack(delivery_tag)`.
- On retryable failure: `channel.basic_nack(delivery_tag, requeue=False)`
  — DLX picks it up; `x-delivery-limit=5` caps retries.

The same shape, mutatis mutandis, for `VerdictPublisher` (publishes
to `audittrace.scan.verdicts` exchange) and `AuditEmitter`
(publishes to `audittrace.scan.audit` exchange).

## Why ADR-046 stays on Redis Streams

Different domain. Chat-completion persistence is high-frequency
(every chat completion), low-criticality (a lost row is annoying,
not catastrophic — the chat itself succeeded). The fast XADD path
+ in-cluster Redis is a good fit. Migrating ADR-046 to RabbitMQ
buys nothing immediate and is migration risk for working code.

The two brokers coexist:

- Redis Streams: ADR-046 chat persistence
- RabbitMQ: ADR-048 scan-control

Both are in-cluster, both behind Istio mTLS, both seeded by
`setup-vault.sh`. Operators learn both patterns; the runbook
overhead is real but small.

## Acceptance criteria

This ADR is accepted when:

1. The Bitnami `rabbitmq` subchart deploys in the AuditTrace-AI
   chart with `clustering.enabled=false` (single-node, dev).
2. `templates/rabbitmq/job-amqp-topology-bootstrap.yaml` runs
   post-install and creates the four exchanges + four queues +
   bindings + content-control user with scoped permissions.
3. Vault paths `kv/audittrace/rabbitmq/admin` +
   `kv/audittrace/content-control/rabbitmq` are seeded from
   `secrets/rabbitmq_*.txt` files.
4. `authorizationpolicy-rabbitmq.yaml` whitelists memory-server-sa
   + content-control SA on AMQP port 5672.
5. ADR-041's dependency table updates to 10 named dependencies.
6. Memory-server's PR-B3 ships the `ScanRequestPublisher`
   asyncio.Queue + outbox-pattern producer.
7. Content-control's PR-A3 ships the aio-pika `RabbitMQScanRequestConsumer` /
   `RabbitMQVerdictPublisher` / `RabbitMQAuditEmitter` adapters.

## Reversibility

The hexagonal architecture in audittrace-content-control means the
broker is swappable. If RabbitMQ ever becomes operationally painful,
swapping to NATS JetStream or pgmq is a new adapter module + a
factory branch — no business-logic changes. The investment in
this ADR is the broker choice, not the code coupling.

## Cross-references

- ADR-048 — content-control trust boundary (the source ADR)
- ADR-046 — async chat persistence (stays on Redis Streams)
- ADR-041 — named-dependency taxonomy (10th dependency added)
- ADR-027 — MinIO bucket layout (event notification path NOT used)
- ADR-049 — test + evidence + reconstructibility gate
- audittrace-content-control PR-A3 — aio-pika consumer/publisher adapters
- AuditTrace-AI PR-B3 — outbox + asyncio.Queue + AMQP publisher
