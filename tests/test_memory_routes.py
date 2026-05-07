"""Tests for POST /memory/upload and POST /memory/index routes."""

from __future__ import annotations

from io import BytesIO
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_upload_file(
    content: bytes = b"# ADR-001\nSome content", filename: str = "ADR-001.md"
):
    """Return kwargs suitable for ``client.post(..., files=...)``."""
    return {"file": (filename, BytesIO(content), "text/markdown")}


# ── auth gate tests ─────────────────────────────────────────────────────────


class TestUploadAuth:
    """POST /memory/upload requires per-layer ``memory:<layer>:write``
    (or ``audittrace:admin``) matching the ``layer`` query parameter."""

    def test_upload_requires_token_no_token(self, client: TestClient) -> None:
        """Request without a bearer token is rejected when auth is enabled."""
        with patch("audittrace.auth.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                auth_enabled=True, auth_required=True
            )
            response = client.post(
                "/memory/upload",
                params={"layer": "episodic"},
                files=_make_upload_file(),
            )
        assert response.status_code == 401

    def test_upload_query_only_scope_is_rejected(self, client: TestClient) -> None:
        """A read-only token (``audittrace:query``) cannot upload — 403."""
        with (
            patch("audittrace.auth.get_settings") as mock_settings,
            patch("audittrace.auth._get_jwks_keys") as mock_jwks,
            patch("audittrace.auth._decode_jwt_with_allowed_issuers") as mock_decode,
        ):
            mock_settings.return_value = MagicMock(
                auth_enabled=True, auth_required=True
            )
            mock_jwks.return_value = ["fake-key"]
            mock_decode.return_value = {
                "sub": "test-user",
                "scope": "audittrace:query",
            }
            response = client.post(
                "/memory/upload",
                params={"layer": "episodic"},
                files=_make_upload_file(),
                headers={"Authorization": "Bearer fake-token"},
            )
        assert response.status_code == 403
        # The 403 names the missing scope so a UI client knows what to ask for.
        assert "memory:episodic:write" in response.json()["detail"]

    def test_upload_per_layer_write_succeeds(self, client: TestClient) -> None:
        """A token with ``memory:episodic:write`` can upload to ``layer=episodic``.

        This is the M3 LibreChat end-user flow: per-layer write replaces
        the old admin-only gate so UI sessions don't need broad admin tokens.
        """
        mock_minio = MagicMock()
        with (
            patch("audittrace.auth.get_settings") as mock_settings,
            patch("audittrace.auth._get_jwks_keys") as mock_jwks,
            patch("audittrace.auth._decode_jwt_with_allowed_issuers") as mock_decode,
            patch(
                "audittrace.routes.memory._get_minio_client", return_value=mock_minio
            ),
        ):
            mock_settings.return_value = MagicMock(
                auth_enabled=True, auth_required=True
            )
            mock_jwks.return_value = ["fake-key"]
            mock_decode.return_value = {
                "sub": "test-user",
                "scope": "memory:episodic:write",
            }
            response = client.post(
                "/memory/upload",
                params={"layer": "episodic"},
                files=_make_upload_file(b"hi", "doc.md"),
                headers={"Authorization": "Bearer fake-token"},
            )
        assert response.status_code == 200
        assert response.json()["key"] == "episodic/doc.md"

    def test_upload_cross_layer_denied(self, client: TestClient) -> None:
        """A ``memory:procedural:write`` token cannot upload to ``layer=episodic``.

        Cross-layer write must be denied: tokens are scoped per layer for
        a reason. This is the principal least-privilege check protecting
        the M3 UI flow.
        """
        with (
            patch("audittrace.auth.get_settings") as mock_settings,
            patch("audittrace.auth._get_jwks_keys") as mock_jwks,
            patch("audittrace.auth._decode_jwt_with_allowed_issuers") as mock_decode,
        ):
            mock_settings.return_value = MagicMock(
                auth_enabled=True, auth_required=True
            )
            mock_jwks.return_value = ["fake-key"]
            mock_decode.return_value = {
                "sub": "test-user",
                "scope": "memory:procedural:write",
            }
            response = client.post(
                "/memory/upload",
                params={"layer": "episodic"},
                files=_make_upload_file(),
                headers={"Authorization": "Bearer fake-token"},
            )
        assert response.status_code == 403
        assert "memory:episodic:write" in response.json()["detail"]

    def test_upload_admin_works_for_any_layer(self, client: TestClient) -> None:
        """``audittrace:admin`` continues to bypass the per-layer gate
        (operator path: bulk operations, scripted ingestion)."""
        mock_minio = MagicMock()
        with (
            patch("audittrace.auth.get_settings") as mock_settings,
            patch("audittrace.auth._get_jwks_keys") as mock_jwks,
            patch("audittrace.auth._decode_jwt_with_allowed_issuers") as mock_decode,
            patch(
                "audittrace.routes.memory._get_minio_client", return_value=mock_minio
            ),
        ):
            mock_settings.return_value = MagicMock(
                auth_enabled=True, auth_required=True
            )
            mock_jwks.return_value = ["fake-key"]
            mock_decode.return_value = {
                "sub": "ops-user",
                "scope": "audittrace:admin",
            }
            response = client.post(
                "/memory/upload",
                params={"layer": "procedural"},
                files=_make_upload_file(b"skill", "SKILL.md"),
                headers={"Authorization": "Bearer admin-token"},
            )
        assert response.status_code == 200


class TestIndexAuth:
    """POST /memory/index — bulk mode is admin-only; single-file mode
    requires per-layer ``memory:<layer>:write`` (or admin)."""

    def test_index_requires_token_no_token(self, client: TestClient) -> None:
        """Request without a bearer token is rejected when auth is enabled."""
        with patch("audittrace.auth.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                auth_enabled=True, auth_required=True
            )
            response = client.post("/memory/index")
        assert response.status_code == 401

    def test_index_bulk_requires_admin_per_layer_token_denied(
        self, client: TestClient
    ) -> None:
        """Bulk rebuild (no ?file=) is destructive whole-collection delete-and-
        recreate; cross-user by design. A ``memory:episodic:write`` token must
        not be able to drive it — admin only.
        """
        with (
            patch("audittrace.auth.get_settings") as mock_settings,
            patch("audittrace.auth._get_jwks_keys") as mock_jwks,
            patch("audittrace.auth._decode_jwt_with_allowed_issuers") as mock_decode,
        ):
            mock_settings.return_value = MagicMock(
                auth_enabled=True, auth_required=True
            )
            mock_jwks.return_value = ["fake-key"]
            mock_decode.return_value = {
                "sub": "test-user",
                "scope": "memory:episodic:write",
            }
            response = client.post(
                "/memory/index",
                headers={"Authorization": "Bearer fake-token"},
            )
        assert response.status_code == 403
        assert "audittrace:admin" in response.json()["detail"]

    def test_index_single_file_with_layer_write_succeeds(
        self, client: TestClient
    ) -> None:
        """Single-file mode (``?file=episodic/...``) accepts the matching
        per-layer write scope. Same M3 UI-flow contract as /memory/upload."""
        mock_minio = MagicMock()
        # Empty list_objects so the body short-circuits with no work — the
        # auth gate fires first regardless, which is what this test asserts.
        mock_minio.list_objects.return_value = iter([])
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection.return_value = MagicMock()
        with (
            patch("audittrace.auth.get_settings") as mock_settings,
            patch("audittrace.auth._get_jwks_keys") as mock_jwks,
            patch("audittrace.auth._decode_jwt_with_allowed_issuers") as mock_decode,
            patch(
                "audittrace.routes.memory._get_minio_client", return_value=mock_minio
            ),
            patch("audittrace.routes.memory.get_chromadb", return_value=mock_chroma),
        ):
            mock_settings.return_value = MagicMock(
                auth_enabled=True, auth_required=True
            )
            mock_jwks.return_value = ["fake-key"]
            mock_decode.return_value = {
                "sub": "test-user",
                "scope": "memory:episodic:write",
            }
            # Bytes for fetch_object — mock returns an empty body so the
            # PDF/text path early-returns with 0 chunks.
            mock_minio.get_object.return_value = MagicMock(
                read=lambda: b"", close=MagicMock(), release_conn=MagicMock()
            )
            response = client.post(
                "/memory/index",
                params={
                    "collections": "ai_research_papers",
                    "file": "episodic/foo.pdf",
                },
                headers={"Authorization": "Bearer fake-token"},
            )
        # Auth must have passed; whether the body succeeds or fails on the
        # empty-body path, the status must NOT be 403/401. Accept any
        # 2xx/4xx that isn't auth-related.
        assert response.status_code not in (401, 403), response.text

    def test_index_single_file_cross_layer_denied(self, client: TestClient) -> None:
        """A ``memory:procedural:write`` token cannot index a file from
        the episodic layer (``?file=episodic/...``). Symmetric with upload."""
        with (
            patch("audittrace.auth.get_settings") as mock_settings,
            patch("audittrace.auth._get_jwks_keys") as mock_jwks,
            patch("audittrace.auth._decode_jwt_with_allowed_issuers") as mock_decode,
        ):
            mock_settings.return_value = MagicMock(
                auth_enabled=True, auth_required=True
            )
            mock_jwks.return_value = ["fake-key"]
            mock_decode.return_value = {
                "sub": "test-user",
                "scope": "memory:procedural:write",
            }
            response = client.post(
                "/memory/index",
                params={
                    "collections": "ai_research_papers",
                    "file": "episodic/foo.pdf",
                },
                headers={"Authorization": "Bearer fake-token"},
            )
        assert response.status_code == 403
        assert "memory:episodic:write" in response.json()["detail"]


# ── upload behaviour ─────────────────────────────────────────────────────────


class TestUpload:
    """POST /memory/upload stores files in MinIO via the minio SDK."""

    def test_upload_stores_in_minio(self, client: TestClient) -> None:
        """Verify put_object is called with the correct bucket/key/body."""
        mock_minio = MagicMock()
        with patch(
            "audittrace.routes.memory._get_minio_client", return_value=mock_minio
        ):
            response = client.post(
                "/memory/upload",
                params={"layer": "episodic"},
                files=_make_upload_file(b"hello world", "ADR-042.md"),
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "uploaded"
        assert data["key"] == "episodic/ADR-042.md"
        assert data["size_bytes"] == len(b"hello world")
        assert data["bucket"] == "memory-shared"

        mock_minio.put_object.assert_called_once()
        call_args = mock_minio.put_object.call_args
        assert call_args[0][0] == "memory-shared"  # bucket
        assert call_args[0][1] == "episodic/ADR-042.md"  # key

    def test_upload_procedural_layer(self, client: TestClient) -> None:
        """Procedural layer routes to the procedural/ prefix."""
        mock_minio = MagicMock()
        with patch(
            "audittrace.routes.memory._get_minio_client", return_value=mock_minio
        ):
            response = client.post(
                "/memory/upload",
                params={"layer": "procedural"},
                files=_make_upload_file(b"skill doc", "SKILL-deploy.md"),
            )

        assert response.status_code == 200
        assert response.json()["key"] == "procedural/SKILL-deploy.md"

    def test_upload_with_filename_override(self, client: TestClient) -> None:
        """Explicit filename param overrides the upload filename."""
        mock_minio = MagicMock()
        with patch(
            "audittrace.routes.memory._get_minio_client", return_value=mock_minio
        ):
            response = client.post(
                "/memory/upload",
                params={"layer": "episodic", "filename": "ADR-099.md"},
                files=_make_upload_file(b"content", "original.md"),
            )

        assert response.status_code == 200
        assert response.json()["key"] == "episodic/ADR-099.md"

    def test_upload_rejects_invalid_layer(self, client: TestClient) -> None:
        """Unknown layer value yields 422 from Pydantic/FastAPI validation."""
        mock_minio = MagicMock()
        with patch(
            "audittrace.routes.memory._get_minio_client", return_value=mock_minio
        ):
            response = client.post(
                "/memory/upload",
                params={"layer": "bogus"},
                files=_make_upload_file(),
            )
        assert response.status_code == 422

    def test_upload_minio_failure_returns_502(self, client: TestClient) -> None:
        """When MinIO is unreachable the endpoint returns 502."""
        mock_minio = MagicMock()
        mock_minio.put_object.side_effect = Exception("connection refused")
        with patch(
            "audittrace.routes.memory._get_minio_client", return_value=mock_minio
        ):
            response = client.post(
                "/memory/upload",
                params={"layer": "episodic"},
                files=_make_upload_file(),
            )
        assert response.status_code == 502


# ── index behaviour ──────────────────────────────────────────────────────────


def _mock_minio_object(name: str) -> MagicMock:
    """Create a mock MinIO object with an object_name attribute."""
    obj = MagicMock()
    obj.object_name = name
    return obj


class TestIndex:
    """POST /memory/index reads from MinIO and writes to ChromaDB."""

    def _mock_minio_with_objects(self) -> MagicMock:
        """Build a mock MinIO client that returns a few objects."""
        mock_minio = MagicMock()

        def list_objects(bucket: str, prefix: str = "", **_kw: Any) -> list[Any]:
            if prefix == "episodic/":
                return [
                    _mock_minio_object("episodic/ADR-001.md"),
                    _mock_minio_object("episodic/ADR-002.md"),
                ]
            elif prefix == "procedural/":
                return [
                    _mock_minio_object("procedural/SKILL-deploy.md"),
                ]
            return []

        mock_minio.list_objects.side_effect = list_objects

        def get_object(bucket: str, key: str) -> MagicMock:
            response = MagicMock()
            response.read.return_value = b"# Test document\nSome content here."
            return response

        mock_minio.get_object.side_effect = get_object
        return mock_minio

    def test_index_reads_from_minio_and_writes_chromadb(
        self, client: TestClient
    ) -> None:
        """Full flow: list objects, read content, chunk, upsert to ChromaDB."""
        mock_minio = self._mock_minio_with_objects()
        mock_collection = MagicMock()
        mock_collection.count.return_value = 0
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection.return_value = mock_collection

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "decisions,skills"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "indexed"
        assert "decisions" in data["collections"]
        assert "skills" in data["collections"]
        assert data["total_chunks"] > 0
        assert data["duration_s"] >= 0

        # ChromaDB collection.upsert must have been called
        assert mock_collection.upsert.call_count >= 1

    def test_index_default_collections(self, client: TestClient) -> None:
        """Without the collections param, defaults to decisions/skills/semantic."""
        mock_minio = self._mock_minio_with_objects()
        mock_collection = MagicMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection.return_value = mock_collection

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
        ):
            response = client.post("/memory/index")

        assert response.status_code == 200
        data = response.json()
        assert set(data["collections"].keys()) == {
            "decisions",
            "skills",
            "semantic",
        }

    def test_index_empty_minio(self, client: TestClient) -> None:
        """When MinIO has no objects, index completes with 0 chunks."""
        mock_minio = MagicMock()
        mock_minio.list_objects.return_value = []
        mock_chroma = MagicMock()

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
        ):
            response = client.post("/memory/index")

        assert response.status_code == 200
        assert response.json()["total_chunks"] == 0


