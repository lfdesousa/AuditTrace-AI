# mypy: disable-error-code="no-untyped-call"
# redis.asyncio's xclaim/xack stubs are missing types in redis-py 5.x;
# our usage is correct, mypy just doesn't know the return shape. The
# disable applies file-wide rather than per-line because there are
# several call sites and the project gates ≥90 per-file coverage.
"""ADR-046 — Redis Streams async chat-completion persistence.

Producer (called from ``routes/chat.py`` when ``X-Persist-Mode: async``):
serialises the InteractionRecord constructor kwargs + any pending
ToolCall rows into a single stream entry, ``XADD`` it, and returns
the stream id. On any Redis failure the caller falls back to the sync
``_persist_interaction`` path so the audit invariant
(``feedback_traceability_requirement``) is never violated.

Consumer (per-pod ``asyncio.Task`` started in ``server.py::lifespan``):
``XREADGROUP`` from ``audittrace:persist:stream`` under the consumer
name ``consumer-${HOSTNAME}``, deserialise, run ``_persist_interaction``
+ ``_flush_pending_tool_calls``, and ``XACK`` on success. Transient
errors leave the entry un-acked (Redis re-delivers via ``XPENDING``
IDLE check after ``async_persist_pending_idle_ms``). Poison messages
(parse failure or ``delivery_count > max_deliveries``) move to
``audittrace:persist:dlq`` and are XACKed off the main stream.

Multi-pod safety is by Redis consumer-group routing: every pod runs
one consumer in the same group; each entry is delivered to exactly
one consumer across the cluster.

See ADR-046 for the full design + verification gates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict
from datetime import datetime
from typing import Any

from opentelemetry import metrics
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import ResponseError

from audittrace.config import Settings

logger = logging.getLogger(__name__)


# ──────────────────────────── Telemetry ────────────────────────────

_meter = metrics.get_meter("audittrace.async_persist")
_enqueued_counter = _meter.create_counter(
    name="audittrace.async_persist.enqueued_total",
    description="Producer-side XADD outcomes (ok | fallback).",
)
_completed_counter = _meter.create_counter(
    name="audittrace.async_persist.completed_total",
    description="Consumer terminal-state outcomes (ok | dlq).",
)
_consumer_errors_counter = _meter.create_counter(
    name="audittrace.async_persist.consumer_errors_total",
    description="Consumer iteration error classes (transient | poison | cancel).",
)
_queue_lag_histogram = _meter.create_histogram(
    name="audittrace.async_persist.queue_lag_seconds",
    description="End-to-end queue lag — XACK timestamp minus enqueued timestamp.",
    unit="s",
)


# ────────────────────── Serialisation helpers ──────────────────────


def serialise_record(
    *,
    kwargs: dict[str, Any],
    pending_tool_calls: Iterable[Any] | None,
) -> dict[str, str]:
    """Build the flat string→string map ``XADD`` accepts.

    Schema:
      - ``record_json``     — JSON-encoded ``_persist_interaction`` kwargs
      - ``tool_calls_json`` — JSON-encoded list of PendingToolCall dicts
                              (datetime → ISO string for transport)
      - ``enqueued_ts``     — float seconds since epoch (queue_lag metric)
      - ``trace_id``        — convenience for triage / DLQ inspection
    """
    tc_payload: list[dict[str, Any]] = []
    for tc in pending_tool_calls or []:
        d = asdict(tc) if hasattr(tc, "__dataclass_fields__") else dict(tc)
        for k, v in list(d.items()):
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        tc_payload.append(d)
    return {
        "record_json": json.dumps(kwargs, default=str),
        "tool_calls_json": json.dumps(tc_payload, default=str),
        "enqueued_ts": f"{time.time():.6f}",
        "trace_id": str(kwargs.get("trace_id") or ""),
    }


def deserialise_record(
    fields: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]], float]:
    """Inverse of :func:`serialise_record`.

    Returns ``(kwargs, tool_calls_dicts, enqueued_ts)``. Tool-call
    dicts come back with ``started_at`` still as a string; the consumer
    + DLQ tool reconstruct ``PendingToolCall`` instances before passing
    to ``_flush_pending_tool_calls``.
    """
    record = json.loads(fields["record_json"])
    tcs_raw = fields.get("tool_calls_json") or "[]"
    tcs = json.loads(tcs_raw)
    enqueued_ts = float(fields.get("enqueued_ts") or 0.0)
    return record, tcs, enqueued_ts


def reconstruct_pending_tool_calls(
    tcs: list[dict[str, Any]],
) -> list[Any]:
    """Rehydrate dicts into ``PendingToolCall`` dataclasses.

    Defers the import to avoid a circular dependency between
    ``async_persist`` (a service) and ``routes._memory_tool_loop``
    (where ``PendingToolCall`` lives).
    """
    from audittrace.routes._memory_tool_loop import PendingToolCall

    recs: list[Any] = []
    for raw in tcs:
        d = dict(raw)
        started_at = d.get("started_at")
        if isinstance(started_at, str):
            d["started_at"] = datetime.fromisoformat(started_at)
        recs.append(PendingToolCall(**d))
    return recs


# ────────────────────── Producer (request-side) ───────────────────


class AsyncPersistProducer:
    """Thin async-Redis wrapper for the chat-completion producer path.

    One process-wide instance. Construct lazily via
    :func:`get_async_persist_producer` so test harnesses can swap the
    underlying client via :func:`reset_for_tests` + a test-injected
    Redis instance.
    """

    def __init__(self, *, settings: Settings, redis: AsyncRedis[str]) -> None:
        self._settings = settings
        self._redis = redis

    async def enqueue(
        self,
        *,
        kwargs: dict[str, Any],
        pending_tool_calls: Iterable[Any] | None,
    ) -> str | None:
        """``XADD`` the entry. Returns the stream id on success.

        Returns ``None`` on any Redis failure so the caller can fall
        back to sync persistence. Counter
        ``audittrace.async_persist.enqueued_total{outcome="fallback"}``
        is incremented on the failure path.
        """
        try:
            entry = serialise_record(
                kwargs=kwargs, pending_tool_calls=pending_tool_calls
            )
            stream_id = await self._redis.xadd(
                self._settings.async_persist_stream, entry
            )
            _enqueued_counter.add(1, {"outcome": "ok"})
            return (
                stream_id.decode() if isinstance(stream_id, bytes) else str(stream_id)
            )
        except Exception as exc:
            logger.warning(
                "async-persist enqueue failed (will fall back to sync): %s",
                exc,
            )
            _enqueued_counter.add(1, {"outcome": "fallback"})
            return None


# ──────────────────────── Consumer (worker) ───────────────────────


class AsyncPersistConsumer:
    """Per-pod background worker. Mirrors :class:`SessionSummarizer`.

    Lifecycle:
      - ``run()`` — infinite loop, exception-survival,
        ``CancelledError`` re-raised.
      - ``run_once()`` — atomic testable unit, returns count processed.

    Multi-pod safety: every replica runs one consumer in the same
    consumer group; Redis routes each entry to exactly one consumer.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        persist_callable: Callable[..., int | None],
        flush_tool_calls_callable: Callable[..., None],
        redis: AsyncRedis[str],
        consumer_name: str | None = None,
    ) -> None:
        self._settings = settings
        self._persist = persist_callable
        self._flush = flush_tool_calls_callable
        self._redis = redis
        self._consumer_name = consumer_name or _default_consumer_name()
        self._group_initialised = False

    @property
    def consumer_name(self) -> str:
        return self._consumer_name

    async def _ensure_group(self) -> None:
        """Create the consumer group if missing. Idempotent."""
        if self._group_initialised:
            return
        try:
            await self._redis.xgroup_create(
                name=self._settings.async_persist_stream,
                groupname=self._settings.async_persist_group,
                id="0",
                mkstream=True,
            )
            logger.info(
                "async-persist consumer group created: group=%s stream=%s",
                self._settings.async_persist_group,
                self._settings.async_persist_stream,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):  # pragma: no cover - defensive
                raise
        self._group_initialised = True

    async def run(self) -> None:
        logger.info(
            "async-persist consumer started — group=%s consumer=%s stream=%s",
            self._settings.async_persist_group,
            self._consumer_name,
            self._settings.async_persist_stream,
        )
        try:
            await self._ensure_group()
            while True:
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover - defensive cycle-survival
                    logger.exception("async-persist consumer cycle failed")
                    _consumer_errors_counter.add(1, {"error_class": "transient"})
        except asyncio.CancelledError:
            logger.info("async-persist consumer cancelled — exiting")
            _consumer_errors_counter.add(1, {"error_class": "cancel"})
            raise

    async def run_once(self) -> int:
        """One iteration: reclaim+process idle pending → read new.

        Returns total entries processed. ``_reclaim_and_process_idle_pending``
        uses ``XCLAIM`` (which returns the message data directly *and*
        bumps ``delivery_count``), so retries surface naturally via the
        same ``_handle_one`` path that handles new messages. No separate
        ``XREADGROUP id=0`` pass is needed.
        """
        await self._ensure_group()
        pending_count = await self._reclaim_and_process_idle_pending()
        new_result = await self._redis.xreadgroup(
            groupname=self._settings.async_persist_group,
            consumername=self._consumer_name,
            streams={self._settings.async_persist_stream: ">"},
            count=self._settings.async_persist_batch_size,
            block=self._settings.async_persist_block_ms,
        )
        new_count = await self._process_batch(new_result)
        return pending_count + new_count

    async def _reclaim_and_process_idle_pending(self) -> int:
        """Steal entries pending longer than ``pending_idle_ms`` and
        process them inline.

        ``XCLAIM`` returns the claimed entries as ``[(id, fields), ...]``
        — same shape as the inner list of an ``XREADGROUP`` reply. We
        feed that into the standard ``_process_batch`` so retries flow
        through ``_handle_one`` (which checks ``delivery_count`` and
        moves the message to the DLQ if the cap is exceeded).

        The prior owner of the pending entries is presumed dead — pod
        restart, OOM, network partition. ``XCLAIM`` increments
        ``delivery_count`` on every successful claim; that's the signal
        feeding the DLQ guard.
        """
        try:
            pending = await self._redis.xpending_range(
                self._settings.async_persist_stream,
                self._settings.async_persist_group,
                min="-",
                max="+",
                count=self._settings.async_persist_batch_size,
                idle=self._settings.async_persist_pending_idle_ms,
            )
        except ResponseError:  # pragma: no cover - defensive (group missing)
            return 0
        if not pending:
            return 0
        ids = [_extract_message_id(p) for p in pending]
        ids = [i for i in ids if i]
        if not ids:  # pragma: no cover - defensive
            return 0
        try:
            claimed = await self._redis.xclaim(
                self._settings.async_persist_stream,
                self._settings.async_persist_group,
                self._consumer_name,
                min_idle_time=self._settings.async_persist_pending_idle_ms,
                message_ids=ids,
            )
        except ResponseError as exc:  # pragma: no cover - defensive
            logger.warning("async-persist xclaim failed: %s", exc)
            return 0
        if not claimed:
            return 0
        logger.info("async-persist reclaimed %d idle pending entries", len(claimed))
        # XCLAIM returns the (id, fields) pairs directly. Re-shape to the
        # XREADGROUP reply form so _process_batch handles them.
        synthetic = [(self._settings.async_persist_stream, claimed)]
        return await self._process_batch(synthetic)

    async def _process_batch(self, xread_result: Any) -> int:
        if not xread_result:
            return 0
        processed = 0
        for _stream, entries in xread_result:
            for entry_id, fields in entries:
                await self._handle_one(entry_id, fields)
                processed += 1
        return processed

    async def _handle_one(
        self,
        entry_id: str | bytes,
        fields: dict[str | bytes, str | bytes] | dict[str, str],
    ) -> None:
        """Per-entry pipeline: deserialise → DLQ-guard → persist → XACK."""
        eid = _decode(entry_id)
        decoded_fields = {_decode(k): _decode(v) for k, v in fields.items()}

        # Parse first — surfaces poison-message early.
        try:
            kwargs, tcs, enqueued_ts = deserialise_record(decoded_fields)
        except Exception as exc:
            logger.warning("async-persist parse failure id=%s: %s", eid, exc)
            await self._move_to_dlq(
                eid, decoded_fields, reason=f"parse_error: {exc}", attempt=0
            )
            _consumer_errors_counter.add(1, {"error_class": "poison"})
            _completed_counter.add(1, {"outcome": "dlq"})
            return

        # Delivery-count check. > max_deliveries → DLQ.
        delivery_count = await self._delivery_count(eid)
        if delivery_count > self._settings.async_persist_max_deliveries:
            await self._move_to_dlq(
                eid,
                decoded_fields,
                reason=f"max_deliveries={delivery_count}",
                attempt=delivery_count,
            )
            _consumer_errors_counter.add(1, {"error_class": "poison"})
            _completed_counter.add(1, {"outcome": "dlq"})
            return

        # Persist + flush. Sync work runs in a thread.
        try:
            interaction_id = await asyncio.to_thread(self._persist, **kwargs)
            if tcs and interaction_id is not None:
                pending = reconstruct_pending_tool_calls(tcs)
                await asyncio.to_thread(self._flush, pending, interaction_id)
        except Exception as exc:
            logger.warning(
                "async-persist transient failure id=%s (will retry): %s",
                eid,
                exc,
            )
            _consumer_errors_counter.add(1, {"error_class": "transient"})
            return  # leave un-acked; Redis re-delivers

        # XACK + telemetry.
        await self._redis.xack(
            self._settings.async_persist_stream,
            self._settings.async_persist_group,
            eid,
        )
        if enqueued_ts > 0:
            _queue_lag_histogram.record(time.time() - enqueued_ts)
        _completed_counter.add(1, {"outcome": "ok"})

    async def _delivery_count(self, eid: str) -> int:
        try:
            xpending = await self._redis.xpending_range(
                self._settings.async_persist_stream,
                self._settings.async_persist_group,
                min=eid,
                max=eid,
                count=1,
            )
            if xpending:
                entry = xpending[0]
                return int(entry.get("times_delivered") or 1)
        except Exception:  # pragma: no cover - defensive
            pass
        return 1

    async def _move_to_dlq(
        self,
        orig_id: str,
        fields: dict[str, str],
        *,
        reason: str,
        attempt: int,
    ) -> None:
        """XADD to DLQ stream + XACK off main.

        Best-effort on the DLQ write — if Redis is degraded for the DLQ
        but not the main stream, we still XACK so the message stops
        bouncing. The operator noticing a non-zero
        ``consumer_errors_total{error_class="poison"}`` rate without
        matching DLQ depth is the alert signal.
        """
        dlq_entry = dict(fields)
        dlq_entry["orig_id"] = orig_id
        dlq_entry["reason"] = reason
        dlq_entry["attempt"] = str(attempt)
        try:
            await self._redis.xadd(self._settings.async_persist_dlq, dlq_entry)
        except Exception as exc:
            logger.error(
                "async-persist DLQ write failed id=%s: %s — XACKing anyway",
                orig_id,
                exc,
            )
        await self._redis.xack(
            self._settings.async_persist_stream,
            self._settings.async_persist_group,
            orig_id,
        )
        logger.warning(
            "async-persist moved to DLQ id=%s reason=%s attempt=%d",
            orig_id,
            reason,
            attempt,
        )


