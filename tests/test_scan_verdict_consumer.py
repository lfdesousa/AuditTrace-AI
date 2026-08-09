"""Tests for services/scan_verdict_consumer.py — ADR-048 PR-B4
verdict-side memory_items.scan_status updater.

Also covers SPEC #387 Phase 1 (WU-1) — the auto-index outbox enqueue on a
clean verdict (``TestAutoIndexOutbox`` below)."""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.routes.memory_upload import manifest as manifest_mod
from audittrace.services.index_worker import IndexRequestEnvelope
from audittrace.services.scan_verdict_consumer import (
    ScanVerdictConsumer,
    _episodic_bare_key,
    _episodic_uri,
)


async def _make_factory():
    _f = InMemoryPostgresFactory()
    await _f.create_schema()
    return _f.get_session_factory()


def _settings(url: str = "amqp://x:y@audittrace-rabbitmq:5672/") -> MagicMock:
    s = MagicMock()
    s.scan_amqp_url = url
    return s


def _aio_pika_mock() -> MagicMock:
    """Build a MagicMock for the lazy ``import aio_pika`` site.

    Test code sometimes needs to raise a genuine ``aio_pika.exceptions``
    class (e.g. ``ChannelNotFoundEntity`` / ``AMQPError``) as a mock
    side effect, but the mocked ``aio_pika`` package can't satisfy
    ``from aio_pika.exceptions import …`` submodule imports on its
    own. Bind the REAL exceptions module so those imports resolve."""
    import aio_pika as _real_aio_pika  # noqa: PLC0415

    aio_pika = MagicMock()
    aio_pika.exceptions = _real_aio_pika.exceptions
    sys.modules.setdefault("aio_pika.exceptions", _real_aio_pika.exceptions)
    return aio_pika


async def _seed_pending(factory, scan_id: str, *, user_id: str = "alice") -> None:
    async with factory() as session:
        await manifest_mod.insert_pending_scan(
            session,
            scan_id=scan_id,
            user_id=user_id,
            object_uri=f"s3://memory-shared/quarantine/{user_id}/{scan_id}/paper.pdf",
            object_sha256="0" * 64,
            size_bytes=42,
            title="paper.pdf",
            trace_id="trace-abc",
        )


class TestEpisodicUri:
    def test_basic_layout(self) -> None:
        assert (
            _episodic_uri(
                bucket="memory-shared",
                prefix="episodic/papers/",
                scan_id="sid-1",
                quarantine_key="s3://memory-shared/quarantine/alice/sid-1/foo.pdf",
            )
            == "s3://memory-shared/episodic/papers/sid-1/foo.pdf"
        )

    def test_falls_back_to_object_bin_for_empty_filename(self) -> None:
        # Trailing slash → no filename component → fallback.
        assert (
            _episodic_uri(
                bucket="b",
                prefix="p/",
                scan_id="s",
                quarantine_key="s3://b/quarantine/u/s/",
            )
            == "s3://b/p/s/object.bin"
        )


class TestEpisodicBareKey:
    """SPEC #387 (WU-1) — the bucket-relative form ``_episodic_uri`` is
    now built FROM, and that the auto-index enqueue uses directly."""

    def test_basic_layout(self) -> None:
        assert (
            _episodic_bare_key(
                prefix="episodic/papers/",
                scan_id="sid-1",
                quarantine_key="s3://memory-shared/quarantine/alice/sid-1/foo.pdf",
            )
            == "episodic/papers/sid-1/foo.pdf"
        )

    def test_episodic_uri_is_built_from_bare_key(self) -> None:
        # Falsifiability: _episodic_uri must be exactly
        # f"s3://{bucket}/{bare_key}" — not an independently maintained
        # string, so the two can never drift apart.
        bare = _episodic_bare_key(
            prefix="p/", scan_id="s", quarantine_key="s3://b/quarantine/u/s/f.pdf"
        )
        uri = _episodic_uri(
            bucket="b",
            prefix="p/",
            scan_id="s",
            quarantine_key="s3://b/quarantine/u/s/f.pdf",
        )
        assert uri == f"s3://b/{bare}"


