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
    channel.get_exchange = AsyncMock(return_value=exchange)
    channel.close = AsyncMock()
    connection.channel = AsyncMock(return_value=channel)
    connection.close = AsyncMock()
    aio_pika.connect_robust = AsyncMock(return_value=connection)
    aio_pika.Message = MagicMock()
    aio_pika.DeliveryMode.PERSISTENT = 2
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
        # connect_robust + channel + get_exchange each called once.
        aio_pika.connect_robust.assert_awaited_once()
        conn.channel.assert_awaited_once()
        ch.get_exchange.assert_awaited_once_with("audittrace.scan")

    async def test_missing_url_raises(self) -> None:
        client = ScanAmqpClient(_settings(url=""))
        with pytest.raises(RuntimeError, match="scan_amqp_url is required"):
            await client.ensure_connected()


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