# ────────────────────── Module-level singletons ──────────────────


_async_persist_redis: AsyncRedis[str] | None = None
_async_persist_producer: AsyncPersistProducer | None = None


def get_async_persist_redis() -> AsyncRedis[str]:
    """Lazy async Redis client. One per process."""
    global _async_persist_redis
    if _async_persist_redis is None:
        from audittrace.config import get_settings

        s = get_settings()
        _async_persist_redis = AsyncRedis.from_url(
            s.redis_url,
            password=s.redis_password,
            decode_responses=True,
        )
    return _async_persist_redis


def get_async_persist_producer() -> AsyncPersistProducer:
    """Lazy producer instance. One per process."""
    global _async_persist_producer
    if _async_persist_producer is None:
        from audittrace.config import get_settings

        _async_persist_producer = AsyncPersistProducer(
            settings=get_settings(),
            redis=get_async_persist_redis(),
        )
    return _async_persist_producer


def reset_for_tests() -> None:
    """Drop the cached singletons so tests can inject fresh clients."""
    global _async_persist_redis, _async_persist_producer
    _async_persist_redis = None
    _async_persist_producer = None


def install_test_producer(producer: AsyncPersistProducer) -> None:
    """Inject a producer (e.g. backed by fakeredis) for the duration
    of one test. Pair with :func:`reset_for_tests` in teardown."""
    global _async_persist_producer
    _async_persist_producer = producer


# ──────────────────────────── helpers ────────────────────────────


def _default_consumer_name() -> str:
    """Pod-stable consumer name. Uses the k8s-set ``HOSTNAME`` env var
    (= pod name) when present, falls back to the OS hostname."""
    return f"consumer-{os.environ.get('HOSTNAME') or socket.gethostname()}"


def _decode(value: Any) -> str:
    if isinstance(value, bytes):  # pragma: no cover - decode_responses=True today
        return value.decode("utf-8")
    return str(value)


def _extract_message_id(pending_entry: Any) -> str:
    """Robust extraction of the message id from XPENDING output.

    redis-py 5.x returns a list of dicts with ``message_id``; older
    versions returned tuples. Be liberal in what we accept.
    """
    if isinstance(pending_entry, dict):
        mid = pending_entry.get("message_id") or pending_entry.get("id")
        return _decode(mid) if mid else ""
    if (
        isinstance(  # pragma: no cover - redis-py 5+ returns dicts
            pending_entry, (list, tuple)
        )
        and pending_entry
    ):
        return _decode(pending_entry[0])
    return ""  # pragma: no cover - defensive
