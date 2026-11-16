"""MEMORY-SERVER-SILENT-TRUNCATION ADDENDUM-1, WU-2 (gate r1 B3): a PDF is
bounded at UPLOAD by ``pdf_max_size_mb``, the bound its fold already applies,
and claiming to be a PDF does not lift the text bound."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from audittrace.config import get_settings
from audittrace.routes import memory as m
from tests.memory_truncation_support import memory_client

TEXT_LIMIT = 4096
PDF_LIMIT = 1024 * 1024
PDF = "application/pdf"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("AUDITTRACE_MEMORY_MAX_DOCUMENT_BYTES", str(TEXT_LIMIT))
    monkeypatch.setenv("AUDITTRACE_PDF_MAX_SIZE_MB", "1")
    get_settings.cache_clear()
    with memory_client() as c:
        yield c
    get_settings.cache_clear()


def _upload(
    client: TestClient, body: bytes, content_type: str, filename: str = "paper.pdf"
) -> tuple[Any, MagicMock]:
    store = MagicMock()
    with patch.object(m, "_get_minio_client", return_value=store):
        answer = client.post(
            "/memory/upload",
            params={"layer": "episodic", "filename": filename},
            files={"file": (filename, body, content_type)},
        )
    return answer, store


class TestPdfUploadBound:
    def test_a_pdf_one_byte_over_its_bound_is_refused_and_not_quarantined(
        self, client: TestClient
    ) -> None:
        body = b"%PDF-1.7\n" + b"x" * (PDF_LIMIT + 1 - 9)

        refused, store = _upload(client, body, PDF)

        assert refused.status_code == 413
        assert refused.json() == {
            "detail": f"memory document exceeds the limit of {PDF_LIMIT} bytes"
        }
        assert store.mock_calls == []

    def test_control_a_pdf_at_its_bound_reaches_the_pdf_pipeline(
        self, client: TestClient
    ) -> None:
        """The harness runs with the scan pipeline off, so the PDF branch
        answers 503: it was NOT refused for its size."""
        body = b"%PDF-1.7\n" + b"x" * (PDF_LIMIT - 9)

        answer, _ = _upload(client, body, PDF)

        assert answer.status_code == 503
        assert "scan pipeline disabled" in answer.json()["detail"]

    def test_claiming_pdf_without_the_magic_bytes_keeps_the_text_bound(
        self, client: TestClient
    ) -> None:
        refused, store = _upload(client, b"x" * (TEXT_LIMIT + 1), PDF, "paper.md")

        assert refused.status_code == 413
        assert refused.json() == {
            "detail": f"memory document exceeds the limit of {TEXT_LIMIT} bytes"
        }
        assert store.mock_calls == []
        assert client.get("/memory/episodic/paper.md").status_code == 404
