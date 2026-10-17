"""DB CRUD on ``memory_items`` for the PDF scan flow.

Three call-sites:

* ``insert_pending_scan`` — invoked by the POST /memory/upload
  PDF branch BEFORE the asyncio.Queue.put.
* ``get_by_scan_id``       — invoked by GET /memory/upload/status.
* (PR-B4) ``update_scan_status`` — verdict consumer transitions
  ``pending_scan`` → terminal state.

All three use the regular ``memory_items`` ORM (no RLS, per
migration 009 — manifest is operator-global by design). Async-only
data layer (#263): callers pass an ``AsyncSession``."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from sqlalchemy import select

from audittrace.db.models import MemoryItem

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


async def insert_pending_scan(
    session: AsyncSession,
    *,
    scan_id: str,
    user_id: str,
    object_uri: str,
    object_sha256: str,
    size_bytes: int,
    title: str | None,
    trace_id: str,
) -> MemoryItem:
    """Create a pending-scan manifest row.

    The row's ``id`` is the AMQP scan_id — they are intentionally
    the same UUID so reconstruction queries (manifest ↔ trace ↔
    Langfuse span) all join on one column.

    ``layer="episodic"`` because PDFs land in the episodic layer
    once promoted (``episodic/papers/``); the quarantine prefix is
    the staging area, not a layer of its own.
    """
    now = _now_ms()
    row = MemoryItem(
        id=scan_id,
        layer="episodic",
        # Quarantine URI — the full s3:// path, not the eventual
        # episodic key. Once content-control's verdict consumer
        # promotes (PR-A3 + PR-B4), the consumer rewrites this
        # column to the episodic destination.
        key=object_uri,
        title=title,
        size_bytes=size_bytes,
        created_at_ms=now,
        modified_at_ms=now,
        created_by_user_id=user_id,
        modified_by_user_id=user_id,
        scan_status="pending_scan",
        published_at_ms=None,  # outbox marker
        trace_id=trace_id,
        document_sha256=object_sha256,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    logger.info(
        "memory_upload.manifest.inserted",
        extra={"scan_id": scan_id, "user_id": user_id},
    )
    return row


async def get_by_scan_id(session: AsyncSession, scan_id: str) -> MemoryItem | None:
    """Lookup for /memory/upload/status. Returns None on miss
    so the route can surface 404 cleanly."""
    return (
        await session.execute(select(MemoryItem).where(MemoryItem.id == scan_id))
    ).scalar_one_or_none()
