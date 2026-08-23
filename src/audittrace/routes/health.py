"""Health and metrics routes."""

import logging
import time
from typing import Any

from fastapi import APIRouter, Security
from sqlalchemy import func, select

from audittrace.auth import validate_jwt
from audittrace.config import Settings, get_settings
from audittrace.db.models import MemoryItem
from audittrace.logging_config import log_call
from audittrace.models import HealthResponse, MetricsResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# SPEC #387 P2b — the scan pipeline's DLQ queue name, per the ADR-057
# topology bootstrap (charts/audittrace/templates/rabbitmq/
# job-amqp-topology-bootstrap.yaml). Hardcoded here the same way
# ``ScanVerdictConsumer`` hardcodes its own queue-name default — this
# route module has no other reason to import the scan-pipeline
# services.
_SCAN_DLQ_QUEUE_NAME = "audittrace.scan.requests.dlq"
# Single attempt, short timeout. Unlike the scan pipeline's own eager
# producer connect (``ScanAmqpClient``, 8-attempt backoff budgeted
# against a 150s k8s startupProbe), a /health call must never block a
# readiness probe waiting out a broker that's actually down.
_SCAN_DLQ_AMQP_TIMEOUT_SECONDS = 3.0


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _async_persist_health_fields() -> dict[str, str]:
    """ADR-046 §7 — surface async-persist runtime state on /health.

    Returns a dict of string fields safe to merge into HealthResponse.
    Best-effort: any Redis-side failure degrades to ``"unknown"`` so a
    Redis blip doesn't fail the readiness probe.
    """
    settings = get_settings()
    fields: dict[str, str] = {
        "async_persist_enabled": str(settings.async_persist_enabled).lower(),
    }
    if not settings.async_persist_enabled:
        return fields
    try:
        from audittrace.services.async_persist import get_async_persist_redis

        redis = get_async_persist_redis()
        # XLEN of the DLQ — non-zero is alert-worthy.
        try:
            dlq_depth = await redis.xlen(settings.async_persist_dlq)
            fields["async_persist_dlq_depth"] = str(dlq_depth)
        except Exception:  # pragma: no cover - DLQ may not exist yet
            fields["async_persist_dlq_depth"] = "0"
        # Consumer lag = pending entries on the main stream.
        try:
            pending = await redis.xpending(  # type: ignore[no-untyped-call]
                settings.async_persist_stream, settings.async_persist_group
            )
            fields["async_persist_consumer_lag"] = str(pending.get("pending") or 0)
        except Exception:  # pragma: no cover - group may not exist yet
            fields["async_persist_consumer_lag"] = "unknown"
    except Exception:  # pragma: no cover - Redis unreachable
        fields["async_persist_dlq_depth"] = "unknown"
        fields["async_persist_consumer_lag"] = "unknown"
    return fields


async def _count_memory_items(session: Any, *predicates: Any, field_name: str) -> str:
    """One best-effort ``COUNT(*)`` against ``memory_items``.

    Isolated per-field (not one big try/except around all three DB
    fields) so a single bad predicate or a transient mid-batch DB
    failure degrades ONLY the affected field to ``"unknown"`` — the
    other counts on the same request still surface real values.
    """
    try:
        stmt = select(func.count(MemoryItem.id)).where(*predicates)
        result = (await session.execute(stmt)).scalar_one()
        return str(result)
    except Exception as exc:
        logger.warning(
            "health.scan_index_field_failed",
            extra={"field": field_name, "reason": str(exc)},
        )
        return "unknown"


async def _get_scan_dlq_message_count(settings: Settings) -> int:
    """The ADR-057 passive-declare AMQP round-trip against the scan
    pipeline's DLQ. Isolated as its own coroutine (not inlined into
    ``_scan_dlq_depth``) purely so tests can monkeypatch this seam
    directly rather than mocking aio_pika internals — mirrors how
    ``_async_persist_health_fields`` tests monkeypatch
    ``get_async_persist_redis`` at the call boundary. Raises on any
    connectivity/queue-declare failure; the caller degrades to
    ``"unknown"``.
    """
    import aio_pika  # noqa: PLC0415 — avoid import on the disabled path

    async with await aio_pika.connect(
        settings.scan_amqp_url, timeout=_SCAN_DLQ_AMQP_TIMEOUT_SECONDS
    ) as connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(_SCAN_DLQ_QUEUE_NAME, passive=True)
        message_count = queue.declaration_result.message_count
        return int(message_count) if message_count is not None else 0


async def _scan_dlq_depth(settings: Settings) -> str:
    """Best-effort scan-pipeline DLQ depth (ADR-057).

    Degrades to ``"unknown"`` when the scan pipeline itself is
    disabled/unconfigured (nothing to report on) OR the AMQP
    round-trip fails for any reason — a broker blip must never fail
    the readiness probe.
    """
    if not settings.scan_pipeline_enabled or not settings.scan_amqp_url:
        return "unknown"
    try:
        return str(await _get_scan_dlq_message_count(settings))
    except Exception as exc:
        logger.warning("health.scan_dlq_depth_failed", extra={"reason": str(exc)})
        return "unknown"