class TestApplyVerdict:
    async def test_clean_promotes_key_and_sets_scanned_clean(self) -> None:
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        await _seed_pending(factory, "scan-1")
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=factory,
            episodic_bucket="memory-shared",
            episodic_prefix="episodic/papers/",
        )
        await consumer._apply_verdict(
            {
                "scan_id": "scan-1",
                "kind": "clean",
                "scanner": "clamav@1.3.1",
                "signature_db_hash": "abc",
                "scan_duration_ms": 12,
                "threats": [],
            }
        )
        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-1")
        assert row is not None
        assert row.scan_status == "scanned_clean"
        # Promoted to episodic/papers/<scan_id>/<filename>.
        assert row.key == "s3://memory-shared/episodic/papers/scan-1/paper.pdf"
        # SPEC #387 (WU-1) — the auto-index outbox marker.
        assert row.indexed_at_ms is None

    async def test_rejected_sets_rejected_malware_and_keeps_quarantine_key(
        self,
    ) -> None:
        # Rejected → memory-server stops caring about the byte
        # location (content-control deleted them); we only flip
        # the manifest's scan_status. The original key stays.
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        await _seed_pending(factory, "scan-2")
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=factory,
        )
        await consumer._apply_verdict(
            {
                "scan_id": "scan-2",
                "kind": "rejected",
                "scanner": "clamav",
                "threats": [{"name": "EICAR", "family": "test", "confidence": 1.0}],
            }
        )
        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-2")
        assert row is not None
        assert row.scan_status == "rejected_malware"
        assert row.key.startswith("s3://memory-shared/quarantine/")

    async def test_scan_failed_sets_scan_failed_status(self) -> None:
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        await _seed_pending(factory, "scan-3")
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=factory,
        )
        await consumer._apply_verdict(
            {
                "scan_id": "scan-3",
                "kind": "scan_failed",
                "scanner": "clamav",
            }
        )
        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-3")
        assert row is not None
        assert row.scan_status == "scan_failed"

    async def test_unknown_verdict_kind_raises(self) -> None:
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=await _make_factory(),
        )
        with pytest.raises(ValueError, match="unknown verdict kind"):
            await consumer._apply_verdict({"scan_id": "x", "kind": "WHATEVER"})

    async def test_missing_manifest_row_logs_and_returns(self) -> None:
        # Duplicate verdict for an already-reaped row: don't nack
        # because re-delivery would loop forever.
        #
        # We assert via a logger.warning patch rather than caplog
        # because some other test in the suite calls
        # ``setup_logging`` which does ``root.handlers.clear()`` and
        # detaches pytest's LogCaptureHandler. Patching the module
        # logger is hermetic and survives test ordering.
        from unittest.mock import patch

        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=factory,
        )
        with patch(
            "audittrace.services.scan_verdict_consumer.logger.warning"
        ) as mock_warn:
            await consumer._apply_verdict({"scan_id": "ghost", "kind": "clean"})
        # Exactly one row_missing warning, no exception raised, and
        # the manifest row is never created.
        warn_messages = [c.args[0] for c in mock_warn.call_args_list]
        assert "scan_verdict_consumer.row_missing" in warn_messages
        async with factory() as session:
            assert await manifest_mod.get_by_scan_id(session, "ghost") is None


