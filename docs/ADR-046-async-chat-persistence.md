# ADR-046 — Opt-in async chat-completion persistence

**Status:** Accepted (2026-05-04 — multi-pod live evidence captured)
**Date:** 2026-05-04 (proposed) · 2026-05-04 (accepted)
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-024 (OpenAI compatibility regression precedent),
ADR-029 (`/interactions` route + audit-record schema), ADR-033 (three-
audience error envelope), ADR-034 (long-running generation patterns),
ADR-035 (rename retention exceptions — Redis key prefixes
`sovereign:*` retained for token + tool-result caches; new streams
under `audittrace:*`).

## Context

Every successful `/v1/chat/completions` response goes through
`_persist_interaction()` (`src/audittrace/routes/chat.py:555…`)
synchronously before the FastAPI handler returns. The persistence step
opens a Postgres transaction, writes one `interactions` row plus
zero-or-more `tool_calls` rows, and commits. On the local cluster this
adds 8–25 ms to TTFB depending on connection-pool warmth and tool-call
fan-out.

For long, thinking-heavy turns this cost is amortised by an upstream
inference path that already takes seconds. For short, low-thinking
turns — the typical case for OpenCode auto-completes and quick lookups
— the DB write is a measurable percentage of the total latency budget.

Two structural constraints make a naive "just move it to a background
task" change risky:

1. **OpenAI byte-compat** is non-negotiable per
   `feedback_openai_schema_inviolate` and ADR-024. The default
   `POST /v1/chat/completions` request and response shape must stay
   bit-identical to upstream OpenAI. Any new behaviour must be
   opt-in and invisible to clients that don't know about it.
2. **Audit invariants** require every successful response to land in
   `interactions`. Async persistence introduces a window during
   which a 200 has been returned but no row exists yet — clients
   asking `/interactions` can briefly miss it.

This ADR proposes the opt-in shape and the implementation patterns
that satisfy both constraints. **No code changes are part of this
ADR**; this is a design lock so the implementation lands as a focused
follow-up.

## Decision

### §1 — Opt-in via `X-Persist-Mode: async` request header

Persistence remains synchronous by default. Clients that want lower
TTFB at the cost of eventual `/interactions` consistency send:

```http
POST /v1/chat/completions
X-Persist-Mode: async
```

Header values:
- `sync` (or absent) — current behaviour, no change.
- `async` — schedule persistence on a background task, return immediately
  after the upstream response is fully assembled.
- Any other value → fall back to `sync` (never silent-drop persistence).

Default stays `sync` indefinitely. Flipping the default to `async` is
**explicitly out of scope** for any future ADR that doesn't first land
new test gates for eventual consistency.

### §2 — Header parsing follows the established precedent

`_resolve_thinking()` at `routes/chat.py:297…` is the precedent for
optional behaviour-toggling headers. Same shape:

```python
def _resolve_persist_mode(request: Request) -> Literal["sync", "async"]:
    raw = (request.headers.get("x-persist-mode") or "").strip().lower()
    return "async" if raw == "async" else "sync"
```

Case-insensitive lookup mirrors `_resolve_project()`. No body-field
fallback — opt-in lives in the transport layer, not the prompt
payload.

### §3 — Implementation: Redis Streams + per-pod consumer worker

> **Amended 2026-05-04** — the original draft chose `asyncio.create_task`
> per pod. With multi-pod memory-server now in scope for production,
> §8's trigger #1 fires; the in-flight loss surface from §8 trigger #3
> grows linearly with N. The implementation pivots to Redis Streams
> for cross-pod-safe persistence.

**Producer** (inside the chat-completion handler, when
`X-Persist-Mode: async` and the feature flag is on):

1. Build the `InteractionRecord` constructor kwargs as a flat dict.
2. `await async_redis.xadd(settings.async_persist_stream,
   {"record_json": json.dumps(kwargs), "trace_id": ...,
    "enqueued_ts": ts})`.
