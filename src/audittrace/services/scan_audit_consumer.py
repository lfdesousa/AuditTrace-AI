"""ADR-048 PR-B4 — security-audit consumer.

Consumes the ``audittrace.scan.audit`` queue (declared by PR-B2.5
topology Job, bound via routing key ``scan.audit.*``). On each
message:

    1. Parse the JSON ``SecurityAuditRow`` (cross-repo contract).
    2. INSERT a row into the ``interactions`` table with
       ``event_class='security'`` so SOC tooling can alert on
       ``rejected_malware`` outcomes without scanning every chat
       row.

Field mapping into ``InteractionRecord`` (no schema change — uses
existing columns + JSON in ``error_detail`` for the structured
payload):

    project        = "content-control"
    source         = "scan-audit"
    question       = f"scan_id={scan_id} sha256={object_sha256}"
    answer         = ""    (no LLM answer for security audit)
    user_id        = from audit row
    trace_id       = from audit row
    event_class    = "security"
    status         = "success" if verdict==clean else "failed"
    failure_class  = the verdict kind (clean/rejected/scan_failed)
    error_detail   = JSON-encoded row body (scanner, sigdb_hash,
                     threat_name, threat_family, confidence,
                     scanner_version, object_uri).
    timestamp      = ISO-now (consumed-at, not scan-at)

Discipline mirrors ``ScanVerdictConsumer``:

- aio_pika consumer with bounded prefetch.
- ``message.process(requeue=False)`` for ack/nack semantics; DLX
  catches recurring failures.
- Sync DB call wrapped in ``asyncio.to_thread`` (Danjou §3).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from audittrace.db.models import InteractionRecord

if TYPE_CHECKING:
    from sqlalchemy.orm import sessionmaker

    from audittrace.config import Settings

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ScanAuditConsumer:
    """aio_pika consumer for the security-audit topic queue."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker[Any],
        queue_name: str = "audittrace.scan.audit",
        prefetch_count: int = 16,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._queue_name = queue_name
        self._prefetch_count = prefetch_count
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
        import aio_pika  # noqa: PLC0415

        self._connection = await aio_pika.connect_robust(self._settings.scan_amqp_url)
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=self._prefetch_count)
        self._queue = await self._channel.get_queue(self._queue_name)
        logger.info(
            "scan_audit_consumer.connected",
            extra={"queue": self._queue_name},
        )

    def _persist_audit(self, payload: dict[str, Any]) -> None:
        """Synchronous INSERT — wrapped in ``asyncio.to_thread`` by
        the caller. Raises on parse / DB errors so the AMQP CM
        nacks."""
        scan_id = payload["scan_id"]
        verdict = payload["verdict"]
        obj = payload.get("object", {})
        sha256 = obj.get("sha256", "")
        object_uri = obj.get("uri", "")
        # Closed-set discipline: status mirrors verdict kind.
        status = "success" if verdict == "clean" else "failed"

        # Structured error_detail captures the full audit context
        # in JSON so SOC tooling can `jq -r '.threat_name'` etc.
        # without a schema migration.
        detail = json.dumps(
            {
                "scan_id": scan_id,
                "verdict": verdict,
                "scanner_name": payload.get("scanner_name"),
                "scanner_version": payload.get("scanner_version"),
                "signature_db_hash": payload.get("signature_db_hash"),
                "threat_name": payload.get("threat_name"),
                "threat_family": payload.get("threat_family"),
                "confidence": payload.get("confidence"),
                "object_uri": object_uri,
                "object_sha256": sha256,
            }
        )

        row = InteractionRecord(
            project="content-control",
            source="scan-audit",
            question=f"scan_id={scan_id} sha256={sha256}",
            answer="",
            timestamp=_now_iso(),
            user_id=payload.get("user_id"),
            trace_id=payload.get("trace_id"),
            status=status,
            failure_class=verdict,
            error_detail=detail,
            event_class="security",
        )
        with self._session_factory() as session:
            session.add(row)
            session.commit()
        logger.info(
            "scan_audit_consumer.persisted",
            extra={
                "scan_id": scan_id,
                "verdict": verdict,
                "user_id": payload.get("user_id"),
            },
        )

    async def _process_one(self, message: Any) -> None:
        async with message.process(requeue=False):
            payload = json.loads(message.body.decode("utf-8"))
            await asyncio.to_thread(self._persist_audit, payload)

    async def run(self) -> None:
        await self._ensure_connected()
        assert self._queue is not None
        logger.info("scan_audit_consumer.run.start")
        try:
            async with self._queue.iterator() as it:
                async for message in it:
                    try:
                        await self._process_one(message)
                    except Exception as exc:
                        logger.error(
                            "scan_audit_consumer.process_failed",
                            extra={"reason": str(exc)},
                        )
        except asyncio.CancelledError:
            logger.info("scan_audit_consumer.run.cancelled")
            raise

    async def aclose(self) -> None:
        if self._channel is not None:
            try:
                await self._channel.close()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "scan_audit_consumer.channel_close_failed",
                    extra={"reason": str(exc)},
                )
            finally:
                self._channel = None
        if self._connection is not None:
            try:
                await self._connection.close()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "scan_audit_consumer.connection_close_failed",
                    extra={"reason": str(exc)},
                )
            finally:
                self._connection = None
        self._queue = None