class TestAutoIndexOutbox:
    """SPEC #387 Phase 1 (WU-1) — a clean verdict resets the outbox marker
    AND enqueues an ``IndexRequestEnvelope`` with the deterministic
    collection route, closing GAP-1 (nothing previously triggered
    indexing after a clean verdict)."""

    async def test_clean_verdict_enqueues_envelope_with_ai_research_papers(
        self,
    ) -> None:
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        await _seed_pending(factory, "scan-idx-1", user_id="bob")
        index_queue: asyncio.Queue[IndexRequestEnvelope] = asyncio.Queue()
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=factory,
            episodic_bucket="memory-shared",
            episodic_prefix="episodic/papers/",
            index_queue=index_queue,
        )

        await consumer._apply_verdict(
            {"scan_id": "scan-idx-1", "kind": "clean", "scanner": "clamav"}
        )

        assert index_queue.qsize() == 1
        env = index_queue.get_nowait()
        assert env.scan_id == "scan-idx-1"
        assert env.key == "episodic/papers/scan-idx-1/paper.pdf"
        # RED PROOF: a .pdf key must route to ai_research_papers, not the
        # .md-only default — this is the WU-1 half of GAP-2's closure.
        assert env.collection == "ai_research_papers"
        assert env.user_id == "bob"
        assert env.trace_id == "trace-abc"

    async def test_reset_of_already_set_indexed_at_ms_on_reverdict(self) -> None:
        # Idempotent outbox reset: a duplicate clean verdict for an
        # already-indexed row must re-arm the marker (and re-enqueue),
        # rather than trusting a stale timestamp from before this
        # delivery.
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        await _seed_pending(factory, "scan-idx-2")
        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-idx-2")
            assert row is not None
            row.indexed_at_ms = 42
            await session.commit()

        consumer = ScanVerdictConsumer(settings=_settings(), session_factory=factory)
        await consumer._apply_verdict(
            {"scan_id": "scan-idx-2", "kind": "clean", "scanner": "clamav"}
        )

        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-idx-2")
        assert row is not None
        assert row.indexed_at_ms is None

    async def test_rejected_verdict_does_not_enqueue(self) -> None:
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        await _seed_pending(factory, "scan-idx-3")
        index_queue: asyncio.Queue[IndexRequestEnvelope] = asyncio.Queue()
        consumer = ScanVerdictConsumer(
            settings=_settings(), session_factory=factory, index_queue=index_queue
        )

        await consumer._apply_verdict(
            {"scan_id": "scan-idx-3", "kind": "rejected", "scanner": "clamav"}
        )

        assert index_queue.empty()

    async def test_scan_failed_verdict_does_not_enqueue(self) -> None:
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        await _seed_pending(factory, "scan-idx-4")
        index_queue: asyncio.Queue[IndexRequestEnvelope] = asyncio.Queue()
        consumer = ScanVerdictConsumer(
            settings=_settings(), session_factory=factory, index_queue=index_queue
        )

        await consumer._apply_verdict(
            {"scan_id": "scan-idx-4", "kind": "scan_failed", "scanner": "clamav"}
        )

        assert index_queue.empty()

    async def test_no_index_queue_configured_does_not_raise(self) -> None:
        # index_queue defaults to None — a consumer used standalone
        # (existing tests, or the index pipeline disabled) must not
        # crash on a clean verdict.
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        await _seed_pending(factory, "scan-idx-5")
        consumer = ScanVerdictConsumer(settings=_settings(), session_factory=factory)

        await consumer._apply_verdict(
            {"scan_id": "scan-idx-5", "kind": "clean", "scanner": "clamav"}
        )

        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-idx-5")
        assert row is not None
        assert row.scan_status == "scanned_clean"


