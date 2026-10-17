"""Tests for services/scan_request_janitor.py — periodic
re-enqueue of orphaned outbox rows (ADR-048 PR-B3).

Also covers SCAN-URI-BUG (fixed 2026-08-23): WU-1 proves the
``scan_status IS NOT NULL`` filter excludes `.md` manifest folds
(decisions/skills/procedural) from re-enqueue; WU-2 proves the
defense-in-depth s3:// guard skips + logs any surviving candidate
whose ``object_uri`` is not an s3:// URI rather than enqueueing it."""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from audittrace.db.models import MemoryItem
from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.routes.memory_upload import manifest as manifest_mod
from audittrace.services.memory_manifest import MemoryManifestService
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


async def _seed_md_manifest_fold(factory, *, key: str, age_seconds: int) -> str:
    """Insert a `.md` manifest-fold row exactly the way
    ``MemoryManifestService.record_create`` (the real
    POST /memory/episodic write path) creates one: ``scan_status`` and
    ``published_at_ms`` are BOTH left NULL — this row never goes near
    the scan pipeline. Returns the row's ``id`` (its manifest PK, not
    a scan_id — `.md` folds don't have one)."""
    manifest = MemoryManifestService(session_factory=factory)
    entry = await manifest.record_create(
        layer="episodic",
        key=key,
        title=key,
        size_bytes=1,
        user_id="alice",
    )
    async with factory() as session:
        row = (
            await session.execute(
                select(MemoryItem).filter_by(layer="episodic", key=key)
            )
        ).scalar_one()
        row.created_at_ms = int(time.time() * 1000) - (age_seconds * 1000)
        await session.commit()
    return entry.id


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

    async def test_excludes_md_manifest_fold_rows(self, factory) -> None:
        """SCAN-URI-BUG WU-1 — the REALISTIC shape of the confirmed root
        cause. A genuine UPLOAD row (`scan_status='pending_scan'`,
        `key='s3://…'`) and a decisions `.md` manifest fold
        (`scan_status IS NULL`, `key='decisions/<doc_id>'` — no s3://
        scheme, since a `.md` fold's key is never a quarantine URI) both
        share the `published_at_ms IS NULL` shape past the grace window.
        Only the UPLOAD row is a genuine scan candidate.

        NOTE (reviewer finding #1, 2026-08-23): in THIS realistic shape,
        WU-2's s3:// guard also happens to exclude the `.md` fold (its
        key has no s3:// scheme), so this test alone cannot prove
        WU-1's `scan_status IS NOT NULL` predicate is what's doing the
        work — see
        ``test_excludes_md_manifest_fold_rows_independent_of_wu2_guard``
        below for the isolating proof. This test stays because it's the
        real-world regression case SCAN-URI-BUG was root-caused from.
        """
        await _seed_pending(factory, scan_id="upload-1", age_seconds=200)
        await _seed_md_manifest_fold(
            factory, key="decisions/2026-08-22-some-decision.md", age_seconds=200
        )

        janitor = ScanRequestJanitor(
            settings=_settings(grace=60),
            session_factory=factory,
            queue=asyncio.Queue(),
        )
        envelopes = await janitor._scan_orphans()
        scan_ids = {e.scan_id for e in envelopes}
        assert scan_ids == {"upload-1"}

    async def test_excludes_md_manifest_fold_rows_independent_of_wu2_guard(
        self, factory
    ) -> None:
        """SCAN-URI-BUG WU-1 — the ISOLATING proof (reviewer finding #1,
        2026-08-23). The `.md`-fold fixture here is given a
        ``key="s3://…"`` — a value that WOULD pass WU-2's s3:// scheme
        guard — so WU-2 CANNOT be what excludes it from the result.
        Only the `scan_status IS NOT NULL` predicate can exclude a row
        whose key already satisfies the s3:// scheme. This is
        deliberately an UNREALISTIC row shape (a real `.md` fold never
        carries an s3:// key) — its only purpose is to prove WU-1 is
        independently load-bearing, not merely redundant with WU-2.

        Falsifiability: with WU-2 still fully in place, drop
        ``.where(MemoryItem.scan_status.is_not(None))`` from
        ``_scan_orphans`` — this row reappears in the result (WU-2 would
        happily pass an s3:// uri through) — RED. Restoring the filter
        returns this test to green without touching WU-2 at all.
        """
        await _seed_pending(factory, scan_id="upload-1", age_seconds=200)
        await _seed_md_manifest_fold(
            factory, key="s3://quarantine/decisions-fold.md", age_seconds=200
        )

        janitor = ScanRequestJanitor(
            settings=_settings(grace=60),
            session_factory=factory,
            queue=asyncio.Queue(),
        )
        envelopes = await janitor._scan_orphans()
        scan_ids = {e.scan_id for e in envelopes}
        assert scan_ids == {"upload-1"}


class TestObjectUriGuard:
    """SCAN-URI-BUG WU-2 — defense-in-depth s3:// guard at enqueue.

    Even a row that DID pass the WU-1 ``scan_status IS NOT NULL``
    filter must never be enqueued if its ``object_uri`` (the manifest
    row's ``key``) is not an ``s3://`` URI — a future write path could
    mis-populate a row with a ``scan_status`` set but a non-quarantine
    key, and this guard is the last line of defense before the scan
    consumer + DLQ.
    """

    @pytest_asyncio.fixture
    async def factory(self) -> object:
        f = InMemoryPostgresFactory()
        await f.create_schema()
        return f.get_session_factory()

    async def test_skips_non_s3_object_uri(self, factory, caplog) -> None:
        # A row that IS a genuine scan candidate (scan_status set) but
        # whose key was somehow written as a bare manifest key rather
        # than an s3:// quarantine URI.
        await _seed_pending(factory, scan_id="malformed-1", age_seconds=200)
        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "malformed-1")
            assert row is not None
            row.key = "decisions/not-an-s3-uri.md"
            await session.commit()

        janitor = ScanRequestJanitor(
            settings=_settings(grace=60),
            session_factory=factory,
            queue=asyncio.Queue(),
        )
        with caplog.at_level(logging.WARNING):
            envelopes = await janitor._scan_orphans()

        # Falsifiability: neuter this guard (remove the scheme check in
        # `_scan_orphans`) — the malformed candidate reaches the
        # returned envelope list (and, downstream, the queue + scan
        # consumer + DLQ) and this assertion goes RED.
        assert envelopes == []
        assert any(
            r.message == "scan_janitor.non_s3_object_uri_skipped"
            and r.scan_id == "malformed-1"
            and r.object_uri_scheme == "(none)"
            for r in caplog.records
        )

    async def test_still_enqueues_genuine_s3_candidate(self, factory) -> None:
        # Companion non-vacuous proof: the guard is a filter, not a
        # blanket skip — a genuine s3:// candidate still passes.
        await _seed_pending(factory, scan_id="genuine-1", age_seconds=200)

        janitor = ScanRequestJanitor(
            settings=_settings(grace=60),
            session_factory=factory,
            queue=asyncio.Queue(),
        )
        envelopes = await janitor._scan_orphans()
        assert {e.scan_id for e in envelopes} == {"genuine-1"}
        assert envelopes[0].object_uri.startswith("s3://")


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