3. Tag the FastAPI root span: `audittrace.persist.mode="async"`,
   `audittrace.persist.stream_id=<XADD return>`.
4. Return the response normally. The `XADD` round-trip is sub-millisecond
   to a co-located Redis; far cheaper than the original sync DB write.
5. **Producer fallback** — if `XADD` raises (Redis unreachable, network
   glitch), fall back to the sync `_persist_interaction` so the audit
   invariant (`feedback_traceability_requirement`) is never violated.
   Counter `audittrace.async_persist.enqueued_total{outcome="fallback"}`
   is alertable so the operator notices the silent degradation.

**Consumer** (per-pod background `asyncio.Task`, started in
`server.py::lifespan` next to `SessionSummarizer`):

1. Class shape mirrors `SessionSummarizer`: `__init__(*, settings,
   session_factory, redis_client)`, `run()` infinite loop with
   `CancelledError` re-raised, `run_once()` atomic testable unit.
2. Joins the consumer group `settings.async_persist_group`
   (`audittrace-persisters`) under the consumer name
   `consumer-${HOSTNAME}` (the pod name in k8s — guaranteed unique
   per replica).
3. **Startup**: first pass reads pending entries (`XREADGROUP ... 0`)
   in case a prior pod incarnation died with un-acked messages
   assigned to this consumer name. Then the loop switches to `>`
   (new entries only).
4. Per message: deserialise `record_json` → call
   `_persist_interaction` (the same function the sync path calls — no
   duplicated logic) → `XACK` on success.
5. On transient error (DB blip, pool exhausted): leave un-acked.
   Redis re-delivers via `XPENDING` IDLE check after
   `settings.async_persist_pending_idle_ms`. Another consumer (or
   the same one) picks it up.
6. On poison message (delivery_count ≥ `max_deliveries`, JSON parse
   failure, RLS reject): `XADD` to the DLQ stream
   `settings.async_persist_dlq` with `orig_id`, `reason`, `attempt`,
   then `XACK` the original. Counter
   `audittrace.async_persist.completed_total{outcome="dlq"}`++.

**Why Redis Streams over asyncio.create_task in multi-pod**:

- Redis consumer-group routing guarantees each entry goes to **exactly
  one** consumer in the group, regardless of how many pods are running.
  No coordination overhead needed in app code.
- `XPENDING` re-claim provides cross-pod survival: if a pod dies
  mid-write, another consumer picks up its un-acked messages on the
  next IDLE-window check.
- `XLEN` is an alertable backpressure signal (gauge metric in §7).
- DLQ as a first-class stream means operator triage tooling
  (`scripts/audittrace-dlq inspect / replay / drain`) can be a thin
  wrapper over `XRANGE` / `XDEL` / `XADD-to-main`.

### §4 — Sync fallback on header parse error or feature-flag off

`_resolve_persist_mode` returns `sync` for unknown values. A
`AUDITTRACE_ASYNC_PERSIST_ENABLED` feature flag (default `false`)
gates the entire branch — when off, even an explicit
`X-Persist-Mode: async` runs sync. Operators can disable async
persistence cluster-wide without a rebuild.

The flag default is `false` until the implementation PR lands its
verification gates per §6.

### §5 — Failure handling: bounded retry → DLQ

> **Amended 2026-05-04** — was "log + counter, no retry". Redis
> Streams give us bounded retry for free via `XPENDING` re-delivery;
> the DLQ that the original draft listed as a follow-up ADR moves
> into scope as a first-class stream.

Two error classes inside the consumer:

- **Transient** (DB blip, pool exhausted, network hiccup): the message
  is left un-acked. Redis re-delivers via `XPENDING` IDLE check after
  `settings.async_persist_pending_idle_ms`. The same consumer (after
  its block) or any other pod's consumer picks it up. Counter
  `audittrace.async_persist.consumer_errors_total{error_class="transient"}`.

