"""Outbox janitor — periodic re-enqueue of ``memory_items`` rows
whose ``published_at_ms`` is still NULL after the grace window.

Runs as a lifespan-owned background task alongside
``ScanRequestPublisher``. Every ``Settings.scan_janitor_interval_seconds``
the janitor:

    SELECT id, created_by_user_id, trace_id, key, ...
    FROM memory_items
    WHERE published_at_ms IS NULL
      AND created_at_ms < (now_ms - grace_ms)
      AND deleted_at_ms IS NULL
    LIMIT batch_size

For each row, it constructs a ``ScanRequestEnvelope`` and pushes it
back onto the producer queue. Idempotent: the publisher's ``WHERE
published_at_ms IS NULL`` UPDATE means a healthy publish-in-flight
isn't double-marked.

Why a separate task (not the publisher itself):

* The publisher is a hot loop on the queue — adding a periodic DB
  poll there would tangle two concerns.
* The janitor's failure modes (DB unreachable, batch query slow)
  are independent of the publisher's (broker hiccup).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from audittrace.db.models import MemoryItem
from audittrace.services.scan_request_publisher import ScanRequestEnvelope

if TYPE_CHECKING:
    from sqlalchemy.orm import sessionmaker

    from audittrace.config import Settings

logger = logging.getLogger(__name__)

# Bounded batch keeps the janitor's per-tick cost predictable.
_JANITOR_BATCH_SIZE = 100


def _now_ms() -> int:
    return int(time.time() * 1000)


class ScanRequestJanitor:
    """Periodic re-enqueue of orphaned manifest rows."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker[Any],
        queue: asyncio.Queue[ScanRequestEnvelope],
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._queue = queue

    def _scan_orphans(self) -> list[ScanRequestEnvelope]:
        """One DB poll. Returns up to ``_JANITOR_BATCH_SIZE`` envelopes
        ready to re-enqueue. Synchronous Session — wrap in to_thread
        at the call site."""
        cutoff = _now_ms() - (self._settings.scan_janitor_grace_seconds * 1000)
        envelopes: list[ScanRequestEnvelope] = []
        with self._session_factory() as session:
            stmt = (
                select(MemoryItem)
                .where(MemoryItem.published_at_ms.is_(None))
                .where(MemoryItem.created_at_ms < cutoff)
                .where(MemoryItem.deleted_at_ms.is_(None))
                .limit(_JANITOR_BATCH_SIZE)
            )
            for row in session.execute(stmt).scalars():
                envelopes.append(
                    ScanRequestEnvelope(
                        scan_id=row.id,
                        user_id=row.created_by_user_id,
                        trace_id=row.trace_id or "",
                        # Janitor reconstructs the quarantine URI from
                        # `key` — the route stores the full s3:// path
                        # (see manifest.py:insert_pending_scan).
                        object_uri=row.key,
                        object_sha256=row.document_sha256 or "",
                        size_bytes=row.size_bytes or 0,
                        claimed_content_type="application/pdf",
                        traceparent="",
                    )
                )
        return envelopes

    async def _tick_once(self) -> int:
        """One janitor cycle. Returns count re-enqueued."""
        envelopes = await asyncio.to_thread(self._scan_orphans)
        for env in envelopes:
            await self._queue.put(env)
        if envelopes:
            logger.info(
                "scan_janitor.re_enqueued",
                extra={"count": len(envelopes)},
            )
        return len(envelopes)

    async def run(self) -> None:
        logger.info(
            "scan_janitor.run.start",
            extra={"interval_s": self._settings.scan_janitor_interval_seconds},
        )
        try:
            while True:
                try:
                    await self._tick_once()
                except Exception as exc:
                    logger.error(
                        "scan_janitor.tick_failed",
                        extra={"reason": str(exc)},
                    )
                await asyncio.sleep(self._settings.scan_janitor_interval_seconds)
        except asyncio.CancelledError:
            logger.info("scan_janitor.run.cancelled")
            raise
