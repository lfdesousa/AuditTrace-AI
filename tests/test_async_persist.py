"""Tests for ADR-046 — Redis Streams async chat-completion persistence.

Covers the producer + consumer surface using fakeredis (async) + an
in-memory Postgres factory (the same one SessionSummarizer tests use).
No real Redis, no real cluster.

The streaming + producer-fallback paths exercise the same code branch
the live cluster runs; pinning them at unit level is the regression
guard against the chart values flipping ``async_persist_enabled=true``
on a build whose code paths can't tolerate a Redis blip.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import Request

from audittrace.config import Settings
from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.routes._memory_tool_loop import PendingToolCall
from audittrace.routes.chat import _resolve_persist_mode
from audittrace.services.async_persist import (
    AsyncPersistConsumer,
    AsyncPersistProducer,
    deserialise_record,
    reconstruct_pending_tool_calls,
    serialise_record,
)

# ──────────────────────────── Fixtures ───────────────────────────────


def _settings(**overrides) -> Settings:
    base: dict = {
        "async_persist_enabled": True,
        "async_persist_stream": "test:persist:stream",
        "async_persist_dlq": "test:persist:dlq",
        "async_persist_group": "test-persisters",
        "async_persist_block_ms": 10,  # short for tests
        "async_persist_batch_size": 10,
        "async_persist_max_deliveries": 3,
        "async_persist_pending_idle_ms": 100,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
async def fake_redis() -> FakeRedis:
    return FakeRedis(decode_responses=True)


@pytest.fixture
def pg_factory() -> InMemoryPostgresFactory:
    return InMemoryPostgresFactory()


def _kwargs() -> dict:
    return {
        "project": "P",
        "source": "chat",
        "question": "What is KV cache?",
        "answer": "An optimisation that caches attention keys/values.",
        "prompt_tokens": 12,
        "completion_tokens": 24,
        "session_id": "sess-async-1",
        "model": "qwen3.6-35b",
        "user_id": "user-luis",
        "duration_ms": 234,
    }


def _pending_tool_call() -> PendingToolCall:
    return PendingToolCall(
        tool_name="recall_decisions",
        user_id="user-luis",
        agent_type="opencode",
        args=json.dumps({"query": "KV cache"}),
        result_summary="ADR-009",
        error=None,
        started_at=datetime(2026, 5, 4, 12, 0, 0),
        duration_ms=12,
        granted_scope="audittrace:query",
        metadata={},
    )


def _make_request(headers: dict[str, str]) -> Request:
    """Minimal FastAPI Request stand-in for header-parsing tests."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


# ──────────────────────── Header-parsing tests ────────────────────────


class TestResolvePersistMode:
    """ADR-046 §2 — header-parsing precedent."""

    def test_absent_header_returns_sync(self):
        assert _resolve_persist_mode(_make_request({})) == "sync"

    def test_explicit_sync(self):
        assert (
            _resolve_persist_mode(_make_request({"X-Persist-Mode": "sync"})) == "sync"
        )

    def test_explicit_async(self):
        assert (
            _resolve_persist_mode(_make_request({"X-Persist-Mode": "async"})) == "async"
        )

    def test_case_insensitive(self):
        assert (
            _resolve_persist_mode(_make_request({"X-Persist-Mode": "ASYNC"})) == "async"
        )

    def test_unknown_value_falls_back_to_sync(self):
        # Per ADR-046 §1: typo never silently drops persistence.
        assert (
            _resolve_persist_mode(_make_request({"X-Persist-Mode": "fire-and-forget"}))
            == "sync"
        )


# ──────────────────────── Serialisation tests ────────────────────────


