"""ADR-048 PR-B4 — security-audit consumer.

Consumes the ``audittrace.scan.audit`` queue (also declared by the
PR-B2.5 topology Job, bound via routing key ``scan.audit.*``; #393 —
this consumer self-declares the queue idempotently too, so a cold
boot no longer depends on winning a race against the bootstrap Job).
On each message:

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
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from audittrace.db.models import InteractionRecord
from audittrace.db.rls import set_current_user_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
        session_factory: async_sessionmaker[AsyncSession],
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

    # #393 — canonical queue-declare args. MUST match the
    # ``job-amqp-topology-bootstrap`` Job byte-for-byte
    # (``charts/audittrace/templates/rabbitmq/job-amqp-topology-bootstrap.yaml``,
    # the ``audittrace.scan.audit`` PUT body: ``{"auto_delete":false,
    # "durable":true,"arguments":{"x-queue-type":"quorum"}}``) — that
    # Job is the single source of truth for the topology. A mismatch
    # makes RabbitMQ raise ``ChannelPreconditionFailed`` on declare.
    _CANONICAL_QUEUE_ARGUMENTS: dict[str, str] = {"x-queue-type": "quorum"}

    # 2026-05-14 B4b — see scan_verdict_consumer for the symmetric
    # rationale. Same fresh-install race; same retry budget.
    #
    # #393 FIX: switched from passive ``get_queue`` to idempotent
    # active ``declare_queue`` (mirrors the exchange fix in
    # ``ScanAmqpClient.ensure_connected``, PR-B10). The retry loop
    # below is now a guard against transient AMQP errors while
    # (re)opening the channel/declaring the queue, not the primary
    # mechanism for waiting out a not-yet-created queue — the
    # consumer declares the queue itself, so whichever side (this
    # consumer or the chart's ``job-amqp-topology-bootstrap`` Job)
    # runs first wins and the other adopts the existing queue.
    _QUEUE_MAX_ATTEMPTS: int = 6

    # #384 WS3 — INITIAL-connect resilience; see ScanVerdictConsumer for
    # the full rationale. ``connect_robust`` was previously outside the
    # retry loop, so a cold-reboot RabbitMQ killed the consumer task
    # permanently once the lifespan fail-open removed the crash-loop
    # crutch. Wrapped in capped-backoff retry + supervised in ``run()``.
    _CONNECT_MAX_ATTEMPTS: int = 6
    _CONNECT_BACKOFF_CAP_SECONDS: float = 30.0

    async def _connect_with_retry(
        self, aio_pika: Any, amqp_error_cls: type[BaseException]
    ) -> Any:
        """Open ``connect_robust`` with capped-backoff retry over
        connection-level errors (#384 WS3).

        Mirrors ``ScanVerdictConsumer._connect_with_retry``: the FIRST
        connect against a cold-reboot broker raises, so retry it here; on
        exhaustion re-raise the last connection error so ``run()`` can
        supervise it and keep retrying until RabbitMQ is up (I2)."""
        last_exc: BaseException | None = None
        for attempt in range(self._CONNECT_MAX_ATTEMPTS):
            try:
                return await aio_pika.connect_robust(self._settings.scan_amqp_url)
            except (ConnectionError, OSError, TimeoutError, amqp_error_cls) as exc:
                last_exc = exc
                if attempt == self._CONNECT_MAX_ATTEMPTS - 1:
                    break
                delay = min(2**attempt, self._CONNECT_BACKOFF_CAP_SECONDS)
                logger.warning(
                    "scan_audit_consumer.connect_retry",
                    extra={
                        "attempt": attempt + 1,
                        "max_attempts": self._CONNECT_MAX_ATTEMPTS,
                        "delay_seconds": delay,
                        "reason": str(exc),
                    },
                )
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    async def _ensure_connected(self) -> None:
        if self._queue is not None:
            return
        if not self._settings.scan_amqp_url:
            raise RuntimeError(
                "scan_amqp_url is required when scan_pipeline_enabled=true"
            )
        import asyncio  # noqa: PLC0415

        import aio_pika  # noqa: PLC0415
        from aio_pika.exceptions import AMQPError  # noqa: PLC0415

        # (C) connection-level retry — wait out a cold-reboot RabbitMQ
        # instead of dying (#384 WS3).
        self._connection = await self._connect_with_retry(aio_pika, AMQPError)
        last_exc: Exception | None = None
        for attempt in range(self._QUEUE_MAX_ATTEMPTS):
            try:
                # A closed channel can't be reused, so open a fresh one on
                # every retry attempt.
                self._channel = await self._connection.channel()
                await self._channel.set_qos(prefetch_count=self._prefetch_count)
                # #393 — idempotent ACTIVE declare (was passive
                # ``get_queue``, which required the bootstrap Job to have
                # already declared the queue). RabbitMQ no-ops if the
                # queue already exists with matching arguments, so this
                # composes safely with the bootstrap Job regardless of
                # which side runs first (I2).
                self._queue = await self._channel.declare_queue(
                    self._queue_name,
                    durable=True,
                    arguments=self._CANONICAL_QUEUE_ARGUMENTS,
                )
                break
            except (ConnectionError, OSError, TimeoutError, AMQPError) as exc:
                last_exc = exc
                if attempt == self._QUEUE_MAX_ATTEMPTS - 1:
                    break
                delay = 2**attempt
                logger.warning(
                    "scan_audit_consumer.queue_declare_retry",
                    extra={
                        "attempt": attempt + 1,
                        "max_attempts": self._QUEUE_MAX_ATTEMPTS,
                        "delay_seconds": delay,
                        "queue": self._queue_name,
                        "reason": str(exc),
                    },
                )
                await asyncio.sleep(delay)
        if self._queue is None:
            raise RuntimeError(
                f"scan_audit_consumer: queue {self._queue_name!r} declare "
                f"failed after {self._QUEUE_MAX_ATTEMPTS} attempts "
                f"(last error: {last_exc})"
            ) from last_exc
        logger.info(
            "scan_audit_consumer.connected",
            extra={"queue": self._queue_name},
        )

    async def _persist_audit(self, payload: dict[str, Any]) -> None:
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

        # The consumer task does NOT pass through FastAPI's auth middleware,
        # so the ``app.current_user_id`` RLS ContextVar is unset. Migration
        # 005 put ``WITH CHECK (user_id = current_setting('app.current_user_id',
        # true))`` on ``interactions``; with the GUC unset the check becomes
        # ``user_id = ''`` and every INSERT is rejected — which silently
        # dropped every scan-audit row. Set the GUC from the audit row's
        # user_id (the uploading user owns their scan-audit row, owner-scoped
        # and readable back under their own JWT). Mirrors async_persist +
        # session_summarizer. (#357)
        user_id = payload.get("user_id")
        if not user_id:
            # ADR-058 invariant: never persist an audit row with a NULL owner
            # (permanently unreadable under RLS). A scan-audit with no user_id
            # is a producer-side traceability defect — fail loud so it is not
            # lost silently.
            raise ValueError(
                f"scan-audit row for scan_id={scan_id} has no user_id; "
                "refusing to persist an unattributable audit row"
            )

        row = InteractionRecord(
            project="content-control",
            source="scan-audit",
            question=f"scan_id={scan_id} sha256={sha256}",
            answer="",
            timestamp=_now_iso(),
            user_id=user_id,
            trace_id=payload.get("trace_id"),
            status=status,
            failure_class=verdict,
            error_detail=detail,
            event_class="security",
        )
        set_current_user_id(user_id)
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
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
            await self._persist_audit(payload)

    async def run(self) -> None:
        from aio_pika.exceptions import AMQPError  # noqa: PLC0415

        # (C) Supervised connect — retry connection-level failures so the
        # consumer attaches once RabbitMQ is up, zero operator touch
        # (invariant I2). ``CancelledError`` is re-raised, never swallowed;
        # a missing URL / absent queue still surfaces as a RuntimeError.
        attempt = 0
        while True:
            try:
                await self._ensure_connected()
                break
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, TimeoutError, AMQPError) as exc:
                attempt += 1
                delay = min(2**attempt, self._CONNECT_BACKOFF_CAP_SECONDS)
                logger.warning(
                    "scan_audit_consumer.reconnect",
                    extra={
                        "attempt": attempt,
                        "delay_seconds": delay,
                        "reason": str(exc),
                    },
                )
                with contextlib.suppress(Exception):
                    await self.aclose()
                await asyncio.sleep(delay)
        assert self._queue is not None
        logger.info("scan_audit_consumer.run.start")
        try:
            async with self._queue.iterator() as it:
                async for message in it:
                    try:
                        await self._process_one(message)
                    except Exception:
                        # exc_info via logger.exception so the DB error (e.g.
                        # an RLS WITH CHECK rejection) renders in the JSON log.
                        # StructuredFormatter drops extra{} fields but keeps
                        # exception tracebacks, so extra={"reason": ...}
                        # previously masked this failure entirely. (#357)
                        logger.exception("scan_audit_consumer.process_failed")
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