class TestEnsureConnected:
    def _patch_aio_pika(self) -> tuple[Any, Any, Any, Any]:
        aio_pika = _aio_pika_mock()
        connection = AsyncMock()
        channel = AsyncMock()
        queue = AsyncMock()
        channel.set_qos = AsyncMock()
        channel.declare_queue = AsyncMock(return_value=queue)
        channel.close = AsyncMock()
        connection.channel = AsyncMock(return_value=channel)
        connection.close = AsyncMock()
        aio_pika.connect_robust = AsyncMock(return_value=connection)
        return aio_pika, connection, channel, queue

    async def test_lazy_connect_runs_once(self) -> None:
        aio_pika, conn, ch, queue = self._patch_aio_pika()
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanVerdictConsumer(
                settings=_settings(),
                session_factory=await _make_factory(),
            )
            await consumer._ensure_connected()
            await consumer._ensure_connected()
        aio_pika.connect_robust.assert_awaited_once()
        ch.set_qos.assert_awaited_once()
        ch.declare_queue.assert_awaited_once_with(
            "audittrace.scan.verdicts",
            durable=True,
            arguments={"x-queue-type": "quorum"},
        )

    async def test_missing_url_raises(self) -> None:
        consumer = ScanVerdictConsumer(
            settings=_settings(url=""),
            session_factory=await _make_factory(),
        )
        with pytest.raises(RuntimeError, match="scan_amqp_url is required"):
            await consumer._ensure_connected()

    async def test_declare_args_pin_canonical_bootstrap_job_topology(self) -> None:
        """#393 — guards against silent arg drift vs the canonical
        declaration in ``job-amqp-topology-bootstrap.yaml``
        (``{"auto_delete":false,"durable":true,"arguments":
        {"x-queue-type":"quorum"}}``). A mismatch here would make
        RabbitMQ raise ``ChannelPreconditionFailed`` in production —
        this test pins the args so drift is caught in CI, not at a
        live broker."""
        aio_pika, _conn, ch, _q = self._patch_aio_pika()
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanVerdictConsumer(
                settings=_settings(),
                session_factory=await _make_factory(),
            )
            await consumer._ensure_connected()
        _name, kwargs = ch.declare_queue.await_args
        assert kwargs["durable"] is True
        assert kwargs["arguments"] == {"x-queue-type": "quorum"}

    async def test_attaches_via_active_declare_when_queue_absent(self) -> None:
        """FALSIFIABILITY (#393): simulate "queue does not pre-exist" by
        making the PASSIVE lookup (``get_queue``) raise
        ``ChannelNotFoundEntity`` — exactly what a genuinely-absent
        queue does. The consumer must still attach because it declares
        the queue itself via the ACTIVE ``declare_queue`` path, which
        never touches ``get_queue`` at all.

        RED PROOF: reverting ``scan_verdict_consumer.py`` to the old
        passive ``self._channel.get_queue(self._queue_name)`` call
        makes this test fail — ``get_queue``'s side effect
        (``ChannelNotFoundEntity``) propagates, the bounded retry
        exhausts, and ``_ensure_connected`` raises ``RuntimeError``
        instead of attaching. This is the exact regression #393 fixes."""
        from aio_pika.exceptions import ChannelNotFoundEntity

        aio_pika, _conn, ch, queue = self._patch_aio_pika()
        ch.get_queue = AsyncMock(side_effect=ChannelNotFoundEntity("not_found"))
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanVerdictConsumer(
                settings=_settings(),
                session_factory=await _make_factory(),
            )
            await consumer._ensure_connected()
        assert consumer._queue is queue
        ch.declare_queue.assert_awaited_once()
        ch.get_queue.assert_not_called()

    async def test_queue_declare_retries_transient_error_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The retry loop is now a guard against transient AMQP errors
        while (re)declaring — not the primary "wait for the queue to
        exist" mechanism (that's the active declare itself)."""
        from aio_pika.exceptions import AMQPError

        # Avoid 1+2+4+8+16 s of real sleep in the unit run.
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        aio_pika, conn, ch, queue = self._patch_aio_pika()
        ch.declare_queue = AsyncMock(side_effect=[AMQPError("transient"), queue])
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanVerdictConsumer(
                settings=_settings(),
                session_factory=await _make_factory(),
            )
            await consumer._ensure_connected()
        # Two channel-opens, two declare_queue calls — first failed,
        # second succeeded.
        assert ch.declare_queue.await_count == 2
        assert conn.channel.await_count == 2

    async def test_queue_declare_exhausts_attempts_and_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bounded budget: the consumer must give up rather than
        spin forever against a persistently-failing declare (e.g. a
        genuine arg mismatch / broker misconfiguration), surfacing a
        RuntimeError naming the queue."""
        from aio_pika.exceptions import AMQPError

        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        aio_pika, conn, ch, queue = self._patch_aio_pika()
        ch.declare_queue = AsyncMock(side_effect=AMQPError("still failing"))
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanVerdictConsumer(
                settings=_settings(),
                session_factory=await _make_factory(),
            )
            with pytest.raises(
                RuntimeError,
                match=r"audittrace\.scan\.verdicts.*declare failed after \d+ attempts",
            ):
                await consumer._ensure_connected()
        assert ch.declare_queue.await_count == consumer._QUEUE_MAX_ATTEMPTS


class TestConnectLevelRetry:
    """#384 WS3 — the INITIAL ``connect_robust`` is now wrapped in a
    capped-backoff retry that catches connection-level errors (a
    cold-reboot RabbitMQ is not AMQP-ready and resets the connection).
    Previously ``connect_robust`` sat OUTSIDE the retry loop, so a
    ``ConnectionResetError`` killed the consumer task permanently."""

    async def test_connect_retries_on_connection_reset_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        aio_pika = _aio_pika_mock()
        connection = AsyncMock()
        aio_pika.connect_robust = AsyncMock(
            side_effect=[
                ConnectionResetError(104, "Connection reset by peer"),
                connection,
            ]
        )
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=await _make_factory(),
        )
        got = await consumer._connect_with_retry(
            aio_pika, aio_pika.exceptions.AMQPError
        )
        assert got is connection
        assert aio_pika.connect_robust.await_count == 2

    async def test_connect_exhaustion_reraises_connection_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On exhaustion the connection-level error is re-raised (NOT
        # wrapped in RuntimeError) so run()'s supervised loop can catch
        # it and keep retrying until RabbitMQ is up.
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        aio_pika = _aio_pika_mock()
        aio_pika.connect_robust = AsyncMock(
            side_effect=ConnectionResetError(104, "reset")
        )
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=await _make_factory(),
        )
        with pytest.raises(ConnectionResetError):
            await consumer._connect_with_retry(aio_pika, aio_pika.exceptions.AMQPError)
        assert aio_pika.connect_robust.await_count == consumer._CONNECT_MAX_ATTEMPTS

    async def test_connect_catches_aio_pika_amqp_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aio_pika.exceptions import AMQPError

        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        aio_pika = _aio_pika_mock()
        connection = AsyncMock()
        aio_pika.connect_robust = AsyncMock(
            side_effect=[AMQPError("broker not ready"), connection]
        )
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=await _make_factory(),
        )
        got = await consumer._connect_with_retry(aio_pika, AMQPError)
        assert got is connection
        assert aio_pika.connect_robust.await_count == 2


class _ParkingIter:
    """queue.iterator() stand-in that yields nothing and parks so a
    supervised run() reaches the message loop and waits for cancel."""

    async def __aenter__(self) -> _ParkingIter:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    def __aiter__(self) -> _ParkingIter:
        return self

    async def __anext__(self) -> Any:
        import asyncio as _asyncio

        await _asyncio.Event().wait()  # park forever


class TestSupervisedRun:
    """#384 WS3 — a consumer whose initial connect fails must RETRY in
    run(), not die permanently (invariant I2). CancelledError must
    propagate, never be swallowed."""

    async def test_run_retries_after_connect_failure_then_attaches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio as _asyncio

        # Fast-sleep the consumer's backoff (still yields to the loop)
        # WITHOUT neutralising the test's own pump. Capture the real
        # sleep before patching.
        real_sleep = _asyncio.sleep

        async def _fast_sleep(_delay: float, *a: Any, **k: Any) -> None:
            await real_sleep(0)

        monkeypatch.setattr("asyncio.sleep", _fast_sleep)
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=await _make_factory(),
        )
        calls = {"n": 0}

        async def fake_ensure() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                # Cold RabbitMQ: first supervised attempt fails.
                raise ConnectionResetError(104, "Connection reset by peer")
            # Second attempt: RabbitMQ is up — attach.
            queue = MagicMock()
            queue.iterator = MagicMock(return_value=_ParkingIter())
            consumer._queue = queue

        monkeypatch.setattr(consumer, "_ensure_connected", fake_ensure)

        task = _asyncio.create_task(consumer.run())
        # Pump the loop until the consumer has retried and attached.
        for _ in range(50):
            await real_sleep(0)
            if consumer._queue is not None:
                break
        # The consumer retried and is now parked in the message loop —
        # NOT dead. Cancelling yields a clean CancelledError.
        assert calls["n"] == 2  # retried once, then attached
        assert consumer._queue is not None
        task.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await task

    async def test_run_propagates_cancelled_during_connect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio as _asyncio

        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=await _make_factory(),
        )
        monkeypatch.setattr(
            consumer,
            "_ensure_connected",
            AsyncMock(side_effect=_asyncio.CancelledError()),
        )
        with pytest.raises(_asyncio.CancelledError):
            await consumer.run()


class TestProcessOne:
    async def test_message_process_owns_ack_nack(self) -> None:
        # Verifies the consumer uses ``message.process`` async-CM so
        # ack/nack semantics are owned by aio_pika, not by us.
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        await _seed_pending(factory, "scan-9")
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=factory,
        )
        message = MagicMock()
        process_cm = AsyncMock()
        process_cm.__aenter__ = AsyncMock(return_value=None)
        process_cm.__aexit__ = AsyncMock(return_value=False)
        message.process = MagicMock(return_value=process_cm)
        message.body = b'{"scan_id": "scan-9", "kind": "clean"}'

        await consumer._process_one(message)

        message.process.assert_called_once_with(requeue=False)
        process_cm.__aenter__.assert_awaited_once()
        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-9")
        assert row is not None
        assert row.scan_status == "scanned_clean"


class TestRunLoop:
    async def test_run_iterates_queue_and_cancels_cleanly(self) -> None:
        # Mock aio_pika so the run loop's queue.iterator() gives
        # one message and then closes; the loop processes and exits
        # via CancelledError on shutdown.
        import asyncio as _asyncio

        aio_pika = _aio_pika_mock()
        connection = AsyncMock()
        channel = AsyncMock()
        channel.set_qos = AsyncMock()
        channel.close = AsyncMock()
        connection.channel = AsyncMock(return_value=channel)
        connection.close = AsyncMock()
        aio_pika.connect_robust = AsyncMock(return_value=connection)

        # Build a single message with a valid verdict body.
        message = MagicMock()
        process_cm = AsyncMock()
        process_cm.__aenter__ = AsyncMock(return_value=None)
        process_cm.__aexit__ = AsyncMock(return_value=False)
        message.process = MagicMock(return_value=process_cm)
        message.body = b'{"scan_id": "scan-run", "kind": "clean"}'

        # queue.iterator() must be an async-iterable async-CM.
        class _FakeIter:
            def __init__(self) -> None:
                self._yielded = False

            async def __aenter__(self) -> _FakeIter:
                return self

            async def __aexit__(self, *a: Any) -> None:
                return None

            def __aiter__(self) -> _FakeIter:
                return self

            async def __anext__(self) -> Any:
                if self._yielded:
                    # Pause forever so the test can cancel the task.
                    await _asyncio.Event().wait()
                self._yielded = True
                return message

        queue = MagicMock()
        queue.iterator = MagicMock(return_value=_FakeIter())
        channel.declare_queue = AsyncMock(return_value=queue)

        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        await _seed_pending(factory, "scan-run")
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanVerdictConsumer(
                settings=_settings(),
                session_factory=factory,
            )
            task = _asyncio.create_task(consumer.run())
            # Yield to let the loop pull the one message.
            await _asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(_asyncio.CancelledError):
                await task

        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-run")
        assert row is not None
        assert row.scan_status == "scanned_clean"

    async def test_run_logs_and_continues_on_per_message_exception(
        self,
    ) -> None:
        # An exception inside _process_one is caught + logged; the
        # loop continues. CancelledError stops it cleanly.
        import asyncio as _asyncio

        aio_pika = _aio_pika_mock()
        connection = AsyncMock()
        channel = AsyncMock()
        channel.set_qos = AsyncMock()
        channel.close = AsyncMock()
        connection.channel = AsyncMock(return_value=channel)
        connection.close = AsyncMock()
        aio_pika.connect_robust = AsyncMock(return_value=connection)

        # Message body is bad JSON → json.loads in _process_one
        # raises BEFORE entering the message.process CM.
        bad_message = MagicMock()
        process_cm = AsyncMock()
        process_cm.__aenter__ = AsyncMock(return_value=None)
        process_cm.__aexit__ = AsyncMock(return_value=False)
        bad_message.process = MagicMock(return_value=process_cm)
        bad_message.body = b"not-json"

        class _FakeIter:
            def __init__(self) -> None:
                self._yielded = False

            async def __aenter__(self) -> _FakeIter:
                return self

            async def __aexit__(self, *a: Any) -> None:
                return None

            def __aiter__(self) -> _FakeIter:
                return self

            async def __anext__(self) -> Any:
                if self._yielded:
                    await _asyncio.Event().wait()
                self._yielded = True
                return bad_message

        queue = MagicMock()
        queue.iterator = MagicMock(return_value=_FakeIter())
        channel.declare_queue = AsyncMock(return_value=queue)

        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            with patch(
                "audittrace.services.scan_verdict_consumer.logger.error"
            ) as mock_err:
                consumer = ScanVerdictConsumer(
                    settings=_settings(),
                    session_factory=await _make_factory(),
                )
                task = _asyncio.create_task(consumer.run())
                await _asyncio.sleep(0.05)
                task.cancel()
                with pytest.raises(_asyncio.CancelledError):
                    await task
        assert any("process_failed" in c.args[0] for c in mock_err.call_args_list)


class TestAclose:
    async def test_aclose_idempotent_when_not_connected(self) -> None:
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=await _make_factory(),
        )
        await consumer.aclose()  # never connected → no-op

    async def test_aclose_closes_channel_and_connection(self) -> None:
        aio_pika, conn, ch, _q = self._patch()
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanVerdictConsumer(
                settings=_settings(),
                session_factory=await _make_factory(),
            )
            await consumer._ensure_connected()
            await consumer.aclose()
        ch.close.assert_awaited_once()
        conn.close.assert_awaited_once()

    def _patch(self) -> tuple[Any, Any, Any, Any]:
        aio_pika = _aio_pika_mock()
        connection = AsyncMock()
        channel = AsyncMock()
        queue = AsyncMock()
        channel.set_qos = AsyncMock()
        channel.declare_queue = AsyncMock(return_value=queue)
        channel.close = AsyncMock()
        connection.channel = AsyncMock(return_value=channel)
        connection.close = AsyncMock()
        aio_pika.connect_robust = AsyncMock(return_value=connection)
        return aio_pika, connection, channel, queue