# ── chunking unit tests ─────────────────────────────────────────────────────


class TestChunking:
    """Unit tests for the _chunk_text helper."""

    def test_short_text_single_chunk(self) -> None:
        from audittrace.routes.memory import _chunk_text

        result = _chunk_text("hello", chunk_size=100, overlap=10)
        assert result == ["hello"]

    def test_long_text_multiple_chunks(self) -> None:
        from audittrace.routes.memory import _chunk_text

        text = "a" * 3000
        result = _chunk_text(text, chunk_size=1500, overlap=200)
        assert len(result) >= 2
        # Overlapping: second chunk starts 1300 chars in
        assert result[1][:10] == "a" * 10

    def test_whitespace_only_chunks_skipped(self) -> None:
        from audittrace.routes.memory import _chunk_text

        # Text that produces a trailing whitespace-only chunk
        text = "a" * 100 + "   "
        result = _chunk_text(text, chunk_size=100, overlap=10)
        # Only the non-whitespace chunk should survive
        assert all(c.strip() for c in result)


# ── _get_minio_client unit test ─────────────────────────────────────────────


class TestGetMinioClient:
    """Cover the _get_minio_client factory function."""

    def test_creates_minio_client(self) -> None:
        from audittrace.routes.memory import _get_minio_client

        with patch("audittrace.routes.memory.get_settings") as mock_gs:
            mock_gs.return_value = MagicMock(
                minio_url="http://minio:9000",
                minio_access_key="key",
                minio_secret_key="secret",
            )
            with patch("audittrace.routes.memory.Minio") as mock_minio:
                mock_minio.return_value = MagicMock()
                _get_minio_client()
                mock_minio.assert_called_once_with(
                    "minio:9000",
                    access_key="key",
                    secret_key="secret",
                    secure=False,
                )


# ── index error-path tests ──────────────────────────────────────────────────


class TestIndexErrorPaths:
    """Cover warning/error branches in the index endpoint."""

    def test_index_skips_failed_object_reads(self, client: TestClient) -> None:
        """When get_object raises, the object is skipped with a warning."""
        mock_minio = MagicMock()

        def list_objects(bucket: str, prefix: str = "", **_kw: Any) -> list[Any]:
            if prefix == "episodic/":
                return [_mock_minio_object("episodic/ADR-001.md")]
            return []

        mock_minio.list_objects.side_effect = list_objects
        mock_minio.get_object.side_effect = Exception("network error")

        mock_chroma = MagicMock()

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "decisions"},
            )

        assert response.status_code == 200
        # No chunks indexed because the read failed
        assert response.json()["collections"]["decisions"] == 0

    def test_index_skips_failed_procedural_reads(self, client: TestClient) -> None:
        """Procedural get_object failure is handled gracefully."""
        mock_minio = MagicMock()

        def list_objects(bucket: str, prefix: str = "", **_kw: Any) -> list[Any]:
            if prefix == "procedural/":
                return [_mock_minio_object("procedural/SKILL-test.md")]
            return []

        mock_minio.list_objects.side_effect = list_objects
        mock_minio.get_object.side_effect = Exception("timeout")

        mock_chroma = MagicMock()

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "skills"},
            )

        assert response.status_code == 200
        assert response.json()["collections"]["skills"] == 0

    def test_list_objects_uses_recursive_listing(self, client: TestClient) -> None:
        """MinIO's default ``list_objects`` returns only direct
        children of *prefix*. Nested files (the ai_research_papers
        corpus stores PDFs at e.g. ``episodic/papers/books/foo.pdf``)
        would silently return zero objects without ``recursive=True``.
        Caught live 2026-05-06: a /memory/index?collections=ai_research_papers
        returned 0 chunks even though 13 PDFs were sitting in MinIO."""
        mock_minio = MagicMock()
        mock_minio.list_objects.return_value = []
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection.return_value = MagicMock()

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
        ):
            client.post("/memory/index", params={"collections": "decisions"})

        # Every list_objects call must request recursive listing.
        assert mock_minio.list_objects.call_count >= 1
        for call in mock_minio.list_objects.call_args_list:
            assert call.kwargs.get("recursive") is True, (
                f"non-recursive list_objects call: {call!r}. "
                "Nested files (papers/, etc.) will be silently skipped."
            )

    def test_index_rejects_unknown_collection(self, client: TestClient) -> None:
        """`?collections=` validates against the known set; unknown
        names 400 with a clear message rather than silently no-op."""
        mock_minio = MagicMock()
        mock_minio.list_objects.return_value = []
        mock_chroma = MagicMock()

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "ai_research,decisions"},
            )

        assert response.status_code == 400
        assert "ai_research" in response.json()["detail"]

    def test_index_default_excludes_ai_research_papers(
        self, client: TestClient
    ) -> None:
        """ai_research_papers is opt-in only — must NOT appear in the
        default rebuild target set, otherwise routine /memory/index
        calls drag the 50 MB+ paper corpus through the embedder every
        time."""
        mock_minio = MagicMock()
        mock_minio.list_objects.return_value = []
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection.return_value = MagicMock()

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
        ):
            response = client.post("/memory/index")

        assert response.status_code == 200
        assert "ai_research_papers" not in response.json()["collections"]

    def test_index_ai_research_papers_extracts_pdf_pages(
        self, client: TestClient
    ) -> None:
        """ai_research_papers extracts text per-page from PDFs in
        episodic/ and indexes each page as one or more chunks. Skips
        non-PDF files so the same MinIO bucket can host both the
        legacy .md corpus and the paper corpus."""
        mock_minio = MagicMock()

        def list_objects(bucket: str, prefix: str = "", **_kw: Any) -> list[Any]:
            if prefix == "episodic/":
                return [
                    _mock_minio_object("episodic/papers/research/foo.pdf"),
                    _mock_minio_object("episodic/ADR-007.md"),  # must be skipped
                ]
            return []

        mock_minio.list_objects.side_effect = list_objects
        response_obj = MagicMock()
        response_obj.read.return_value = b"%PDF-1.4 ... pretend bytes"
        mock_minio.get_object.return_value = response_obj

        mock_collection = MagicMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection.return_value = mock_collection

        # Mock pymupdf — two pages with non-empty text. The route
        # uses ``with pymupdf.open(...) as doc:`` (deterministic
        # cleanup, see feedback_use_context_managers), so the mock
        # must support the context-manager protocol.
        fake_page_1 = MagicMock()
        fake_page_1.get_text.return_value = "Page one body text."
        fake_page_2 = MagicMock()
        fake_page_2.get_text.return_value = "Page two body text."
        fake_doc = MagicMock()
        fake_doc.__iter__.return_value = iter([fake_page_1, fake_page_2])
        fake_doc.__enter__.return_value = fake_doc
        fake_doc.__exit__.return_value = None
        # Bomb-defense caps (#18) read these properties — explicit
        # values keep MagicMock auto-comparison from tripping them.
        fake_doc.page_count = 2
        fake_doc.xref_length.return_value = 10
        fake_pymupdf = MagicMock()
        fake_pymupdf.open.return_value = fake_doc

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
            patch.dict(
                "sys.modules",
                {"pymupdf": fake_pymupdf},
            ),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "ai_research_papers"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        # 2 pages, each fits in one chunk → 2 chunks total. The .md
        # file MUST be skipped (only PDFs are indexed into this
        # collection).
        assert body["collections"]["ai_research_papers"] == 2
        # Verify metadata shape: one of the upsert() calls should
        # carry `page` and `source_key` fields. Upsert (not add) is
        # the idempotent path so per-file client loops can re-run.
        assert mock_collection.upsert.called
        call_kwargs = mock_collection.upsert.call_args.kwargs
        assert call_kwargs["metadatas"][0]["file_type"] == "pdf"
        assert call_kwargs["metadatas"][0]["page"] in (1, 2)
        assert call_kwargs["metadatas"][0]["source_key"] == "papers/research/foo.pdf"
        # PDF doc context manager should have exited exactly once
        # (one PDF processed). __exit__ replaces the prior explicit
        # .close() call now that the route uses `with`.
        assert fake_doc.__exit__.call_count == 1

    def test_index_single_file_mode_skips_minio_listing(
        self, client: TestClient
    ) -> None:
        """?file=<key> mode synthesises the object list from the
        provided key — no list_objects call. This is the contract
        that makes the per-file client loop bounded: one HTTP call
        ⇒ one MinIO read ⇒ one collection.upsert pass."""
        mock_minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = b"# A note\nbody"
        mock_minio.get_object.return_value = response_obj

        mock_collection = MagicMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection.return_value = mock_collection

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
        ):
            response = client.post(
                "/memory/index",
                params={
                    "collections": "decisions",
                    "file": "episodic/ADR-007.md",
                },
            )

        assert response.status_code == 200, response.text
        # No bucket-wide listing in single-file mode.
        mock_minio.list_objects.assert_not_called()
        # Collection is NOT delete-and-recreated.
        mock_chroma.delete_collection.assert_not_called()
        # Upsert (not add) is the idempotent path.
        mock_collection.upsert.assert_called()
        mock_collection.add.assert_not_called()
        # Exactly the one file was read.
        assert mock_minio.get_object.call_count == 1
        call_args = mock_minio.get_object.call_args
        assert call_args.args[1] == "episodic/ADR-007.md"

    def test_index_single_file_requires_one_collection(
        self, client: TestClient
    ) -> None:
        """?file= is per-collection — passing a comma-separated set
        is ambiguous (which file matches which collection?). 400."""
        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=MagicMock(),
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=MagicMock(),
            ),
        ):
            response = client.post(
                "/memory/index",
                params={
                    "collections": "decisions,skills",
                    "file": "episodic/foo.md",
                },
            )
        assert response.status_code == 400
        assert "exactly one collection" in response.json()["detail"]

    def test_index_single_file_validates_layer_prefix(self, client: TestClient) -> None:
        """?file= keys must live under episodic/ or procedural/.
        Anything else is 400 — defends against typos that would
        silently produce empty results."""
        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=MagicMock(),
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=MagicMock(),
            ),
        ):
            response = client.post(
                "/memory/index",
                params={
                    "collections": "decisions",
                    "file": "papers/foo.md",  # missing layer prefix
                },
            )
        assert response.status_code == 400
        # Detail is the new auth-gate message which lists the valid layer
        # prefixes from MemoryLayer enum (future-proof to additions).
        detail = response.json()["detail"]
        assert "episodic" in detail and "procedural" in detail

    def test_index_delete_collection_exception_swallowed(
        self, client: TestClient
    ) -> None:
        """delete_collection failure is swallowed so the create succeeds."""
        mock_minio = MagicMock()

        def list_objects(bucket: str, prefix: str = "", **_kw: Any) -> list[Any]:
            if prefix == "episodic/":
                return [_mock_minio_object("episodic/ADR-001.md")]
            return []

        mock_minio.list_objects.side_effect = list_objects

        response_obj = MagicMock()
        response_obj.read.return_value = b"# ADR\ncontent"
        mock_minio.get_object.return_value = response_obj

        mock_collection = MagicMock()
        mock_chroma = MagicMock()
        mock_chroma.delete_collection.side_effect = Exception("not found")
        mock_chroma.get_or_create_collection.return_value = mock_collection

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "decisions"},
            )

        assert response.status_code == 200
        assert response.json()["collections"]["decisions"] > 0
        mock_collection.upsert.assert_called()