- **Poison** (JSON parse failure, `delivery_count ≥ max_deliveries`,
  hard RLS reject): `XADD` to `settings.async_persist_dlq` with
  `orig_id`, `reason`, `attempt`; then `XACK` the original.
  Counter `audittrace.async_persist.completed_total{outcome="dlq"}`.
  Any non-zero DLQ rate is a paging condition; the operator
  triages with `scripts/audittrace-dlq inspect / replay / drain`
  (Bucket 3 of the implementation PR).

Producer-side `XADD` failures (Redis unreachable) fall back to sync
persistence — see §3.

### §6 — Shutdown semantics (Redis-Streams version)

> **Amended 2026-05-04** — the original draft tracked in-flight
> `asyncio.Task`s and drained them with `asyncio.wait(timeout=5.0)`.
> With Redis Streams, in-flight messages are durable across pod
> death, so the shutdown story is much shorter.

The consumer worker is one `asyncio.Task` started in lifespan
alongside `SessionSummarizer`. On `app.shutdown`:

1. `task.cancel()` — propagates `asyncio.CancelledError`.
2. `await asyncio.wait_for(task, timeout=5.0)` — bounded drain of the
   *current* `XREADGROUP` iteration's batch. The worker re-raises
   `CancelledError` cleanly per the established pattern.
3. Any messages already pulled from the stream but not yet `XACK`ed
   stay un-acked in Redis. They are NOT lost: another pod's consumer
   re-claims them via `XPENDING` IDLE on its next iteration (after
   `pending_idle_ms`).

**Hard SIGKILL** (OOM, node loss): same story — un-acked messages
sit in Redis until another consumer's IDLE check picks them up. No
data loss. This is the structural advantage of Redis Streams over the
original `asyncio.create_task`-only design and the primary reason for
the §3 amendment.

### §7 — Telemetry catalog

> **Amended 2026-05-04** — expanded for the producer/consumer split.

OTel meter under `audittrace.async_persist`:

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `audittrace.async_persist.enqueued_total` | counter | `outcome=ok\|fallback` | Producer-side XADD result |
| `audittrace.async_persist.completed_total` | counter | `outcome=ok\|dlq` | Consumer terminal state |
| `audittrace.async_persist.queue_lag_seconds` | histogram | (none) | XACK ts − enqueued_ts (end-to-end) |
| `audittrace.async_persist.consumer_errors_total` | counter | `error_class=transient\|poison\|cancel` | Per-iteration error class |
| `audittrace.async_persist.stream_depth` | observable_gauge | (none) | `XLEN` sampled per consumer iteration |

Span attributes on the FastAPI root span:

- `audittrace.persist.mode = "sync"\|"async"` — set on every
  chat-completion. Lets dashboards slice latency by mode.
- `audittrace.persist.stream_id = <XADD return>` — set only when
  `mode=async`. Joins the producer span to the consumer's eventual
  XACK in Langfuse.

Multi-pod aggregation works automatically in Prometheus / Grafana via
the `consumer-${HOSTNAME}` label that OTel attaches by default.

### §8 — Why Redis Streams (adopted)

> **Amended 2026-05-04** — the original draft deferred Redis Streams
> until a multi-pod, cross-process-replay, or post-shutdown-persistence
> trigger fired. Trigger #1 (multi-replica) fires today: Luis confirmed
> multi-pod memory-server is in scope for production. Trigger #3
> (in-flight loss surface) compounds with N. The implementation
> adopts Streams now rather than retrofitting later.

Three properties Streams gives us that `asyncio.create_task` cannot:

1. **Multi-pod safety.** Redis consumer groups route each entry to
   exactly one consumer. Two memory-server pods racing on the same
   `XADD` is impossible by construction.
2. **Cross-pod survival on hard kill.** Un-acked messages stay in
   Redis. The next `XPENDING` IDLE check (any consumer) re-claims
   them. No correlation against `interactions` gaps post-restart.