async def _scan_index_health_fields() -> dict[str, str]:
    """SPEC #387 P2b — surface scan/index pipeline backlog on /health.

    Mirrors ``_async_persist_health_fields``'s best-effort contract:
    any DB/AMQP failure degrades the AFFECTED field to ``"unknown"``,
    never the readiness probe itself. Four additive string fields
    merged into ``/health``'s open ``components`` map:

    * ``pending_scan_orphans`` — ``pending_scan`` rows stuck past the
      janitor's grace window (SCAN-URI-BUG companion signal).
    * ``unindexed_count`` — clean-scanned rows the index janitor
      hasn't drained yet (SPEC #387 outbox backlog).
    * ``dead_lettered_index_count`` — terminal index failures (SPEC
      #450; the target of the replay CLI, SPEC #387 P2a).
    * ``scan_dlq_depth`` — the scan pipeline's own RabbitMQ DLQ depth
      (ADR-057).
    """
    settings = get_settings()
    fields: dict[str, str] = {}

    session_factory = None
    try:
        from audittrace.dependencies import get_postgres_factory  # noqa: PLC0415

        session_factory = get_postgres_factory().get_session_factory()
    except Exception as exc:
        logger.warning("health.scan_index_db_unavailable", extra={"reason": str(exc)})

    if session_factory is None:
        fields["pending_scan_orphans"] = "unknown"
        fields["unindexed_count"] = "unknown"
        fields["dead_lettered_index_count"] = "unknown"
    else:
        cutoff = _now_ms() - (settings.scan_janitor_grace_seconds * 1000)
        try:
            # #364 session-scope discipline: the three counts are
            # captured into LOCAL names and folded into `fields` while
            # STILL inside the `async with` block, never after it
            # closes — `fields` itself is never assigned-to from a
            # session-bound expression, so it is safe to keep
            # populating (scan_dlq_depth) and return once the block
            # has exited.
            #
            # Reviewer finding #2 (2026-08-23): `_count_memory_items`
            # only guards the QUERY — opening the session itself
            # (``session_factory().__aenter__``) can also fail (pool
            # exhaustion, a genuinely unreachable DB despite
            # SQLAlchemy's lazy-connect usually masking this in dev).
            # This outer try/except wraps session OPEN + all three
            # queries + session CLOSE, so a failure at ANY of those
            # points still degrades all three DB-derived fields to
            # "unknown" instead of raising out of `/health`.
            async with session_factory() as session:
                pending_scan_orphans = await _count_memory_items(
                    session,
                    MemoryItem.scan_status == "pending_scan",
                    MemoryItem.created_at_ms < cutoff,
                    MemoryItem.deleted_at_ms.is_(None),
                    field_name="pending_scan_orphans",
                )
                unindexed_count = await _count_memory_items(
                    session,
                    MemoryItem.scan_status == "scanned_clean",
                    MemoryItem.indexed_at_ms.is_(None),
                    MemoryItem.index_failed_at_ms.is_(None),
                    MemoryItem.deleted_at_ms.is_(None),
                    field_name="unindexed_count",
                )
                dead_lettered_index_count = await _count_memory_items(
                    session,
                    MemoryItem.index_failed_at_ms.is_not(None),
                    MemoryItem.deleted_at_ms.is_(None),
                    field_name="dead_lettered_index_count",
                )
                fields["pending_scan_orphans"] = pending_scan_orphans
                fields["unindexed_count"] = unindexed_count
                fields["dead_lettered_index_count"] = dead_lettered_index_count
        except Exception as exc:
            logger.warning(
                "health.scan_index_session_open_failed", extra={"reason": str(exc)}
            )
            fields.setdefault("pending_scan_orphans", "unknown")
            fields.setdefault("unindexed_count", "unknown")
            fields.setdefault("dead_lettered_index_count", "unknown")

    fields["scan_dlq_depth"] = await _scan_dlq_depth(settings)
    return fields


@router.get("/health", response_model=HealthResponse)
@log_call(logger=logger)
async def health_check() -> HealthResponse:
    """Health check endpoint for Kubernetes/docker probes."""
    settings = get_settings()
    components: dict[str, str] = {
        "server": "running",
        "llama_url": settings.llama_url,
        "chroma_url": settings.chroma_url,
        # Backlog #10 — surface so an operator can see the summariser's
        # ctx ceiling in one curl, no kubectl exec needed.
        "summariser_ctx_tokens": str(settings.summarizer_ctx_tokens),
    }
    components.update(await _async_persist_health_fields())
    components.update(await _scan_index_health_fields())
    return HealthResponse(
        status="ok",
        version=__import__(
            "audittrace.server", fromlist=["_resolve_version"]
        )._resolve_version(),
        components=components,
    )


@router.get("/metrics", response_model=MetricsResponse)
@log_call(logger=logger)
async def metrics(
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["audittrace:admin"]),
) -> MetricsResponse:
    """Application-level metrics endpoint.

    Note: operation-level metrics (latency, error counts) are exported
    via OpenTelemetry — see ADR-014.4. This endpoint is a lightweight
    summary for ad-hoc checks.
    """
    return MetricsResponse(
        chroma_collections=0,
        total_chunks=0,
        active_sessions=0,
        uptime_seconds=0,
    )
