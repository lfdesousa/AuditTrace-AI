"""Tests for services/index_janitor.py — SPEC #387 Phase 1 (WU-3) auto-index
janitor: the durability guarantee for place→index.

Falsifiability anchor (SPEC Acceptance §4, guard #2 — Durability): a test
proving the janitor re-enqueues a stuck ``scanned_clean AND indexed_at_ms
IS NULL`` row past the grace window, and does NOT re-enqueue a fresh row,
an already-indexed row, a non-clean-scan row, or a soft-deleted row.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.routes.memory_upload import manifest as manifest_mod
from audittrace.services.index_janitor import IndexJanitor
from audittrace.services.index_worker import IndexRequestEnvelope


def _settings(grace: int = 60, interval: int = 30) -> MagicMock:
    s = MagicMock()
    s.index_janitor_grace_seconds = grace
    s.index_janitor_interval_seconds = interval
    return s


async def _seed(
    factory,
    *,
    scan_id: str,
    age_seconds: int,
    scan_status: str | None = "scanned_clean",
    indexed_at_ms: int | None = None,
    deleted: bool = False,
) -> None:
    """Insert a manifest row simulating a scan-pipeline promotion, with
    ``created_at_ms`` back-dated by ``age_seconds`` and the outbox/status
    columns set to the caller's chosen state."""
    async with factory() as session:
        row = await manifest_mod.insert_pending_scan(
            session,
            scan_id=scan_id,
            user_id="alice",
            object_uri=f"s3://memory-shared/quarantine/alice/{scan_id}/x.pdf",
            object_sha256="0" * 64,
            size_bytes=1,
            title="x.pdf",
            trace_id="trace-1",
        )
        row.created_at_ms = int(time.time() * 1000) - (age_seconds * 1000)
        row.scan_status = scan_status
        row.key = f"s3://memory-shared/episodic/papers/{scan_id}/x.pdf"
        row.indexed_at_ms = indexed_at_ms
        if deleted:
            row.deleted_at_ms = int(time.time() * 1000)
        await session.commit()


class TestScanOrphans:
    @pytest_asyncio.fixture
    async def factory(self) -> object:
        f = InMemoryPostgresFactory()
        await f.create_schema()
        return f.get_session_factory()

    async def test_picks_up_stuck_clean_rows_past_grace(self, factory) -> None:
        await _seed(factory, scan_id="stuck-1", age_seconds=120)
        await _seed(factory, scan_id="stuck-2", age_seconds=200)

        janitor = IndexJanitor(
            settings=_settings(grace=60),
            session_factory=factory,
            queue=asyncio.Queue(),
        )
        envelopes = await janitor._scan_orphans()
        scan_ids = {e.scan_id for e in envelopes}
        assert scan_ids == {"stuck-1", "stuck-2"}
        # Envelope carries the BARE key, not the manifest's s3:// URI,
        # and the routed collection (a promoted .pdf → ai_research_papers).
        env = next(e for e in envelopes if e.scan_id == "stuck-1")
        assert env.key == "episodic/papers/stuck-1/x.pdf"
        assert env.collection == "ai_research_papers"
        assert env.user_id == "alice"
        assert env.trace_id == "trace-1"

    async def test_skips_rows_younger_than_grace(self, factory) -> None:
        # 10s old, grace 60s — the indexer is probably still in flight.
        await _seed(factory, scan_id="fresh-1", age_seconds=10)

        janitor = IndexJanitor(
            settings=_settings(grace=60),
            session_factory=factory,
            queue=asyncio.Queue(),
        )
        assert await janitor._scan_orphans() == []

    async def test_skips_already_indexed_rows(self, factory) -> None:
        await _seed(factory, scan_id="done-1", age_seconds=200, indexed_at_ms=12345)
        janitor = IndexJanitor(
            settings=_settings(grace=60),
            session_factory=factory,
            queue=asyncio.Queue(),
        )
        assert await janitor._scan_orphans() == []

    async def test_skips_soft_deleted_rows(self, factory) -> None:
        await _seed(factory, scan_id="del-1", age_seconds=200, deleted=True)
        janitor = IndexJanitor(
            settings=_settings(grace=60),
            session_factory=factory,
            queue=asyncio.Queue(),
        )
        assert await janitor._scan_orphans() == []

    async def test_skips_rows_not_scanned_clean(self, factory) -> None:
        # A pending/rejected/scan_failed row is not eligible for
        # auto-index at all — only scanned_clean is (SPEC §2's GAP-1
        # trigger condition).
        await _seed(
            factory, scan_id="pending-1", age_seconds=200, scan_status="pending_scan"
        )
        await _seed(
            factory,
            scan_id="rejected-1",
            age_seconds=200,
            scan_status="rejected_malware",
        )
        janitor = IndexJanitor(
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
        await _seed(factory, scan_id="orphan-x", age_seconds=200)
        queue: asyncio.Queue[IndexRequestEnvelope] = asyncio.Queue()
        janitor = IndexJanitor(
            settings=_settings(grace=60),
            session_factory=factory,
            queue=queue,
        )
        n = await janitor._tick_once()
        assert n == 1
        env = await queue.get()
        assert env.scan_id == "orphan-x"

    async def test_tick_with_no_orphans_pushes_nothing(self) -> None:
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        queue: asyncio.Queue[IndexRequestEnvelope] = asyncio.Queue()
        janitor = IndexJanitor(
            settings=_settings(grace=60),
            session_factory=factory,
            queue=queue,
        )
        assert await janitor._tick_once() == 0
        assert queue.empty()


class TestRunLoop:
    async def test_run_cancels_cleanly(self) -> None:
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        queue: asyncio.Queue[IndexRequestEnvelope] = asyncio.Queue()
        janitor = IndexJanitor(
            settings=_settings(grace=60, interval=1),
            session_factory=factory,
            queue=queue,
        )
        task = asyncio.create_task(janitor.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_run_logs_and_continues_on_tick_failure(self) -> None:
        broken_factory = MagicMock(side_effect=RuntimeError("db unreachable"))
        janitor = IndexJanitor(
            settings=_settings(grace=60, interval=1),
            session_factory=broken_factory,
            queue=asyncio.Queue(),
        )
        task = asyncio.create_task(janitor.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