class TestPdfProvenance:
    """Per-chunk provenance schema (gap-inventory item #21).

    Asserts that every PDF chunk carries the full provenance set:
    bbox_x0/y0/x1/y1, text_source, extraction_confidence,
    document_hash (sha256 of raw bytes), signature_status (placeholder
    until #12 lands), ingested_by_user_id, ingestion_ts_ms.

    Static defaults for text_source / confidence / signature_status
    are pinned here so future commits in the tier-A series surface
    in the diff when they flip these fields (#1 OCR, #12 signatures).
    """

    def test_pdf_chunks_carry_full_provenance_schema(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        """Every PDF chunk metadata dict has the 12 item-#21+#12 fields.
        Signature check is disabled here so ``signature_status`` is the
        deterministic ``"check_skipped"`` value — separate tests in
        ``TestPdfSignatureValidation`` cover the real signature paths.
        """
        from audittrace import config as config_mod

        monkeypatch.setenv("AUDITTRACE_PDF_SIGNATURE_CHECK_ENABLED", "false")
        config_mod.get_settings.cache_clear()
        import hashlib

        raw_bytes = b"%PDF-1.4 ... pretend bytes"
        expected_hash = hashlib.sha256(raw_bytes).hexdigest()

        mock_minio = MagicMock()

        def list_objects(bucket: str, prefix: str = "", **_kw: Any) -> list[Any]:
            if prefix == "episodic/":
                return [_mock_minio_object("episodic/papers/research/foo.pdf")]
            return []

        mock_minio.list_objects.side_effect = list_objects
        # _read_minio_object uses ``with client.get_object(...) as response``,
        # so the response mock must return itself from __enter__ for the
        # configured .read() bytes to actually flow through. Without this,
        # ``bytes(MagicMock())`` produces b'\\x00' and the document_hash
        # assertion below fails.
        response_obj = MagicMock()
        response_obj.read.return_value = raw_bytes
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj

        mock_collection = MagicMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection.return_value = mock_collection

        # Realistic page rect — US Letter portrait (612 × 792 pt).
        # Set rect attrs explicitly so float(rect.x0) returns the
        # actual page dimension instead of MagicMock's __float__
        # default of 1.0 — we want to verify _page_bbox extracts
        # the four floats in order.
        rect_mock = MagicMock(x0=0.0, y0=0.0, x1=612.0, y1=792.0)
        fake_page = MagicMock()
        fake_page.get_text.return_value = "Body text of page one."
        fake_page.rect = rect_mock
        fake_doc = MagicMock()
        fake_doc.__iter__.return_value = iter([fake_page])
        fake_doc.__enter__.return_value = fake_doc
        fake_doc.__exit__.return_value = None
        fake_doc.page_count = 1
        fake_doc.xref_length.return_value = 10
        fake_pymupdf = MagicMock()
        fake_pymupdf.open.return_value = fake_doc

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
            patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "ai_research_papers"},
            )

        assert response.status_code == 200, response.text
        assert mock_collection.upsert.called
        meta = mock_collection.upsert.call_args.kwargs["metadatas"][0]

        # Existing fields preserved (regression guard).
        assert meta["source"] == "foo.pdf"
        assert meta["source_key"] == "papers/research/foo.pdf"
        assert meta["category"] == "episodic"
        assert meta["file_type"] == "pdf"
        assert meta["page"] == 1
        assert meta["chunk"] == 0

        # Item #21 — bbox flattened (ChromaDB metadata is
        # str|int|float|bool only; tuples are not supported).
        assert meta["bbox_x0"] == 0.0
        assert meta["bbox_y0"] == 0.0
        assert meta["bbox_x1"] == 612.0
        assert meta["bbox_y1"] == 792.0

        # Item #21 — text-extraction provenance. Defaults pinned
        # for v1; #1 (OCR fallback) will flip text_source/confidence.
        assert meta["text_source"] == "native"
        assert meta["extraction_confidence"] == 1.0

        # Item #21 — document identity. SHA-256 of the raw bytes,
        # canonical for the entire downstream lifecycle.
        assert meta["document_hash"] == expected_hash
        assert len(meta["document_hash"]) == 64  # hex digest length

        # Item #12 — signature provenance. With the check explicitly
        # disabled at the top of this test, the helper returns the
        # deterministic ``"check_skipped"`` status.
        assert meta["signature_status"] == "check_skipped"

        # Item #21 — ingestion identity. user_id comes from
        # require_user (sentinel "audittrace-admin" in bypass mode);
        # ingestion_ts_ms is the wall clock at request entry.
        assert isinstance(meta["ingested_by_user_id"], str)
        assert meta["ingested_by_user_id"]  # non-empty
        assert isinstance(meta["ingestion_ts_ms"], int)
        assert meta["ingestion_ts_ms"] > 1_700_000_000_000  # post-2023

        # Cache hygiene — next test gets fresh Settings.
        config_mod.get_settings.cache_clear()

    def test_pdf_chunks_share_document_hash_and_ingestion_ts(
        self, client: TestClient
    ) -> None:
        """All chunks from one document share document_hash +
        ingestion_ts_ms — letting an auditor group "this index call
        produced these chunks" by exact match on either field."""
        mock_minio = MagicMock()

        def list_objects(bucket: str, prefix: str = "", **_kw: Any) -> list[Any]:
            if prefix == "episodic/":
                return [_mock_minio_object("episodic/papers/multi.pdf")]
            return []

        mock_minio.list_objects.side_effect = list_objects
        response_obj = MagicMock()
        response_obj.read.return_value = b"%PDF-1.4 multi-page bytes"
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj

        mock_collection = MagicMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection.return_value = mock_collection

        # Three pages with the same rect.
        rect = MagicMock(x0=0.0, y0=0.0, x1=595.0, y1=842.0)  # A4
        pages = []
        for i in range(3):
            p = MagicMock()
            p.get_text.return_value = f"Page {i + 1} body."
            p.rect = rect
            pages.append(p)
        fake_doc = MagicMock()
        fake_doc.__iter__.return_value = iter(pages)
        fake_doc.__enter__.return_value = fake_doc
        fake_doc.__exit__.return_value = None
        fake_doc.page_count = 3
        fake_doc.xref_length.return_value = 10
        fake_pymupdf = MagicMock()
        fake_pymupdf.open.return_value = fake_doc

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
            patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "ai_research_papers"},
            )

        assert response.status_code == 200, response.text
        # Each page calls _upsert_in_batches once (one chunk per page);
        # aggregate metadatas across all upsert calls.
        all_metas: list[dict[str, Any]] = []
        for call in mock_collection.upsert.call_args_list:
            all_metas.extend(call.kwargs["metadatas"])
        assert len(all_metas) == 3
        # Single value of document_hash + ingestion_ts_ms across all chunks.
        hashes = {m["document_hash"] for m in all_metas}
        ts = {m["ingestion_ts_ms"] for m in all_metas}
        assert len(hashes) == 1
        assert len(ts) == 1


