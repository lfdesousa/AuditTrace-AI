"""Tests for routes/memory_upload/handler.py — the PDF branch
that the existing /memory/upload route delegates to (ADR-048 PR-B3).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.identity import UserContext
from audittrace.routes.memory_upload.handler import handle_pdf_upload
from audittrace.routes.memory_upload.manifest import get_by_scan_id
from audittrace.services.scan_request_publisher import ScanRequestEnvelope


def _user(user_id: str = "alice") -> UserContext:
    return UserContext(
        user_id=user_id,
        username=user_id,
        agent_type="test",
        scopes=("memory:episodic:write",),
    )


def _settings(*, enabled: bool = True) -> MagicMock:
    s = MagicMock()
    s.scan_pipeline_enabled = enabled
    s.minio_shared_bucket = "memory-shared"
    return s


class TestPdfBranch:
    async def test_happy_path_returns_202_body(self) -> None:
        minio = MagicMock()
        factory = InMemoryPostgresFactory().get_session_factory()
        queue: asyncio.Queue[ScanRequestEnvelope] = asyncio.Queue()

        body = await handle_pdf_upload(
            settings=_settings(),
            minio_client=minio,
            session_factory=factory,
            queue=queue,
            user=_user(),
            filename="paper.pdf",
            content=b"%PDF-1.7 content bytes",
            content_type="application/pdf",
        )

        # Response shape
        assert body["status"] == "pending_scan"
        assert "scan_id" in body
        assert body["poll_url"] == f"/memory/upload/status?scan_id={body['scan_id']}"
        assert body["object_uri"].startswith("s3://memory-shared/quarantine/alice/")
        assert body["size_bytes"] == len(b"%PDF-1.7 content bytes")

        # Side effects: MinIO PUT, manifest INSERT, queue PUT.
        minio.put_object.assert_called_once()
        async with factory() as session:
            row = await get_by_scan_id(session, body["scan_id"])
        assert row is not None
        assert row.scan_status == "pending_scan"
        assert row.published_at_ms is None
        assert row.created_by_user_id == "alice"
        env = await queue.get()
        assert env.scan_id == body["scan_id"]
        assert env.user_id == "alice"
        assert env.size_bytes == len(b"%PDF-1.7 content bytes")
        assert env.claimed_content_type == "application/pdf"

    async def test_propagates_active_traceparent(self) -> None:
        """When an OTel span is active, the manifest row + envelope
        carry the W3C trace_id + traceparent header, so content-control
        can stitch the cross-service span tree."""
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.trace import TracerProvider

        # Wire a real provider so spans report a valid span_context.
        provider = TracerProvider()
        otel_trace.set_tracer_provider(provider)
        tracer = otel_trace.get_tracer(__name__)

        minio = MagicMock()
        factory = InMemoryPostgresFactory().get_session_factory()
        queue: asyncio.Queue[ScanRequestEnvelope] = asyncio.Queue()

        with tracer.start_as_current_span("test-upload"):
            body = await handle_pdf_upload(
                settings=_settings(),
                minio_client=minio,
                session_factory=factory,
                queue=queue,
                user=_user(),
                filename="x.pdf",
                content=b"%PDF-1.7 x",
                content_type="application/pdf",
            )
        env = await queue.get()
        # Both fields populated and the AMQP traceparent is the W3C shape.
        assert env.trace_id != ""
        assert env.traceparent.startswith("00-")
        # Manifest row carries the trace_id too.
        from audittrace.routes.memory_upload.manifest import get_by_scan_id

        async with factory() as session:
            row = await get_by_scan_id(session, body["scan_id"])
        assert row is not None
        assert row.trace_id == env.trace_id

    async def test_disabled_pipeline_returns_503(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await handle_pdf_upload(
                settings=_settings(enabled=False),
                minio_client=MagicMock(),
                session_factory=InMemoryPostgresFactory().get_session_factory(),
                queue=asyncio.Queue(),
                user=_user(),
                filename="x.pdf",
                content=b"%PDF-",
                content_type="application/pdf",
            )
        assert exc.value.status_code == 503
        assert "scan pipeline disabled" in exc.value.detail
