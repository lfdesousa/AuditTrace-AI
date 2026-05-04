# ADR-046 — Opt-in async chat-completion persistence

**Status:** Proposed
**Date:** 2026-05-04
**Related:** ADR-024 (OpenAI compatibility regression precedent),
ADR-029 (`/interactions` route + audit-record schema), ADR-033 (three-
audience error envelope), ADR-034 (long-running generation patterns)

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

### §3 — Implementation: `asyncio.create_task` (no Redis Streams)

The codebase's established pattern for background work is
`asyncio.create_task`. `SessionSummarizer` uses it
(`server.py` lifespan ↔ `services/session_summarizer.py`) with
defensive cancel-on-shutdown semantics (5s timeout, try/except
TimeoutError + CancelledError). Async chat persistence will mirror
this pattern.

Why not Redis Streams (the EOD memo's first phrasing): adding a queue
introduces a moving part — broker, consumer worker, dead-letter handling
— for marginal value at single-pod scale. The codebase already has a
proven asyncio-task lifecycle. Re-evaluate Streams when we scale beyond
one memory-server replica or add cross-pod fan-out (see §8).

### §4 — Sync fallback on header parse error or feature-flag off

`_resolve_persist_mode` returns `sync` for unknown values. A
`AUDITTRACE_ASYNC_PERSIST_ENABLED` feature flag (default `false`)
gates the entire branch — when off, even an explicit
`X-Persist-Mode: async` runs sync. Operators can disable async
persistence cluster-wide without a rebuild.

The flag default is `false` until the implementation PR lands its
verification gates per §6.

### §5 — Failure handling: log + counter, no retry

Background-task failures (DB transient error, RLS misconfiguration,
Postgres restart mid-write) are caught at the task boundary:

```python
async def _persist_async(record: InteractionRecord) -> None:
    try:
        await _persist_interaction(record)
    except Exception:
        logger.exception("async persist failed", extra={"trace_id": record.trace_id})
        ASYNC_PERSIST_FAILED_TOTAL.inc()
```

No retry. The same write would re-fail the same way; queueing it
would just delay the failure and risk unbounded memory growth on the
in-flight set. A real DLQ is a future ADR (`§Follow-ups`).

The `audittrace_async_persist_failed_total` Prometheus counter is
the operator's signal — a non-zero rate is a paging condition.

### §6 — Shutdown semantics

The lifespan context tracks in-flight async-persist tasks in a set
keyed by trace_id. On `app.shutdown`:

```python
remaining = list(state.async_persist_tasks)
if remaining:
    done, pending = await asyncio.wait(remaining, timeout=5.0)
    for t in pending:
        t.cancel()
        logger.warning("async persist abandoned at shutdown", trace_id=...)
```

Bounded 5 s mirrors the summariser's shutdown handling. Tasks
abandoned past the timeout emit a structured warning so the operator
can correlate against `interactions` gaps post-restart.

Hard SIGKILL (OOM, node loss) loses any in-flight tasks. This is the
acceptance trade-off for async persistence; clients that cannot
tolerate this risk keep the default `sync` mode.

### §7 — Telemetry

- **Span attribute**: `audittrace.persist.mode = "sync"|"async"` on
  the FastAPI root span. Lets us slice latency dashboards by mode.
- **Latency histogram**: existing chat-completion latency histogram
  gets a `persist_mode` label. The hypothesis "async cuts p95 by N
  ms" becomes a Grafana query.
- **Counter**: `audittrace_async_persist_total` (label
  `outcome=ok|failed`) for fan-out tracking and the SLO above.

### §8 — Why not Redis Streams (yet)

Defer until at least one of these holds:
1. Memory-server scales beyond one replica (writes from multiple
   pods need ordering / dedup beyond what asyncio.create_task can
   give).
2. We need cross-process replay (e.g. for cold-start backfill or
   debug rerun).
3. Persistence has to outlive the writing pod's lifetime by more than
   the shutdown grace window.

None of these are true today. ADR-046 remains intentionally narrow:
move the existing sync write into an asyncio task, opt-in only.

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
