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
    """POST /memory/upload requires audittrace:admin scope."""

    def test_upload_requires_admin_scope_no_token(self, client: TestClient) -> None:
        """Request without a bearer token is rejected when auth is enabled."""
        with patch("audittrace.auth.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(auth_enabled=True)
            response = client.post(
                "/memory/upload",
                params={"layer": "episodic"},
                files=_make_upload_file(),
            )
        assert response.status_code == 401

    def test_upload_requires_admin_scope_wrong_scope(self, client: TestClient) -> None:
        """Token with insufficient scope is rejected (403)."""
        with (
            patch("audittrace.auth.get_settings") as mock_settings,
            patch("audittrace.auth._get_jwks_keys") as mock_jwks,
            patch("audittrace.auth._decode_jwt_with_allowed_issuers") as mock_decode,
        ):
            mock_settings.return_value = MagicMock(auth_enabled=True)
            mock_jwks.return_value = ["fake-key"]
            mock_decode.return_value = {"scope": "audittrace:query"}
            response = client.post(
                "/memory/upload",
                params={"layer": "episodic"},
                files=_make_upload_file(),
                headers={"Authorization": "Bearer fake-token"},
            )
        assert response.status_code == 403


class TestIndexAuth:
    """POST /memory/index requires audittrace:admin scope."""

    def test_index_requires_admin_scope_no_token(self, client: TestClient) -> None:
        with patch("audittrace.auth.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(auth_enabled=True)
            response = client.post("/memory/index")
        assert response.status_code == 401


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

        def list_objects(bucket: str, prefix: str = "") -> list[Any]:
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

        # ChromaDB collection.add must have been called
        assert mock_collection.add.call_count >= 1

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

        def list_objects(bucket: str, prefix: str = "") -> list[Any]:
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

        def list_objects(bucket: str, prefix: str = "") -> list[Any]:
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

    def test_index_delete_collection_exception_swallowed(
        self, client: TestClient
    ) -> None:
        """delete_collection failure is swallowed so the create succeeds."""
        mock_minio = MagicMock()

        def list_objects(bucket: str, prefix: str = "") -> list[Any]:
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
        mock_collection.add.assert_called()