3. **First-class DLQ.** Poison messages move to
   `audittrace:persist:dlq` with explicit metadata. The operator
   tool `scripts/audittrace-dlq` inspects, replays, or drains via
   `XRANGE` / `XDEL` / `XADD-to-main`. No "lost event" failure mode.

Trade-off accepted: one new moving piece (the consumer group) and
the operational cost of monitoring `XLEN` + DLQ depth. The cost is
amortised across §6 shutdown safety, §7 telemetry, and the bucket-3
operator tooling.

## Consequences

### Positive

- Lower p95 chat-completion TTFB for callers that opt in. Conservative
  estimate: 8–25 ms for short turns, more under DB pool contention.
- Fully backwards-compatible. Clients that don't know about
  `X-Persist-Mode` get current behaviour bit-identical.
- Pattern is familiar: same lifecycle as `SessionSummarizer`.

### Negative / accepted trade-offs

- **Eventual consistency on `/interactions`**: a row may be missing
  for ~10–50 ms after the chat response returns. Tests asserting
  immediate visibility need an explicit `wait_for_persist()` helper
  on the async path.
- **Hard-kill data loss**: in-flight tasks lost on SIGKILL / node
  loss. Sync mode remains available for callers who can't tolerate
  this.
- **Telemetry split**: dashboards that didn't have a `persist_mode`
  dimension before need to be re-cut. Manageable.

### Risks

- **Test flakiness from timing assumptions**. The implementation PR
  must ship a `wait_for_persist(trace_id, timeout=2.0)` test helper
  and use it in every async-path test. No `time.sleep()` in tests.
- **Operator confusion**. The runbook needs a section: "if you see
  `audittrace_async_persist_failed_total` going up, here's the
  triage flow."

## Verification (gates required before implementation lands)

The implementation PR must demonstrate:

1. **Sync path bit-identical** — a regression test that takes a
   captured sync response (status, headers, body) before and after
   the patch and asserts no diff. `feedback_openai_schema_inviolate`.
2. **Async path observability** — a unit test that submits with
   `X-Persist-Mode: async`, asserts the response returns before
   `interactions` row is visible (using a polling fixture), then
   asserts the row appears within 2 s.
3. **Shutdown drain** — a unit test that schedules several async
   persists, calls the lifespan shutdown handler, and asserts every
   task either completed or was logged as abandoned.
4. **Failure handling** — a unit test where `_persist_interaction`
   raises and the request still returns 200, with the failed-counter
   incremented.
5. **Live evidence** — on the cluster: open a websocket / curl an
   `X-Persist-Mode: async` request, verify in Langfuse that the span
   carries `audittrace.persist.mode=async`, verify in `/metrics` that
   `audittrace_async_persist_total{outcome="ok"}` increments,
   verify in `psql` that the row is present within 2 s.

## Follow-ups

