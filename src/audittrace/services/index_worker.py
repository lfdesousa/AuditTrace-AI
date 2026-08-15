"""Auto-index outbox worker — SPEC #387 Phase 1 WU-2.

Drains the in-process queue that ``ScanVerdictConsumer`` (WU-1) feeds on
every ``scanned_clean`` verdict, running the SAME index logic
``POST /memory/index`` uses (``_index_pdf_objects`` — nomic embed, chunk,
upsert into ChromaDB, write the PDF manifest fields) directly, with no
HTTP round-trip. Mirrors ``ScanRequestPublisher``'s shape (Hohpe
Transactional Outbox + Danjou §1 producer task), one hop later:

    1. ``ScanVerdictConsumer._apply_verdict`` marks ``indexed_at_ms = NULL``
       and enqueues an ``IndexRequestEnvelope``.
    2. This worker drains the queue, embeds + upserts the chunks, and sets
       ``indexed_at_ms`` — idempotent (``WHERE indexed_at_ms IS NULL``).
    3. An embed/index failure is logged and the row is LEFT un-marked so
       ``IndexJanitor`` (WU-3) re-drives it past the grace window — the
       failure must surface, never be swallowed silently.

Failure semantics mirror the scan-request outbox 1:1: a broker/embed
hiccup leaves ``indexed_at_ms`` NULL; the janitor's grace-window
re-enqueue is the durability guarantee, not this worker's happy path.

SPEC #450 adds a THIRD outcome for the case above never modelled: a
failure that will never resolve no matter how many times the janitor
re-drives it (a structurally-corrupt PDF). ``_record_failure`` decides,
on every failed attempt, whether to dead-letter the row
(``index_failed_at_ms`` + ``index_failure_code``) — permanent-code
failures dead-letter on attempt 1, everything else dead-letters once
``Settings.index_max_attempts`` is reached. ``IndexJanitor`` excludes
dead-lettered rows from re-enqueue.

The actual "index this object" work is behind an injectable ``indexer``
callable — mirrors ``ScanRequestPublisher``'s injected ``amqp_client`` —
so the outbox semantics (mark-on-success / leave-NULL-on-failure) are
unit-testable without a live ChromaDB/MinIO/pymupdf stack; production
wires the real :func:`default_indexer`, which resolves the DI-container
clients LAZILY (first use, not worker construction) via the same
function-scoped-import discipline ``_index_pdf_objects`` itself already
uses to avoid a circular import with ``routes.memory``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from audittrace.db.models import MemoryItem
from audittrace.routes.memory_pdf.classification import (
    PERMANENT_INDEX_FAILURE_CODES,
)
from audittrace.services.index_routing import AI_RESEARCH_PAPERS_COLLECTION

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from audittrace.config import Settings

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class IndexRequestEnvelope:
    """In-process message handed from ``ScanVerdictConsumer`` (WU-1) to
    ``IndexWorker`` (this module), and re-synthesised by ``IndexJanitor``
    (WU-3) from a stuck manifest row. ``key`` is BUCKET-RELATIVE (not the
    manifest's stored ``s3://bucket/...`` URI — see
    ``index_routing.bare_key_from_uri``)."""

    scan_id: str
    key: str
    collection: str
    user_id: str
    trace_id: str


async def default_indexer(envelope: IndexRequestEnvelope, settings: Settings) -> bool:
    """Real indexing work for one envelope. Returns ``True`` on success.

    Resolves the DI-container clients (ChromaDB, object storage, the
    manifest service) and the heavy PDF pipeline LAZILY — only once an
    envelope actually drains, never at worker construction time. This
    matters for two reasons: it mirrors the ``POST /memory/index`` route
    handler's own per-call resolution (no stale client cached across a
    long-lived worker), and it means constructing an ``IndexWorker`` never
    requires the DI container to already be populated (some call sites —
    notably a targeted unit test of the scan-pipeline bootstrap — construct
    the pipeline before ``register_default_dependencies`` has run).
    """
    from audittrace.dependencies import (  # noqa: PLC0415
        get_chromadb,
        get_memory_manifest_service,
    )
    from audittrace.routes.memory import (  # noqa: PLC0415
        _get_minio_client,
        _physical_collection,
    )
    from audittrace.routes.memory_pdf.pipeline import (
        _index_pdf_objects,  # noqa: PLC0415
    )

    if envelope.collection != AI_RESEARCH_PAPERS_COLLECTION:
        # Defensive — the scan pipeline is PDF-only today (GAP-7, out of
        # scope for Phase 1), so `collection_for_key` should never route
        # an auto-index envelope anywhere else. Fail loud rather than
        # silently mis-index.
        logger.warning(
            "index_worker.unsupported_collection",
            extra={"scan_id": envelope.scan_id, "collection": envelope.collection},
        )
        return False

    bucket = (
        settings.aws_bucket
        if settings.object_storage_backend == "aws"
        else settings.minio_shared_bucket
    )
    chroma_client = get_chromadb()
    physical_col = _physical_collection(envelope.collection)
    collection = await chroma_client.get_or_create_collection(
        name=physical_col,
        embedding_function=None,
    )
    layer_prefix = envelope.key.split("/", 1)[0] + "/"
    outcomes: list[bool] = []
    await _index_pdf_objects(
        collection,
        _get_minio_client(),
        bucket,
        [{"key": envelope.key, "filename": envelope.key.rsplit("/", 1)[-1]}],
        envelope.collection,
        category=layer_prefix.rstrip("/"),
        layer_prefix=layer_prefix,
        user_id=envelope.user_id,
        ingestion_ts_ms=_now_ms(),
        manifest_service=get_memory_manifest_service(),
        outcomes=outcomes,
    )
    return bool(outcomes) and outcomes[-1]


class IndexWorker:
    """Background task that drains the index queue and indexes each object."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        queue: asyncio.Queue[IndexRequestEnvelope],
        indexer: Callable[[IndexRequestEnvelope, Settings], Awaitable[bool]]
        | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._queue = queue
        self._indexer = indexer or default_indexer

    async def _mark_indexed(self, scan_id: str) -> None:
        """Idempotent outbox-clear: ``WHERE indexed_at_ms IS NULL`` so a
        crash-restart race that re-indexes the same scan_id doesn't
        clobber an already-set timestamp (mirrors
        ``ScanRequestPublisher._publish_one``'s ``published_at_ms``
        update)."""
        try:
            async with self._session_factory() as session:
                await session.execute(
                    update(MemoryItem)
                    .where(MemoryItem.id == scan_id)
                    .where(MemoryItem.indexed_at_ms.is_(None))
                    .values(indexed_at_ms=_now_ms())
                )
                await session.commit()
        except Exception as exc:  # DB outage while marking — logged, not raised
            logger.error(
                "index_worker.outbox_update_failed",
                extra={"scan_id": scan_id, "reason": str(exc)},
            )

    async def _record_failure(self, envelope: IndexRequestEnvelope) -> None:
        """Terminal-state decision on a failed attempt (SPEC #450 WU-3).

        One session, mirroring ``_mark_indexed``'s single-session
        pattern: reads the row's ``extraction_warnings`` + current
        ``index_attempts``, increments the counter, and decides whether
        this failure is terminal:

          * any warning code in ``extraction_warnings`` is a member of
            ``PERMANENT_INDEX_FAILURE_CODES`` → dead-letter NOW, stamping
            ``index_failed_at_ms`` + that code (attempt 1 is enough — the
            pipeline classifies this exact failure identically on every
            retry, so retrying is pure waste).
          * otherwise, once ``index_attempts`` reaches
            ``settings.index_max_attempts`` → dead-letter with
            ``"max_attempts_exceeded"``.
          * otherwise → leave the terminal columns NULL; the row stays
            eligible for ``IndexJanitor`` re-enqueue (current behaviour).

        Reads the row **after** the pipeline's manifest flush
        (``routes/memory_pdf/pipeline.py``), which commits
        ``extraction_warnings`` in its own session before
        ``default_indexer`` returns ``ok=False`` — so this read always
        sees the current attempt's warnings, not a stale value.
        """
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(MemoryItem).where(MemoryItem.id == envelope.scan_id)
                    )
                ).scalar_one_or_none()
                if row is None:
                    logger.error(
                        "index_worker.outbox_update_failed",
                        extra={
                            "scan_id": envelope.scan_id,
                            "reason": "row_missing",
                        },
                    )
                    return
                attempts = (row.index_attempts or 0) + 1
                warning_codes: set[str] = {
                    w["code"]
                    for w in (row.extraction_warnings or [])
                    if w.get("code") is not None
                }
                permanent_hits = warning_codes & PERMANENT_INDEX_FAILURE_CODES
                code: str | None = None
                if permanent_hits:
                    code = min(permanent_hits)
                elif attempts >= self._settings.index_max_attempts:
                    code = "max_attempts_exceeded"
                await session.execute(
                    update(MemoryItem)
                    .where(MemoryItem.id == envelope.scan_id)
                    .values(
                        index_attempts=attempts,
                        index_failed_at_ms=_now_ms() if code else None,
                        index_failure_code=code,
                    )
                )
                await session.commit()
        except Exception as exc:  # DB outage while recording — logged, not raised
            logger.error(
                "index_worker.outbox_update_failed",
                extra={"scan_id": envelope.scan_id, "reason": str(exc)},
            )
            return
        if code:
            logger.warning(
                "index_worker.index_dead_lettered",
                extra={
                    "scan_id": envelope.scan_id,
                    "key": envelope.key,
                    "code": code,
                    "attempts": attempts,
                },
            )

    async def _index_one(self, envelope: IndexRequestEnvelope) -> None:
        """Index one object; mark ``indexed_at_ms`` ONLY on success.

        Any exception (embed-server outage, ChromaDB error, corrupt PDF)
        — or the indexer simply returning ``False`` (the PDF pipeline
        itself rejected the object: bomb-defense, encrypted, extension
        mismatch) — is caught/observed here and logged; the run loop
        continues with the next envelope. Every failure ALSO runs the
        SPEC #450 terminal-state decision (``_record_failure``): a
        permanently-unindexable object (or one that has exhausted the
        attempt cap) is dead-lettered and excluded from
        ``IndexJanitor`` re-enqueue; anything else keeps the row's
        un-marked ``indexed_at_ms`` as the durability hook the janitor
        reads. Never swallowed silently: every failure logs
        ``index_worker.index_failed`` with the reason (SPEC #387 WU-2 —
        "an embed/index failure must surface … leave the row un-marked
        for retry, do not swallow").
        """
        try:
            ok = await self._indexer(envelope, self._settings)
        except Exception as exc:  # broad: embed outage / Chroma / PDF-parse
            logger.warning(
                "index_worker.index_failed",
                extra={
                    "scan_id": envelope.scan_id,
                    "key": envelope.key,
                    "reason": str(exc),
                },
            )
            await self._record_failure(envelope)
            return
        if not ok:
            logger.warning(
                "index_worker.index_failed",
                extra={
                    "scan_id": envelope.scan_id,
                    "key": envelope.key,
                    "reason": "indexer returned ok=False",
                },
            )
            await self._record_failure(envelope)
            return
        await self._mark_indexed(envelope.scan_id)

    async def run(self) -> None:
        """Drain the queue forever. Cancellable via lifespan."""
        logger.info("index_worker.run.start")
        try:
            while True:
                envelope = await self._queue.get()
                try:
                    await self._index_one(envelope)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            logger.info("index_worker.run.cancelled")
            raise
