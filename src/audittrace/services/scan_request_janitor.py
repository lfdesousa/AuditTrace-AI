"""Outbox janitor — periodic re-enqueue of ``memory_items`` rows
whose ``published_at_ms`` is still NULL after the grace window.

Runs as a lifespan-owned background task alongside
``ScanRequestPublisher``. Every ``Settings.scan_janitor_interval_seconds``
the janitor:

    SELECT id, created_by_user_id, trace_id, key, ...
    FROM memory_items
    WHERE published_at_ms IS NULL
      AND scan_status IS NOT NULL
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

**SCAN-URI-BUG (fixed 2026-08-23, root-caused 2026-08-22).**
``published_at_ms IS NULL`` alone does NOT identify a genuine scan
candidate: `.md` manifest folds (decisions/skills/procedural,
``MemoryManifestService.record_create``) never publish AND never
populate ``scan_status`` either, so their ``published_at_ms`` also
reads NULL forever. The janitor was scooping those rows up too and
re-enqueuing ``object_uri=row.key`` — for a `.md` fold, ``key`` is
the semantic manifest key (``"<collection>/<doc_id>"``), NOT an
``s3://`` quarantine URI. The scan consumer rejects that
(``object_uri must use s3:// scheme``) → DLQ → poison flood (churn,
no data loss). Confirmed discriminator (WU-1): ``scan_status IS NOT
NULL`` — only rows the upload/scan path itself marked with a scan
status are genuinely awaiting a scan verdict. WU-2 adds a
defense-in-depth guard at enqueue time so a malformed/non-s3
``object_uri`` can never reach the consumer + DLQ again even if a
future write path mis-populates a row that happens to carry a
``scan_status``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from sqlalchemy import select

from audittrace.db.models import MemoryItem
from audittrace.services.scan_request_publisher import ScanRequestEnvelope

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from audittrace.config import Settings

logger = logging.getLogger(__name__)

# Bounded batch keeps the janitor's per-tick cost predictable.
_JANITOR_BATCH_SIZE = 100

# WU-2 defense-in-depth — only a genuine quarantine URI may reach the
# scan consumer's queue. Named constant so the guard's intent reads at
# the call site rather than a bare string literal.
_REQUIRED_OBJECT_URI_SCHEME = "s3"


def _now_ms() -> int:
    return int(time.time() * 1000)


class ScanRequestJanitor:
    """Periodic re-enqueue of orphaned manifest rows."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        queue: asyncio.Queue[ScanRequestEnvelope],
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._queue = queue

    async def _scan_orphans(self) -> list[ScanRequestEnvelope]:
        """One DB poll. Returns up to ``_JANITOR_BATCH_SIZE`` envelopes
        ready to re-enqueue. Synchronous Session — wrap in to_thread
        at the call site.

        WU-1 (SCAN-URI-BUG): the ``scan_status IS NOT NULL`` predicate
        is the load-bearing fix — it excludes `.md` manifest folds
        (decisions/skills/procedural), which share the
        ``published_at_ms IS NULL`` shape with genuine scan candidates
        but never populate ``scan_status``. WU-2: any surviving
        candidate whose ``key`` is not an ``s3://`` URI is skipped +
        WARNING-logged rather than enqueued — defense-in-depth against
        a future write path mis-populating a row that carries a
        ``scan_status`` but not a quarantine URI.
        """
        cutoff = _now_ms() - (self._settings.scan_janitor_grace_seconds * 1000)
        envelopes: list[ScanRequestEnvelope] = []
        async with self._session_factory() as session:
            stmt = (
                select(MemoryItem)
                .where(MemoryItem.published_at_ms.is_(None))
                .where(MemoryItem.scan_status.is_not(None))
                .where(MemoryItem.created_at_ms < cutoff)
                .where(MemoryItem.deleted_at_ms.is_(None))
                .limit(_JANITOR_BATCH_SIZE)
            )
            for row in (await session.execute(stmt)).scalars():
                scheme = urlparse(row.key).scheme
                if scheme != _REQUIRED_OBJECT_URI_SCHEME:
                    logger.warning(
                        "scan_janitor.non_s3_object_uri_skipped",
                        extra={
                            "scan_id": row.id,
                            "object_uri_scheme": scheme or "(none)",
                        },
                    )
                    continue
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
        envelopes = await self._scan_orphans()
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
