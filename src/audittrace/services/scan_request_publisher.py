"""Hohpe Transactional Outbox + Danjou §1 producer task.

Owns the in-process side of /memory/upload → AMQP. Every PDF
upload goes through:

    1. /memory/upload PUTs to MinIO ``quarantine/<user>/<scan_id>/<file>``
    2. /memory/upload INSERTs ``memory_items(..., scan_status='pending_scan',
       published_at_ms=NULL)``
    3. /memory/upload appends ``ScanRequestEnvelope`` to ``self._queue``
    4. /memory/upload returns 202

This task owns step 5+:

    5. ``async for envelope in self._queue:`` drains the queue.
    6. Calls ``ScanAmqpClient.publish_scan_request`` (basic_publish).
    7. UPDATEs ``memory_items.published_at_ms = now_ms`` for the
       envelope's scan_id.

Failure semantics:

* Broker hiccup → publish raises → envelope NOT requeued in-process.
  The manifest row stays at ``published_at_ms IS NULL``; the janitor
  query (60 s grace, ``ScanRequestJanitor``) re-enqueues.
* Process restart with un-drained ``asyncio.Queue`` → entries lost
  in-memory. Same recovery path: published_at_ms IS NULL → janitor.

The publisher therefore IS the happy path; the janitor is the only
durability guarantee.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import update

from audittrace.db.models import MemoryItem

if TYPE_CHECKING:
    from sqlalchemy.orm import sessionmaker

    from audittrace.services.scan_amqp_client import ScanAmqpClient

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class ScanRequestEnvelope:
    """In-process message handed from /memory/upload to the
    publisher task. Mirrors the AMQP payload shape so the publisher
    only does ``json.dumps`` not field marshalling."""

    scan_id: str
    user_id: str
    trace_id: str
    object_uri: str
    object_sha256: str
    size_bytes: int
    claimed_content_type: str
    traceparent: str

    def as_amqp_payload(self) -> dict[str, Any]:
        """Translate to the cross-repo ScanRequest contract (v1.yaml)."""
        return {
            "scan_id": self.scan_id,
            "user_id": self.user_id,
            "trace_id": self.trace_id,
            "traceparent": self.traceparent,
            "object": {
                "uri": self.object_uri,
                "sha256": self.object_sha256,
                "size_bytes": self.size_bytes,
                "claimed_content_type": self.claimed_content_type,
            },
            "enqueued_at_ms": _now_ms(),
        }


class ScanRequestPublisher:
    """Background task that drains the asyncio.Queue and publishes."""

    def __init__(
        self,
        *,
        amqp_client: ScanAmqpClient,
        queue: asyncio.Queue[ScanRequestEnvelope],
        session_factory: sessionmaker[Any],
    ) -> None:
        self._amqp = amqp_client
        self._queue = queue
        self._session_factory = session_factory

    async def _publish_one(self, envelope: ScanRequestEnvelope) -> None:
        """Publish + mark the manifest row published_at_ms.

        Errors are logged but NOT re-raised: the run loop continues
        with the next envelope. The manifest's NULL marker is the
        durability hook for the janitor."""
        try:
            await self._amqp.publish_scan_request(envelope.as_amqp_payload())
        except Exception as exc:  # broad: any aio_pika / network class
            logger.warning(
                "scan_publisher.publish_failed",
                extra={"scan_id": envelope.scan_id, "reason": str(exc)},
            )
            return
        # ``UPDATE … WHERE id=:scan_id AND published_at_ms IS NULL``
        # makes the marker idempotent under crash-restart races.
        try:
            with self._session_factory() as session:
                session.execute(
                    update(MemoryItem)
                    .where(MemoryItem.id == envelope.scan_id)
                    .where(MemoryItem.published_at_ms.is_(None))
                    .values(published_at_ms=_now_ms())
                )
                session.commit()
        except Exception as exc:
            logger.error(
                "scan_publisher.outbox_update_failed",
                extra={"scan_id": envelope.scan_id, "reason": str(exc)},
            )

    async def run(self) -> None:
        """Drain the queue forever. Cancellable via lifespan."""
        logger.info("scan_publisher.run.start")
        try:
            while True:
                envelope = await self._queue.get()
                try:
                    await self._publish_one(envelope)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            logger.info("scan_publisher.run.cancelled")
            raise