class TestSerialisation:
    def test_roundtrip_preserves_kwargs(self):
        kwargs = _kwargs() | {"trace_id": "abc123"}
        entry = serialise_record(kwargs=kwargs, pending_tool_calls=None)
        rec, tcs, ts = deserialise_record(entry)
        assert rec == kwargs
        assert tcs == []
        assert ts > 0

    def test_pending_tool_calls_round_trip(self):
        kwargs = _kwargs()
        tc = _pending_tool_call()
        entry = serialise_record(kwargs=kwargs, pending_tool_calls=[tc])
        rec, tcs, _ = deserialise_record(entry)
        recs = reconstruct_pending_tool_calls(tcs)
        assert len(recs) == 1
        assert recs[0].tool_name == "recall_decisions"
        assert recs[0].started_at == datetime(2026, 5, 4, 12, 0, 0)


# ──────────────────────── Producer tests ────────────────────────


class TestProducer:
    @pytest.mark.asyncio
    async def test_enqueue_success_returns_stream_id(self):
        redis = FakeRedis(decode_responses=True)
        producer = AsyncPersistProducer(settings=_settings(), redis=redis)
        sid = await producer.enqueue(kwargs=_kwargs(), pending_tool_calls=None)
        assert sid is not None
        # Confirm the stream actually got the entry.
        entries = await redis.xrange("test:persist:stream", "-", "+")
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_enqueue_failure_returns_none(self):
        # Arrange: a redis client whose xadd raises.
        broken = MagicMock()
        broken.xadd = AsyncMock(side_effect=ConnectionError("redis is down"))
        producer = AsyncPersistProducer(settings=_settings(), redis=broken)
        sid = await producer.enqueue(kwargs=_kwargs(), pending_tool_calls=None)
        assert sid is None  # caller falls back to sync


# ──────────────────────── Consumer tests ────────────────────────


def _persist_recorder():
    """Build a dual mock: persist + flush captures, configurable error."""
    persist = MagicMock(return_value=42)  # interaction_id
    flush = MagicMock(return_value=None)
    return persist, flush


