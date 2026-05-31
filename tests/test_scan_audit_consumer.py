"""Tests for services/scan_audit_consumer.py — ADR-048 PR-B4
security-audit row writer."""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from audittrace.db.models import InteractionRecord
from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.services.scan_audit_consumer import ScanAuditConsumer


async def _make_factory():
    _f = InMemoryPostgresFactory()
    await _f.create_schema()
    return _f.get_session_factory()


def _settings(url: str = "amqp://x:y@audittrace-rabbitmq:5672/") -> MagicMock:
    s = MagicMock()
    s.scan_amqp_url = url
    return s


def _audit_payload(
    *, verdict: str = "clean", scan_id: str = "scan-1"
) -> dict[str, Any]:
    return {
        "scan_id": scan_id,
        "user_id": "alice",
        "trace_id": "trace-abc",
        "event_class": "security",
        "verdict": verdict,
        "object": {
            "uri": f"s3://memory-shared/quarantine/alice/{scan_id}/x.pdf",
            "sha256": "0" * 64,
            "size_bytes": 42,
            "claimed_content_type": "application/pdf",
        },
        "scanner_name": "clamav",
        "scanner_version": "1.3.1",
        "signature_db_hash": "deadbeef",
        "threat_name": "EICAR-Test-Signature" if verdict == "rejected" else None,
        "threat_family": "test" if verdict == "rejected" else None,
        "confidence": 1.0 if verdict == "rejected" else None,
        "detected_content_type": None,
    }


