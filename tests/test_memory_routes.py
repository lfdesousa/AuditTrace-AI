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
