"""Tests for services/scan_request_janitor.py — periodic
re-enqueue of orphaned outbox rows (ADR-048 PR-B3)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.routes.memory_upload import manifest as manifest_mod
from audittrace.services.scan_request_janitor import ScanRequestJanitor
from audittrace.services.scan_request_publisher import ScanRequestEnvelope


def _settings(grace: int = 60, interval: int = 30) -> MagicMock:
    s = MagicMock()
    s.scan_janitor_grace_seconds = grace
    s.scan_janitor_interval_seconds = interval
    return s


async def _seed_pending(factory, *, scan_id: str, age_seconds: int) -> None:
    """Insert a pending-scan row whose created_at_ms simulates an
    age of ``age_seconds`` ago."""
    async with factory() as session:
        row = await manifest_mod.insert_pending_scan(
            session,
            scan_id=scan_id,
            user_id="alice",
            object_uri=f"s3://x/{scan_id}",
            object_sha256="0" * 64,
            size_bytes=1,
            title="x",
            trace_id="t",
        )
        row.created_at_ms = int(time.time() * 1000) - (age_seconds * 1000)
        await session.commit()


class TestScanOrphans:
    @pytest_asyncio.fixture
    async def factory(self) -> object:
        f = InMemoryPostgresFactory()
        await f.create_schema()
        return f.get_session_factory()

    async def test_picks_up_orphans_older_than_grace(self, factory) -> None:
        await _seed_pending(factory, scan_id="orphan-1", age_seconds=120)
        await _seed_pending(factory, scan_id="orphan-2", age_seconds=200)

        janitor = ScanRequestJanitor(
            settings=_settings(grace=60),
            session_factory=factory,
            queue=asyncio.Queue(),
        )
        envelopes = await janitor._scan_orphans()
        scan_ids = {e.scan_id for e in envelopes}
        assert scan_ids == {"orphan-1", "orphan-2"}

    async def test_skips_rows_younger_than_grace(self, factory) -> None:
        # 10s old, grace 60s — janitor must NOT pick this up
        # (the publisher is probably about to publish it).
        await _seed_pending(factory, scan_id="fresh-1", age_seconds=10)

        janitor = ScanRequestJanitor(
            settings=_settings(grace=60),
            session_factory=factory,
            queue=asyncio.Queue(),
        )
        assert await janitor._scan_orphans() == []

    async def test_skips_already_published_rows(self, factory) -> None:
        await _seed_pending(factory, scan_id="done-1", age_seconds=200)
        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "done-1")
            assert row is not None
            row.published_at_ms = 12345
            await session.commit()

        janitor = ScanRequestJanitor(
            settings=_settings(grace=60),
            session_factory=factory,
            queue=asyncio.Queue(),
        )
        assert await janitor._scan_orphans() == []

    async def test_skips_soft_deleted_rows(self, factory) -> None:
        await _seed_pending(factory, scan_id="del-1", age_seconds=200)
        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "del-1")
            assert row is not None
            row.deleted_at_ms = int(time.time() * 1000)
            await session.commit()

        janitor = ScanRequestJanitor(
            settings=_settings(grace=60),
            session_factory=factory,
            queue=asyncio.Queue(),
        )
        assert await janitor._scan_orphans() == []


class TestTickOnce:
    async def test_tick_pushes_to_queue(self) -> None:
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        await _seed_pending(factory, scan_id="orphan-x", age_seconds=200)
        queue: asyncio.Queue[ScanRequestEnvelope] = asyncio.Queue()
        janitor = ScanRequestJanitor(
            settings=_settings(grace=60),
            session_factory=factory,
            queue=queue,
        )
        n = await janitor._tick_once()
        assert n == 1
        env = await queue.get()
        assert env.scan_id == "orphan-x"


class TestRunLoop:
    async def test_run_cancels_cleanly(self) -> None:
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        queue: asyncio.Queue[ScanRequestEnvelope] = asyncio.Queue()
        janitor = ScanRequestJanitor(
            # Tiny interval so the loop ticks quickly in test.
            settings=_settings(grace=60, interval=1),
            session_factory=factory,
            queue=queue,
        )
        task = asyncio.create_task(janitor.run())
        # Let it spin once.
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
