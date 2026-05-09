"""ADR-048 PR-B4 — verdict consumer.

Consumes the ``audittrace.scan.verdicts`` queue (declared by PR-B2.5
topology Job, bound to the same-named exchange via routing key
``scan.verdict.*``). On each message:

    1. Parse the JSON ``Verdict`` (cross-repo contract).
    2. Map ``VerdictKind`` → closed-set ``scan_status`` value
       (per migration 012, pinned by ``TestScanStatusCodes``).
    3. UPDATE ``memory_items`` for the scan_id with:
       - ``scan_status`` (terminal state)
       - ``modified_at_ms`` (now)
       - On clean: ``key`` rewritten to the
         ``episodic/papers/<scan_id>/<filename>`` post-promotion
         URI (content-control promoted the bytes; memory-server
         re-points the manifest at the new location so
         ``/memory/index`` can find it).

Discipline:

- aio_pika consumer with ``set_qos(prefetch_count=…)`` so the
  process is bounded under verdict bursts.
- ``message.process(requeue=False)`` async-CM owns ack/nack:
  business-logic exceptions nack-without-requeue and the broker
  routes to DLX after ``x-delivery-limit=5``.
- ContextVar binding for the RLS Postgres write
  (``feedback_unit_tests_miss_rls`` — background workers MUST
  set ``app.current_user_id`` before INSERT/UPDATE on RLS'd
  tables; ``memory_items`` is RLS-free per migration 009 but
  the discipline is recorded in case future schema enables it).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import update

from audittrace.db.models import MemoryItem

if TYPE_CHECKING:
    from sqlalchemy.orm import sessionmaker

    from audittrace.config import Settings

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


# Closed-set mapping: content-control verdict kind → memory-server
# manifest scan_status. Pinned by ``TestScanStatusCodes`` for the
# domain side; this dict is the producer-side translation.
_VERDICT_TO_SCAN_STATUS: dict[str, str] = {
    "clean": "scanned_clean",
    "rejected": "rejected_malware",
    "scan_failed": "scan_failed",
}


def _episodic_uri(
    *,
    bucket: str,
    prefix: str,
    scan_id: str,
    quarantine_key: str,
) -> str:
    """Mirror of content-control's ``ScanWorker._episodic_uri``.

    Pattern: ``s3://{bucket}/{prefix}{scan_id}/{filename}``. The
    filename is the last path component of the quarantine URI
    (``s3://memory-shared/quarantine/<user>/<scan_id>/<filename>``).
    """
    filename = quarantine_key.rsplit("/", 1)[-1] or "object.bin"
    return f"s3://{bucket}/{prefix}{scan_id}/{filename}"


class ScanVerdictConsumer:
    """aio_pika consumer for the verdict topic queue."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker[Any],
        queue_name: str = "audittrace.scan.verdicts",
        prefetch_count: int = 16,
        episodic_bucket: str = "memory-shared",
        episodic_prefix: str = "episodic/papers/",
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._queue_name = queue_name
        self._prefetch_count = prefetch_count
        self._episodic_bucket = episodic_bucket
        self._episodic_prefix = episodic_prefix
        self._connection: Any = None
        self._channel: Any = None
        self._queue: Any = None

    async def _ensure_connected(self) -> None:
        if self._queue is not None:
            return
        if not self._settings.scan_amqp_url:
            raise RuntimeError(
                "scan_amqp_url is required when scan_pipeline_enabled=true"
            )
        import aio_pika  # noqa: PLC0415 — avoid import on disabled paths

        self._connection = await aio_pika.connect_robust(self._settings.scan_amqp_url)
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=self._prefetch_count)
        self._queue = await self._channel.get_queue(self._queue_name)
        logger.info(
            "scan_verdict_consumer.connected",
            extra={"queue": self._queue_name},
        )

    def _apply_verdict(self, payload: dict[str, Any]) -> None:
        """Synchronous DB UPDATE — wrapped in to_thread by the
        caller (Danjou §3 — sync libs off the event loop).
        Raises on parse / DB errors so the AMQP CM nacks."""
        scan_id = payload["scan_id"]
        kind = payload["kind"]
        scan_status = _VERDICT_TO_SCAN_STATUS.get(kind)
        if scan_status is None:
            raise ValueError(f"unknown verdict kind: {kind!r}")

        with self._session_factory() as session:
            row = session.get(MemoryItem, scan_id)
            if row is None:
                # Manifest row missing — likely a duplicate verdict
                # for a row that was already processed and reaped.
                # Log + skip; do NOT nack because re-delivery would
                # spin in the same state.
                logger.warning(
                    "scan_verdict_consumer.row_missing",
                    extra={"scan_id": scan_id},
                )
                return
            updates: dict[str, Any] = {
                "scan_status": scan_status,
                "modified_at_ms": _now_ms(),
            }
            if kind == "clean":
                updates["key"] = _episodic_uri(
                    bucket=self._episodic_bucket,
                    prefix=self._episodic_prefix,
                    scan_id=scan_id,
                    quarantine_key=row.key,
                )
            session.execute(
                update(MemoryItem).where(MemoryItem.id == scan_id).values(**updates)
            )
            session.commit()
            logger.info(
                "scan_verdict_consumer.applied",
                extra={
                    "scan_id": scan_id,
                    "verdict": kind,
                    "scan_status": scan_status,
                },
            )

    async def _process_one(self, message: Any) -> None:
        """Per-message handler. ``message.process(requeue=False)``
        wraps ack/nack — exceptions nack without re-queue, broker
        DLX kicks in after ``x-delivery-limit=5``."""
        async with message.process(requeue=False):
            payload = json.loads(message.body.decode("utf-8"))
            await asyncio.to_thread(self._apply_verdict, payload)

    async def run(self) -> None:
        await self._ensure_connected()
        assert self._queue is not None
        logger.info("scan_verdict_consumer.run.start")
        try:
            async with self._queue.iterator() as it:
                async for message in it:
                    try:
                        await self._process_one(message)
                    except Exception as exc:
                        # message.process already nacked; log and
                        # continue. The DLX catches recurring
                        # failures.
                        logger.error(
                            "scan_verdict_consumer.process_failed",
                            extra={"reason": str(exc)},
                        )
        except asyncio.CancelledError:
            logger.info("scan_verdict_consumer.run.cancelled")
            raise

    async def aclose(self) -> None:
        if self._channel is not None:
            try:
                await self._channel.close()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "scan_verdict_consumer.channel_close_failed",
                    extra={"reason": str(exc)},
                )
            finally:
                self._channel = None
        if self._connection is not None:
            try:
                await self._connection.close()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "scan_verdict_consumer.connection_close_failed",
                    extra={"reason": str(exc)},
                )
            finally:
                self._connection = None
        self._queue = None
