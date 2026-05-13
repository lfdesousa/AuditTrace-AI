"""Tests for services/scan_amqp_client.py — thin aio_pika wrapper
(ADR-048 PR-B3)."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from audittrace.services.scan_amqp_client import ScanAmqpClient


def _patch_aio_pika() -> tuple[Any, Any, Any, Any]:
    """Build a mocked aio_pika module + connection / channel /
    exchange. Returns (aio_pika, connection, channel, exchange)."""
    aio_pika = MagicMock()
    connection = AsyncMock()
    channel = AsyncMock()
    exchange = AsyncMock()
    # PR-B10: switched from passive get_exchange → active
    # declare_exchange. Mock the new entry point.
    channel.declare_exchange = AsyncMock(return_value=exchange)
    channel.close = AsyncMock()
    connection.channel = AsyncMock(return_value=channel)
    connection.close = AsyncMock()
    aio_pika.connect_robust = AsyncMock(return_value=connection)
    aio_pika.Message = MagicMock()
    aio_pika.DeliveryMode.PERSISTENT = 2
    aio_pika.ExchangeType.TOPIC = "topic"
    return aio_pika, connection, channel, exchange


def _settings(url: str = "amqp://x:y@audittrace-rabbitmq:5672/") -> MagicMock:
    s = MagicMock()
    s.scan_amqp_url = url
    s.scan_request_exchange = "audittrace.scan"
    s.scan_request_routing_key = "scan.requested"
    return s


class TestEnsureConnected:
    async def test_lazy_connect_runs_once(self) -> None:
        aio_pika, conn, ch, exchange = _patch_aio_pika()
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            client = ScanAmqpClient(_settings())
            await client.ensure_connected()
            await client.ensure_connected()  # no-op second time
        # connect_robust + channel + declare_exchange each called once.
        aio_pika.connect_robust.assert_awaited_once()
        conn.channel.assert_awaited_once()
        ch.declare_exchange.assert_awaited_once_with(
            "audittrace.scan",
            type="topic",
            durable=True,
        )

    async def test_missing_url_raises(self) -> None:
        client = ScanAmqpClient(_settings(url=""))
        with pytest.raises(RuntimeError, match="scan_amqp_url is required"):
            await client.ensure_connected()

    async def test_connect_retries_on_connection_refused(self) -> None:
        """PR-B10 — fresh-cluster kube-proxy programming lag.

        On a fresh kind cluster, the Service IP may not be routable
        for ~1s after the kubelet marks the RabbitMQ pod Ready. The
        first ``aio_pika.connect_robust`` call gets
        ``ConnectionRefusedError``; subsequent retries succeed.
        Verify the client retries with exponential backoff and
        eventually returns a Connection.
        """
        aio_pika, conn, _ch, _ex = _patch_aio_pika()
        # First two calls raise; third succeeds.
        aio_pika.connect_robust = AsyncMock(
            side_effect=[
                ConnectionRefusedError("kube-proxy not ready"),
                ConnectionRefusedError("kube-proxy not ready"),
                conn,
            ]
        )
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            with patch("asyncio.sleep", new=AsyncMock()) as sleep_mock:
                client = ScanAmqpClient(_settings())
                await client.ensure_connected()
        assert aio_pika.connect_robust.await_count == 3
        # Backoff sleeps after attempt 1 (1s) and attempt 2 (2s).
        sleep_mock.assert_any_await(1)
        sleep_mock.assert_any_await(2)

    async def test_connect_gives_up_after_max_attempts(self) -> None:
        """If RabbitMQ stays unreachable past the retry budget, the
        client raises ``RuntimeError`` so the lifespan fails fast and
        kubelet restarts the pod. Better than silently hanging."""
        aio_pika, _conn, _ch, _ex = _patch_aio_pika()
        aio_pika.connect_robust = AsyncMock(
            side_effect=ConnectionRefusedError("broker down")
        )
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            with patch("asyncio.sleep", new=AsyncMock()):
                client = ScanAmqpClient(_settings())
                with pytest.raises(RuntimeError, match="connect failed after 6"):
                    await client.ensure_connected()
        assert aio_pika.connect_robust.await_count == 6


class TestPublish:
    async def test_publish_writes_to_exchange(self) -> None:
        aio_pika, _conn, _ch, exchange = _patch_aio_pika()
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            client = ScanAmqpClient(_settings())
            await client.publish_scan_request(
                {
                    "scan_id": "sid-1",
                    "user_id": "alice",
                    "traceparent": "00-abc-def-01",
                }
            )
        exchange.publish.assert_awaited_once()
        # routing_key is the configured one
        kwargs = exchange.publish.call_args.kwargs
        assert kwargs["routing_key"] == "scan.requested"

    async def test_publish_serializes_payload_as_json(self) -> None:
        aio_pika, _conn, _ch, exchange = _patch_aio_pika()
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            client = ScanAmqpClient(_settings())
            await client.publish_scan_request({"scan_id": "sid-2"})
        # Message constructor called once, with body=<json bytes>
        aio_pika.Message.assert_called_once()
        body = aio_pika.Message.call_args.kwargs["body"]
        assert b'"scan_id": "sid-2"' in body


class TestAclose:
    async def test_aclose_closes_channel_and_connection(self) -> None:
        aio_pika, conn, ch, _exchange = _patch_aio_pika()
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            client = ScanAmqpClient(_settings())
            await client.ensure_connected()
            await client.aclose()
        ch.close.assert_awaited_once()
        conn.close.assert_awaited_once()

    async def test_aclose_idempotent_when_not_connected(self) -> None:
        client = ScanAmqpClient(_settings())
        # Never called ensure_connected — aclose must not raise.
        await client.aclose()


class TestAsyncContextManager:
    """`async with ScanAmqpClient(...)` is the canonical usage —
    mirrors AsyncExitStack composition in the lifespan (Luis
    2026-05-10 — systematic context-manager use)."""

    async def test_async_with_connects_and_acloses(self) -> None:
        aio_pika, conn, ch, _exchange = _patch_aio_pika()
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            async with ScanAmqpClient(_settings()) as client:
                assert client is not None
                await client.publish_scan_request({"scan_id": "x"})
        # On exit, channel + connection were both closed.
        ch.close.assert_awaited_once()
        conn.close.assert_awaited_once()

    async def test_async_with_acloses_on_exception(self) -> None:
        aio_pika, conn, ch, _exchange = _patch_aio_pika()
        with patch.dict(sys.modules, {"aio_pika": aio_pika}):
            with pytest.raises(RuntimeError, match="boom"):
                async with ScanAmqpClient(_settings()):
                    raise RuntimeError("boom")
        ch.close.assert_awaited_once()
        conn.close.assert_awaited_once()