class TestConsumer:
    @pytest.mark.asyncio
    async def test_single_message_round_trip_xacks(self):
        redis = FakeRedis(decode_responses=True)
        s = _settings()
        # Producer enqueues one message.
        producer = AsyncPersistProducer(settings=s, redis=redis)
        await producer.enqueue(kwargs=_kwargs(), pending_tool_calls=None)

        persist, flush = _persist_recorder()
        consumer = AsyncPersistConsumer(
            settings=s,
            persist_callable=persist,
            flush_tool_calls_callable=flush,
            redis=redis,
            consumer_name="test-consumer",
        )
        await consumer._ensure_group()
        processed = await consumer.run_once()
        assert processed == 1
        persist.assert_called_once()
        # No tool calls → flush not invoked
        flush.assert_not_called()

        # XACK happened — pending list is empty.
        pending = await redis.xpending_range(
            s.async_persist_stream, s.async_persist_group, min="-", max="+", count=10
        )
        assert pending == []

    @pytest.mark.asyncio
    async def test_message_with_tool_calls_flushes(self):
        redis = FakeRedis(decode_responses=True)
        s = _settings()
        producer = AsyncPersistProducer(settings=s, redis=redis)
        await producer.enqueue(
            kwargs=_kwargs(), pending_tool_calls=[_pending_tool_call()]
        )

        persist, flush = _persist_recorder()
        consumer = AsyncPersistConsumer(
            settings=s,
            persist_callable=persist,
            flush_tool_calls_callable=flush,
            redis=redis,
            consumer_name="c-tc",
        )
        await consumer._ensure_group()
        await consumer.run_once()
        flush.assert_called_once()
        flushed_pending, flushed_id = flush.call_args.args
        assert flushed_id == 42
        assert len(flushed_pending) == 1
        assert flushed_pending[0].tool_name == "recall_decisions"

    @pytest.mark.asyncio
    async def test_transient_error_leaves_unacked(self):
        redis = FakeRedis(decode_responses=True)
        s = _settings()
        producer = AsyncPersistProducer(settings=s, redis=redis)
        await producer.enqueue(kwargs=_kwargs(), pending_tool_calls=None)

        # Persist raises — simulates DB blip.
        persist = MagicMock(side_effect=RuntimeError("postgres unreachable"))
        flush = MagicMock()
        consumer = AsyncPersistConsumer(
            settings=s,
            persist_callable=persist,
            flush_tool_calls_callable=flush,
            redis=redis,
            consumer_name="c-transient",
        )
        await consumer._ensure_group()
        await consumer.run_once()
        # Message is in pending — Redis will redeliver.
        pending = await redis.xpending_range(
            s.async_persist_stream, s.async_persist_group, min="-", max="+", count=10
        )
        assert len(pending) == 1
        # DLQ stream got nothing.
        dlq = await redis.xrange(s.async_persist_dlq, "-", "+")
        assert dlq == []

    @pytest.mark.asyncio
    async def test_poison_message_moves_to_dlq(self):
        redis = FakeRedis(decode_responses=True)
        s = _settings()
        # Manually XADD a malformed entry (no record_json field).
        await redis.xadd(s.async_persist_stream, {"garbage": "yes"})

        persist, flush = _persist_recorder()
        consumer = AsyncPersistConsumer(
            settings=s,
            persist_callable=persist,
            flush_tool_calls_callable=flush,
            redis=redis,
            consumer_name="c-poison",
        )
        await consumer._ensure_group()
        await consumer.run_once()

        # DLQ has the entry.
        dlq = await redis.xrange(s.async_persist_dlq, "-", "+")
        assert len(dlq) == 1
        _id, fields = dlq[0]
        assert "parse_error" in fields["reason"]
        assert "orig_id" in fields
        # Original stream is XACKed.
        pending = await redis.xpending_range(
            s.async_persist_stream, s.async_persist_group, min="-", max="+", count=10
        )
        assert pending == []
        # Persist was never called (parse failed before).
        persist.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_deliveries_moves_to_dlq(self):
        """A well-formed message that keeps failing eventually hits the
        delivery cap and lands in the DLQ."""
        redis = FakeRedis(decode_responses=True)
        s = _settings(async_persist_max_deliveries=2)
        producer = AsyncPersistProducer(settings=s, redis=redis)
        await producer.enqueue(kwargs=_kwargs(), pending_tool_calls=None)

        # Persist always fails — message stays un-acked, retries climb.
        persist = MagicMock(side_effect=RuntimeError("permanent failure"))
        flush = MagicMock()
        consumer = AsyncPersistConsumer(
            settings=s,
            persist_callable=persist,
            flush_tool_calls_callable=flush,
            redis=redis,
            consumer_name="c-max",
        )
        await consumer._ensure_group()
        # Run iterations until DLQ gets the message OR safety cap.
        for _ in range(10):
            await consumer.run_once()
            dlq = await redis.xrange(s.async_persist_dlq, "-", "+")
            if dlq:
                break
            # Force re-delivery by waiting past the idle window
            # (fakeredis honours wall-clock time).
            await asyncio.sleep(s.async_persist_pending_idle_ms / 1000 + 0.05)
        dlq = await redis.xrange(s.async_persist_dlq, "-", "+")
        assert len(dlq) == 1
        _, fields = dlq[0]
        assert "max_deliveries" in fields["reason"]

    @pytest.mark.asyncio
    async def test_consumer_group_idempotent_create(self):
        """`_ensure_group` is safe to call repeatedly; second call is a
        no-op (BUSYGROUP swallowed)."""
        redis = FakeRedis(decode_responses=True)
        s = _settings()
        persist, flush = _persist_recorder()
        consumer = AsyncPersistConsumer(
            settings=s,
            persist_callable=persist,
            flush_tool_calls_callable=flush,
            redis=redis,
            consumer_name="c-idem",
        )
        await consumer._ensure_group()
        await consumer._ensure_group()  # must not raise

    @pytest.mark.asyncio
    async def test_run_cancellation_propagates(self):
        """``run()`` re-raises CancelledError so lifespan can drain.

        Mocks the underlying redis client directly (rather than using
        fakeredis) because fakeredis's ``xreadgroup(block=N)`` ignores
        cancellation — that's a test-harness limitation, not a
        production semantics issue. Real Redis honours cancellation at
        every await boundary.
        """
        redis = MagicMock()
        redis.xgroup_create = AsyncMock(return_value=None)
        redis.xreadgroup = AsyncMock(side_effect=asyncio.CancelledError)
        redis.xpending_range = AsyncMock(return_value=[])

        persist, flush = _persist_recorder()
        consumer = AsyncPersistConsumer(
            settings=_settings(async_persist_block_ms=0),
            persist_callable=persist,
            flush_tool_calls_callable=flush,
            redis=redis,
            consumer_name="c-cancel",
        )
        with pytest.raises(asyncio.CancelledError):
            await consumer.run()

    @pytest.mark.asyncio
    async def test_busygroup_swallowed_when_group_pre_exists(self):
        """A second consumer pointed at a pre-existing group must not
        crash on BUSYGROUP — the consumer-group creation is idempotent
        and that's the multi-pod assumption."""
        redis = FakeRedis(decode_responses=True)
        s = _settings()
        # First consumer creates the group.
        c1 = AsyncPersistConsumer(
            settings=s,
            persist_callable=MagicMock(),
            flush_tool_calls_callable=MagicMock(),
            redis=redis,
            consumer_name="c1",
        )
        await c1._ensure_group()
        # Second consumer instance: group already exists. Reset its
        # ``_group_initialised`` flag so it actually calls xgroup_create
        # and exercises the BUSYGROUP swallow path.
        c2 = AsyncPersistConsumer(
            settings=s,
            persist_callable=MagicMock(),
            flush_tool_calls_callable=MagicMock(),
            redis=redis,
            consumer_name="c2",
        )
        await c2._ensure_group()  # must not raise

    @pytest.mark.asyncio
    async def test_dlq_write_failure_still_xacks(self):
        """If XADD to the DLQ stream fails, the original entry must
        still be XACKed off the main stream — otherwise the message
        bounces back to the consumer indefinitely."""
        redis = FakeRedis(decode_responses=True)
        s = _settings()
        # Inject a poison message manually.
        await redis.xadd(s.async_persist_stream, {"garbage": "yes"})

        # Wrap xadd to fail ONLY on the DLQ stream.
        real_xadd = redis.xadd

        async def selective_xadd(stream, fields, *a, **kw):
            if stream == s.async_persist_dlq:
                raise ConnectionError("DLQ stream unreachable")
            return await real_xadd(stream, fields, *a, **kw)

        redis.xadd = selective_xadd  # type: ignore[method-assign]

        persist, flush = _persist_recorder()
        consumer = AsyncPersistConsumer(
            settings=s,
            persist_callable=persist,
            flush_tool_calls_callable=flush,
            redis=redis,
            consumer_name="c-dlq-fail",
        )
        await consumer._ensure_group()
        await consumer.run_once()
        # Main stream still ACKed even though DLQ write failed.
        pending = await redis.xpending_range(
            s.async_persist_stream, s.async_persist_group, min="-", max="+", count=10
        )
        assert pending == []

    @pytest.mark.asyncio
    async def test_consumer_name_default_uses_hostname(self, monkeypatch):
        monkeypatch.setenv("HOSTNAME", "audittrace-memory-server-abc123")
        c = AsyncPersistConsumer(
            settings=_settings(),
            persist_callable=MagicMock(),
            flush_tool_calls_callable=MagicMock(),
            redis=FakeRedis(decode_responses=True),
        )
        assert c.consumer_name == "consumer-audittrace-memory-server-abc123"

    @pytest.mark.asyncio
    async def test_consumer_name_falls_back_to_socket_when_hostname_unset(
        self, monkeypatch
    ):
        monkeypatch.delenv("HOSTNAME", raising=False)
        c = AsyncPersistConsumer(
            settings=_settings(),
            persist_callable=MagicMock(),
            flush_tool_calls_callable=MagicMock(),
            redis=FakeRedis(decode_responses=True),
        )
        # Just assert the prefix; the actual hostname depends on the
        # test runner.
        assert c.consumer_name.startswith("consumer-")
        assert c.consumer_name != "consumer-"


