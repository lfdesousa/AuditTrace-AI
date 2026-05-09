"""Tests for routes/memory_upload/quarantine.py — PDF detection +
quarantine MinIO PUT helpers (ADR-048 PR-B3)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from audittrace.routes.memory_upload import quarantine as q


class TestIsPdfUpload:
    """PDF detection requires BOTH content-type AND magic bytes."""

    def test_real_pdf_passes(self) -> None:
        assert (
            q.is_pdf_upload(
                claimed_content_type="application/pdf",
                content=b"%PDF-1.7\nfake-pdf-bytes",
            )
            is True
        )

    def test_charset_suffix_still_pdf(self) -> None:
        # multipart often sends `application/pdf; charset=binary` — must
        # still match.
        assert (
            q.is_pdf_upload(
                claimed_content_type="application/pdf; charset=binary",
                content=b"%PDF-2.0\nx",
            )
            is True
        )

    def test_uppercase_content_type(self) -> None:
        assert (
            q.is_pdf_upload(
                claimed_content_type="APPLICATION/PDF",
                content=b"%PDF-1.4",
            )
            is True
        )

    def test_pdf_content_type_but_wrong_bytes(self) -> None:
        # spoofed content-type → reject (this is exactly the attack
        # surface ADR-048 closes).
        assert (
            q.is_pdf_upload(
                claimed_content_type="application/pdf",
                content=b"# Not a PDF, this is markdown\n",
            )
            is False
        )

    def test_pdf_bytes_but_wrong_content_type(self) -> None:
        assert (
            q.is_pdf_upload(
                claimed_content_type="text/markdown",
                content=b"%PDF-1.4 bytes",
            )
            is False
        )

    def test_no_content_type_at_all(self) -> None:
        assert (
            q.is_pdf_upload(
                claimed_content_type=None,
                content=b"%PDF-1.4",
            )
            is False
        )

    def test_empty_content(self) -> None:
        assert (
            q.is_pdf_upload(
                claimed_content_type="application/pdf",
                content=b"",
            )
            is False
        )


class TestSha256Hex:
    def test_sha256_hex_known_vector(self) -> None:
        # SHA-256("hello") = 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824
        assert q.sha256_hex(b"hello") == (
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        )

    def test_sha256_hex_empty(self) -> None:
        # Empty SHA-256 is a known vector
        assert q.sha256_hex(b"") == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )


class TestQuarantineKey:
    def test_key_layout(self) -> None:
        key = q.quarantine_key(
            user_id="alice",
            scan_id="123e4567-e89b-12d3-a456-426614174000",
            filename="paper.pdf",
        )
        assert key == "quarantine/alice/123e4567-e89b-12d3-a456-426614174000/paper.pdf"

    def test_uri_layout(self) -> None:
        uri = q.quarantine_uri(bucket="memory-shared", key="quarantine/x/y/z.pdf")
        assert uri == "s3://memory-shared/quarantine/x/y/z.pdf"


class TestPutQuarantine:
    """The PUT helper proxies to MinIO with the right (bucket,key,size,
    content-type) tuple and translates failures to 502."""

    def _settings(self) -> MagicMock:
        s = MagicMock()
        s.minio_shared_bucket = "memory-shared"
        return s

    def test_happy_path_calls_put_object(self) -> None:
        client = MagicMock()
        key, uri = q.put_quarantine(
            settings=self._settings(),
            minio_client=client,
            user_id="alice",
            scan_id="sid-1",
            filename="paper.pdf",
            content=b"%PDF-1.7 contents",
            content_type="application/pdf",
        )
        assert key == "quarantine/alice/sid-1/paper.pdf"
        assert uri == "s3://memory-shared/quarantine/alice/sid-1/paper.pdf"
        client.put_object.assert_called_once()
        # Positional args: bucket, key, body, length, content_type
        args = client.put_object.call_args
        assert args[0][0] == "memory-shared"
        assert args[0][1] == "quarantine/alice/sid-1/paper.pdf"
        assert args[1]["length"] == len(b"%PDF-1.7 contents")
        assert args[1]["content_type"] == "application/pdf"

    def test_minio_failure_raises_502(self) -> None:
        client = MagicMock()
        client.put_object.side_effect = OSError("connection refused")
        with pytest.raises(HTTPException) as exc_info:
            q.put_quarantine(
                settings=self._settings(),
                minio_client=client,
                user_id="alice",
                scan_id="sid-1",
                filename="paper.pdf",
                content=b"%PDF-",
                content_type="application/pdf",
            )
        assert exc_info.value.status_code == 502
