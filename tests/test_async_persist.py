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

import argparse
import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import Request

from audittrace.config import Settings
from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.identity import SENTINEL_SUBJECT
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
    """Build a dual mock: persist + flush captures, configurable error.

    AsyncMock because the persist/flush callables are async coroutine
    functions (#263) and the consumer awaits them directly.
    """
    persist = AsyncMock(return_value=42)  # interaction_id
    flush = AsyncMock(return_value=None)
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
        persist = AsyncMock(side_effect=RuntimeError("postgres unreachable"))
        flush = AsyncMock()
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
        persist = AsyncMock(side_effect=RuntimeError("permanent failure"))
        flush = AsyncMock()
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
            persist_callable=AsyncMock(),
            flush_tool_calls_callable=AsyncMock(),
            redis=redis,
            consumer_name="c1",
        )
        await c1._ensure_group()
        # Second consumer instance: group already exists. Reset its
        # ``_group_initialised`` flag so it actually calls xgroup_create
        # and exercises the BUSYGROUP swallow path.
        c2 = AsyncPersistConsumer(
            settings=s,
            persist_callable=AsyncMock(),
            flush_tool_calls_callable=AsyncMock(),
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
            persist_callable=AsyncMock(),
            flush_tool_calls_callable=AsyncMock(),
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
            persist_callable=AsyncMock(),
            flush_tool_calls_callable=AsyncMock(),
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


class TestHealthSurface:
    """ADR-046 §7 — /health exposes async-persist runtime state."""

    @pytest.mark.asyncio
    async def test_disabled_returns_only_flag(self, monkeypatch):
        from audittrace.routes import health as health_mod

        # Default Settings has async_persist_enabled=False.
        fields = await health_mod._async_persist_health_fields()
        assert fields == {"async_persist_enabled": "false"}

    @pytest.mark.asyncio
    async def test_enabled_surfaces_dlq_depth_and_lag(self, monkeypatch):
        from audittrace.routes import health as health_mod

        # Redis fake with one DLQ entry + one pending main entry.
        redis = FakeRedis(decode_responses=True)
        await redis.xadd("audittrace:persist:dlq", {"x": "1"})
        await redis.xadd("audittrace:persist:stream", {"x": "1"})
        await redis.xgroup_create(
            "audittrace:persist:stream", "audittrace-persisters", id="0"
        )
        await redis.xreadgroup(
            "audittrace-persisters", "c1", {"audittrace:persist:stream": ">"}, count=10
        )

        # Force settings to enabled.
        from audittrace.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "async_persist_enabled", True)
        # Inject the fake redis as the lazy singleton.
        from audittrace.services import async_persist as ap

        ap.reset_for_tests()
        monkeypatch.setattr(ap, "get_async_persist_redis", lambda: redis)

        fields = await health_mod._async_persist_health_fields()
        assert fields["async_persist_enabled"] == "true"
        assert fields["async_persist_dlq_depth"] == "1"
        assert fields["async_persist_consumer_lag"] == "1"
        ap.reset_for_tests()


