"""Health and metrics routes."""

import logging
from typing import Any

from fastapi import APIRouter, Depends

from audittrace.auth import require_scope
from audittrace.config import get_settings
from audittrace.logging_config import log_call
from audittrace.models import HealthResponse, MetricsResponse

logger = logging.getLogger(__name__)

router = APIRouter()


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
    _auth: dict[str, Any] = Depends(require_scope("audittrace:admin")),
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