class TestPersistAudit:
    async def test_clean_writes_success_row_with_event_class_security(self) -> None:
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        consumer = ScanAuditConsumer(settings=_settings(), session_factory=factory)
        await consumer._persist_audit(_audit_payload(verdict="clean"))

        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(InteractionRecord).where(
                            InteractionRecord.event_class == "security"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        row = rows[0]
        assert row.project == "content-control"
        assert row.source == "scan-audit"
        assert row.user_id == "alice"
        assert row.trace_id == "trace-abc"
        assert row.event_class == "security"
        assert row.status == "success"
        assert row.failure_class == "clean"
        assert row.answer == ""
        assert "scan_id=scan-1" in row.question
        # error_detail is structured JSON
        detail = json.loads(row.error_detail)
        assert detail["scan_id"] == "scan-1"
        assert detail["scanner_name"] == "clamav"
        assert detail["signature_db_hash"] == "deadbeef"
        assert detail["threat_name"] is None

    async def test_rejected_writes_failed_status_with_threat_metadata(self) -> None:
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        consumer = ScanAuditConsumer(settings=_settings(), session_factory=factory)
        await consumer._persist_audit(
            _audit_payload(verdict="rejected", scan_id="scan-r")
        )

        async with factory() as session:
            row = (
                await session.execute(
                    select(InteractionRecord).where(
                        InteractionRecord.event_class == "security"
                    )
                )
            ).scalar_one()
        assert row.status == "failed"
        assert row.failure_class == "rejected"
        detail = json.loads(row.error_detail)
        assert detail["verdict"] == "rejected"
        assert detail["threat_name"] == "EICAR-Test-Signature"
        assert detail["threat_family"] == "test"
        assert detail["confidence"] == 1.0

    async def test_scan_failed_writes_failed_status(self) -> None:
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        consumer = ScanAuditConsumer(settings=_settings(), session_factory=factory)
        await consumer._persist_audit(_audit_payload(verdict="scan_failed"))

        async with factory() as session:
            row = (
                await session.execute(
                    select(InteractionRecord).where(
                        InteractionRecord.event_class == "security"
                    )
                )
            ).scalar_one()
        assert row.status == "failed"
        assert row.failure_class == "scan_failed"


def _aio_pika_mock() -> MagicMock:
    """See ``tests/test_scan_verdict_consumer.py::_aio_pika_mock``;
    same purpose — bind the REAL ``aio_pika.exceptions`` submodule to
    a MagicMock so ``from aio_pika.exceptions import …`` resolves
    inside ``_ensure_connected``."""
    import aio_pika as _real_aio_pika  # noqa: PLC0415

    aio_pika = MagicMock()
    aio_pika.exceptions = _real_aio_pika.exceptions
    sys.modules.setdefault("aio_pika.exceptions", _real_aio_pika.exceptions)
    return aio_pika


class TestEnsureConnected:
    def _patch_aio_pika(self) -> tuple[Any, Any, Any, Any]:
        aio_pika = _aio_pika_mock()
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
        aio_pika, _conn, ch, _q = self._patch_aio_pika()
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanAuditConsumer(
                settings=_settings(),
                session_factory=await _make_factory(),
            )
            await consumer._ensure_connected()
            await consumer._ensure_connected()
        aio_pika.connect_robust.assert_awaited_once()
        ch.get_queue.assert_awaited_once_with("audittrace.scan.audit")

    async def test_missing_url_raises(self) -> None:
        consumer = ScanAuditConsumer(
            settings=_settings(url=""),
            session_factory=await _make_factory(),
        )
        with pytest.raises(RuntimeError, match="scan_amqp_url is required"):
            await consumer._ensure_connected()

    async def test_queue_not_found_retries_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Symmetric to scan_verdict_consumer's retry test — B4b
        fresh-install race against the topology-bootstrap Job."""
        from aio_pika.exceptions import ChannelNotFoundEntity

        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        aio_pika, conn, ch, queue = self._patch_aio_pika()
        ch.get_queue = AsyncMock(
            side_effect=[ChannelNotFoundEntity("not_found"), queue]
        )
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanAuditConsumer(
                settings=_settings(),
                session_factory=await _make_factory(),
            )
            await consumer._ensure_connected()
        assert ch.get_queue.await_count == 2
        assert conn.channel.await_count == 2

    async def test_queue_not_found_exhausts_attempts_and_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aio_pika.exceptions import ChannelNotFoundEntity

        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        aio_pika, _conn, ch, _q = self._patch_aio_pika()
        ch.get_queue = AsyncMock(side_effect=ChannelNotFoundEntity("not_found"))
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanAuditConsumer(
                settings=_settings(),
                session_factory=await _make_factory(),
            )
            with pytest.raises(
                RuntimeError,
                match=r"audittrace\.scan\.audit.*not found after \d+ attempts",
            ):
                await consumer._ensure_connected()
        assert ch.get_queue.await_count == consumer._QUEUE_MAX_ATTEMPTS


class TestProcessOne:
    async def test_message_process_owns_ack_nack(self) -> None:
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        consumer = ScanAuditConsumer(settings=_settings(), session_factory=factory)
        message = MagicMock()
        process_cm = AsyncMock()
        process_cm.__aenter__ = AsyncMock(return_value=None)
        process_cm.__aexit__ = AsyncMock(return_value=False)
        message.process = MagicMock(return_value=process_cm)
        message.body = json.dumps(_audit_payload(verdict="clean")).encode("utf-8")

        await consumer._process_one(message)

        message.process.assert_called_once_with(requeue=False)
        async with factory() as session:
            count = (
                (
                    await session.execute(
                        select(InteractionRecord).where(
                            InteractionRecord.event_class == "security"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(count) == 1


class TestRunLoop:
    async def test_run_iterates_queue_and_cancels_cleanly(self) -> None:
        import asyncio as _asyncio

        aio_pika = _aio_pika_mock()
        connection = AsyncMock()
        channel = AsyncMock()
        channel.set_qos = AsyncMock()
        channel.close = AsyncMock()
        connection.channel = AsyncMock(return_value=channel)
        connection.close = AsyncMock()
        aio_pika.connect_robust = AsyncMock(return_value=connection)

        message = MagicMock()
        process_cm = AsyncMock()
        process_cm.__aenter__ = AsyncMock(return_value=None)
        process_cm.__aexit__ = AsyncMock(return_value=False)
        message.process = MagicMock(return_value=process_cm)
        message.body = json.dumps(_audit_payload(verdict="clean")).encode("utf-8")

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
                return message

        queue = MagicMock()
        queue.iterator = MagicMock(return_value=_FakeIter())
        channel.get_queue = AsyncMock(return_value=queue)

        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanAuditConsumer(settings=_settings(), session_factory=factory)
            task = _asyncio.create_task(consumer.run())
            await _asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(_asyncio.CancelledError):
                await task

        async with factory() as session:
            count = (
                (
                    await session.execute(
                        select(InteractionRecord).where(
                            InteractionRecord.event_class == "security"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(count) == 1

    async def test_run_logs_and_continues_on_per_message_exception(
        self,
    ) -> None:
        import asyncio as _asyncio

        aio_pika = _aio_pika_mock()
        connection = AsyncMock()
        channel = AsyncMock()
        channel.set_qos = AsyncMock()
        channel.close = AsyncMock()
        connection.channel = AsyncMock(return_value=channel)
        connection.close = AsyncMock()
        aio_pika.connect_robust = AsyncMock(return_value=connection)

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
                "audittrace.services.scan_audit_consumer.logger.error"
            ) as mock_err:
                consumer = ScanAuditConsumer(
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
        consumer = ScanAuditConsumer(
            settings=_settings(),
            session_factory=await _make_factory(),
        )
        await consumer.aclose()

    async def test_aclose_closes_channel_and_connection(self) -> None:
        aio_pika = _aio_pika_mock()
        connection = AsyncMock()
        channel = AsyncMock()
        queue = AsyncMock()
        channel.set_qos = AsyncMock()
        channel.get_queue = AsyncMock(return_value=queue)
        channel.close = AsyncMock()
        connection.channel = AsyncMock(return_value=channel)
        connection.close = AsyncMock()
        aio_pika.connect_robust = AsyncMock(return_value=connection)
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanAuditConsumer(
                settings=_settings(),
                session_factory=await _make_factory(),
            )
            await consumer._ensure_connected()
            await consumer.aclose()
        channel.close.assert_awaited_once()
        connection.close.assert_awaited_once()