class TestPdfBombDefense:
    """PDF bomb defenses (gap-inventory item #18).

    Four layers of guard, each catching a different bomb shape:
      1. Raw byte-size cap (rejects oversized files before parser load)
      2. Page-count + xref-count caps (rejects shape-bombs after open)
      3. Wall-clock timeout (page-boundary granularity)
      4. Per-page extracted-text-size cap (decompression-ratio defense)

    Each test sets the relevant cap to a tiny value via env var so the
    test PDF's mock values trigger rejection. Per the
    feedback_run_tests_before_commit pattern, monkeypatch.setenv +
    config.get_settings.cache_clear() is the canonical override.
    """

    @staticmethod
    def _build_minio_with_pdf(raw_bytes: bytes) -> Any:
        mock_minio = MagicMock()

        def list_objects(bucket: str, prefix: str = "", **_kw: Any) -> list[Any]:
            if prefix == "episodic/":
                return [_mock_minio_object("episodic/papers/research/foo.pdf")]
            return []

        mock_minio.list_objects.side_effect = list_objects
        response_obj = MagicMock()
        response_obj.read.return_value = raw_bytes
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj
        return mock_minio

    @staticmethod
    def _build_doc(page_count: int, xref_length: int, page_text: str) -> Any:
        rect = MagicMock(x0=0.0, y0=0.0, x1=612.0, y1=792.0)
        pages = []
        for _ in range(page_count):
            p = MagicMock()
            p.get_text.return_value = page_text
            p.rect = rect
            pages.append(p)
        fake_doc = MagicMock()
        fake_doc.__iter__.return_value = iter(pages)
        fake_doc.__enter__.return_value = fake_doc
        fake_doc.__exit__.return_value = None
        fake_doc.page_count = page_count
        fake_doc.xref_length.return_value = xref_length
        return fake_doc

    def test_oversized_file_rejected_before_parser_load(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        """Layer 1: file size > pdf_max_size_mb → reject before pymupdf.open."""
        from audittrace import config as config_mod

        # Cap to 0 MB so any non-empty file trips the gate.
        monkeypatch.setenv("AUDITTRACE_PDF_MAX_SIZE_MB", "0")
        config_mod.get_settings.cache_clear()
        try:
            mock_minio = self._build_minio_with_pdf(b"some bytes here")
            mock_collection = MagicMock()
            mock_chroma = MagicMock()
            mock_chroma.get_or_create_collection.return_value = mock_collection
            fake_pymupdf = MagicMock()

            with (
                patch(
                    "audittrace.routes.memory._get_minio_client",
                    return_value=mock_minio,
                ),
                patch(
                    "audittrace.routes.memory.get_chromadb",
                    return_value=mock_chroma,
                ),
                patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
            ):
                response = client.post(
                    "/memory/index",
                    params={"collections": "ai_research_papers"},
                )

            assert response.status_code == 200, response.text
            assert response.json()["collections"]["ai_research_papers"] == 0
            # pymupdf.open MUST NOT be called — layer 1 rejects before
            # the parser is even instantiated.
            assert not fake_pymupdf.open.called
            assert not mock_collection.upsert.called
        finally:
            config_mod.get_settings.cache_clear()

    def test_too_many_pages_rejected(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        """Layer 2: doc.page_count > pdf_max_pages → reject the file."""
        from audittrace import config as config_mod

        monkeypatch.setenv("AUDITTRACE_PDF_MAX_PAGES", "1")
        config_mod.get_settings.cache_clear()
        try:
            mock_minio = self._build_minio_with_pdf(b"%PDF-1.4")
            mock_collection = MagicMock()
            mock_chroma = MagicMock()
            mock_chroma.get_or_create_collection.return_value = mock_collection

            # Doc claims 2 pages; cap is 1 → reject.
            fake_doc = self._build_doc(page_count=2, xref_length=10, page_text="body")
            fake_pymupdf = MagicMock()
            fake_pymupdf.open.return_value = fake_doc

            with (
                patch(
                    "audittrace.routes.memory._get_minio_client",
                    return_value=mock_minio,
                ),
                patch(
                    "audittrace.routes.memory.get_chromadb",
                    return_value=mock_chroma,
                ),
                patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
            ):
                response = client.post(
                    "/memory/index",
                    params={"collections": "ai_research_papers"},
                )

            assert response.status_code == 200, response.text
            assert response.json()["collections"]["ai_research_papers"] == 0
            # No page was iterated — the doc was rejected after open.
            assert not mock_collection.upsert.called
        finally:
            config_mod.get_settings.cache_clear()

    def test_too_many_xrefs_rejected(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        """Layer 2: doc.xref_length > pdf_max_xref_count → reject."""
        from audittrace import config as config_mod

        monkeypatch.setenv("AUDITTRACE_PDF_MAX_XREF_COUNT", "5")
        config_mod.get_settings.cache_clear()
        try:
            mock_minio = self._build_minio_with_pdf(b"%PDF-1.4")
            mock_collection = MagicMock()
            mock_chroma = MagicMock()
            mock_chroma.get_or_create_collection.return_value = mock_collection

            fake_doc = self._build_doc(page_count=1, xref_length=100, page_text="body")
            fake_pymupdf = MagicMock()
            fake_pymupdf.open.return_value = fake_doc

            with (
                patch(
                    "audittrace.routes.memory._get_minio_client",
                    return_value=mock_minio,
                ),
                patch(
                    "audittrace.routes.memory.get_chromadb",
                    return_value=mock_chroma,
                ),
                patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
            ):
                response = client.post(
                    "/memory/index",
                    params={"collections": "ai_research_papers"},
                )

            assert response.status_code == 200, response.text
            assert response.json()["collections"]["ai_research_papers"] == 0
            assert not mock_collection.upsert.called
        finally:
            config_mod.get_settings.cache_clear()

    def test_parse_timeout_breaks_page_loop(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        """Layer 3: pdf_parse_timeout_seconds=0 → first page check trips."""
        from audittrace import config as config_mod

        monkeypatch.setenv("AUDITTRACE_PDF_PARSE_TIMEOUT_SECONDS", "0")
        config_mod.get_settings.cache_clear()
        try:
            mock_minio = self._build_minio_with_pdf(b"%PDF-1.4")
            mock_collection = MagicMock()
            mock_chroma = MagicMock()
            mock_chroma.get_or_create_collection.return_value = mock_collection

            fake_doc = self._build_doc(page_count=10, xref_length=10, page_text="body")
            fake_pymupdf = MagicMock()
            fake_pymupdf.open.return_value = fake_doc

            with (
                patch(
                    "audittrace.routes.memory._get_minio_client",
                    return_value=mock_minio,
                ),
                patch(
                    "audittrace.routes.memory.get_chromadb",
                    return_value=mock_chroma,
                ),
                patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
            ):
                response = client.post(
                    "/memory/index",
                    params={"collections": "ai_research_papers"},
                )

            assert response.status_code == 200, response.text
            # With timeout=0, the budget check at top of the first
            # page-iteration trips — no pages indexed.
            assert response.json()["collections"]["ai_research_papers"] == 0
            assert not mock_collection.upsert.called
        finally:
            config_mod.get_settings.cache_clear()

    def test_oversized_page_text_skipped_but_other_pages_indexed(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        """Layer 4: per-page text > pdf_max_page_text_bytes → skip page,
        keep processing others. One bad page in an otherwise legit doc
        is rare but plausible — abort-the-whole-file is too aggressive."""
        from audittrace import config as config_mod

        monkeypatch.setenv("AUDITTRACE_PDF_MAX_PAGE_TEXT_BYTES", "100")
        config_mod.get_settings.cache_clear()
        try:
            mock_minio = self._build_minio_with_pdf(b"%PDF-1.4")
            mock_collection = MagicMock()
            mock_chroma = MagicMock()
            mock_chroma.get_or_create_collection.return_value = mock_collection

            rect = MagicMock(x0=0.0, y0=0.0, x1=612.0, y1=792.0)
            small_page = MagicMock()
            small_page.get_text.return_value = "tiny body text"  # under cap
            small_page.rect = rect
            big_page = MagicMock()
            big_page.get_text.return_value = "x" * 1000  # over cap (100)
            big_page.rect = rect
            fake_doc = MagicMock()
            fake_doc.__iter__.return_value = iter([small_page, big_page])
            fake_doc.__enter__.return_value = fake_doc
            fake_doc.__exit__.return_value = None
            fake_doc.page_count = 2
            fake_doc.xref_length.return_value = 10
            fake_pymupdf = MagicMock()
            fake_pymupdf.open.return_value = fake_doc

            with (
                patch(
                    "audittrace.routes.memory._get_minio_client",
                    return_value=mock_minio,
                ),
                patch(
                    "audittrace.routes.memory.get_chromadb",
                    return_value=mock_chroma,
                ),
                patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
            ):
                response = client.post(
                    "/memory/index",
                    params={"collections": "ai_research_papers"},
                )

            assert response.status_code == 200, response.text
            # Small page indexed (1 chunk); big page skipped.
            assert response.json()["collections"]["ai_research_papers"] == 1
            assert mock_collection.upsert.call_count == 1
        finally:
            config_mod.get_settings.cache_clear()


class TestPdfRedactions:
    """Unflattened redaction handling (gap-inventory item #8).

    The default policy is ``reject`` — any page with a redaction
    annotation aborts the whole file. Auditors get a structured log
    line; the corpus stays clean. ``clip-extract`` (env override) is
    for advanced operators who explicitly want partial content from
    redacted documents.
    """

    @staticmethod
    def _mk_redact_annot(rect_tuple: tuple[float, float, float, float]) -> Any:
        """Build a fake pymupdf-annot with redaction subtype."""
        annot = MagicMock()
        annot.type = (12, "Redact")  # PDF_ANNOT_REDACT == 12
        rect = MagicMock(
            x0=rect_tuple[0],
            y0=rect_tuple[1],
            x1=rect_tuple[2],
            y1=rect_tuple[3],
        )
        annot.rect = rect
        return annot

    @staticmethod
    def _mk_minio_with_one_pdf() -> Any:
        mock_minio = MagicMock()

        def list_objects(bucket: str, prefix: str = "", **_kw: Any) -> list[Any]:
            if prefix == "episodic/":
                return [_mock_minio_object("episodic/papers/research/foo.pdf")]
            return []

        mock_minio.list_objects.side_effect = list_objects
        response_obj = MagicMock()
        response_obj.read.return_value = b"%PDF-1.4 redacted bytes"
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj
        return mock_minio

    def test_redactions_reject_default_policy(self, client: TestClient) -> None:
        """Default policy=reject: whole document is skipped on first
        redaction-bearing page; no chunks are emitted."""
        mock_minio = self._mk_minio_with_one_pdf()
        mock_collection = MagicMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection.return_value = mock_collection

        rect_mock = MagicMock(x0=0.0, y0=0.0, x1=612.0, y1=792.0)
        redact_annot = self._mk_redact_annot((100.0, 100.0, 200.0, 200.0))
        fake_page = MagicMock()
        fake_page.get_text.return_value = "would-be body text"
        fake_page.rect = rect_mock
        fake_page.annots.return_value = [redact_annot]
        fake_doc = MagicMock()
        fake_doc.__iter__.return_value = iter([fake_page])
        fake_doc.__enter__.return_value = fake_doc
        fake_doc.__exit__.return_value = None
        fake_doc.page_count = 1
        fake_doc.xref_length.return_value = 10
        fake_pymupdf = MagicMock()
        fake_pymupdf.open.return_value = fake_doc

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
            patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "ai_research_papers"},
            )

        assert response.status_code == 200, response.text
        # Whole file rejected — zero chunks indexed.
        assert response.json()["collections"]["ai_research_papers"] == 0
        assert not mock_collection.upsert.called

    def test_redactions_clip_extract_drops_intersecting_blocks(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        """policy=clip-extract: blocks intersecting any redaction rect
        are dropped; surviving blocks are joined and indexed with
        redaction_status='clipped'."""
        from audittrace import config as config_mod

        monkeypatch.setenv("AUDITTRACE_PDF_REDACTION_POLICY", "clip-extract")
        config_mod.get_settings.cache_clear()
        try:
            mock_minio = self._mk_minio_with_one_pdf()
            mock_collection = MagicMock()
            mock_chroma = MagicMock()
            mock_chroma.get_or_create_collection.return_value = mock_collection

            rect_mock = MagicMock(x0=0.0, y0=0.0, x1=612.0, y1=792.0)
            # Redaction at (100,100)-(200,200).
            redact_annot = self._mk_redact_annot((100.0, 100.0, 200.0, 200.0))

            # Two blocks: one outside the redaction (will survive),
            # one inside (will be dropped).
            def fake_get_text(*args: Any, **_kw: Any) -> Any:
                if args and args[0] == "blocks":
                    return [
                        # Block 0 — outside redaction → survives.
                        (0.0, 0.0, 50.0, 50.0, "Surviving content.", 0, 0),
                        # Block 1 — inside redaction → dropped.
                        (110.0, 110.0, 190.0, 190.0, "Redacted content.", 1, 0),
                    ]
                return "(unused — clip path uses 'blocks' mode)"

            fake_page = MagicMock()
            fake_page.get_text.side_effect = fake_get_text
            fake_page.rect = rect_mock
            fake_page.annots.return_value = [redact_annot]
            fake_doc = MagicMock()
            fake_doc.__iter__.return_value = iter([fake_page])
            fake_doc.__enter__.return_value = fake_doc
            fake_doc.__exit__.return_value = None
            fake_doc.page_count = 1
            fake_doc.xref_length.return_value = 10
            fake_pymupdf = MagicMock()
            fake_pymupdf.open.return_value = fake_doc

            with (
                patch(
                    "audittrace.routes.memory._get_minio_client",
                    return_value=mock_minio,
                ),
                patch(
                    "audittrace.routes.memory.get_chromadb",
                    return_value=mock_chroma,
                ),
                patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
            ):
                response = client.post(
                    "/memory/index",
                    params={"collections": "ai_research_papers"},
                )

            assert response.status_code == 200, response.text
            # One chunk indexed (only the surviving block).
            assert response.json()["collections"]["ai_research_papers"] == 1
            assert mock_collection.upsert.called
            call_kwargs = mock_collection.upsert.call_args.kwargs
            assert "Surviving content." in call_kwargs["documents"][0]
            assert "Redacted content." not in call_kwargs["documents"][0]
            # Schema check: redaction_status="clipped" on the chunk.
            assert call_kwargs["metadatas"][0]["redaction_status"] == "clipped"
        finally:
            config_mod.get_settings.cache_clear()

    def test_no_redactions_marks_status_none(self, client: TestClient) -> None:
        """Page with zero redaction annotations: chunk metadata has
        redaction_status='none' (the v1 default)."""
        mock_minio = self._mk_minio_with_one_pdf()
        mock_collection = MagicMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection.return_value = mock_collection

        rect_mock = MagicMock(x0=0.0, y0=0.0, x1=612.0, y1=792.0)
        fake_page = MagicMock()
        fake_page.get_text.return_value = "Clean page body."
        fake_page.rect = rect_mock
        fake_page.annots.return_value = []  # no annotations at all
        fake_doc = MagicMock()
        fake_doc.__iter__.return_value = iter([fake_page])
        fake_doc.__enter__.return_value = fake_doc
        fake_doc.__exit__.return_value = None
        fake_doc.page_count = 1
        fake_doc.xref_length.return_value = 10
        fake_pymupdf = MagicMock()
        fake_pymupdf.open.return_value = fake_doc

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
            patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "ai_research_papers"},
            )

        assert response.status_code == 200, response.text
        assert response.json()["collections"]["ai_research_papers"] == 1
        meta = mock_collection.upsert.call_args.kwargs["metadatas"][0]
        assert meta["redaction_status"] == "none"

    def test_unknown_redaction_policy_rejects_for_safety(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        """A misconfigured policy value (typo, env-var leak) MUST NOT
        silently leak redacted content. Reject the document instead."""
        from audittrace import config as config_mod

        monkeypatch.setenv("AUDITTRACE_PDF_REDACTION_POLICY", "warn-and-skip")
        config_mod.get_settings.cache_clear()
        try:
            mock_minio = self._mk_minio_with_one_pdf()
            mock_collection = MagicMock()
            mock_chroma = MagicMock()
            mock_chroma.get_or_create_collection.return_value = mock_collection

            rect_mock = MagicMock(x0=0.0, y0=0.0, x1=612.0, y1=792.0)
            redact_annot = self._mk_redact_annot((100.0, 100.0, 200.0, 200.0))
            fake_page = MagicMock()
            fake_page.get_text.return_value = "would-be body"
            fake_page.rect = rect_mock
            fake_page.annots.return_value = [redact_annot]
            fake_doc = MagicMock()
            fake_doc.__iter__.return_value = iter([fake_page])
            fake_doc.__enter__.return_value = fake_doc
            fake_doc.__exit__.return_value = None
            fake_doc.page_count = 1
            fake_doc.xref_length.return_value = 10
            fake_pymupdf = MagicMock()
            fake_pymupdf.open.return_value = fake_doc

            with (
                patch(
                    "audittrace.routes.memory._get_minio_client",
                    return_value=mock_minio,
                ),
                patch(
                    "audittrace.routes.memory.get_chromadb",
                    return_value=mock_chroma,
                ),
                patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
            ):
                response = client.post(
                    "/memory/index",
                    params={"collections": "ai_research_papers"},
                )

            assert response.status_code == 200, response.text
            # Misconfig → safe-by-default reject.
            assert response.json()["collections"]["ai_research_papers"] == 0
            assert not mock_collection.upsert.called
        finally:
            config_mod.get_settings.cache_clear()


class TestPdfSignatureValidation:
    """PDF signature validation (gap-inventory item #12).

    Tests the ``_pdf_signature_status`` helper directly across the full
    status taxonomy (check_skipped / none / signed_valid /
    signed_invalid / signed_tampered / check_failed). Plus a smoke
    test that runs the helper against a real unsigned PDF generated
    in-memory via pymupdf — catches contract drift between this code
    and pyhanko's ``embedded_signatures`` / ``PdfSignatureStatus``
    APIs without committing a signed-PDF binary fixture to the repo.

    Detect-and-record only in v1: every status is recorded on every
    chunk; nothing rejects on signature failure. Reject-on-invalid is
    a future revision.
    """

    def test_signature_check_disabled_returns_check_skipped(self) -> None:
        from audittrace.routes.memory import _pdf_signature_status

        status, count = _pdf_signature_status(
            b"%PDF-1.4 ignored", enabled=False, trust_store_path=""
        )
        assert status == "check_skipped"
        assert count == 0

    def test_pdf_with_no_signatures_returns_none_real_pdf(self) -> None:
        """Real pyhanko + real (unsigned) PDF generated via pymupdf.
        Smoke test for contract drift between our helper and pyhanko's
        ``embedded_signatures`` field — runs every CI pass."""
        import pymupdf  # type: ignore[import-untyped]

        from audittrace.routes.memory import _pdf_signature_status

        # Build a one-page PDF in-memory; no signatures.
        doc = pymupdf.open()
        doc.new_page()
        raw = doc.tobytes()
        doc.close()

        status, count = _pdf_signature_status(raw, enabled=True, trust_store_path="")
        assert status == "none"
        assert count == 0

    def test_signed_valid_returns_signed_valid(self) -> None:
        """Mock pyhanko: one signature, all checks pass."""
        from audittrace.routes.memory import _pdf_signature_status

        fake_reader = MagicMock()
        fake_reader.embedded_signatures = [MagicMock()]
        fake_status = MagicMock(intact=True, valid=True, trusted=True)

        with (
            patch(
                "pyhanko.pdf_utils.reader.PdfFileReader",
                return_value=fake_reader,
            ),
            patch(
                "pyhanko.sign.validation.validate_pdf_signature",
                return_value=fake_status,
            ),
        ):
            status, count = _pdf_signature_status(
                b"%PDF-1.4 ignored", enabled=True, trust_store_path=""
            )
        assert status == "signed_valid"
        assert count == 1

    def test_signed_invalid_returns_signed_invalid(self) -> None:
        """Signature exists, content intact, but cert chain not
        trusted (or signature math broken). Distinct from tampering —
        the document hasn't been modified, but the auth claim is
        unverifiable."""
        from audittrace.routes.memory import _pdf_signature_status

        fake_reader = MagicMock()
        fake_reader.embedded_signatures = [MagicMock()]
        # intact (content unchanged) but valid=False (sig math broken).
        fake_status = MagicMock(intact=True, valid=False, trusted=False)

        with (
            patch(
                "pyhanko.pdf_utils.reader.PdfFileReader",
                return_value=fake_reader,
            ),
            patch(
                "pyhanko.sign.validation.validate_pdf_signature",
                return_value=fake_status,
            ),
        ):
            status, count = _pdf_signature_status(
                b"%PDF-1.4 ignored", enabled=True, trust_store_path=""
            )
        assert status == "signed_invalid"
        assert count == 1

    def test_signed_with_untrusted_chain_returns_signed_invalid(self) -> None:
        """Signature math valid + content intact but cert chain not
        trusted by the configured trust store. ``signed_invalid`` —
        same status as bad-math, since both fail the trust contract."""
        from audittrace.routes.memory import _pdf_signature_status

        fake_reader = MagicMock()
        fake_reader.embedded_signatures = [MagicMock()]
        # intact + valid but trusted=False (chain not in trust store).
        fake_status = MagicMock(intact=True, valid=True, trusted=False)

        with (
            patch(
                "pyhanko.pdf_utils.reader.PdfFileReader",
                return_value=fake_reader,
            ),
            patch(
                "pyhanko.sign.validation.validate_pdf_signature",
                return_value=fake_status,
            ),
        ):
            status, count = _pdf_signature_status(
                b"%PDF-1.4 ignored", enabled=True, trust_store_path=""
            )
        assert status == "signed_invalid"
        assert count == 1

    def test_signed_tampered_returns_signed_tampered(self) -> None:
        """``intact=False`` is the strongest negative signal:
        cryptographic proof that the document was modified after
        signing. Reported separately from generic ``signed_invalid``
        so auditors can prioritise tampering over trust-chain noise."""
        from audittrace.routes.memory import _pdf_signature_status

        fake_reader = MagicMock()
        fake_reader.embedded_signatures = [MagicMock()]
        fake_status = MagicMock(intact=False, valid=True, trusted=True)

        with (
            patch(
                "pyhanko.pdf_utils.reader.PdfFileReader",
                return_value=fake_reader,
            ),
            patch(
                "pyhanko.sign.validation.validate_pdf_signature",
                return_value=fake_status,
            ),
        ):
            status, count = _pdf_signature_status(
                b"%PDF-1.4 ignored", enabled=True, trust_store_path=""
            )
        assert status == "signed_tampered"
        assert count == 1

    def test_signature_validation_exception_returns_check_failed(self) -> None:
        """Any unexpected exception during validation (malformed PDF,
        OCSP timeout, pyhanko bug) is recorded as ``check_failed`` —
        distinct from ``signed_invalid`` so auditors can separate
        "we tried and broke" from "we tried and the document was
        provably bad". v1 never rejects; the chunk lands with the
        status, the corpus stays consistent."""
        from audittrace.routes.memory import _pdf_signature_status

        with patch(
            "pyhanko.pdf_utils.reader.PdfFileReader",
            side_effect=ValueError("corrupted xref"),
        ):
            status, count = _pdf_signature_status(
                b"\x00bad bytes", enabled=True, trust_store_path=""
            )
        assert status == "check_failed"
        assert count == 0

    def test_multiple_signatures_aggregate_to_worst_status(self) -> None:
        """When a document has N signatures, the file's status is the
        worst across them — one tampered signature poisons the file
        even if other signatures are valid. (Tampering > invalid >
        valid in severity order.)"""
        from audittrace.routes.memory import _pdf_signature_status

        fake_reader = MagicMock()
        # Three signatures.
        fake_reader.embedded_signatures = [MagicMock(), MagicMock(), MagicMock()]
        # First two valid, third tampered.
        statuses = [
            MagicMock(intact=True, valid=True, trusted=True),
            MagicMock(intact=True, valid=True, trusted=True),
            MagicMock(intact=False, valid=True, trusted=True),
        ]

        with (
            patch(
                "pyhanko.pdf_utils.reader.PdfFileReader",
                return_value=fake_reader,
            ),
            patch(
                "pyhanko.sign.validation.validate_pdf_signature",
                side_effect=statuses,
            ),
        ):
            status, count = _pdf_signature_status(
                b"%PDF-1.4 ignored", enabled=True, trust_store_path=""
            )
        assert status == "signed_tampered"
        assert count == 3

    def test_signature_status_propagates_to_chunk_metadata(
        self, client: TestClient
    ) -> None:
        """Integration: full /memory/index call with mocked pyhanko
        returning ``signed_valid``. Every chunk metadata dict carries
        the same status (signature is per-document, not per-chunk —
        amortises the OCSP/CRL roundtrips)."""
        mock_minio = MagicMock()

        def list_objects(bucket: str, prefix: str = "", **_kw: Any) -> list[Any]:
            if prefix == "episodic/":
                return [_mock_minio_object("episodic/papers/research/foo.pdf")]
            return []

        mock_minio.list_objects.side_effect = list_objects
        response_obj = MagicMock()
        response_obj.read.return_value = b"%PDF-1.4 mock bytes"
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj

        mock_collection = MagicMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection.return_value = mock_collection

        rect_mock = MagicMock(x0=0.0, y0=0.0, x1=612.0, y1=792.0)
        fake_page = MagicMock()
        fake_page.get_text.return_value = "Body."
        fake_page.rect = rect_mock
        fake_page.annots.return_value = []
        fake_doc = MagicMock()
        fake_doc.__iter__.return_value = iter([fake_page])
        fake_doc.__enter__.return_value = fake_doc
        fake_doc.__exit__.return_value = None
        fake_doc.page_count = 1
        fake_doc.xref_length.return_value = 10
        fake_pymupdf = MagicMock()
        fake_pymupdf.open.return_value = fake_doc

        # Mock pyhanko: one valid signature.
        fake_reader = MagicMock()
        fake_reader.embedded_signatures = [MagicMock()]
        fake_sig_status = MagicMock(intact=True, valid=True, trusted=True)

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
            patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
            patch(
                "pyhanko.pdf_utils.reader.PdfFileReader",
                return_value=fake_reader,
            ),
            patch(
                "pyhanko.sign.validation.validate_pdf_signature",
                return_value=fake_sig_status,
            ),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "ai_research_papers"},
            )

        assert response.status_code == 200, response.text
        meta = mock_collection.upsert.call_args.kwargs["metadatas"][0]
        assert meta["signature_status"] == "signed_valid"


class TestPdfHelperCoverage:
    """Direct unit coverage for the PDF helper functions —
    defensive branches that the route-level tests don't exercise.

    These tests exist so the per-file coverage gate on
    ``audittrace/routes/memory.py`` stays >= 90% as the module
    grows. Each test pins one specific defensive branch.
    """

    def test_page_bbox_falls_back_on_attribute_error(self) -> None:
        """``_page_bbox`` returns zeros when ``page.rect`` access
        raises — keeps the metadata schema stable on malformed PDFs.
        Use a plain class instead of MagicMock here: setting
        ``PropertyMock`` on ``type(MagicMock())`` mutates global
        state shared by every other MagicMock in the suite."""
        from audittrace.routes.memory import _page_bbox

        class _BadPage:
            @property
            def rect(self) -> Any:
                raise AttributeError("no rect on this page")

        x0, y0, x1, y1 = _page_bbox(_BadPage())
        assert (x0, y0, x1, y1) == (0.0, 0.0, 0.0, 0.0)

    def test_redaction_rects_swallows_annots_exception(self) -> None:
        """Some malformed PDFs raise inside ``page.annots()`` — we
        return an empty list rather than crashing the file."""
        from audittrace.routes.memory import _redaction_rects

        bad_page = MagicMock()
        bad_page.annots.side_effect = RuntimeError("malformed annot table")
        assert _redaction_rects(bad_page) == []

    def test_redaction_rects_skips_annot_with_no_type(self) -> None:
        """An annot whose ``.type`` is None or empty is skipped —
        defense against pymupdf returning a degraded-shape annot."""
        from audittrace.routes.memory import _redaction_rects

        annot_no_type = MagicMock()
        annot_no_type.type = None  # type missing
        annot_empty_type = MagicMock()
        annot_empty_type.type = ()  # truthy-falsy edge
        page = MagicMock()
        page.annots.return_value = [annot_no_type, annot_empty_type]
        assert _redaction_rects(page) == []

    def test_text_clipped_drops_short_block_tuple(self) -> None:
        """Block tuples shorter than 5 elements (defensive against
        pymupdf API drift) are skipped, not crashed on."""
        from audittrace.routes.memory import _text_clipped_around_redactions

        page = MagicMock()
        page.get_text.return_value = [
            (0.0, 0.0, 50.0),  # malformed — len < 5
            (10.0, 10.0, 50.0, 50.0, "kept text", 0, 0),
        ]
        result = _text_clipped_around_redactions(page, redaction_rects=[])
        assert result == "kept text"

    def test_text_clipped_drops_non_string_block_text(self) -> None:
        """Block whose [4] element isn't a string (image block, byte
        stream, etc.) is skipped — only string content is indexable."""
        from audittrace.routes.memory import _text_clipped_around_redactions

        page = MagicMock()
        page.get_text.return_value = [
            (0.0, 0.0, 50.0, 50.0, b"binary stream", 0, 1),
            (60.0, 60.0, 100.0, 100.0, "real text", 1, 0),
            (110.0, 110.0, 150.0, 150.0, "   ", 2, 0),  # whitespace-only
        ]
        result = _text_clipped_around_redactions(page, redaction_rects=[])
        assert result == "real text"

    def test_validation_context_caches_across_calls(self) -> None:
        """Second call with same trust_store_path returns the cached
        ValidationContext — confirms the singleton-with-lock pattern
        is doing its job (no per-call allocation)."""
        import audittrace.routes.memory as routes_memory

        routes_memory._VALIDATION_CONTEXT = None
        routes_memory._VC_TRUST_STORE_PATH = ""

        from audittrace.routes.memory import _get_validation_context

        vc1 = _get_validation_context("")
        vc2 = _get_validation_context("")
        assert vc1 is vc2

    def test_validation_context_rebuilds_on_trust_store_change(self) -> None:
        """When trust_store_path changes between calls (operator
        updated Settings), the context is rebuilt — deliberate
        cache invalidation point so the new trust store takes
        effect without a process restart. Path that doesn't exist
        triggers the OSError fallback to system roots — still a
        valid context, just rebuilt."""
        import audittrace.routes.memory as routes_memory

        routes_memory._VALIDATION_CONTEXT = None
        routes_memory._VC_TRUST_STORE_PATH = ""

        from audittrace.routes.memory import _get_validation_context

        vc1 = _get_validation_context("")
        vc2 = _get_validation_context("/nonexistent/trust/store.pem")
        assert vc1 is not vc2

    def test_signature_check_unavailable_when_pyhanko_missing(self) -> None:
        """If pyhanko.pdf_utils.reader can't import, the helper
        returns ``check_unavailable`` instead of crashing — graceful
        degradation per PYTHON-ENGINEERING §4."""
        import sys

        from audittrace.routes.memory import _pdf_signature_status

        # Force the inner import to fail by injecting a None sentinel
        # into sys.modules — Python raises ImportError when an entry
        # is None on import attempt.
        with patch.dict(sys.modules, {"pyhanko.pdf_utils.reader": None}):
            status, count = _pdf_signature_status(
                b"%PDF-1.4 ignored", enabled=True, trust_store_path=""
            )
        assert status == "check_unavailable"
        assert count == 0


# ─────────────────────────────────────────────────────────────────────────────
# Memory-layer CRUD backoffice (PR A)
# ─────────────────────────────────────────────────────────────────────────────


class TestEpisodicCrud:
    """POST/GET/PUT/DELETE /memory/episodic — full CRUD round-trip on the
    Mock service (no real MinIO needed)."""

    def test_create_lists_then_reads(self, client: TestClient) -> None:
        # Create
        r = client.post(
            "/memory/episodic",
            json={
                "filename": "ADR-test.md",
                "content": "# ADR-test\n\nbody",
                "title": "Test ADR",
            },
        )
        assert r.status_code == 200, r.text
        entry = r.json()
        assert entry["layer"] == "episodic"
        assert entry["key"] == "ADR-test.md"
        assert entry["title"] == "Test ADR"
        assert entry["size_bytes"] == len(b"# ADR-test\n\nbody")
        assert entry["created_at_ms"] == entry["modified_at_ms"]
        assert entry["deleted_at_ms"] is None
        # List
        r = client.get("/memory/episodic")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["key"] == "ADR-test.md"
        # Read
        r = client.get("/memory/episodic/ADR-test.md")
        assert r.status_code == 200
        body = r.json()
        assert body["content"] == "# ADR-test\n\nbody"
        assert body["manifest"]["key"] == "ADR-test.md"

    def test_update_bumps_modified_at(self, client: TestClient) -> None:
        r = client.post(
            "/memory/episodic",
            json={"filename": "ADR-bump.md", "content": "v1"},
        )
        assert r.status_code == 200
        first = r.json()
        # PUT
        r = client.put(
            "/memory/episodic/ADR-bump.md",
            json={"content": "v2", "title": "v2 title"},
        )
        assert r.status_code == 200
        second = r.json()
        assert second["created_at_ms"] == first["created_at_ms"]
        assert second["modified_at_ms"] >= first["modified_at_ms"]
        assert second["title"] == "v2 title"
        assert second["size_bytes"] == 2  # "v2"
        # Confirm read returns v2
        r = client.get("/memory/episodic/ADR-bump.md")
        assert r.json()["content"] == "v2"

    def test_delete_soft(self, client: TestClient) -> None:
        client.post(
            "/memory/episodic",
            json={"filename": "ADR-soft.md", "content": "x"},
        )
        r = client.delete("/memory/episodic/ADR-soft.md")
        assert r.status_code == 200
        deleted = r.json()
        assert deleted["deleted_at_ms"] is not None
        # List default hides soft-deleted
        r = client.get("/memory/episodic")
        keys = {i["key"] for i in r.json()["items"]}
        assert "ADR-soft.md" not in keys
        # List include_deleted=true shows them
        r = client.get("/memory/episodic?include_deleted=true")
        keys = {i["key"] for i in r.json()["items"]}
        assert "ADR-soft.md" in keys

    def test_recreate_after_soft_delete_revives(self, client: TestClient) -> None:
        client.post(
            "/memory/episodic",
            json={"filename": "ADR-revive.md", "content": "1"},
        )
        client.delete("/memory/episodic/ADR-revive.md")
        # Recreate same key
        r = client.post(
            "/memory/episodic",
            json={"filename": "ADR-revive.md", "content": "2"},
        )
        assert r.status_code == 200
        revived = r.json()
        assert revived["deleted_at_ms"] is None
        assert revived["size_bytes"] == 1

    def test_filename_validation(self, client: TestClient) -> None:
        # Missing .md
        r = client.post(
            "/memory/episodic",
            json={"filename": "no-extension", "content": "x"},
        )
        assert r.status_code == 400
        # Path traversal
        r = client.post(
            "/memory/episodic",
            json={"filename": "../etc/passwd.md", "content": "x"},
        )
        assert r.status_code == 400

    def test_read_404_when_missing(self, client: TestClient) -> None:
        r = client.get("/memory/episodic/never-existed.md")
        assert r.status_code == 404

    def test_update_404_when_missing(self, client: TestClient) -> None:
        # The service `write` will create the S3 object, but the manifest
        # update raises LookupError → 404. (Caller should POST instead.)
        r = client.put(
            "/memory/episodic/missing-manifest.md",
            json={"content": "x"},
        )
        assert r.status_code == 404


class TestProceduralCrud:
    """SKILL CRUD — same shape as episodic, condensed."""

    def test_create_list_read_update_delete_round_trip(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/memory/procedural",
            json={"filename": "SKILL-foo.md", "content": "# Foo skill\n"},
        )
        assert r.status_code == 200
        r = client.get("/memory/procedural")
        assert r.json()["total"] == 1
        r = client.get("/memory/procedural/SKILL-foo.md")
        assert r.status_code == 200
        assert "Foo skill" in r.json()["content"]
        r = client.put(
            "/memory/procedural/SKILL-foo.md",
            json={"content": "updated"},
        )
        assert r.status_code == 200
        r = client.delete("/memory/procedural/SKILL-foo.md")
        assert r.status_code == 200


class TestSemanticCrud:
    """Semantic CRUD — keyed by collection/document_id."""

    def test_create_list_read_update_delete(self, client: TestClient) -> None:
        # Create
        r = client.post(
            "/memory/semantic",
            json={
                "collection": "decisions",
                "document_id": "doc-001",
                "text": "the quick brown fox",
                "metadata": {"source": "test"},
            },
        )
        assert r.status_code == 200, r.text
        entry = r.json()
        assert entry["layer"] == "semantic"
        assert entry["key"] == "decisions/doc-001"
        # List, optionally filtered by collection
        r = client.get("/memory/semantic?collection=decisions")
        assert r.json()["total"] == 1
        r = client.get("/memory/semantic?collection=does-not-exist")
        assert r.json()["total"] == 0
        # Read
        r = client.get("/memory/semantic/decisions/doc-001")
        assert r.status_code == 200
        assert r.json()["content"] == "the quick brown fox"
        # Update
        r = client.put(
            "/memory/semantic/decisions/doc-001",
            json={"text": "lazy dog"},
        )
        assert r.status_code == 200
        r = client.get("/memory/semantic/decisions/doc-001")
        assert r.json()["content"] == "lazy dog"
        # Delete
        r = client.delete("/memory/semantic/decisions/doc-001")
        assert r.status_code == 200
        assert r.json()["deleted_at_ms"] is not None

    def test_create_validates_required_fields(self, client: TestClient) -> None:
        r = client.post("/memory/semantic", json={"collection": "x"})
        assert r.status_code == 400


class TestTimestampShape:
    """The user explicitly chose Unix-epoch milliseconds UTC for created/
    modified/deleted timestamps. Test the contract: integer-typed BIGINT
    that's plausibly current time."""

    def test_created_at_ms_is_integer_milliseconds_now(
        self, client: TestClient
    ) -> None:
        import time

        now_ms = int(time.time() * 1000)
        r = client.post(
            "/memory/episodic",
            json={"filename": "ADR-ts.md", "content": "x"},
        )
        entry = r.json()
        # Type: integer
        assert isinstance(entry["created_at_ms"], int)
        # Plausibility: within ±5 s of "now" (test runner is local)
        assert abs(entry["created_at_ms"] - now_ms) < 5_000
        # > 1e12 means we're in milliseconds, not seconds
        assert entry["created_at_ms"] > 10**12


# ── Edge cases / coverage-completeness ──────────────────────────────────────


class TestEpisodicEdgeCases:
    def test_create_missing_content(self, client: TestClient) -> None:
        r = client.post("/memory/episodic", json={"filename": "ADR-x.md"})
        assert r.status_code == 400

    def test_create_missing_filename(self, client: TestClient) -> None:
        r = client.post("/memory/episodic", json={"content": "x"})
        assert r.status_code == 400

    def test_update_missing_content(self, client: TestClient) -> None:
        client.post("/memory/episodic", json={"filename": "ADR-u.md", "content": "x"})
        r = client.put("/memory/episodic/ADR-u.md", json={})
        assert r.status_code == 400

    def test_update_invalid_filename(self, client: TestClient) -> None:
        r = client.put("/memory/episodic/no-extension", json={"content": "x"})
        assert r.status_code == 400

    def test_delete_invalid_filename(self, client: TestClient) -> None:
        r = client.delete("/memory/episodic/../escape.md")
        # FastAPI normalises the path; result depends on routing but
        # should not 200.
        assert r.status_code in (400, 404, 405)

    def test_delete_404_when_missing_manifest(self, client: TestClient) -> None:
        r = client.delete("/memory/episodic/never-existed.md")
        assert r.status_code == 404

    def test_update_after_soft_delete_returns_409(self, client: TestClient) -> None:
        client.post("/memory/episodic", json={"filename": "ADR-c.md", "content": "x"})
        client.delete("/memory/episodic/ADR-c.md")
        r = client.put("/memory/episodic/ADR-c.md", json={"content": "y"})
        # PUT on a soft-deleted row -> manifest.record_update raises
        # RuntimeError -> route maps to 409.
        assert r.status_code == 409


class TestProceduralEdgeCases:
    def test_create_missing_fields(self, client: TestClient) -> None:
        r = client.post("/memory/procedural", json={})
        assert r.status_code == 400

    def test_filename_validation(self, client: TestClient) -> None:
        r = client.post(
            "/memory/procedural",
            json={"filename": "no-extension", "content": "x"},
        )
        assert r.status_code == 400

    def test_read_404_when_missing(self, client: TestClient) -> None:
        r = client.get("/memory/procedural/never.md")
        assert r.status_code == 404

    def test_update_404_when_missing_manifest(self, client: TestClient) -> None:
        r = client.put("/memory/procedural/orphan.md", json={"content": "x"})
        assert r.status_code == 404

    def test_delete_404_when_missing(self, client: TestClient) -> None:
        r = client.delete("/memory/procedural/never.md")
        assert r.status_code == 404

    def test_update_missing_content_field(self, client: TestClient) -> None:
        client.post(
            "/memory/procedural",
            json={"filename": "SKILL-up.md", "content": "x"},
        )
        r = client.put("/memory/procedural/SKILL-up.md", json={})
        assert r.status_code == 400

    def test_update_after_soft_delete_returns_409(self, client: TestClient) -> None:
        client.post(
            "/memory/procedural",
            json={"filename": "SKILL-d.md", "content": "x"},
        )
        client.delete("/memory/procedural/SKILL-d.md")
        r = client.put("/memory/procedural/SKILL-d.md", json={"content": "y"})
        assert r.status_code == 409

    def test_list_include_deleted_includes_soft(self, client: TestClient) -> None:
        client.post(
            "/memory/procedural",
            json={"filename": "SKILL-l.md", "content": "x"},
        )
        client.delete("/memory/procedural/SKILL-l.md")
        r = client.get("/memory/procedural?include_deleted=true")
        keys = {i["key"] for i in r.json()["items"]}
        assert "SKILL-l.md" in keys


class TestSemanticEdgeCases:
    def test_create_missing_fields(self, client: TestClient) -> None:
        r = client.post("/memory/semantic", json={"collection": "x", "text": "y"})
        assert r.status_code == 400  # missing document_id

    def test_create_text_required(self, client: TestClient) -> None:
        r = client.post(
            "/memory/semantic",
            json={"collection": "x", "document_id": "y"},
        )
        assert r.status_code == 400

    def test_read_404_when_missing(self, client: TestClient) -> None:
        r = client.get("/memory/semantic/some-collection/never")
        assert r.status_code == 404

    def test_update_text_required(self, client: TestClient) -> None:
        client.post(
            "/memory/semantic",
            json={
                "collection": "decisions",
                "document_id": "doc-u",
                "text": "v1",
            },
        )
        r = client.put("/memory/semantic/decisions/doc-u", json={})
        assert r.status_code == 400

    def test_update_404_when_missing_manifest(self, client: TestClient) -> None:
        r = client.put("/memory/semantic/decisions/orphan", json={"text": "x"})
        assert r.status_code == 404

    def test_delete_404_when_missing(self, client: TestClient) -> None:
        r = client.delete("/memory/semantic/decisions/never")
        assert r.status_code == 404

    def test_list_filtered_by_collection(self, client: TestClient) -> None:
        client.post(
            "/memory/semantic",
            json={
                "collection": "alpha",
                "document_id": "a-1",
                "text": "x",
            },
        )
        client.post(
            "/memory/semantic",
            json={"collection": "beta", "document_id": "b-1", "text": "y"},
        )
        r = client.get("/memory/semantic?collection=alpha")
        keys = {i["key"] for i in r.json()["items"]}
        assert keys == {"alpha/a-1"}


class TestRouteEdgeCases:
    def test_delete_existing_idempotent_via_endpoint(self, client: TestClient) -> None:
        client.post("/memory/episodic", json={"filename": "ADR-i.md", "content": "x"})
        r1 = client.delete("/memory/episodic/ADR-i.md")
        assert r1.status_code == 200
        # Second delete on a soft-deleted row returns the same entry
        # (idempotent at the manifest layer)
        r2 = client.delete("/memory/episodic/ADR-i.md")
        assert r2.status_code == 200
        # And `deleted_at_ms` is the same as the first call
        assert r1.json()["deleted_at_ms"] == r2.json()["deleted_at_ms"]


class TestServiceFailurePaths:
    """Service-layer RuntimeError → 502 mapping; ValueError → 400. These
    paths fire when MinIO is reachable but rejects the operation (network
    blip, object-store full, etc.). Mocked here to exercise the route's
    error-mapping logic deterministically."""

    def test_episodic_create_502_on_service_runtime_error(
        self, client: TestClient
    ) -> None:
        with patch("audittrace.routes.memory.get_episodic_service") as mock_get:
            svc = MagicMock()
            svc.write.side_effect = RuntimeError("S3 write blocked")
            mock_get.return_value = svc
            r = client.post(
                "/memory/episodic",
                json={"filename": "ADR-fail.md", "content": "x"},
            )
        assert r.status_code == 502
        assert "S3 write blocked" in r.json()["detail"]

    def test_episodic_update_502_on_service_runtime_error(
        self, client: TestClient
    ) -> None:
        # Seed manifest first so the update reaches the write step
        client.post("/memory/episodic", json={"filename": "ADR-pf.md", "content": "x"})
        with patch("audittrace.routes.memory.get_episodic_service") as mock_get:
            svc = MagicMock()
            svc.write.side_effect = RuntimeError("S3 conflict")
            mock_get.return_value = svc
            r = client.put("/memory/episodic/ADR-pf.md", json={"content": "y"})
        assert r.status_code == 502

    def test_episodic_hard_delete_502_on_service_runtime_error(
        self, client: TestClient
    ) -> None:
        client.post("/memory/episodic", json={"filename": "ADR-hd.md", "content": "x"})
        with patch("audittrace.routes.memory.get_episodic_service") as mock_get:
            svc = MagicMock()
            svc.delete.side_effect = RuntimeError("S3 unreachable")
            mock_get.return_value = svc
            r = client.delete("/memory/episodic/ADR-hd.md?hard=true")
        # Sentinel context is admin → hard-delete passes scope gate.
        assert r.status_code == 502

    def test_procedural_create_502_on_service_runtime_error(
        self, client: TestClient
    ) -> None:
        with patch("audittrace.routes.memory.get_procedural_service") as mock_get:
            svc = MagicMock()
            svc.write.side_effect = RuntimeError("S3 down")
            mock_get.return_value = svc
            r = client.post(
                "/memory/procedural",
                json={"filename": "SKILL-pf.md", "content": "x"},
            )
        assert r.status_code == 502

    def test_procedural_update_502_on_service_runtime_error(
        self, client: TestClient
    ) -> None:
        client.post(
            "/memory/procedural",
            json={"filename": "SKILL-pu.md", "content": "x"},
        )
        with patch("audittrace.routes.memory.get_procedural_service") as mock_get:
            svc = MagicMock()
            svc.write.side_effect = RuntimeError("S3 conflict")
            mock_get.return_value = svc
            r = client.put(
                "/memory/procedural/SKILL-pu.md",
                json={"content": "y"},
            )
        assert r.status_code == 502

    def test_procedural_hard_delete_502_on_service_runtime_error(
        self, client: TestClient
    ) -> None:
        client.post(
            "/memory/procedural",
            json={"filename": "SKILL-hd.md", "content": "x"},
        )
        with patch("audittrace.routes.memory.get_procedural_service") as mock_get:
            svc = MagicMock()
            svc.delete.side_effect = RuntimeError("S3 down")
            mock_get.return_value = svc
            r = client.delete("/memory/procedural/SKILL-hd.md?hard=true")
        assert r.status_code == 502

    def test_semantic_create_502_on_service_runtime_error(
        self, client: TestClient
    ) -> None:
        with patch("audittrace.routes.memory.get_semantic_service") as mock_get:
            svc = MagicMock()
            svc.upsert.side_effect = RuntimeError("Chroma timeout")
            mock_get.return_value = svc
            r = client.post(
                "/memory/semantic",
                json={
                    "collection": "decisions",
                    "document_id": "doc-fail",
                    "text": "x",
                },
            )
        assert r.status_code == 502

    def test_semantic_update_502_on_service_runtime_error(
        self, client: TestClient
    ) -> None:
        client.post(
            "/memory/semantic",
            json={
                "collection": "decisions",
                "document_id": "doc-su",
                "text": "v1",
            },
        )
        with patch("audittrace.routes.memory.get_semantic_service") as mock_get:
            svc = MagicMock()
            svc.upsert.side_effect = RuntimeError("Chroma conflict")
            mock_get.return_value = svc
            r = client.put(
                "/memory/semantic/decisions/doc-su",
                json={"text": "v2"},
            )
        assert r.status_code == 502

    def test_semantic_hard_delete_502_on_service_runtime_error(
        self, client: TestClient
    ) -> None:
        client.post(
            "/memory/semantic",
            json={
                "collection": "decisions",
                "document_id": "doc-shd",
                "text": "x",
            },
        )
        with patch("audittrace.routes.memory.get_semantic_service") as mock_get:
            svc = MagicMock()
            svc.delete_document.side_effect = RuntimeError("Chroma down")
            mock_get.return_value = svc
            r = client.delete("/memory/semantic/decisions/doc-shd?hard=true")
        assert r.status_code == 502


class TestHardDeleteAdminGate:
    """``?hard=true`` requires audittrace:admin in addition to the
    write scope. Sentinel context is admin so the test_mode bypass
    passes; explicitly assert the non-admin path 403s."""

    def test_hard_delete_blocked_without_admin_scope(self, client: TestClient) -> None:
        # Seed
        client.post("/memory/episodic", json={"filename": "ADR-hd.md", "content": "x"})
        # Patch require_user to return a non-admin user_context.
        from audittrace.identity import UserContext

        non_admin = UserContext(
            user_id="user-non-admin",
            username="non-admin",
            agent_type="dev",
            scopes=("memory:episodic:write",),  # has write but NOT admin
            is_admin=False,
        )
        with patch(
            "audittrace.routes.memory.require_user",
            return_value=lambda: non_admin,
        ):
            # Use FastAPI's dependency_overrides for cleaner injection
            from audittrace.auth import require_user as auth_require_user
            from audittrace.routes.memory import router  # noqa: F401

            client.app.dependency_overrides[auth_require_user] = lambda: non_admin
            try:
                r = client.delete("/memory/episodic/ADR-hd.md?hard=true")
            finally:
                client.app.dependency_overrides.pop(auth_require_user, None)
        assert r.status_code == 403
        assert "audittrace:admin" in r.json()["detail"]


class TestS3DiscoveryMerge:
    """The list endpoints must merge S3 objects (pre-PR-A items uploaded
    via /memory/upload or seeded via index-chromadb) into the response
    so the Memory tab reflects ALL content, not just operator-created
    items. Found in PR A's 2026-05-03 live test: the page showed 0
    items because the manifest table was empty — the underlying ADRs
    were in MinIO already.
    """

    def test_episodic_list_includes_s3_objects(self, client: TestClient) -> None:
        # Seed the mock episodic service with an "uploaded" item that
        # has no manifest row — the manifest is empty at this point.
        from audittrace.dependencies import get_episodic_service

        ep = get_episodic_service()
        ep.add_document(
            content="# ADR-pre-existing\n\ncontent",
            title="Pre-existing ADR",
            file="ADR-pre-existing.md",
        )

        r = client.get("/memory/episodic")
        assert r.status_code == 200
        body = r.json()
        keys = {i["key"] for i in body["items"]}
        assert "ADR-pre-existing.md" in keys
        # The discovered entry has no manifest authorship/timestamps
        discovered = next(i for i in body["items"] if i["key"] == "ADR-pre-existing.md")
        assert discovered["discovered"] is True
        assert discovered["id"] is None
        assert discovered["created_by_user_id"] is None
        assert discovered["modified_at_ms"] is None

    def test_procedural_list_includes_s3_objects(self, client: TestClient) -> None:
        from audittrace.dependencies import get_procedural_service

        pr = get_procedural_service()
        pr.add_document(
            content="# SKILL-pre\n\nbody",
            skill="PreSkill",
            file="SKILL-pre.md",
        )

        r = client.get("/memory/procedural")
        assert r.status_code == 200
        body = r.json()
        keys = {i["key"] for i in body["items"]}
        assert "SKILL-pre.md" in keys

    def test_manifest_row_takes_precedence_over_s3_object(
        self, client: TestClient
    ) -> None:
        """An operator-created item is in BOTH the manifest and S3.
        The list endpoint must surface the manifest version (with
        authorship metadata), not the S3 discovery synthesis."""
        # Create via the new POST endpoint; this writes both to S3 and
        # to the manifest.
        client.post(
            "/memory/episodic",
            json={"filename": "ADR-tracked.md", "content": "# tracked"},
        )

        r = client.get("/memory/episodic")
        assert r.status_code == 200
        rows = [i for i in r.json()["items"] if i["key"] == "ADR-tracked.md"]
        assert len(rows) == 1, "key duplicated between manifest + S3 discovery"
        assert rows[0].get("discovered") is None
        assert rows[0]["id"] is not None
        assert rows[0]["created_by_user_id"]

    def test_soft_deleted_key_is_not_resurrected_via_s3(
        self, client: TestClient
    ) -> None:
        """When a manifest row is soft-deleted, the S3 object is left
        in place by design. The list endpoint must NOT re-discover the
        S3 object as a "new" entry — that would silently reverse the
        operator's delete intent."""
        client.post(
            "/memory/episodic",
            json={"filename": "ADR-killed.md", "content": "# killed"},
        )
        r = client.delete("/memory/episodic/ADR-killed.md")
        assert r.status_code == 200

        r = client.get("/memory/episodic")  # default include_deleted=False
        keys = {i["key"] for i in r.json()["items"]}
        assert "ADR-killed.md" not in keys, (
            "soft-deleted manifest row was resurrected via S3 discovery"
        )


class TestConversationalLayer:
    """Layer 3 — chat sessions + interactions. Read-only RLS-scoped
    surface separate from the audit routes. Backed by the same
    Postgres tables as `/sessions` + `/interactions` but gated on the
    user-facing `memory:conversational:read-own` scope."""

    def test_list_requires_conversational_read_scope(self, client: TestClient) -> None:
        # auth_enabled=False in tests so the scope check is bypassed;
        # we only assert the route exists and returns the documented
        # shape.
        r = client.get("/memory/conversational")
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "total" in body
        assert "limit" in body
        assert "offset" in body

    def test_read_unknown_session_returns_404(self, client: TestClient) -> None:
        r = client.get("/memory/conversational/does-not-exist")
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()


# ── Tier-B: PDF robustness (ADR-050) ─────────────────────────────────────────


class TestExtractionWarningCodes:
    """Closed-set discipline on the JSONB ``extraction_warnings.code``
    enum. Adding a new code without an ADR-050 amendment is a quiet
    documentation drift; this test pins the set so the drift surfaces
    in CI."""

    def test_warning_codes_match_adr_050_closed_set(self) -> None:
        from audittrace.routes.memory import _PDF_WARNING_CODES

        # The exact set documented in ADR-050 §extraction_warnings.
        # Adding a code: bump the ADR + add it here. Removing one:
        # same. CI fails the diff if these drift.
        expected = {
            # tier-A bomb defenses (item #18)
            "max_size",
            "max_pages",
            "max_xref",
            "max_page_text",
            "parse_timeout",
            # tier-A redaction (item #8)
            "redaction_clipped",
            "redaction_rejected",
            # tier-B (this PR)
            "encrypted",
            "no_text_layer",
            "ocr_low_confidence",
            "attachment",
            "attachment_quarantine_failed",
            "form_fields",
        }
        assert _PDF_WARNING_CODES == expected


class TestPdfIsEncrypted:
    """Direct unit tests for ``_pdf_is_encrypted`` (tier-B item #15)."""

    def test_real_bool_attrs_detected(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _pdf_is_encrypted

        encrypted = SimpleNamespace(is_encrypted=True, needs_pass=True)
        assert _pdf_is_encrypted(encrypted) is True

    def test_clear_pdf_returns_false(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _pdf_is_encrypted

        clear = SimpleNamespace(is_encrypted=False, needs_pass=False)
        assert _pdf_is_encrypted(clear) is False

    def test_encrypted_but_authenticated_is_not_refused(self) -> None:
        """An owner-password PDF (encrypted, but password not required
        to read) returns False — pymupdf can read content, we proceed."""
        from types import SimpleNamespace

        from audittrace.routes.memory import _pdf_is_encrypted

        owner_pwd = SimpleNamespace(is_encrypted=True, needs_pass=False)
        assert _pdf_is_encrypted(owner_pwd) is False

    def test_magicmock_attrs_evaluate_false(self) -> None:
        """Defensive: MagicMock attrs must NOT be treated as truthy.
        Test fixtures throughout the suite would otherwise be rejected
        as encrypted."""
        from unittest.mock import MagicMock

        from audittrace.routes.memory import _pdf_is_encrypted

        m = MagicMock()
        # Attributes accessed on a default MagicMock return more
        # MagicMocks (truthy by default). We rely on strict ``is True``
        # comparison to evaluate as False here.
        assert _pdf_is_encrypted(m) is False


class TestPdfEncryptedReject:
    """Tier-B item #15 — encrypted PDFs refuse with 0 chunks +
    extraction_warning, no password endpoint exposed."""

    def test_encrypted_pdf_yields_zero_chunks_and_warning(
        self, client: TestClient
    ) -> None:
        # Direct-ish unit test: feed an encrypted document into the
        # helper and assert the warning shape. The route-level path
        # is covered by TestPdfManifestColumns below — keeping this
        # one focused so failures localise.
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from audittrace.routes.memory import _flush_pdf_manifest, _pdf_is_encrypted

        encrypted_doc = SimpleNamespace(is_encrypted=True, needs_pass=True)
        assert _pdf_is_encrypted(encrypted_doc) is True

        # Flush a manifest with the encrypted-warning shape and
        # verify the structured entry.
        manifest = MagicMock()
        _flush_pdf_manifest(
            manifest_service=manifest,
            layer="episodic",
            key="episodic/locked.pdf",
            user_id="u",
            size_bytes=4096,
            page_count=None,
            signature_status="check_skipped",
            ocr_coverage_pct=None,
            attachment_count=0,
            form_field_count=0,
            extraction_warnings=[{"code": "encrypted", "page": None}],
            document_sha256="abc123",
        )
        manifest.upsert_pdf_metadata.assert_called_once()
        call = manifest.upsert_pdf_metadata.call_args
        assert call.args == ("episodic", "episodic/locked.pdf")
        assert call.kwargs["page_count"] is None
        assert call.kwargs["extraction_warnings"] == [
            {"code": "encrypted", "page": None}
        ]
        # Critical contract: the function name does NOT carry a
        # ``password`` parameter (per ADR-050 §#15: no password
        # endpoint, ever).
        import inspect

        from audittrace.routes.memory import upload_memory_file

        sig = inspect.signature(upload_memory_file)
        assert "password" not in sig.parameters


class TestAcroFormHelper:
    """Tier-B item #7 — AcroForm widget extraction returns
    label/value pairs as a single page-level chunk."""

    def test_widgets_present_yields_text_and_count(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from audittrace.routes.memory import _acroform_text_for_page

        widgets = [
            SimpleNamespace(
                field_name="name", field_label="Full name", field_value="Alice"
            ),
            SimpleNamespace(
                field_name="dob", field_label="Date of birth", field_value="2000-01-01"
            ),
            # Empty value — should be skipped (no semantic signal)
            SimpleNamespace(
                field_name="middle", field_label="Middle name", field_value=""
            ),
        ]
        page = MagicMock()
        page.widgets.return_value = widgets
        text, count = _acroform_text_for_page(page)
        assert count == 2
        assert "Full name: Alice" in text
        assert "Date of birth: 2000-01-01" in text
        # Empty field omitted.
        assert "Middle name" not in text

    def test_no_widgets_returns_none(self) -> None:
        from unittest.mock import MagicMock

        from audittrace.routes.memory import _acroform_text_for_page

        page = MagicMock()
        page.widgets.return_value = []
        text, count = _acroform_text_for_page(page)
        assert text is None
        assert count == 0

    def test_widgets_call_failure_returns_none(self) -> None:
        from unittest.mock import MagicMock

        from audittrace.routes.memory import _acroform_text_for_page

        page = MagicMock()
        page.widgets.side_effect = RuntimeError("malformed widget tree")
        text, count = _acroform_text_for_page(page)
        assert text is None
        assert count == 0


class TestAttachmentQuarantine:
    """Tier-B item #6 — embedded attachments are extracted to MinIO
    and recorded as structured warnings."""

    def test_two_attachments_quarantined(self) -> None:
        from unittest.mock import MagicMock

        from audittrace.routes.memory import _quarantine_pdf_attachments

        # Real bytes payloads so hashlib.sha256 + io.BytesIO work
        # without the defensive fallback firing.
        invoice_bytes = b"<?xml version='1.0'?><Invoice/>"
        evidence_bytes = b"binary-evidence-bundle"

        doc = MagicMock()
        doc.embfile_count.return_value = 2
        doc.embfile_info.side_effect = [
            {"filename": "invoice.xml", "mime": "application/xml"},
            {"filename": "evidence.bin", "mime": "application/octet-stream"},
        ]
        doc.embfile_get.side_effect = [invoice_bytes, evidence_bytes]

        minio_client = MagicMock()
        count, warnings = _quarantine_pdf_attachments(
            doc,
            parent_filename="main.pdf",
            layer_prefix="episodic/",
            minio_client=minio_client,
            bucket="memory-shared",
        )
        assert count == 2
        assert len(warnings) == 2
        # Both succeeded — codes should be ``attachment``, not
        # ``attachment_quarantine_failed``.
        assert all(w["code"] == "attachment" for w in warnings)
        assert warnings[0]["name"] == "invoice.xml"
        assert warnings[0]["mime"] == "application/xml"
        assert warnings[0]["minio_key"] == "episodic/main.pdf/attachments/invoice.xml"
        assert warnings[0]["size"] == len(invoice_bytes)
        # MinIO put_object called twice with the right bucket + keys.
        assert minio_client.put_object.call_count == 2

    def test_no_embedded_files_returns_zero(self) -> None:
        from unittest.mock import MagicMock

        from audittrace.routes.memory import _quarantine_pdf_attachments

        doc = MagicMock()
        doc.embfile_count.return_value = 0
        minio_client = MagicMock()
        count, warnings = _quarantine_pdf_attachments(
            doc,
            parent_filename="main.pdf",
            layer_prefix="episodic/",
            minio_client=minio_client,
            bucket="memory-shared",
        )
        assert count == 0
        assert warnings == []
        assert minio_client.put_object.call_count == 0

    def test_minio_failure_records_quarantine_failed_warning(self) -> None:
        from unittest.mock import MagicMock

        from audittrace.routes.memory import _quarantine_pdf_attachments

        doc = MagicMock()
        doc.embfile_count.return_value = 1
        doc.embfile_info.return_value = {"filename": "x.bin", "mime": "x"}
        doc.embfile_get.return_value = b"data"

        minio_client = MagicMock()
        minio_client.put_object.side_effect = RuntimeError("MinIO down")

        count, warnings = _quarantine_pdf_attachments(
            doc,
            parent_filename="main.pdf",
            layer_prefix="episodic/",
            minio_client=minio_client,
            bucket="memory-shared",
        )
        assert count == 0
        assert len(warnings) == 1
        assert warnings[0]["code"] == "attachment_quarantine_failed"

    def test_excessive_attachment_count_capped(self) -> None:
        from unittest.mock import MagicMock

        from audittrace.routes.memory import _quarantine_pdf_attachments

        doc = MagicMock()
        doc.embfile_count.return_value = 10_000
        minio_client = MagicMock()
        count, warnings = _quarantine_pdf_attachments(
            doc,
            parent_filename="main.pdf",
            layer_prefix="episodic/",
            minio_client=minio_client,
            bucket="memory-shared",
        )
        assert count == 0
        assert len(warnings) == 1
        assert warnings[0]["code"] == "attachment_quarantine_failed"
        assert warnings[0]["error"] == "too_many_attachments"
        # Critical: never call put_object for any of the 10k declared.
        assert minio_client.put_object.call_count == 0


class TestOcrRenderPage:
    """Tier-B item #1 — OCR fallback for raster-only pages."""

    def test_ocr_disabled_returns_no_text_layer(self) -> None:
        from unittest.mock import MagicMock

        from audittrace.routes.memory import _ocr_render_page

        page = MagicMock()
        text, source, conf = _ocr_render_page(
            page, enabled=False, languages="eng", dpi=300
        )
        assert text == ""
        assert source == "no_text_layer"
        assert conf == 0.0

    def test_pytesseract_missing_returns_no_text_layer(self) -> None:
        """Graceful degradation: if pytesseract is not importable,
        the helper returns a no_text_layer signal rather than crashing.
        Simulated by patching the pytesseract import to raise."""
        import builtins
        from unittest.mock import MagicMock, patch

        from audittrace.routes.memory import _ocr_render_page

        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "pytesseract":
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        page = MagicMock()
        with patch("builtins.__import__", side_effect=fake_import):
            text, source, conf = _ocr_render_page(
                page, enabled=True, languages="eng", dpi=300
            )
        assert text == ""
        assert source == "no_text_layer"
        assert conf == 0.0

    def test_ocr_succeeds_returns_text_and_confidence(self) -> None:
        """When Tesseract returns recognised words, the helper emits
        the joined text + mean confidence in [0,1]."""
        from unittest.mock import MagicMock, patch

        from audittrace.routes.memory import _ocr_render_page

        page = MagicMock()
        # page.get_pixmap → pix; pix.tobytes → real PNG-ish bytes
        # are not needed because we mock pytesseract.image_to_data.
        page.get_pixmap.return_value.tobytes.return_value = b"fake-png-bytes"

        fake_data = {
            "text": ["Hello", "", "world", "!"],
            "conf": [95, -1, 90, 80],
        }
        # We need PIL.Image.open to succeed; mock it to return a
        # MagicMock the rest of the code path doesn't inspect.
        with patch("audittrace.routes.memory.io.BytesIO") as mock_bytesio:
            mock_bytesio.return_value = b"any"
            with patch.dict(
                "sys.modules",
                {
                    "pytesseract": MagicMock(
                        image_to_data=MagicMock(return_value=fake_data),
                        Output=MagicMock(DICT="dict"),
                    ),
                    "PIL": MagicMock(),
                    "PIL.Image": MagicMock(),
                },
            ):
                text, source, conf = _ocr_render_page(
                    page, enabled=True, languages="eng", dpi=300
                )
        # "Hello" "world" "!" — the empty-word and -1-conf entries
        # are filtered. Mean of [95, 90, 80] / 100 = 0.883.
        assert source == "ocr"
        assert "Hello" in text
        assert "world" in text
        # Mean conf is (95+90+80)/3/100 ≈ 0.883
        assert 0.85 < conf < 0.90


class TestPdfManifestColumnsLive:
    """Tier-B item #22 — every successful PDF index call lands one
    ``upsert_pdf_metadata`` row carrying the structured fields."""

    def test_clean_pdf_index_writes_manifest_row(self, client: TestClient) -> None:
        """End-to-end through the route: fake a clean 1-page PDF,
        run /memory/index, assert manifest.upsert_pdf_metadata was
        called once with the expected shape."""
        from unittest.mock import MagicMock, patch

        raw_bytes = b"%PDF-1.4 fake-content"

        mock_minio = MagicMock()

        def list_objects(bucket: str, prefix: str = "", **_kw: Any) -> list[Any]:
            if prefix == "episodic/":
                return [_mock_minio_object("episodic/clean.pdf")]
            return []

        mock_minio.list_objects.side_effect = list_objects
        response_obj = MagicMock()
        response_obj.read.return_value = raw_bytes
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj

        mock_collection = MagicMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection.return_value = mock_collection

        rect_mock = MagicMock(x0=0.0, y0=0.0, x1=612.0, y1=792.0)
        fake_page = MagicMock()
        fake_page.get_text.return_value = "Body text of clean page."
        fake_page.rect = rect_mock
        fake_page.widgets.return_value = []
        fake_page.get_images.return_value = []

        fake_doc = MagicMock()
        fake_doc.__iter__.return_value = iter([fake_page])
        fake_doc.__enter__.return_value = fake_doc
        fake_doc.__exit__.return_value = None
        fake_doc.page_count = 1
        fake_doc.xref_length.return_value = 10
        fake_doc.is_encrypted = False
        fake_doc.needs_pass = False
        fake_doc.embfile_count.return_value = 0

        fake_pymupdf = MagicMock()
        fake_pymupdf.open.return_value = fake_doc

        mock_manifest = MagicMock()

        with (
            patch(
                "audittrace.routes.memory._get_minio_client", return_value=mock_minio
            ),
            patch("audittrace.routes.memory.get_chromadb", return_value=mock_chroma),
            patch(
                "audittrace.routes.memory.get_memory_manifest_service",
                return_value=mock_manifest,
            ),
            patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "ai_research_papers"},
            )

        assert response.status_code == 200, response.text
        # Manifest service was called once for the single PDF.
        mock_manifest.upsert_pdf_metadata.assert_called_once()
        kwargs = mock_manifest.upsert_pdf_metadata.call_args.kwargs
        # Tier-B columns are populated with the right shapes.
        assert mock_manifest.upsert_pdf_metadata.call_args.args == (
            "episodic",
            "episodic/clean.pdf",
        )
        assert kwargs["page_count"] == 1
        assert kwargs["attachment_count"] == 0
        assert kwargs["form_field_count"] == 0
        assert kwargs["ocr_coverage_pct"] == 0.0
        # document_sha256 matches the raw bytes hash.
        import hashlib

        assert kwargs["document_sha256"] == hashlib.sha256(raw_bytes).hexdigest()
        # Warnings list is empty for a clean document.
        assert kwargs["extraction_warnings"] == []

    def test_encrypted_pdf_writes_manifest_with_warning(
        self, client: TestClient
    ) -> None:
        """An encrypted PDF: no chunks emitted, but the manifest
        still records the refusal so an auditor can answer 'why
        did this PDF produce zero chunks?'."""
        from unittest.mock import MagicMock, patch

        raw_bytes = b"%PDF-1.4 encrypted-stub"

        mock_minio = MagicMock()
        mock_minio.list_objects.side_effect = lambda bucket, prefix="", **_: (
            [_mock_minio_object("episodic/locked.pdf")] if prefix == "episodic/" else []
        )
        response_obj = MagicMock()
        response_obj.read.return_value = raw_bytes
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj

        mock_collection = MagicMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection.return_value = mock_collection

        fake_doc = MagicMock()
        fake_doc.__enter__.return_value = fake_doc
        fake_doc.__exit__.return_value = None
        fake_doc.is_encrypted = True
        fake_doc.needs_pass = True

        fake_pymupdf = MagicMock()
        fake_pymupdf.open.return_value = fake_doc

        mock_manifest = MagicMock()

        with (
            patch(
                "audittrace.routes.memory._get_minio_client", return_value=mock_minio
            ),
            patch("audittrace.routes.memory.get_chromadb", return_value=mock_chroma),
            patch(
                "audittrace.routes.memory.get_memory_manifest_service",
                return_value=mock_manifest,
            ),
            patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "ai_research_papers"},
            )

        assert response.status_code == 200, response.text
        # Zero chunks (encrypted file is refused).
        assert mock_collection.upsert.call_count == 0
        # Manifest still recorded the refusal — that's the audit-grade
        # contract: every indexed key gets a manifest row, even on refuse.
        mock_manifest.upsert_pdf_metadata.assert_called_once()
        warnings = mock_manifest.upsert_pdf_metadata.call_args.kwargs[
            "extraction_warnings"
        ]
        assert any(w.get("code") == "encrypted" for w in warnings)


class TestPdfFlushManifest:
    """Tier-B item #22 — ``_flush_pdf_manifest`` resilience: missing
    service is a no-op; service errors are logged + swallowed."""

    def test_none_service_is_silent_noop(self) -> None:
        from audittrace.routes.memory import _flush_pdf_manifest

        # Should not raise. No assertion needed beyond "did not crash".
        _flush_pdf_manifest(
            manifest_service=None,
            layer="episodic",
            key="x.pdf",
            user_id="u",
            size_bytes=100,
            page_count=1,
            signature_status="check_skipped",
            ocr_coverage_pct=None,
            attachment_count=0,
            form_field_count=0,
            extraction_warnings=[],
            document_sha256="hash",
        )

    def test_service_failure_is_swallowed(self) -> None:
        """Postgres outage during manifest write must not undo the
        ChromaDB chunk writes already committed. Per ADR-050 §#22."""
        from unittest.mock import MagicMock

        from audittrace.routes.memory import _flush_pdf_manifest

        manifest = MagicMock()
        manifest.upsert_pdf_metadata.side_effect = RuntimeError("pg down")
        # Should NOT raise.
        _flush_pdf_manifest(
            manifest_service=manifest,
            layer="episodic",
            key="x.pdf",
            user_id="u",
            size_bytes=100,
            page_count=1,
            signature_status="check_skipped",
            ocr_coverage_pct=None,
            attachment_count=0,
            form_field_count=0,
            extraction_warnings=[],
            document_sha256="hash",
        )
        manifest.upsert_pdf_metadata.assert_called_once()