class TestModuleSingletons:
    """Cover the lazy-init paths used by chat.py + lifespan."""

    def test_get_redis_lazy_init(self, monkeypatch):
        from audittrace.services import async_persist as ap

        ap.reset_for_tests()
        # Avoid hitting real Redis: monkeypatch from_url.
        called = {}

        def fake_from_url(url, password=None, decode_responses=None):
            called["url"] = url
            called["pw"] = password
            return MagicMock(name="async-redis-stub")

        monkeypatch.setattr(ap.AsyncRedis, "from_url", staticmethod(fake_from_url))
        r1 = ap.get_async_persist_redis()
        r2 = ap.get_async_persist_redis()
        assert r1 is r2  # singleton
        assert called["url"]  # init happened
        ap.reset_for_tests()

    def test_get_producer_lazy_init(self, monkeypatch):
        from audittrace.services import async_persist as ap

        ap.reset_for_tests()
        monkeypatch.setattr(
            ap.AsyncRedis,
            "from_url",
            staticmethod(lambda *a, **kw: MagicMock(name="r")),
        )
        p1 = ap.get_async_persist_producer()
        p2 = ap.get_async_persist_producer()
        assert p1 is p2
        ap.reset_for_tests()

    def test_install_test_producer(self):
        from audittrace.services import async_persist as ap

        sentinel = MagicMock(name="injected")
        ap.install_test_producer(sentinel)
        assert ap.get_async_persist_producer() is sentinel
        ap.reset_for_tests()


