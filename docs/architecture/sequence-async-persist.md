# Async chat-completion persistence (ADR-046)

This sequence covers the opt-in `X-Persist-Mode: async` flow that moves
`_persist_interaction` off the chat-completion synchronous path. The
default path stays synchronous and is bit-identical to the pre-ADR-046
behaviour (per `feedback_openai_schema_inviolate`).

Three viewpoints below: producer happy path, poison → DLQ, and the
operator-driven replay flow.

## Happy path (producer → consumer)

```mermaid
sequenceDiagram
    participant Agent as Caller (OpenCode / curl / ...)
    participant Chat as chat_completions handler\n(routes/chat.py)
    participant Producer as AsyncPersistProducer\n(per-process singleton)
    participant Redis as Redis Streams\naudittrace:persist:stream
    participant Consumer as AsyncPersistConsumer\n(per-pod asyncio.Task)
    participant DB as PostgreSQL\ninteractions, tool_calls

    Agent->>Chat: POST /v1/chat/completions\nX-Persist-Mode: async
    Note over Chat: After upstream LLM responds —\nbuild InteractionRecord kwargs\n+ pending tool_calls
    Chat->>Producer: enqueue(kwargs, pending_tool_calls)
    Producer->>Redis: XADD record_json + tool_calls_json + enqueued_ts
    Redis-->>Producer: stream_id (e.g. "1777904012345-0")
    Producer-->>Chat: stream_id
    Note over Chat: Span attrs:\n  audittrace.persist.mode = async\n  audittrace.persist.stream_id = ...
    Chat-->>Agent: 200 OK (response body unchanged)

    Note over Consumer: Per iteration (block ms):\n1. XPENDING idle > pending_idle_ms\n2. XCLAIM (transfers ownership,\n   bumps delivery_count)\n3. XREADGROUP > for new entries
    Consumer->>Redis: XREADGROUP audittrace-persisters consumer-${HOSTNAME} > stream
    Redis-->>Consumer: [(stream_id, fields)]
    Consumer->>Consumer: deserialise_record(fields)
    Consumer->>DB: _persist_interaction(**kwargs) → interaction_id
    Consumer->>DB: _flush_pending_tool_calls(pending, interaction_id)
    Consumer->>Redis: XACK stream_id
    Note over Consumer: Telemetry:\n  audittrace.async_persist.completed_total{ok}++\n  audittrace.async_persist.queue_lag_seconds.record(ts_now - enqueued_ts)
```

## Poison message (consumer → DLQ)

A poison message is one that consistently fails to land — JSON parse
failure, RLS reject, invalid schema, or `delivery_count >
max_deliveries`. Rather than bouncing forever, the consumer XADDs the
entry to the DLQ stream and XACKs the original.

```mermaid
sequenceDiagram
    participant Redis as Redis Streams
    participant Consumer as AsyncPersistConsumer
    participant DLQ as Redis Streams\naudittrace:persist:dlq
    participant Op as Operator

    Consumer->>Redis: XREADGROUP > / XCLAIM (delivery_count = N)
    Redis-->>Consumer: (stream_id, fields)
    Consumer->>Consumer: deserialise_record OR delivery_count > max_deliveries
    alt poison detected
        Consumer->>DLQ: XADD orig_id, reason, attempt, record_json, ...
        Consumer->>Redis: XACK stream_id
        Note over Consumer: Telemetry:\n  consumer_errors_total{poison}++\n  completed_total{dlq}++
    else transient
        Note over Consumer: leave un-acked → next XPENDING IDLE check\nre-claims it. delivery_count climbs.
    end
```

## Operator replay (`scripts/audittrace-dlq`)

The DLQ is operator-drained — no auto-retry. The CLI runs from the
operator's machine via `kubectl port-forward` against Redis +
Postgres; same auth path as `make verify-deploy`. No HTTP admin
endpoint added (defers OpenAPI surface to the follow-up PR).

```mermaid
sequenceDiagram
    participant Op as Operator
    participant CLI as scripts/audittrace-dlq
    participant DLQ as Redis Streams\naudittrace:persist:dlq
    participant DB as PostgreSQL

    Op->>CLI: audittrace-dlq inspect [--reason ...]
    CLI->>DLQ: XRANGE - + count=N
    DLQ-->>CLI: [(dlq_id, fields), ...]
    CLI-->>Op: rendered table\n(dlq_id age attempt reason orig_id user_id trace_id)

    Op->>CLI: audittrace-dlq replay <dlq_id>
    CLI->>DLQ: XRANGE dlq_id dlq_id
    DLQ-->>CLI: (dlq_id, fields)
    CLI->>CLI: deserialise_record (shared schema)
    CLI->>DB: _persist_interaction(**kwargs) (same code path as consumer)
    DB-->>CLI: interaction_id
    CLI->>DLQ: XDEL dlq_id
    CLI-->>Op: ✓ replayed dlq_id — interaction_id=...

    Op->>CLI: audittrace-dlq drain <dlq_id> --confirm
    CLI->>DLQ: XDEL dlq_id
    CLI-->>Op: drained
```

## Why Redis Streams (vs `asyncio.create_task`)

Three structural advantages relevant to the multi-pod target:

1. **Multi-pod safety by construction.** Redis consumer-group routing
   delivers each entry to exactly one consumer in the
   `audittrace-persisters` group. Two memory-server pods racing on the
   same `XADD` is impossible. Trigger #1 of the original ADR-046 §8
   defer list.

2. **Cross-pod survival on hard kill.** Un-acked messages stay in
   Redis. `XPENDING` IDLE check on any consumer's next iteration
   re-claims them. `kubectl delete pod` mid-flight loses no
   `interactions` rows. Trigger #3 of the original §8 defer list.

3. **First-class DLQ.** The poison handling above is straightforward to
   build because the DLQ is just another stream. Operator triage is
   `XRANGE` / `XDEL` / `XADD-to-main` — modeled as the
   `scripts/audittrace-dlq` CLI.

See ADR-046 §3, §4, §6, §8 for the full design lock; ADR-046 Live
evidence section for the multi-pod proof captured during the
implementation PR's verification step.