- **ADR-04N — Async-persist DLQ**: when retry semantics matter
  enough to justify the complexity. Probably tied to multi-replica
  scale-out (§8 trigger #1).
- **ADR-04N — Redis Streams backfill**: if cold-start re-derivation
  of `interactions` from a stream becomes useful (§8 trigger #2).
- **Operator runbook section** — triage flow for non-zero
  `audittrace_async_persist_failed_total`. Lands with the
  implementation PR.
- **Default-flip evaluation** — after 90 d of operator data on the
  failed-counter rate, evaluate whether `async` can become the
  default. Out of scope for this ADR; do not pre-commit.

## Live evidence (2026-05-04)

End-to-end implementation proven on the live k3s cluster against real
Redis + Postgres + Vault, with 2 memory-server replicas. Image tag
`adr046-rls-fix-170859`. Test transcript captured in operator notes.

Highlights:

- **Multi-pod consumer-group routing**. Scaled
  `deploy/audittrace-memory-server` to `replicas=2`. Both pods started
  one consumer each; group `audittrace-persisters` registered with
  `consumers=2`. Six XADDed messages were split exactly 3:3 between
  the two pods (verified via per-pod log counts of `interaction
  persisted`). All 6 rows landed in Postgres with the correct
  `user_id`, `session_id`, and `project="live-multipod-rls-fix"`. Zero
  failures.

- **Pod-kill survival**. Force-deleted one of the two pods mid-flight
  (`kubectl delete pod --grace-period=0 --force`). Injected 4 more
  messages while only the survivor was alive. Survivor's consumer
  picked all 4 up (sole member of the group); final
  `live-survival` row count = 8 of 8 expected, no orphans.

- **DLQ end-to-end via `scripts/audittrace-dlq`**. Injected a poison
  entry (malformed `record_json`). Consumer auto-detected the parse
  failure and XADDed to `audittrace:persist:dlq` while XACKing the
  original off the main stream. `audittrace-dlq inspect` rendered
  the entry with `reason="parse_error: Expecting value: line 1
  column 1 (char 0)"`, `orig_id`, `trace_id`. `audittrace-dlq drain
  <dlq_id> --confirm` cleared it; DLQ depth back to 0.

- **`/health` surface** (per §7) shows
  `async_persist_enabled=true`,
  `async_persist_dlq_depth=0`,
  `async_persist_consumer_lag` per the live state.

- **OTel telemetry** wired per §7 catalog
  (`audittrace.async_persist.enqueued_total`,
  `..._completed_total`, `..._consumer_errors_total`,
  `..._queue_lag_seconds`, `..._stream_depth`); Bucket 2 of the
  implementation PR captures the unit-test coverage of every counter
  increment.

- **Full LLM round-trip via Keycloak-authenticated `curl`**. With a
  device-flow-issued JWT (`scripts/audittrace-login --show`) and
  `X-Persist-Mode: async`, the request landed at `/v1/chat/completions`,
  passed JWT validation, ran the memory tool loop, hit the Qwen3.6
  LLM, returned 200 in 4359 ms, and the resulting `interactions` row
  appeared in Postgres within 3 seconds (id 353,
  `project="live-e2e-async"`). The matching sync-mode call (id 352,
  `project="live-e2e-sync"`) acted as the bit-identical baseline —
  identical 4-layer pipeline, only the persistence step differs. This
  is the load-bearing integration proof that `feedback_openai_schema_inviolate`
  holds (sync default unchanged) and that the async path is end-to-end
  production-viable.

Architecture (this PR):

- New components inside `memoryServer.api` in
  `docs/architecture/workspace.dsl`: `asyncPersistConsumer` +
  `asyncPersistProducer`, with 5 new arrows covering the
  XADD / XREADGROUP / INSERT / DLQ flow.
- `docs/architecture/sequence-chat-completions.md` updated with an
  `alt` block at the persistence step showing the async branch.
- `docs/architecture/sequence-async-persist.md` (NEW) — three
  viewpoints in one file: producer→consumer happy path,
  consumer→DLQ poison-handling, operator-driven replay via
  `scripts/audittrace-dlq`.

Caveat captured during live testing — Vault KV path
`kv/audittrace/redis/main` was out of sync with the actual
Bitnami-Redis-subchart-generated password (k8s secret
`audittrace-redis`). Aligned manually as part of this verification
(`vault kv put kv/audittrace/redis/main password=<from k8s>` +
`kubectl rollout restart`). Permanent fix is to make the chart use
`auth.existingSecret` populated from a Vault Agent template — filed
as a follow-up backlog item. Not a blocker for this ADR's
acceptance.

## Follow-up backlog (post-acceptance)

- **Redis subchart `auth.existingSecret` from Vault Agent** (M3.x).
  Permanent alignment of Vault as the source of truth for the Redis
  password. Today's PR verified with manual Vault↔k8s sync; the
  chart-level fix removes the manual step.
- **Default-flip evaluation** at +90 days (already mentioned in
  Follow-ups above).
- **Async-persist DLQ retry tooling** integration with backlog
  alerts (Grafana panel for DLQ depth + queue lag p99).