class TestDlqCli:
    """ADR-046 Bucket 3 — operator CLI for the DLQ stream."""

    @staticmethod
    def _load_dlq_module():
        import importlib.util
        from importlib.machinery import SourceFileLoader
        from pathlib import Path

        path = Path(__file__).parent.parent / "scripts" / "audittrace-dlq"
        # The script has no .py suffix, so spec_from_file_location returns
        # None unless we hand it an explicit SourceFileLoader.
        loader = SourceFileLoader("audittrace_dlq", str(path))
        spec = importlib.util.spec_from_loader("audittrace_dlq", loader)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod

    @pytest.mark.asyncio
    async def test_inspect_empty_dlq(self, monkeypatch, capsys):
        redis = FakeRedis(decode_responses=True)
        mod = self._load_dlq_module()
        monkeypatch.setattr(mod, "_client", AsyncMock(return_value=redis))
        monkeypatch.setattr(mod, "_dlq_stream", lambda: "test:dlq")

        rc = await mod.cmd_inspect(
            argparse.Namespace(limit=100, reason=None, cmd="inspect")
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "is empty" in out

    @pytest.mark.asyncio
    async def test_inspect_renders_table_with_one_entry(self, monkeypatch, capsys):
        redis = FakeRedis(decode_responses=True)
        # Manually XADD a DLQ entry.
        await redis.xadd(
            "test:dlq",
            {
                "record_json": json.dumps({"user_id": "user-luis"}),
                "tool_calls_json": "[]",
                "enqueued_ts": "0",
                "trace_id": "abc",
                "orig_id": "1234-0",
                "reason": "max_deliveries=5",
                "attempt": "5",
            },
        )
        mod = self._load_dlq_module()
        monkeypatch.setattr(mod, "_client", AsyncMock(return_value=redis))
        monkeypatch.setattr(mod, "_dlq_stream", lambda: "test:dlq")

        rc = await mod.cmd_inspect(
            argparse.Namespace(limit=100, reason=None, cmd="inspect")
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "max_deliveries=5" in out
        assert "user-luis" in out
        assert "Total: 1 entries" in out

    @pytest.mark.asyncio
    async def test_replay_against_fixed_postgres_succeeds(self, monkeypatch, capsys):
        redis = FakeRedis(decode_responses=True)
        # Store a well-formed entry in DLQ.
        eid = await redis.xadd(
            "test:dlq",
            {
                "record_json": json.dumps(_kwargs()),
                "tool_calls_json": "[]",
                "enqueued_ts": "0",
                "trace_id": "abc",
                "orig_id": "1234-0",
                "reason": "max_deliveries=5",
                "attempt": "5",
            },
        )
        mod = self._load_dlq_module()
        monkeypatch.setattr(mod, "_client", AsyncMock(return_value=redis))
        monkeypatch.setattr(mod, "_dlq_stream", lambda: "test:dlq")
        # Patch _persist_interaction to return a fake interaction id.
        from audittrace.routes import chat as chat_mod

        monkeypatch.setattr(
            chat_mod, "_persist_interaction", MagicMock(return_value=99)
        )

        rc = await mod.cmd_replay(
            argparse.Namespace(dlq_id=eid, all=False, max=100, cmd="replay")
        )
        assert rc == 0
        # DLQ entry XDELed.
        remaining = await redis.xrange("test:dlq", "-", "+")
        assert remaining == []

    @pytest.mark.asyncio
    async def test_replay_still_failing_preserves_entry(self, monkeypatch, capsys):
        redis = FakeRedis(decode_responses=True)
        eid = await redis.xadd(
            "test:dlq",
            {
                "record_json": json.dumps(_kwargs()),
                "tool_calls_json": "[]",
                "enqueued_ts": "0",
                "trace_id": "abc",
                "orig_id": "1234-0",
                "reason": "max_deliveries=5",
                "attempt": "5",
            },
        )
        mod = self._load_dlq_module()
        monkeypatch.setattr(mod, "_client", AsyncMock(return_value=redis))
        monkeypatch.setattr(mod, "_dlq_stream", lambda: "test:dlq")
        # Patch persist to raise.
        from audittrace.routes import chat as chat_mod

        monkeypatch.setattr(
            chat_mod,
            "_persist_interaction",
            MagicMock(side_effect=RuntimeError("still broken")),
        )

        rc = await mod.cmd_replay(
            argparse.Namespace(dlq_id=eid, all=False, max=100, cmd="replay")
        )
        assert rc == 1
        # Entry preserved.
        remaining = await redis.xrange("test:dlq", "-", "+")
        assert len(remaining) == 1

    @pytest.mark.asyncio
    async def test_drain_requires_confirm(self, monkeypatch, capsys):
        redis = FakeRedis(decode_responses=True)
        eid = await redis.xadd("test:dlq", {"record_json": "{}"})
        mod = self._load_dlq_module()
        monkeypatch.setattr(mod, "_client", AsyncMock(return_value=redis))
        monkeypatch.setattr(mod, "_dlq_stream", lambda: "test:dlq")

        # Without --confirm: aborts.
        rc = await mod.cmd_drain(
            argparse.Namespace(
                dlq_id=eid, all=False, older_than=30, confirm=False, cmd="drain"
            )
        )
        assert rc == 2
        # Entry still present.
        remaining = await redis.xrange("test:dlq", "-", "+")
        assert len(remaining) == 1

        # With --confirm: drops.
        rc = await mod.cmd_drain(
            argparse.Namespace(
                dlq_id=eid, all=False, older_than=30, confirm=True, cmd="drain"
            )
        )
        assert rc == 0
        remaining = await redis.xrange("test:dlq", "-", "+")
        assert remaining == []


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


# ──────────────── Branch-level hardening (#364) ────────────────
#
# The paths below are the "second half" of decisions the consumer makes
# on every message. Each one is a place where a wrong answer silently
# costs an audit row (ADR-033: every interaction gets a row) or corrupts
# the per-user RLS context that makes those rows insertable at all.


class TestReconstructAlreadyHydratedToolCalls:
    """``reconstruct_pending_tool_calls`` must be type-tolerant on ``started_at``."""

    def test_datetime_started_at_is_left_intact(self):
        """Tool-call dicts do not always arrive with an ISO *string* timestamp.

        ``deserialise_record`` yields strings, but the DLQ replay tool and
        the XCLAIM reclaim path can hand back dicts built with
        ``dataclasses.asdict`` — where ``started_at`` is still a real
        ``datetime``. Calling ``datetime.fromisoformat`` on that raises
        ``TypeError``, which would abort ``_flush`` and lose every tool-call
        audit row for that interaction. The isinstance guard is what keeps
        the function idempotent; this pins the non-string side of it.
        """
        from dataclasses import asdict

        hydrated = asdict(_pending_tool_call())
        assert isinstance(hydrated["started_at"], datetime)  # pre-condition

        recs = reconstruct_pending_tool_calls([hydrated])

        assert len(recs) == 1
        # The datetime survived untouched — not re-parsed, not stringified.
        assert recs[0].started_at == datetime(2026, 5, 4, 12, 0, 0)
        assert recs[0].tool_name == "recall_decisions"
        # The traceability fields the audit row is keyed on came through too.
        assert recs[0].user_id == "user-luis"
        assert recs[0].granted_scope == "audittrace:query"


class TestReclaimRaceLoses:
    """Multi-pod safety: losing the XCLAIM race must be a no-op, not a re-persist."""

    @pytest.mark.asyncio
    async def test_lost_xclaim_race_does_not_double_persist(self):
        """Two pods can see the same idle pending entry in the same instant.

        Both call ``XPENDING`` and see the entry; only one ``XCLAIM``
        succeeds — the loser gets an empty reply. If the loser fell through
        to ``_process_batch`` anyway it would write a *second*
        InteractionRecord for one chat completion, which is a duplicated
        audit row (worse than a missing one: it inflates the evidence
        trail). This pins the early return.
        """
        redis = FakeRedis(decode_responses=True)
        s = _settings(async_persist_pending_idle_ms=0)
        producer = AsyncPersistProducer(settings=s, redis=redis)
        await producer.enqueue(kwargs=_kwargs(), pending_tool_calls=None)

        # Leave the entry pending: first pass fails to persist, so no XACK.
        failing_persist = AsyncMock(side_effect=RuntimeError("pod died mid-write"))
        consumer = AsyncPersistConsumer(
            settings=s,
            persist_callable=failing_persist,
            flush_tool_calls_callable=AsyncMock(),
            redis=redis,
            consumer_name="c-loser",
        )
        await consumer._ensure_group()
        await consumer.run_once()
        pending = await redis.xpending_range(
            s.async_persist_stream, s.async_persist_group, min="-", max="+", count=10
        )
        assert len(pending) == 1  # pre-condition: entry is idle + pending

        # Now the reclaim attempt loses the race — XCLAIM returns nothing.
        persist, flush = _persist_recorder()
        consumer._persist = persist
        consumer._flush = flush
        redis.xclaim = AsyncMock(return_value=[])

        reclaimed = await consumer._reclaim_and_process_idle_pending()

        assert reclaimed == 0
        # The decisive assertion: no duplicate audit row was written.
        persist.assert_not_called()
        flush.assert_not_called()


class TestRlsContextGuard:
    """The RLS ContextVar must only ever be set from a real user id."""

    @pytest.mark.asyncio
    async def test_blank_user_id_does_not_overwrite_rls_context(self, monkeypatch):
        """A blank ``user_id`` must not clobber the per-user RLS GUC.

        The consumer task is one long-lived asyncio task processing every
        pod's messages back to back. ``set_current_user_id("")`` would leave
        an empty GUC behind, and the *next* message's INSERT would be
        evaluated by Postgres RLS against the wrong (empty) principal —
        rejected with InsufficientPrivilege, silently costing that
        interaction its audit row. The truthiness guard is the protection;
        this pins the falsy side.
        """
        import audittrace.db.rls as rls_module

        seen: list[str] = []
        monkeypatch.setattr(rls_module, "set_current_user_id", seen.append)

        redis = FakeRedis(decode_responses=True)
        s = _settings()
        producer = AsyncPersistProducer(settings=s, redis=redis)
        # Message 1: no user id at all. Message 2: a real one.
        await producer.enqueue(
            kwargs=_kwargs() | {"user_id": ""}, pending_tool_calls=None
        )
        await producer.enqueue(
            kwargs=_kwargs() | {"user_id": "user-frank"}, pending_tool_calls=None
        )

        persist, flush = _persist_recorder()
        consumer = AsyncPersistConsumer(
            settings=s,
            persist_callable=persist,
            flush_tool_calls_callable=flush,
            redis=redis,
            consumer_name="c-rls",
        )
        await consumer._ensure_group()
        assert await consumer.run_once() == 2

        # Only the real principal reached the ContextVar — never "".
        assert seen == ["user-frank"]
        # Both messages still got persisted: the guard skips the GUC, it
        # does not skip the audit row.
        assert persist.call_count == 2


class TestMissingEnqueuedTimestamp:
    """An entry without ``enqueued_ts`` must still complete and XACK."""

    @pytest.mark.asyncio
    async def test_entry_without_enqueued_ts_still_persists_and_acks(self):
        """Stream entries are not guaranteed to carry ``enqueued_ts``.

        A DLQ replay, a hand-injected triage entry, or an entry written by
        an older producer build has no timestamp, so ``deserialise_record``
        returns ``0.0``. Recording a queue-lag sample from that would report
        a ~56-year lag and poison the histogram; more importantly the entry
        must still be persisted and XACKed rather than stalling the stream
        forever. This pins the skip-the-histogram side.
        """
        redis = FakeRedis(decode_responses=True)
        s = _settings()
        # Hand-built entry — deliberately no "enqueued_ts" field.
        await redis.xadd(
            s.async_persist_stream,
            {"record_json": json.dumps(_kwargs()), "tool_calls_json": "[]"},
        )

        persist, flush = _persist_recorder()
        consumer = AsyncPersistConsumer(
            settings=s,
            persist_callable=persist,
            flush_tool_calls_callable=flush,
            redis=redis,
            consumer_name="c-nots",
        )
        await consumer._ensure_group()
        assert await consumer.run_once() == 1

        persist.assert_called_once()
        # XACKed — nothing left pending, so the entry cannot be re-delivered.
        pending = await redis.xpending_range(
            s.async_persist_stream, s.async_persist_group, min="-", max="+", count=10
        )
        assert pending == []
        # And it did not get treated as poison.
        assert await redis.xrange(s.async_persist_dlq, "-", "+") == []


class TestDeliveryCountWithoutPendingEntry:
    """No XPENDING metadata → assume first delivery, never DLQ."""

    @pytest.mark.asyncio
    async def test_absent_pending_entry_is_treated_as_first_delivery(self):
        """``XPENDING`` for a message id can legitimately come back empty.

        It happens when the entry was ACKed by a racing consumer between the
        read and the count, or when the group's PEL was trimmed. If the
        empty reply fell through to a large default the message would be
        classed as exceeding ``max_deliveries`` and dumped in the DLQ —
        discarding a perfectly good interaction instead of persisting it.
        The fallback of 1 is what keeps first-attempt messages on the happy
        path; this pins it.
        """
        redis = MagicMock()
        redis.xgroup_create = AsyncMock(return_value=None)
        redis.xack = AsyncMock(return_value=1)
        redis.xadd = AsyncMock(return_value="9-9")
        # Every XPENDING lookup comes back empty — both the reclaim sweep
        # and the per-message delivery-count probe.
        redis.xpending_range = AsyncMock(return_value=[])
        entry_fields = serialise_record(kwargs=_kwargs(), pending_tool_calls=None)
        redis.xreadgroup = AsyncMock(
            return_value=[("test:persist:stream", [("5-1", entry_fields)])]
        )

        s = _settings(async_persist_max_deliveries=3)
        persist, flush = _persist_recorder()
        consumer = AsyncPersistConsumer(
            settings=s,
            persist_callable=persist,
            flush_tool_calls_callable=flush,
            redis=redis,
            consumer_name="c-nopel",
        )
        await consumer._ensure_group()

        assert await consumer._delivery_count("5-1") == 1

        assert await consumer.run_once() == 1
        # Persisted and ACKed rather than DLQ'd.
        persist.assert_called_once()
        redis.xack.assert_awaited_once()
        # Nothing was written to the DLQ stream.
        dlq_writes = [
            c for c in redis.xadd.await_args_list if c.args[0] == s.async_persist_dlq
        ]
        assert dlq_writes == []


class TestPersistOrEnqueueBranch:
    """ADR-046 — ``_persist_or_enqueue`` is the fork between the Redis
    Streams path and the direct Postgres write. ADR-033's invariant ("every
    interaction gets an audit row") has to hold on BOTH sides of it,
    including when Redis is the thing that broke."""

    @pytest.fixture
    def _async_persist_on(self, monkeypatch):
        from audittrace import config as config_mod

        config_mod.get_settings.cache_clear()
        monkeypatch.setenv("AUDITTRACE_ASYNC_PERSIST_ENABLED", "true")
        yield
        config_mod.get_settings.cache_clear()

    @staticmethod
    def _persist_kwargs() -> dict:
        return {
            "project": "AuditTrace",
            "source": "curl",
            "question": "who changed the RLS policy?",
            "answer": "migration 005",
            "prompt_tokens": 11,
            "completion_tokens": 3,
            "session_id": "curl-2026-07-20-abc",
            "model": "qwen3.5-35b",
            "user_id": SENTINEL_SUBJECT,
        }

    @pytest.mark.asyncio
    async def test_async_mode_enqueues_and_skips_the_inline_db_write(
        self, client, _async_persist_on
    ):
        """The whole point of async mode is to take the Postgres write off
        the chat hot path. If the sync write also ran, the row would be
        written twice (once here, once by the consumer) and the latency win
        would be zero. The handler must return ``None`` so no caller tries
        to hang tool_calls off an id that does not exist yet."""
        from sqlalchemy import select as _select

        from audittrace.db.models import InteractionRecord
        from audittrace.dependencies import get_postgres_factory
        from audittrace.routes.chat import _persist_or_enqueue
        from audittrace.services import async_persist as ap

        redis = FakeRedis(decode_responses=True)
        ap.install_test_producer(
            AsyncPersistProducer(settings=_settings(), redis=redis)
        )
        try:
            result = await _persist_or_enqueue(
                persist_mode="async",
                pending_tool_calls=None,
                **self._persist_kwargs(),
            )
        finally:
            ap.reset_for_tests()

        assert result is None

        entries = await redis.xrange("test:persist:stream", "-", "+")
        assert len(entries) == 1
        enqueued_kwargs, enqueued_tcs, _ = deserialise_record(entries[0][1])
        # The enqueued payload must carry the full traceability triple —
        # the consumer writes the row minutes later with no request context
        # of its own (EU AI Act Art. 12).
        assert enqueued_kwargs["user_id"] == SENTINEL_SUBJECT
        assert enqueued_kwargs["session_id"] == "curl-2026-07-20-abc"
        assert enqueued_kwargs["question"] == "who changed the RLS policy?"
        assert enqueued_tcs == []

        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            rows = (await db.execute(_select(InteractionRecord))).scalars().all()
        assert rows == [], "async mode must not also write inline"

    @pytest.mark.asyncio
    async def test_producer_failure_falls_back_to_a_synchronous_audit_row(
        self, client, _async_persist_on
    ):
        """A Redis blip must degrade latency, never auditability. If the
        XADD fails and we simply returned, the interaction would exist in
        the user's terminal and nowhere else — the exact silent-loss shape
        migration 007 was written to close."""
        from sqlalchemy import select as _select

        from audittrace.db.models import InteractionRecord, ToolCall
        from audittrace.dependencies import get_postgres_factory
        from audittrace.routes.chat import _persist_or_enqueue
        from audittrace.services import async_persist as ap

        broken = MagicMock()
        broken.xadd = AsyncMock(side_effect=ConnectionError("redis is down"))
        ap.install_test_producer(
            AsyncPersistProducer(settings=_settings(), redis=broken)
        )
        pending = [
            PendingToolCall(
                tool_name="recall_decisions",
                user_id=SENTINEL_SUBJECT,
                agent_type="chat",
                args='{"query": "rls"}',
                result_summary="1 hit",
                error=None,
                started_at=datetime.now(),
                duration_ms=4,
                granted_scope="memory:episodic:read",
            )
        ]
        try:
            interaction_id = await _persist_or_enqueue(
                persist_mode="async",
                pending_tool_calls=pending,
                **self._persist_kwargs(),
            )
        finally:
            ap.reset_for_tests()

        assert interaction_id is not None

        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            rows = (await db.execute(_select(InteractionRecord))).scalars().all()
            tool_rows = (await db.execute(_select(ToolCall))).scalars().all()

        assert len(rows) == 1
        assert rows[0].id == interaction_id
        assert rows[0].user_id == SENTINEL_SUBJECT
        assert rows[0].question == "who changed the RLS policy?"
        # The tool-call children land too, linked to the parent that just
        # landed — the fallback is a full sync persist, not a partial one.
        assert len(tool_rows) == 1
        assert tool_rows[0].interaction_id == interaction_id
        assert tool_rows[0].tool_name == "recall_decisions"
