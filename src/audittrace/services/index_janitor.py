"""Auto-index janitor — SPEC #387 Phase 1 WU-3.

Periodic re-enqueue of ``memory_items`` rows whose ``indexed_at_ms`` is
still NULL after the grace window — the durability guarantee for
place→index, mirroring ``ScanRequestJanitor``'s place→publish shape one
hop later. A process restart with an un-drained ``asyncio.Queue``, or a
transient embed-server outage that ``IndexWorker`` (WU-2) already logged
and skipped, both leave a ``scanned_clean AND indexed_at_ms IS NULL`` row
behind; this janitor is what makes that self-heal with ZERO operator
touch — closing SPEC #387 GAP-1 for the crash-restart / outage case, not
just the happy path.

Why a separate task (not folded into ``IndexWorker``): same rationale as
``ScanRequestJanitor`` vs ``ScanRequestPublisher`` — the worker is a hot
loop on the queue; a periodic DB poll is an independent failure mode (DB
unreachable, slow batch query) that shouldn't tangle with the embed/upsert
concern.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from sqlalchemy import select

from audittrace.db.models import MemoryItem
from audittrace.services.index_routing import bare_key_from_uri, collection_for_key
from audittrace.services.index_worker import IndexRequestEnvelope

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from audittrace.config import Settings

logger = logging.getLogger(__name__)

# Bounded batch keeps the janitor's per-tick cost predictable.
_JANITOR_BATCH_SIZE = 100

# Closed-set: only clean-scanned rows are eligible for auto-index —
# mirrors GAP-1's exact trigger condition (SPEC #387 §2).
_ELIGIBLE_SCAN_STATUS = "scanned_clean"


def _now_ms() -> int:
    return int(time.time() * 1000)


class IndexJanitor:
    """Periodic re-enqueue of stuck ``scanned_clean`` / un-indexed rows."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        queue: asyncio.Queue[IndexRequestEnvelope],
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._queue = queue

    async def _scan_orphans(self) -> list[IndexRequestEnvelope]:
        """One DB poll. Returns up to ``_JANITOR_BATCH_SIZE`` envelopes
        ready to re-enqueue. Only rows that are ``scanned_clean``,
        un-indexed, past the grace window, and not soft-deleted qualify —
        the same closed-set predicate SPEC #387 §4 (Durability) names."""
        cutoff = _now_ms() - (self._settings.index_janitor_grace_seconds * 1000)
        envelopes: list[IndexRequestEnvelope] = []
        async with self._session_factory() as session:
            stmt = (
                select(MemoryItem)
                .where(MemoryItem.scan_status == _ELIGIBLE_SCAN_STATUS)
                .where(MemoryItem.indexed_at_ms.is_(None))
                .where(MemoryItem.created_at_ms < cutoff)
                .where(MemoryItem.deleted_at_ms.is_(None))
                .limit(_JANITOR_BATCH_SIZE)
            )
            for row in (await session.execute(stmt)).scalars():
                bare_key = bare_key_from_uri(row.key)
                envelopes.append(
                    IndexRequestEnvelope(
                        scan_id=row.id,
                        key=bare_key,
                        collection=collection_for_key(bare_key),
                        user_id=row.created_by_user_id,
                        trace_id=row.trace_id or "",
                    )
                )
        return envelopes

    async def _tick_once(self) -> int:
        """One janitor cycle. Returns count re-enqueued."""
        envelopes = await self._scan_orphans()
        for env in envelopes:
            await self._queue.put(env)
        if envelopes:
            logger.info(
                "index_janitor.re_enqueued",
                extra={"count": len(envelopes)},
            )
        return len(envelopes)

    async def run(self) -> None:
        logger.info(
            "index_janitor.run.start",
            extra={"interval_s": self._settings.index_janitor_interval_seconds},
        )
        try:
            while True:
                try:
                    await self._tick_once()
                except Exception as exc:
                    logger.error(
                        "index_janitor.tick_failed",
                        extra={"reason": str(exc)},
                    )
                await asyncio.sleep(self._settings.index_janitor_interval_seconds)
        except asyncio.CancelledError:
            logger.info("index_janitor.run.cancelled")
            raise
