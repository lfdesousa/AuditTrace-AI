"""ADR-048 PR-B4 — verdict consumer.

Consumes the ``audittrace.scan.verdicts`` queue (also declared by the
PR-B2.5 topology Job, bound to the same-named exchange via routing
key ``scan.verdict.*``; #393 — this consumer self-declares the queue
idempotently too, so a cold boot no longer depends on winning a race
against the bootstrap Job). On each message:

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
       - On clean: ``indexed_at_ms`` reset to NULL — the SPEC #387
         Phase 1 (WU-1) auto-index outbox marker — and an
         ``IndexRequestEnvelope`` is enqueued onto ``index_queue`` so
         ``IndexWorker`` picks the object up with zero operator touch
         (closes GAP-1: previously nothing triggered indexing after a
         clean verdict at all).

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
import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import update

from audittrace.db.models import MemoryItem
from audittrace.services.index_routing import collection_for_key
from audittrace.services.index_worker import IndexRequestEnvelope

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


def _episodic_bare_key(
    *,
    prefix: str,
    scan_id: str,
    quarantine_key: str,
) -> str:
    """Bucket-relative form of the post-promotion episodic key.

    Pattern: ``{prefix}{scan_id}/{filename}``. The filename is the last
    path component of the quarantine URI (``s3://memory-shared/
    quarantine/<user>/<scan_id>/<filename>``). This is the SAME key
    ``minio_client.get_object(bucket, key)`` / ``_index_pdf_objects``
    expect — SPEC #387 (WU-1) uses it to build the ``IndexRequestEnvelope``
    enqueued on a clean verdict, so the outbox and the manifest's stored
    ``s3://`` URI (see :func:`_episodic_uri`) can never disagree on what
    the filename actually is.
    """
    filename = quarantine_key.rsplit("/", 1)[-1] or "object.bin"
    return f"{prefix}{scan_id}/{filename}"


def _episodic_uri(
    *,
    bucket: str,
    prefix: str,
    scan_id: str,
    quarantine_key: str,
) -> str:
    """Mirror of content-control's ``ScanWorker._episodic_uri``.

    Pattern: ``s3://{bucket}/{prefix}{scan_id}/{filename}`` — the full
    manifest URI, built from :func:`_episodic_bare_key`.
    """
    bare = _episodic_bare_key(
        prefix=prefix, scan_id=scan_id, quarantine_key=quarantine_key
    )
    return f"s3://{bucket}/{bare}"


class ScanVerdictConsumer:
    """aio_pika consumer for the verdict topic queue."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        queue_name: str = "audittrace.scan.verdicts",
        prefetch_count: int = 16,
        episodic_bucket: str = "memory-shared",
        episodic_prefix: str = "episodic/papers/",
        index_queue: asyncio.Queue[IndexRequestEnvelope] | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._queue_name = queue_name
        self._prefetch_count = prefetch_count
        self._episodic_bucket = episodic_bucket
        self._episodic_prefix = episodic_prefix
        # SPEC #387 Phase 1 (WU-1) — the auto-index outbox queue. ``None``
        # (the default) keeps this consumer usable standalone (existing
        # tests, and any caller that doesn't wire the index pipeline) —
        # the clean-verdict branch below simply skips enqueueing.
        self._index_queue = index_queue
        self._connection: Any = None
        self._channel: Any = None
        self._queue: Any = None

    # #393 — canonical queue-declare args. MUST match the
    # ``job-amqp-topology-bootstrap`` Job byte-for-byte
    # (``charts/audittrace/templates/rabbitmq/job-amqp-topology-bootstrap.yaml``,
    # the ``audittrace.scan.verdicts`` PUT body: ``{"auto_delete":false,
    # "durable":true,"arguments":{"x-queue-type":"quorum"}}``) — that
    # Job is the single source of truth for the topology. A mismatch
    # makes RabbitMQ raise ``ChannelPreconditionFailed`` on declare.
    _CANONICAL_QUEUE_ARGUMENTS: dict[str, str] = {"x-queue-type": "quorum"}

    # 2026-05-14 B4b — fresh-install race budget. The chart's
    # `job-amqp-topology-bootstrap` Job is a post-install hook, so on
    # a fresh kind cluster memory-server can come up BEFORE the
    # `audittrace.scan.verdicts` queue exists. Pre-#393, ``get_queue``
    # did a passive declare that raised ``ChannelNotFoundEntity`` if the
    # queue wasn't there yet, killing the channel; the consumer stayed
    # detached and verdict messages piled up with ``consumers=0`` until
    # the retry budget below happened to outlast the bootstrap Job.
    #
    # #393 FIX: switched to idempotent active ``declare_queue`` (mirrors
    # the exchange fix in ``ScanAmqpClient.ensure_connected``, PR-B10).
    # The retry loop below is now a guard against transient AMQP errors
    # while (re)opening the channel/declaring the queue, not the primary
    # mechanism for waiting out a not-yet-created queue — the consumer
    # declares the queue itself, so whichever side (this consumer or the
    # bootstrap Job) runs first wins and the other adopts the existing
    # queue (I2).
    _QUEUE_MAX_ATTEMPTS: int = 6

    # #384 WS3 — INITIAL-connect resilience. ``connect_robust`` was
    # previously called OUTSIDE the retry loop, so a cold-reboot RabbitMQ
    # (not AMQP-ready yet) raising a connection-level error
    # (``ConnectionResetError`` / ``AMQPConnectionError``) killed the
    # consumer task PERMANENTLY. Once the lifespan fail-open (WS3) removes
    # the container crash-loop crutch that used to restart everything,
    # that leaves ``consumers=0`` SILENTLY (violates invariant I2:
    # startup degradations self-heal zero-touch). The connect is now
    # wrapped in a capped-backoff retry, and ``run()`` supervises it so
    # the consumer waits RabbitMQ out and attaches with no operator touch.
    _CONNECT_MAX_ATTEMPTS: int = 6
    _CONNECT_BACKOFF_CAP_SECONDS: float = 30.0

    async def _connect_with_retry(
        self, aio_pika: Any, amqp_error_cls: type[BaseException]
    ) -> Any:
        """Open ``connect_robust`` with capped-backoff retry over
        connection-level errors (#384 WS3).

        ``connect_robust`` only auto-reconnects AFTER a first successful
        connection; the FIRST connect against a cold-reboot broker raises,
        so we retry it here. On exhaustion the last connection error is
        re-raised (not wrapped) so ``run()``'s supervised loop can catch it
        and keep retrying until RabbitMQ is up (invariant I2)."""
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
                    "scan_verdict_consumer.connect_retry",
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

        import aio_pika  # noqa: PLC0415 — avoid import on disabled paths
        from aio_pika.exceptions import AMQPError  # noqa: PLC0415

        # (C) connection-level retry — a cold-reboot RabbitMQ is not
        # AMQP-ready yet and ``connect_robust`` raises a connection-level
        # error. Wait it out with capped backoff instead of dying (#384 WS3).
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
                    "scan_verdict_consumer.queue_declare_retry",
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
                f"scan_verdict_consumer: queue {self._queue_name!r} declare "
                f"failed after {self._QUEUE_MAX_ATTEMPTS} attempts "
                f"(last error: {last_exc})"
            ) from last_exc
        logger.info(
            "scan_verdict_consumer.connected",
            extra={"queue": self._queue_name},
        )

    async def _apply_verdict(self, payload: dict[str, Any]) -> None:
        """Synchronous DB UPDATE — wrapped in to_thread by the
        caller (Danjou §3 — sync libs off the event loop).
        Raises on parse / DB errors so the AMQP CM nacks."""
        scan_id = payload["scan_id"]
        kind = payload["kind"]
        scan_status = _VERDICT_TO_SCAN_STATUS.get(kind)
        if scan_status is None:
            raise ValueError(f"unknown verdict kind: {kind!r}")

        async with self._session_factory() as session:
            row = await session.get(MemoryItem, scan_id)
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
            # Captured BEFORE commit — SPEC #387 (WU-1) needs these to
            # build the IndexRequestEnvelope, and an expired ORM attribute
            # read after commit (session-config-dependent) is a footgun
            # this sidesteps entirely.
            row_user_id = row.created_by_user_id
            row_trace_id = row.trace_id or ""

            updates: dict[str, Any] = {
                "scan_status": scan_status,
                "modified_at_ms": _now_ms(),
            }
            index_bare_key: str | None = None
            if kind == "clean":
                index_bare_key = _episodic_bare_key(
                    prefix=self._episodic_prefix,
                    scan_id=scan_id,
                    quarantine_key=row.key,
                )
                updates["key"] = f"s3://{self._episodic_bucket}/{index_bare_key}"
                # SPEC #387 Phase 1 (WU-1) — the auto-index outbox marker.
                # Explicit reset (not just "already NULL by default") so a
                # duplicate clean-verdict re-delivery for an
                # already-indexed row is idempotently re-queued rather
                # than silently trusting a stale timestamp.
                updates["indexed_at_ms"] = None
            await session.execute(
                update(MemoryItem).where(MemoryItem.id == scan_id).values(**updates)
            )
            await session.commit()
            logger.info(
                "scan_verdict_consumer.applied",
                extra={
                    "scan_id": scan_id,
                    "verdict": kind,
                    "scan_status": scan_status,
                },
            )

        # Enqueue OUTSIDE the session block — the outbox row is already
        # durably NULL-marked, so an enqueue failure (or a process crash
        # right here) is exactly the case IndexJanitor's grace-window
        # re-drive exists for (mirrors ScanRequestPublisher's shape).
        if (
            kind == "clean"
            and index_bare_key is not None
            and self._index_queue is not None
        ):
            await self._index_queue.put(
                IndexRequestEnvelope(
                    scan_id=scan_id,
                    key=index_bare_key,
                    collection=collection_for_key(index_bare_key),
                    user_id=row_user_id,
                    trace_id=row_trace_id,
                )
            )

    async def _process_one(self, message: Any) -> None:
        """Per-message handler. ``message.process(requeue=False)``
        wraps ack/nack — exceptions nack without re-queue, broker
        DLX kicks in after ``x-delivery-limit=5``."""
        async with message.process(requeue=False):
            payload = json.loads(message.body.decode("utf-8"))
            await self._apply_verdict(payload)

    async def run(self) -> None:
        from aio_pika.exceptions import AMQPError  # noqa: PLC0415

        # (C) Supervised connect — a consumer whose initial connect
        # ultimately fails must RETRY, not die permanently (invariant I2:
        # startup degradations self-heal zero-touch). Once RabbitMQ is up
        # the consumer attaches with no operator action. Only
        # connection-level failures are supervised here; a missing URL or a
        # genuinely-absent queue still surfaces as a RuntimeError.
        # ``CancelledError`` is re-raised, never swallowed.
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
                    "scan_verdict_consumer.reconnect",
                    extra={
                        "attempt": attempt,
                        "delay_seconds": delay,
                        "reason": str(exc),
                    },
                )
                # Reset any half-open connection before the next attempt.
                with contextlib.suppress(Exception):
                    await self.aclose()
                await asyncio.sleep(delay)
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
