"""Tests for services/session_gc_janitor.py — WU-6 Part A (Sovereign-Attach
EPIC) of the session-memory retention GC.

Falsifiability anchors (spec §2.5, non-vacuity guards 2 + 4):

* guard 2 — the bounded batch: ``_sweep_once`` must stop calling
  ``gc_expired`` once a call returns fewer than a full batch, and the
  janitor never asks for more than ``_SESSION_GC_BATCH_SIZE`` per call.
* guard 4 — the ``AUDITTRACE_SESSION_GC_ENABLED`` flag:
  ``_maybe_start_session_gc_janitor`` must return ``None`` (no task
  scheduled) when the flag is off.

Guards 1 (cutoff) and 3 (promoted-durable independence) live in
``tests/test_session_memory_service.py`` and
``tests/test_wu6_session_gc_promoted_independence.py`` respectively — the
former is a service-layer concern (``gc_expired``'s own filter), the
latter needs the real ``/memory/promote`` + ``/memory/upload`` HTTP routes.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from unittest.mock import MagicMock

import pytest

from audittrace.identity import sentinel_user_context
from audittrace.server import _maybe_start_session_gc_janitor
from audittrace.services.session_gc_janitor import (
    _SESSION_GC_BATCH_SIZE,
    SessionGCJanitor,
)
from audittrace.services.session_memory import MockSessionMemoryService


def _settings(*, retention_hours: int = 24, interval: int = 300) -> MagicMock:
    s = MagicMock()
    s.session_retention_hours = retention_hours
    s.session_gc_interval_seconds = interval
    # A real DB is "configured" by default (see
    # ``TestMaybeStartSessionGcJanitor`` below for the specific
    # not-configured case) — mirrors ``settings.database_url`` being a
    # real asyncpg URL in production/laptop deployments.
    s.database_url = "postgresql+asyncpg://fake-test-db"
    return s


async def _seed_expired_rows(service: MockSessionMemoryService, count: int) -> None:
    """Write *count* rows directly into the Mock store, then back-date
    them well past any retention window used in these tests."""
    user = sentinel_user_context()
    for i in range(count):
        await service.write(user, f"note-{i}.txt", "expired content")
    # Mock rows are frozen dataclasses — rewrite in place with an
    # ancient created_at_ms so every seeded row is unconditionally
    # eligible regardless of the retention window under test.
    ancient = 1_000  # epoch ms, effectively year-1970
    service._items = [
        type(item)(
            id=item.id,
            user_id=item.user_id,
            filename=item.filename,
            content=item.content,
            created_at_ms=ancient,
        )
        for item in service._items
    ]


class TestSweepOnce:
    async def test_sweep_deletes_all_expired_rows_across_batches(self) -> None:
        """A backlog larger than one batch is fully drained in a SINGLE
        tick — the loop keeps calling ``gc_expired`` until a call returns
        fewer than a full batch."""
        service = MockSessionMemoryService()
        await _seed_expired_rows(service, _SESSION_GC_BATCH_SIZE + 20)
        janitor = SessionGCJanitor(settings=_settings(), service=service)

        total = await janitor._sweep_once()

        assert total == _SESSION_GC_BATCH_SIZE + 20
        assert service._items == []

    async def test_sweep_never_asks_for_more_than_the_batch_bound(self) -> None:
        """Non-vacuity guard 2 (spec §2.5): a spy on ``gc_expired`` proves
        the janitor's ``limit=`` argument is bounded at
        ``_SESSION_GC_BATCH_SIZE`` on every call — neuter this to pass an
        unbounded/huge limit and this test goes RED."""
        service = MockSessionMemoryService()
        await _seed_expired_rows(service, 5)
        seen_limits: list[int] = []
        original = service.gc_expired

        async def _spy(*, older_than_ms: int, limit: int) -> int:
            seen_limits.append(limit)
            return await original(older_than_ms=older_than_ms, limit=limit)

        service.gc_expired = _spy  # type: ignore[method-assign]
        janitor = SessionGCJanitor(settings=_settings(), service=service)

        total = await janitor._sweep_once()

        assert total == 5
        assert seen_limits, "gc_expired was never called"
        assert all(limit == _SESSION_GC_BATCH_SIZE for limit in seen_limits)

    async def test_sweep_with_nothing_expired_is_a_noop(self) -> None:
        service = MockSessionMemoryService()
        user = sentinel_user_context()
        await service.write(user, "fresh.txt", "not expired")
        janitor = SessionGCJanitor(
            settings=_settings(retention_hours=24), service=service
        )

        total = await janitor._sweep_once()

        assert total == 0
        assert len(service._items) == 1

    async def test_sweep_uses_retention_hours_to_compute_cutoff(self) -> None:
        """A row 2 hours old survives a 24h retention window but is
        eligible under a 1h window — proves the janitor actually threads
        ``session_retention_hours`` into the cutoff it hands ``gc_expired``,
        rather than some hardcoded value."""
        service = MockSessionMemoryService()
        user = sentinel_user_context()
        await service.write(user, "two-hours-old.txt", "x")
        two_hours_ago = int(time.time() * 1000) - (2 * 3600 * 1000)
        service._items = [
            type(item)(
                id=item.id,
                user_id=item.user_id,
                filename=item.filename,
                content=item.content,
                created_at_ms=two_hours_ago,
            )
            for item in service._items
        ]

        janitor_long_retention = SessionGCJanitor(
            settings=_settings(retention_hours=24), service=service
        )
        assert await janitor_long_retention._sweep_once() == 0

        janitor_short_retention = SessionGCJanitor(
            settings=_settings(retention_hours=1), service=service
        )
        assert await janitor_short_retention._sweep_once() == 1


class TestRunLoop:
    async def test_run_cancels_cleanly(self) -> None:
        service = MockSessionMemoryService()
        janitor = SessionGCJanitor(settings=_settings(interval=1), service=service)
        task = asyncio.create_task(janitor.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_run_logs_and_continues_on_tick_failure(self) -> None:
        service = MagicMock()
        service.gc_expired = MagicMock(side_effect=RuntimeError("db unreachable"))
        janitor = SessionGCJanitor(settings=_settings(interval=1), service=service)
        task = asyncio.create_task(janitor.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestMaybeStartSessionGcJanitor:
    """Non-vacuity guard 4 (spec §2.5) — the ``AUDITTRACE_SESSION_GC_ENABLED``
    flag. Exercises ``server._maybe_start_session_gc_janitor`` directly
    (decoupled from the rest of lifespan startup — see that function's
    docstring)."""

    async def test_starts_when_enabled(self) -> None:
        settings = _settings()
        settings.session_gc_enabled = True
        service = MockSessionMemoryService()

        task = _maybe_start_session_gc_janitor(settings, service)

        assert task is not None
        try:
            assert task.get_name() == "session-gc-janitor"
            assert not task.done()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_does_not_start_when_disabled(self) -> None:
        """Neuter the ``if not settings.session_gc_enabled: return None``
        check in ``_maybe_start_session_gc_janitor`` (e.g. always fall
        through to scheduling the task) and this assertion goes RED."""
        settings = _settings()
        settings.session_gc_enabled = False
        service = MockSessionMemoryService()

        task = _maybe_start_session_gc_janitor(settings, service)

        assert task is None

    async def test_does_not_start_without_a_configured_database(self) -> None:
        """The flag alone is not enough — no real ``database_url``
        configured (the ``AUDITTRACE_ENV=test`` default; no
        ``AUDITTRACE_POSTGRES_URL``/``_PASSWORD`` set) means no task
        either, mirroring ``summarizer_enabled and summarizer_db_url``'s
        identical shape. This is what keeps the janitor dormant across
        the whole unit-test suite's ``client``/``app`` fixtures (which
        trigger the real FastAPI lifespan) — see this module's other
        guard tests for why: a background task touching the shared
        aiosqlite ``InMemoryPostgresFactory`` engine from the TestClient
        portal loop while a test body touches it from pytest-asyncio's
        own loop deadlocks aiosqlite's loop-bound driver bridge."""
        settings = _settings()
        settings.session_gc_enabled = True
        settings.database_url = None
        service = MockSessionMemoryService()

        task = _maybe_start_session_gc_janitor(settings, service)

        assert task is None
