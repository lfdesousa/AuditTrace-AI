"""MEMORY-SERVER-SILENT-TRUNCATION ADDENDUM-2 (gate r2 S11): a PDF has its own
cap at UPLOAD, below the fold's bound, and an upload is held in ONE buffer."""

from __future__ import annotations

import asyncio
import io
import tracemalloc
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers, UploadFile

from audittrace.config import get_settings
from audittrace.routes import memory as m
from tests.memory_truncation_support import memory_client

MIB = 1024 * 1024


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("AUDITTRACE_PDF_UPLOAD_MAX_SIZE_MB", "1")
    # a text bound far below the file: a two-piece read would have to join them
    monkeypatch.setenv("AUDITTRACE_MEMORY_MAX_DOCUMENT_BYTES", "4096")
    get_settings.cache_clear()
    with memory_client() as c:
        yield c
    get_settings.cache_clear()


class TestPdfUploadCap:
    def test_a_pdf_over_the_upload_cap_is_refused_below_the_fold_bound(
        self, client: TestClient
    ) -> None:
        assert get_settings().pdf_max_size_mb > 1  # the fold's bound is untouched
        store = MagicMock()
        with patch.object(m, "_get_minio_client", return_value=store):
            refused = client.post(
                "/memory/upload",
                params={"layer": "episodic", "filename": "paper.pdf"},
                files={
                    "file": ("paper.pdf", b"%PDF-1.7\n" + b"x" * MIB, "application/pdf")
                },
            )

        assert refused.status_code == 413
        assert refused.json() == {
            "detail": f"memory document exceeds the limit of {MIB} bytes"
        }
        assert store.mock_calls == []

    def test_the_default_upload_cap_is_below_the_fold_bound(self) -> None:
        get_settings.cache_clear()
        settings = get_settings()

        assert settings.pdf_upload_max_size_mb == 64
        assert settings.pdf_upload_max_size_mb < settings.pdf_max_size_mb

    def test_an_upload_at_its_bound_is_held_in_one_buffer(
        self, client: TestClient
    ) -> None:
        """Allocation peak of the bounded read for a PDF exactly at its cap:
        one buffer of that size, not two (no join of two reads)."""
        from audittrace.routes.memory_write_refusal import read_upload_bounded

        body = b"%PDF-1.7\n" + b"x" * (MIB - 9)
        upload_file = UploadFile(
            io.BytesIO(body),
            filename="paper.pdf",
            headers=Headers({"content-type": "application/pdf"}),
        )
        tracemalloc.start()
        try:
            content = asyncio.run(read_upload_bounded(upload_file))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert content == body
        assert peak < 1.5 * len(body)
