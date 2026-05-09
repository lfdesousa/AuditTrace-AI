"""Tests for services/scan_verdict_consumer.py — ADR-048 PR-B4
verdict-side memory_items.scan_status updater."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.routes.memory_upload import manifest as manifest_mod
from audittrace.services.scan_verdict_consumer import (
    ScanVerdictConsumer,
    _episodic_uri,
)


def _settings(url: str = "amqp://x:y@audittrace-rabbitmq:5672/") -> MagicMock:
    s = MagicMock()
    s.scan_amqp_url = url
    return s


def _seed_pending(factory, scan_id: str, *, user_id: str = "alice") -> None:
    with factory() as session:
        manifest_mod.insert_pending_scan(
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


class TestApplyVerdict:
    def test_clean_promotes_key_and_sets_scanned_clean(self) -> None:
        factory = InMemoryPostgresFactory().get_session_factory()
        _seed_pending(factory, "scan-1")
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=factory,
            episodic_bucket="memory-shared",
            episodic_prefix="episodic/papers/",
        )
        consumer._apply_verdict(
            {
                "scan_id": "scan-1",
                "kind": "clean",
                "scanner": "clamav@1.3.1",
                "signature_db_hash": "abc",
                "scan_duration_ms": 12,
                "threats": [],
            }
        )
        with factory() as session:
            row = manifest_mod.get_by_scan_id(session, "scan-1")
        assert row is not None
        assert row.scan_status == "scanned_clean"
        # Promoted to episodic/papers/<scan_id>/<filename>.
        assert row.key == "s3://memory-shared/episodic/papers/scan-1/paper.pdf"

    def test_rejected_sets_rejected_malware_and_keeps_quarantine_key(self) -> None:
        # Rejected → memory-server stops caring about the byte
        # location (content-control deleted them); we only flip
        # the manifest's scan_status. The original key stays.
        factory = InMemoryPostgresFactory().get_session_factory()
        _seed_pending(factory, "scan-2")
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=factory,
        )
        consumer._apply_verdict(
            {
                "scan_id": "scan-2",
                "kind": "rejected",
                "scanner": "clamav",
                "threats": [{"name": "EICAR", "family": "test", "confidence": 1.0}],
            }
        )
        with factory() as session:
            row = manifest_mod.get_by_scan_id(session, "scan-2")
        assert row is not None
        assert row.scan_status == "rejected_malware"
        assert row.key.startswith("s3://memory-shared/quarantine/")

    def test_scan_failed_sets_scan_failed_status(self) -> None:
        factory = InMemoryPostgresFactory().get_session_factory()
        _seed_pending(factory, "scan-3")
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=factory,
        )
        consumer._apply_verdict(
            {
                "scan_id": "scan-3",
                "kind": "scan_failed",
                "scanner": "clamav",
            }
        )
        with factory() as session:
            row = manifest_mod.get_by_scan_id(session, "scan-3")
        assert row is not None
        assert row.scan_status == "scan_failed"

    def test_unknown_verdict_kind_raises(self) -> None:
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=InMemoryPostgresFactory().get_session_factory(),
        )
        with pytest.raises(ValueError, match="unknown verdict kind"):
            consumer._apply_verdict({"scan_id": "x", "kind": "WHATEVER"})

    def test_missing_manifest_row_logs_and_returns(self) -> None:
        # Duplicate verdict for an already-reaped row: don't nack
        # because re-delivery would loop forever.
        #
        # We assert via a logger.warning patch rather than caplog
        # because some other test in the suite calls
        # ``setup_logging`` which does ``root.handlers.clear()`` and
        # detaches pytest's LogCaptureHandler. Patching the module
        # logger is hermetic and survives test ordering.
        from unittest.mock import patch

        factory = InMemoryPostgresFactory().get_session_factory()
        consumer = ScanVerdictConsumer(
            settings=_settings(),
            session_factory=factory,
        )
        with patch(
            "audittrace.services.scan_verdict_consumer.logger.warning"
        ) as mock_warn:
            consumer._apply_verdict({"scan_id": "ghost", "kind": "clean"})
        # Exactly one row_missing warning, no exception raised, and
        # the manifest row is never created.
        warn_messages = [c.args[0] for c in mock_warn.call_args_list]
        assert "scan_verdict_consumer.row_missing" in warn_messages
        with factory() as session:
            assert manifest_mod.get_by_scan_id(session, "ghost") is None


class TestEnsureConnected:
    def _patch_aio_pika(self) -> tuple[Any, Any, Any, Any]:
        aio_pika = MagicMock()
        connection = AsyncMock()
        channel = AsyncMock()
        queue = AsyncMock()
        channel.set_qos = AsyncMock()
        channel.get_queue = AsyncMock(return_value=queue)
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
                session_factory=InMemoryPostgresFactory().get_session_factory(),
            )
            await consumer._ensure_connected()
            await consumer._ensure_connected()
        aio_pika.connect_robust.assert_awaited_once()
        ch.set_qos.assert_awaited_once()
        ch.get_queue.assert_awaited_once_with("audittrace.scan.verdicts")

    async def test_missing_url_raises(self) -> None:
        consumer = ScanVerdictConsumer(
            settings=_settings(url=""),
            session_factory=InMemoryPostgresFactory().get_session_factory(),
        )
        with pytest.raises(RuntimeError, match="scan_amqp_url is required"):
            await consumer._ensure_connected()


class TestProcessOne:
    async def test_message_process_owns_ack_nack(self) -> None:
        # Verifies the consumer uses ``message.process`` async-CM so
        # ack/nack semantics are owned by aio_pika, not by us.
        factory = InMemoryPostgresFactory().get_session_factory()
        _seed_pending(factory, "scan-9")
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
        with factory() as session:
            row = manifest_mod.get_by_scan_id(session, "scan-9")
        assert row is not None
        assert row.scan_status == "scanned_clean"


class TestRunLoop:
    async def test_run_iterates_queue_and_cancels_cleanly(self) -> None:
        # Mock aio_pika so the run loop's queue.iterator() gives
        # one message and then closes; the loop processes and exits
        # via CancelledError on shutdown.
        import asyncio as _asyncio

        aio_pika = MagicMock()
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
        channel.get_queue = AsyncMock(return_value=queue)

        factory = InMemoryPostgresFactory().get_session_factory()
        _seed_pending(factory, "scan-run")
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

        with factory() as session:
            row = manifest_mod.get_by_scan_id(session, "scan-run")
        assert row is not None
        assert row.scan_status == "scanned_clean"

    async def test_run_logs_and_continues_on_per_message_exception(
        self,
    ) -> None:
        # An exception inside _process_one is caught + logged; the
        # loop continues. CancelledError stops it cleanly.
        import asyncio as _asyncio

        aio_pika = MagicMock()
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
        channel.get_queue = AsyncMock(return_value=queue)

        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            with patch(
                "audittrace.services.scan_verdict_consumer.logger.error"
            ) as mock_err:
                consumer = ScanVerdictConsumer(
                    settings=_settings(),
                    session_factory=InMemoryPostgresFactory().get_session_factory(),
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
            session_factory=InMemoryPostgresFactory().get_session_factory(),
        )
        await consumer.aclose()  # never connected → no-op

    async def test_aclose_closes_channel_and_connection(self) -> None:
        aio_pika, conn, ch, _q = self._patch()
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanVerdictConsumer(
                settings=_settings(),
                session_factory=InMemoryPostgresFactory().get_session_factory(),
            )
            await consumer._ensure_connected()
            await consumer.aclose()
        ch.close.assert_awaited_once()
        conn.close.assert_awaited_once()

    def _patch(self) -> tuple[Any, Any, Any, Any]:
        aio_pika = MagicMock()
        connection = AsyncMock()
        channel = AsyncMock()
        queue = AsyncMock()
        channel.set_qos = AsyncMock()
        channel.get_queue = AsyncMock(return_value=queue)
        channel.close = AsyncMock()
        connection.channel = AsyncMock(return_value=channel)
        connection.close = AsyncMock()
        aio_pika.connect_robust = AsyncMock(return_value=connection)
        return aio_pika, connection, channel, queue
