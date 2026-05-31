"""Tests for services/scan_request_publisher.py — Hohpe outbox
+ Danjou §1 producer task (ADR-048 PR-B3)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.routes.memory_upload import manifest as manifest_mod
from audittrace.services.scan_request_publisher import (
    ScanRequestEnvelope,
    ScanRequestPublisher,
)


def _envelope(scan_id: str = "scan-1") -> ScanRequestEnvelope:
    return ScanRequestEnvelope(
        scan_id=scan_id,
        user_id="alice",
        trace_id="trace-abc",
        object_uri=f"s3://memory-shared/quarantine/alice/{scan_id}/paper.pdf",
        object_sha256="0" * 64,
        size_bytes=12,
        claimed_content_type="application/pdf",
        traceparent="00-abc-def-01",
    )


class TestEnvelopePayload:
    def test_amqp_payload_shape_matches_contract(self) -> None:
        # 2026-05-14 B4b: flipped from nested `object.{uri,sha256,size_bytes}`
        # to the FLAT cross-repo contract shape that
        # content-control v0.0.7's scan_request_consumer_rabbitmq
        # actually parses. The nested form was silently incompatible
        # since PR-B3 (1f4967a); prod's image `v1.0.20-flat` was
        # built from a never-merged branch patching this. See the
        # docstring on `ScanRequestEnvelope.as_amqp_payload`.
        env = _envelope()
        p = env.as_amqp_payload()
        assert p["scan_id"] == "scan-1"
        assert p["user_id"] == "alice"
        assert p["trace_id"] == "trace-abc"
        assert p["traceparent"] == "00-abc-def-01"
        assert p["object_uri"] == "s3://memory-shared/quarantine/alice/scan-1/paper.pdf"
        assert p["object_sha256"] == "0" * 64
        assert p["object_size_bytes"] == 12
        assert p["claimed_content_type"] == "application/pdf"
        assert "object" not in p, "regression: nested object key resurfaced"
        assert isinstance(p["enqueued_at_ms"], int)


class TestPublishOne:
    @pytest.fixture
    async def setup(self) -> tuple[Any, Any, Any]:
        amqp = MagicMock()
        amqp.publish_scan_request = AsyncMock()
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        queue: asyncio.Queue[ScanRequestEnvelope] = asyncio.Queue()
        publisher = ScanRequestPublisher(
            amqp_client=amqp, queue=queue, session_factory=factory
        )
        return publisher, amqp, factory

    async def test_happy_path_marks_published_at_ms(self, setup) -> None:
        publisher, amqp, factory = setup
        env = _envelope()
        # Seed the manifest row (route would have done this).
        async with factory() as session:
            await manifest_mod.insert_pending_scan(
                session,
                scan_id=env.scan_id,
                user_id=env.user_id,
                object_uri=env.object_uri,
                object_sha256=env.object_sha256,
                size_bytes=env.size_bytes,
                title="x",
                trace_id=env.trace_id,
            )

        await publisher._publish_one(env)

        amqp.publish_scan_request.assert_awaited_once()
        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, env.scan_id)
        assert row is not None
        assert row.published_at_ms is not None
        assert row.published_at_ms > 0

    async def test_publish_failure_leaves_published_at_ms_null(self, setup) -> None:
        # Broker outage: published_at_ms stays NULL; janitor will
        # re-enqueue. The publisher logs but does not re-raise (the
        # run loop continues).
        publisher, amqp, factory = setup
        amqp.publish_scan_request.side_effect = OSError("broker down")
        env = _envelope("scan-fail")
        async with factory() as session:
            await manifest_mod.insert_pending_scan(
                session,
                scan_id=env.scan_id,
                user_id=env.user_id,
                object_uri=env.object_uri,
                object_sha256=env.object_sha256,
                size_bytes=env.size_bytes,
                title="x",
                trace_id=env.trace_id,
            )

        await publisher._publish_one(env)

        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, env.scan_id)
        assert row is not None
        assert row.published_at_ms is None

    async def test_update_skips_when_already_published(self, setup) -> None:
        # Idempotent UPDATE: WHERE published_at_ms IS NULL means a
        # crash-restart race that re-publishes the same scan_id
        # doesn't corrupt the timestamp.
        publisher, amqp, factory = setup
        env = _envelope("scan-idem")
        async with factory() as session:
            row = await manifest_mod.insert_pending_scan(
                session,
                scan_id=env.scan_id,
                user_id=env.user_id,
                object_uri=env.object_uri,
                object_sha256=env.object_sha256,
                size_bytes=env.size_bytes,
                title="x",
                trace_id=env.trace_id,
            )
            row.published_at_ms = 99999
            await session.commit()

        await publisher._publish_one(env)

        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, env.scan_id)
        assert row is not None
        # Stayed at 99999 — not over-written by NOW().
        assert row.published_at_ms == 99999


class TestRunLoop:
    async def test_run_drains_queue_and_cancels_cleanly(self) -> None:
        amqp = MagicMock()
        amqp.publish_scan_request = AsyncMock()
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        queue: asyncio.Queue[ScanRequestEnvelope] = asyncio.Queue()

        # Seed two manifest rows + queue them.
        for i in range(2):
            scan_id = f"scan-{i}"
            async with factory() as session:
                await manifest_mod.insert_pending_scan(
                    session,
                    scan_id=scan_id,
                    user_id="alice",
                    object_uri=f"s3://x/{i}",
                    object_sha256="0" * 64,
                    size_bytes=1,
                    title="x",
                    trace_id="t",
                )
            await queue.put(_envelope(scan_id))

        publisher = ScanRequestPublisher(
            amqp_client=amqp, queue=queue, session_factory=factory
        )
        task = asyncio.create_task(publisher.run())
        # Wait for the queue to drain.
        await queue.join()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert amqp.publish_scan_request.await_count == 2
