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


@router.get("/health", response_model=HealthResponse)
@log_call(logger=logger)
async def health_check() -> HealthResponse:
    """Health check endpoint for Kubernetes/docker probes."""
    settings = get_settings()
    return HealthResponse(
        status="ok",
        version="0.3.1",
        components={
            "server": "running",
            "llama_url": settings.llama_url,
            "chroma_url": settings.chroma_url,
            # Backlog #10 — surface so an operator can see the summariser's
            # ctx ceiling in one curl, no kubectl exec needed.
            "summariser_ctx_tokens": str(settings.summarizer_ctx_tokens),
        },
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
