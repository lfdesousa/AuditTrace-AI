"""Tests for routes/memory_upload/manifest.py — pending-scan
INSERT + status SELECT (ADR-048 PR-B3)."""

from __future__ import annotations

import pytest

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.routes.memory_upload import manifest as manifest_mod


@pytest.fixture
def session_factory() -> object:
    return InMemoryPostgresFactory().get_session_factory()


class TestInsertPendingScan:
    def test_inserts_with_pending_scan_status(self, session_factory) -> None:
        with session_factory() as session:
            row = manifest_mod.insert_pending_scan(
                session,
                scan_id="scan-1",
                user_id="alice",
                object_uri="s3://memory-shared/quarantine/alice/scan-1/paper.pdf",
                object_sha256="0" * 64,
                size_bytes=12345,
                title="paper.pdf",
                trace_id="abc123",
            )
        assert row.id == "scan-1"
        assert row.scan_status == "pending_scan"
        assert row.published_at_ms is None
        assert row.layer == "episodic"
        assert row.created_by_user_id == "alice"
        assert row.modified_by_user_id == "alice"
        assert row.trace_id == "abc123"
        assert row.document_sha256 == "0" * 64
        assert row.size_bytes == 12345

    def test_published_at_ms_is_null_outbox_marker(self, session_factory) -> None:
        # The outbox pattern relies on this being NULL until the
        # publisher's UPDATE — janitor's WHERE clause would skip
        # the row otherwise.
        with session_factory() as session:
            row = manifest_mod.insert_pending_scan(
                session,
                scan_id="scan-2",
                user_id="alice",
                object_uri="s3://x/y",
                object_sha256="1" * 64,
                size_bytes=1,
                title="x",
                trace_id="",
            )
        assert row.published_at_ms is None


class TestGetByScanId:
    def test_returns_row_when_found(self, session_factory) -> None:
        with session_factory() as session:
            manifest_mod.insert_pending_scan(
                session,
                scan_id="scan-3",
                user_id="alice",
                object_uri="s3://x/y",
                object_sha256="2" * 64,
                size_bytes=1,
                title="x",
                trace_id="t",
            )
        with session_factory() as session:
            row = manifest_mod.get_by_scan_id(session, "scan-3")
        assert row is not None
        assert row.id == "scan-3"

    def test_returns_none_when_missing(self, session_factory) -> None:
        with session_factory() as session:
            assert manifest_mod.get_by_scan_id(session, "nope") is None
