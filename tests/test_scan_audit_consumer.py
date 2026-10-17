"""Tests for services/scan_audit_consumer.py — ADR-048 PR-B4
security-audit row writer."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
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


async def _poll_until(
    condition: Callable[[], Awaitable[bool]],
    *,
    timeout: float = 5.0,
    interval: float = 0.01,
) -> None:
    """Poll an async ``condition`` until it returns ``True``, raising
    ``AssertionError`` once ``timeout`` elapses.

    Deterministic replacement for a fixed-duration ``asyncio.sleep(N)``
    race window (the #254/#357 flake class; see
    ``tests/test_scan_verdict_consumer.py::_poll_until`` for the twin
    this mirrors): the caller proceeds the instant the observable
    side-effect lands instead of gambling on a wall-clock budget that a
    loaded CI host can blow through.

    FALSIFIABILITY: if the awaited side-effect never happens (e.g. the
    consumer's DB-write were neutered), this raises ``AssertionError``
    after ``timeout`` seconds — it never returns while the condition is
    still false, so it cannot mask a genuine regression.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if await condition():
            return
        if loop.time() >= deadline:
            raise AssertionError(
                f"condition not met within {timeout}s (polled every {interval}s)"
            )
        await asyncio.sleep(interval)


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


class TestRlsContextAndOwnerGuard:
    """#357 — the consumer runs outside FastAPI's auth middleware, so it must
    set the ``app.current_user_id`` RLS GUC itself before the INSERT into the
    RLS'd ``interactions`` table, and must refuse to persist an
    unattributable (NULL-owner) audit row (ADR-058 invariant).

    RLS enforcement is a Postgres property (SQLite ignores it — see
    ``feedback_unit_tests_miss_rls`` and ``test_rls_isolation.py``); these
    unit tests pin the *code contract* (the GUC is set, the guard fires).
    The enforcement itself is proven by the live redeploy in the PR body.
    """

    async def test_sets_rls_user_context_from_audit_row(self) -> None:
        factory = await _make_factory()
        consumer = ScanAuditConsumer(settings=_settings(), session_factory=factory)
        with patch(
            "audittrace.services.scan_audit_consumer.set_current_user_id"
        ) as mock_set:
            await consumer._persist_audit(_audit_payload(verdict="clean"))
        # The uploading user owns their scan-audit row → GUC set to their sub
        # so Postgres RLS WITH CHECK accepts the INSERT.
        mock_set.assert_called_once_with("alice")

    async def test_missing_user_id_refuses_unattributable_row(self) -> None:
        factory = await _make_factory()
        consumer = ScanAuditConsumer(settings=_settings(), session_factory=factory)
        payload = _audit_payload(verdict="clean")
        del payload["user_id"]
        with pytest.raises(ValueError, match="no user_id"):
            await consumer._persist_audit(payload)
        # Nothing persisted — a NULL-owner row would be permanently unreadable.
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
        assert rows == []


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
        channel.declare_queue = AsyncMock(return_value=queue)
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
        ch.declare_queue.assert_awaited_once_with(
            "audittrace.scan.audit",
            durable=True,
            arguments={"x-queue-type": "quorum"},
        )

    async def test_missing_url_raises(self) -> None:
        consumer = ScanAuditConsumer(
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
            consumer = ScanAuditConsumer(
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

        RED PROOF: reverting ``scan_audit_consumer.py`` to the old
        passive ``self._channel.get_queue(self._queue_name)`` call
        makes this test fail — ``get_queue``'s side effect
        (``ChannelNotFoundEntity``) propagates, the bounded retry
        exhausts, and ``_ensure_connected`` raises ``RuntimeError``
        instead of attaching. This is the exact regression #393 fixes."""
        from aio_pika.exceptions import ChannelNotFoundEntity

        aio_pika, _conn, ch, queue = self._patch_aio_pika()
        ch.get_queue = AsyncMock(side_effect=ChannelNotFoundEntity("not_found"))
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanAuditConsumer(
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

        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        aio_pika, conn, ch, queue = self._patch_aio_pika()
        ch.declare_queue = AsyncMock(side_effect=[AMQPError("transient"), queue])
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanAuditConsumer(
                settings=_settings(),
                session_factory=await _make_factory(),
            )
            await consumer._ensure_connected()
        assert ch.declare_queue.await_count == 2
        assert conn.channel.await_count == 2

    async def test_queue_declare_exhausts_attempts_and_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aio_pika.exceptions import AMQPError

        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        aio_pika, _conn, ch, _q = self._patch_aio_pika()
        ch.declare_queue = AsyncMock(side_effect=AMQPError("still failing"))
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanAuditConsumer(
                settings=_settings(),
                session_factory=await _make_factory(),
            )
            with pytest.raises(
                RuntimeError,
                match=r"audittrace\.scan\.audit.*declare failed after \d+ attempts",
            ):
                await consumer._ensure_connected()
        assert ch.declare_queue.await_count == consumer._QUEUE_MAX_ATTEMPTS


class TestConnectLevelRetry:
    """#384 WS3 — INITIAL ``connect_robust`` wrapped in capped-backoff
    retry over connection-level errors; on exhaustion the connection
    error is re-raised (not wrapped) so run() can supervise it."""

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
        consumer = ScanAuditConsumer(
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
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        aio_pika = _aio_pika_mock()
        aio_pika.connect_robust = AsyncMock(
            side_effect=ConnectionResetError(104, "reset")
        )
        consumer = ScanAuditConsumer(
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
        consumer = ScanAuditConsumer(
            settings=_settings(),
            session_factory=await _make_factory(),
        )
        got = await consumer._connect_with_retry(aio_pika, AMQPError)
        assert got is connection
        assert aio_pika.connect_robust.await_count == 2


class _ParkingIter:
    """queue.iterator() stand-in that parks so a supervised run()
    reaches the message loop and waits for cancel."""

    async def __aenter__(self) -> _ParkingIter:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    def __aiter__(self) -> _ParkingIter:
        return self

    async def __anext__(self) -> Any:
        import asyncio as _asyncio

        await _asyncio.Event().wait()


class TestSupervisedRun:
    """#384 WS3 — initial connect failure retries in run() (I2);
    CancelledError propagates."""

    async def test_run_retries_after_connect_failure_then_attaches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio as _asyncio

        real_sleep = _asyncio.sleep

        async def _fast_sleep(_delay: float, *a: Any, **k: Any) -> None:
            await real_sleep(0)

        monkeypatch.setattr("asyncio.sleep", _fast_sleep)
        consumer = ScanAuditConsumer(
            settings=_settings(),
            session_factory=await _make_factory(),
        )
        calls = {"n": 0}

        async def fake_ensure() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionResetError(104, "Connection reset by peer")
            queue = MagicMock()
            queue.iterator = MagicMock(return_value=_ParkingIter())
            consumer._queue = queue

        monkeypatch.setattr(consumer, "_ensure_connected", fake_ensure)

        task = _asyncio.create_task(consumer.run())
        for _ in range(50):
            await real_sleep(0)
            if consumer._queue is not None:
                break
        assert calls["n"] == 2
        assert consumer._queue is not None
        task.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await task

    async def test_run_propagates_cancelled_during_connect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio as _asyncio

        consumer = ScanAuditConsumer(
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
        channel.declare_queue = AsyncMock(return_value=queue)

        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()

        async def _security_row_persisted() -> bool:
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
            return len(rows) == 1

        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            consumer = ScanAuditConsumer(settings=_settings(), session_factory=factory)
            task = _asyncio.create_task(consumer.run())
            # Deterministic wait: poll until the run loop has pulled the
            # one queued message, processed it, and committed the
            # security-audit row — load-independent (was a fixed
            # asyncio.sleep(0.05) race window that could lose under host
            # contention, the #254/#357 flake class shared with
            # ScanVerdictConsumer's identical race). Times out
            # (AssertionError) — never passes vacuously — if the
            # consumer never reaches the write.
            await _poll_until(_security_row_persisted)
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
        channel.declare_queue = AsyncMock(return_value=queue)

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
        channel.declare_queue = AsyncMock(return_value=queue)
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