class TestConsumerExtras:
    @pytest.mark.asyncio
    async def test_two_consumers_split_messages(self):
        """Multi-pod safety: two consumers in the same group should each
        see only a subset of the entries (XREADGROUP routes each entry
        to exactly one consumer)."""
        redis = FakeRedis(decode_responses=True)
        s = _settings()
        producer = AsyncPersistProducer(settings=s, redis=redis)
        for i in range(6):
            await producer.enqueue(
                kwargs=_kwargs() | {"answer": f"a{i}"}, pending_tool_calls=None
            )

        persist_a, flush_a = _persist_recorder()
        persist_b, flush_b = _persist_recorder()
        consumer_a = AsyncPersistConsumer(
            settings=s,
            persist_callable=persist_a,
            flush_tool_calls_callable=flush_a,
            redis=redis,
            consumer_name="c-A",
        )
        consumer_b = AsyncPersistConsumer(
            settings=s,
            persist_callable=persist_b,
            flush_tool_calls_callable=flush_b,
            redis=redis,
            consumer_name="c-B",
        )
        await consumer_a._ensure_group()
        # Round-robin: each run_once pulls a batch_size slice; with 6
        # entries and large batch, the first consumer claims them all.
        # Force routing by calling A then B in alternation while
        # entries remain.
        s_small = _settings(async_persist_batch_size=2)
        consumer_a._settings = s_small
        consumer_b._settings = s_small
        await consumer_a.run_once()
        await consumer_b.run_once()
        await consumer_a.run_once()
        # Both consumers should have processed at least one message
        # each.
        assert persist_a.call_count >= 1
        assert persist_b.call_count >= 1
        # Total processed == 6 (assuming all entries pulled by now).
        assert persist_a.call_count + persist_b.call_count == 6
