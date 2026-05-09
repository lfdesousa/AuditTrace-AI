"""PDF branch handler invoked by ``routes/memory.py:upload_memory_file``
when the uploaded content sniffs as a PDF.

Design choice (Luis 2026-05-10): keep `/memory/upload` as the
single entry-point so the WebUI / Bruno / scripts/index-chromadb.py
surface does not fork. This handler owns the PDF-only branch:
quarantine PUT + manifest INSERT + AMQP outbox enqueue + 202
response. Markdown / non-PDF uploads return through the existing
synchronous direct-PUT path in ``routes/memory.py``.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException
from opentelemetry import trace as otel_trace

from audittrace.routes.memory_upload import manifest as manifest_mod
from audittrace.routes.memory_upload import quarantine as quarantine_mod
from audittrace.services.scan_request_publisher import ScanRequestEnvelope

if TYPE_CHECKING:
    import asyncio

    from sqlalchemy.orm import Session, sessionmaker

    from audittrace.config import Settings
    from audittrace.identity import UserContext

logger = logging.getLogger(__name__)


def _current_traceparent() -> tuple[str, str]:
    """Snapshot the active OTel span's W3C trace context.

    Returns ``(trace_id_hex, traceparent_header)``. When no span
    is active (e.g. tracing disabled) returns ``("", "")``.
    """
    span = otel_trace.get_current_span()
    ctx = span.get_span_context()
    if ctx is None or not ctx.is_valid:
        return "", ""
    trace_id_hex = format(ctx.trace_id, "032x")
    span_id_hex = format(ctx.span_id, "016x")
    flags = format(ctx.trace_flags, "02x")
    traceparent = f"00-{trace_id_hex}-{span_id_hex}-{flags}"
    return trace_id_hex, traceparent


async def handle_pdf_upload(
    *,
    settings: Settings,
    minio_client: object,
    session_factory: sessionmaker[Session],
    queue: asyncio.Queue[ScanRequestEnvelope],
    user: UserContext,
    filename: str,
    content: bytes,
    content_type: str,
) -> dict[str, object]:
    """Run the PDF quarantine + 202 flow. Returns the JSON body
    the route hands back as HTTP 202.

    The caller (``routes/memory.py:upload_memory_file``) is
    responsible for setting the response status code via
    ``response.status_code = 202`` after this returns."""
    if not settings.scan_pipeline_enabled:
        # Cluster operator hasn't enabled the scan pipeline yet —
        # /memory/upload of PDFs would land in a manifest row that
        # never gets a verdict. Refuse cleanly.
        raise HTTPException(
            status_code=503,
            detail="scan pipeline disabled (AUDITTRACE_SCAN_PIPELINE_ENABLED=false)",
        )

    scan_id = str(uuid.uuid4())
    sha256 = quarantine_mod.sha256_hex(content)
    size_bytes = len(content)
    trace_id, traceparent = _current_traceparent()

    _key, uri = quarantine_mod.put_quarantine(
        settings=settings,
        minio_client=minio_client,
        user_id=user.user_id,
        scan_id=scan_id,
        filename=filename,
        content=content,
        content_type=content_type,
    )

    with session_factory() as session:
        manifest_mod.insert_pending_scan(
            session,
            scan_id=scan_id,
            user_id=user.user_id,
            object_uri=uri,
            object_sha256=sha256,
            size_bytes=size_bytes,
            title=filename,
            trace_id=trace_id,
        )

    envelope = ScanRequestEnvelope(
        scan_id=scan_id,
        user_id=user.user_id,
        trace_id=trace_id,
        object_uri=uri,
        object_sha256=sha256,
        size_bytes=size_bytes,
        claimed_content_type=content_type,
        traceparent=traceparent,
    )
    await queue.put(envelope)

    logger.info(
        "memory_upload.pdf.accepted",
        extra={
            "scan_id": scan_id,
            "user_id": user.user_id,
            "size_bytes": size_bytes,
            "object_sha256": sha256,
        },
    )
    return {
        "scan_id": scan_id,
        "status": "pending_scan",
        "poll_url": f"/memory/upload/status?scan_id={scan_id}",
        "object_uri": uri,
        "object_sha256": sha256,
        "size_bytes": size_bytes,
    }
