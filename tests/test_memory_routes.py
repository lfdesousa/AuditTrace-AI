"""Tests for POST /memory/upload and POST /memory/index routes."""

from __future__ import annotations

import json
from datetime import UTC
from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _mock_nomic_embed(monkeypatch):
    """ADR-047 — /memory/index vectorises chunks on the nomic server via
    ``_upsert_in_batches``. Stub it so these route tests stay offline; the
    mock ChromaDB ignores the supplied embeddings anyway."""
    monkeypatch.setattr(
        "audittrace.routes.memory.embed_via_nomic",
        AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
    )


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
        # ADR-062 Phase B (WU-B5): private-tier key, not the shared prefix.
        assert response.json()["key"] == "test-user/episodic/doc.md"

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
        mock_chroma.get_or_create_collection = AsyncMock(return_value=AsyncMock())
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])
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
    """POST /memory/upload stores files via the episodic/procedural
    service's PRIVATE-tier write (ADR-062 Phase B, WU-B5).

    Pre-WU-B5 these asserted a direct ``minio_client.put_object`` call
    into the shared/corpus bucket unconditionally — that WAS the leak
    WU-B5 closes (every /memory/upload landed visible to every caller
    regardless of who uploaded). ``SENTINEL_SUBJECT`` is the bypass-mode
    caller identity (``require_user`` with auth disabled — the default
    in this test fixture)."""

    _SENTINEL = "00000000-0000-0000-0000-000000000001"

    @pytest.fixture(autouse=True)
    def _no_real_minio(self):
        """``upload_memory_file`` resolves the object-storage client
        unconditionally near the top (needed by the PDF branch) before
        it even knows whether this upload is PDF or not — patch it out
        so the non-PDF tests below don't need real MinIO/AWS creds."""
        with patch(
            "audittrace.routes.memory._get_minio_client", return_value=MagicMock()
        ):
            yield

    def test_upload_stores_in_minio(self, client: TestClient) -> None:
        """Verify the private-tier write + response shape."""
        response = client.post(
            "/memory/upload",
            params={"layer": "episodic"},
            files=_make_upload_file(b"hello world", "ADR-042.md"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "uploaded"
        assert data["tier"] == "private"
        assert data["key"] == f"{self._SENTINEL}/episodic/ADR-042.md"
        assert data["size_bytes"] == len(b"hello world")
        assert data["bucket"] == "memory-private"

        # Round-trips through the private tier: the caller reads back
        # their own upload without needing a fresh manifest row.
        from audittrace.dependencies import get_episodic_service

        ep = get_episodic_service()
        content = [
            d for d in ep._private[self._SENTINEL] if d.metadata["file"] == "ADR-042.md"
        ]
        assert len(content) == 1
        assert content[0].page_content == "hello world"
        assert content[0].metadata["tier"] == "private"

    def test_upload_procedural_layer(self, client: TestClient) -> None:
        """Procedural layer routes to the caller's private procedural tier."""
        response = client.post(
            "/memory/upload",
            params={"layer": "procedural"},
            files=_make_upload_file(b"skill doc", "SKILL-deploy.md"),
        )

        assert response.status_code == 200
        assert response.json()["key"] == f"{self._SENTINEL}/procedural/SKILL-deploy.md"
        assert response.json()["tier"] == "private"

    def test_upload_with_filename_override(self, client: TestClient) -> None:
        """Explicit filename param overrides the upload filename."""
        response = client.post(
            "/memory/upload",
            params={"layer": "episodic", "filename": "ADR-099.md"},
            files=_make_upload_file(b"content", "original.md"),
        )

        assert response.status_code == 200
        assert response.json()["key"] == f"{self._SENTINEL}/episodic/ADR-099.md"

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
        """When the private-tier object-storage write fails, 502."""
        from audittrace.dependencies import get_episodic_service

        async def _boom(user, file, content):  # noqa: ANN001, ARG001
            raise RuntimeError("connection refused")

        with patch.object(get_episodic_service(), "write", side_effect=_boom):
            response = client.post(
                "/memory/upload",
                params={"layer": "episodic"},
                files=_make_upload_file(),
            )
        assert response.status_code == 502

    def test_upload_non_utf8_content_returns_400(self, client: TestClient) -> None:
        """Non-PDF /memory/upload content must be UTF-8 text — binary
        garbage that fails to decode is rejected cleanly (400), not a
        500 from an unhandled UnicodeDecodeError."""
        response = client.post(
            "/memory/upload",
            params={"layer": "episodic"},
            files=_make_upload_file(b"\xff\xfe\x00binary", "ADR-bin.md"),
        )
        assert response.status_code == 400

    def test_upload_promote_corpus_rejected_for_episodic(
        self, client: TestClient
    ) -> None:
        """ADR-062 Phase B (WU-B5): episodic has no declared corpus-write
        scope — ``?promote=corpus`` is rejected (400), never silently
        accepted or silently downgraded."""
        response = client.post(
            "/memory/upload",
            params={"layer": "episodic", "promote": "corpus"},
            files=_make_upload_file(),
        )
        assert response.status_code == 400

    def test_upload_promote_corpus_rejected_for_procedural(
        self, client: TestClient
    ) -> None:
        response = client.post(
            "/memory/upload",
            params={"layer": "procedural", "promote": "corpus"},
            files=_make_upload_file(),
        )
        assert response.status_code == 400


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
        # AsyncMock: the real chroma client is async (AsyncHttpClient), so
        # get_or_create_collection + the collection's upsert/get/count are
        # coroutines. A sync MagicMock here masked the #263 gap where the
        # sync /memory/index handler never awaited them → 'coroutine' object
        # has no attribute 'upsert' (live laptop 500, 2026-05-31).
        mock_collection = AsyncMock()
        mock_collection.count.return_value = 0
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()

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
        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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
        mock_chroma.get_or_create_collection = AsyncMock(return_value=AsyncMock())
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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


class TestM2WriteTelemetryWiring:
    """Route-level proof that the M2 write-telemetry emit calls are
    actually wired into POST /memory/upload and POST /memory/index — not
    merely exercised via a direct unit call into
    ``write_telemetry.emit_*``. ``test_write_telemetry.py`` proves the
    helper functions behave correctly in isolation; these prove the
    ROUTES call them.

    Spies patch the import SITE inside ``audittrace.routes.memory``
    (``audittrace.routes.memory.emit_memory_write`` /
    ``.emit_chunks_indexed``), not the ``write_telemetry`` module those
    names were imported from — ``routes/memory.py`` does
    ``from audittrace.services.write_telemetry import emit_memory_write,
    emit_chunks_indexed``, which binds a NEW name in the ``routes.memory``
    module namespace; patching the origin module would leave that bound
    name untouched and the spy would never be called (review fix,
    2026-08-09 — a route-deletion neuter of both call sites passed the
    full suite before this class existed)."""

    def test_upload_emits_memory_write_with_layer(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST /memory/upload must call
        ``emit_memory_write(layer=<resolved layer>)`` exactly once."""
        spy = MagicMock()
        monkeypatch.setattr("audittrace.routes.memory.emit_memory_write", spy)

        with patch(
            "audittrace.routes.memory._get_minio_client", return_value=MagicMock()
        ):
            response = client.post(
                "/memory/upload",
                params={"layer": "procedural"},
                files=_make_upload_file(b"skill doc", "SKILL-emit-check.md"),
            )

        assert response.status_code == 200
        spy.assert_called_once_with(layer="procedural")

    def test_index_emits_chunks_indexed_with_matching_count(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST /memory/index must call
        ``emit_chunks_indexed(collection=<name>, chunk_count=<n>)`` for the
        indexed collection, with ``chunk_count`` equal to the same number
        the response reports for that collection."""
        mock_minio = MagicMock()

        def list_objects(bucket: str, prefix: str = "", **_kw: Any) -> list[Any]:
            if prefix == "episodic/":
                return [_mock_minio_object("episodic/ADR-emit-check.md")]
            return []

        mock_minio.list_objects.side_effect = list_objects

        def get_object(bucket: str, key: str) -> MagicMock:
            response = MagicMock()
            response.read.return_value = b"# Test document\nSome content here."
            return response

        mock_minio.get_object.side_effect = get_object

        mock_collection = AsyncMock()
        mock_collection.count.return_value = 0
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()

        spy = MagicMock()
        monkeypatch.setattr("audittrace.routes.memory.emit_chunks_indexed", spy)

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
        expected_chunk_count = response.json()["collections"]["decisions"]
        assert expected_chunk_count > 0, (
            "fixture must produce at least one chunk for this assertion to "
            "be non-vacuous"
        )
        spy.assert_called_once_with(
            collection="decisions", chunk_count=expected_chunk_count
        )


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
    """Cover the _get_minio_client helper.

    Post-ADR-006 the helper returns the DI-container's singleton
    object-storage provider, with a fallback that builds one on demand
    via the shared factory. The Minio class is no longer imported in
    routes/memory.py.
    """

    def test_returns_cached_provider_from_container(self) -> None:
        from audittrace.dependencies import container
        from audittrace.routes.memory import _get_minio_client

        sentinel = object()
        container._instances["object_storage"] = sentinel
        try:
            assert _get_minio_client() is sentinel
        finally:
            container._instances.pop("object_storage", None)

    def test_falls_back_to_factory_when_container_empty(self) -> None:
        from audittrace.dependencies import container
        from audittrace.routes.memory import _get_minio_client

        # Ensure the container has no cached provider.
        container._instances.pop("object_storage", None)

        fake_provider = object()
        with patch(
            "audittrace.dependencies._create_object_storage_provider",
            return_value=fake_provider,
        ):
            assert _get_minio_client() is fake_provider


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
        mock_chroma.get_or_create_collection = AsyncMock(return_value=AsyncMock())
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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
        mock_chroma.get_or_create_collection = AsyncMock(return_value=AsyncMock())
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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

    def test_list_objects_walks_full_subtree(self, client: TestClient) -> None:
        """Post-ADR-006: the ABC contract for ``list_objects`` GUARANTEES
        full-subtree walking; the legacy ``recursive=True`` kwarg is no
        longer accepted (or needed) by the provider. The MinIO backend
        passes ``recursive=True`` internally to minio-py; the AWS backend
        uses the boto3 paginator which is naturally recursive. So this
        test now asserts that the route DOES NOT pass ``recursive=``
        explicitly (would TypeError on the new ABC) and the call still
        succeeds — proving the contract is upstream of the route.

        Caught live 2026-05-06: pre-ADR-006 the route had to pass
        ``recursive=True``; ai_research_papers corpus PDFs at
        ``episodic/papers/books/foo.pdf`` would otherwise silently
        return zero objects.
        """
        mock_minio = MagicMock()
        mock_minio.list_objects.return_value = []
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=AsyncMock())
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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

        # The route must NOT pass `recursive=` — the ABC's contract
        # guarantees recursion. Any explicit kwarg would crash on AWS.
        assert mock_minio.list_objects.call_count >= 1
        for call in mock_minio.list_objects.call_args_list:
            assert "recursive" not in call.kwargs, (
                f"route passed recursive= explicitly: {call!r}. "
                "Drop it — the ABC handles recursion contractually."
            )

    def test_index_rejects_unknown_collection(self, client: TestClient) -> None:
        """`?collections=` validates against the known set; unknown
        names 400 with a clear message rather than silently no-op."""
        mock_minio = MagicMock()
        mock_minio.list_objects.return_value = []
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=AsyncMock())
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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
        mock_chroma.get_or_create_collection = AsyncMock(return_value=AsyncMock())
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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

        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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

        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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

        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.delete_collection = AsyncMock(side_effect=Exception("not found"))
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.list_collections = AsyncMock(return_value=[])

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


class TestIndexZeroChunksLoud:
    """POST /memory/index single-file mode is LOUD (422) when a write
    ATTEMPT indexes 0 chunks (#430 — a content/layer vs target-collection
    mismatch used to return a cheerful 200 that nothing landed). Five
    falsifiable gates from SPEC #430, one test each. Uses bypass-mode
    auth (default ``client`` fixture, admin sentinel identity) — the
    auth surface itself is already covered by TestIndexAuth /
    TestIndexPrivateTier."""

    @staticmethod
    def _mock_chroma() -> tuple[MagicMock, AsyncMock]:
        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])
        return mock_chroma, mock_collection

    def test_single_file_zero_chunks_is_422(self, client: TestClient) -> None:
        """Gate 1 — the repro: a ``procedural/`` key indexed into
        ``collections=decisions`` (``decisions`` only draws from episodic
        objects, so a procedural key resolves to zero) yields 0 chunks ->
        422, and the detail names both the file and the target
        collections. Neuter: drop the loud-fail block added after
        ``_emit_write_audit`` (reverting to the unconditional
        ``"status": "indexed"`` 200) -> this test goes RED (expects 422,
        gets 200)."""
        mock_minio = MagicMock()
        mock_chroma, _mock_collection = self._mock_chroma()

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
                params={"collections": "decisions", "file": "procedural/x.md"},
            )

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "procedural/x.md" in detail
        assert "decisions" in detail
        # decisions only draws from episodic objects; a procedural key
        # never reaches a MinIO read on this branch.
        mock_minio.get_object.assert_not_called()

    def test_single_file_success_unchanged(self, client: TestClient) -> None:
        """Gate 2 — the happy path is untouched: a matching single-file
        index (an episodic key into ``decisions``) still 200s with
        ``status: indexed`` and ``total_chunks > 0``. Proves the fix does
        not regress the case it must never touch."""
        mock_minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = b"# A note\nsome real body text"
        mock_minio.get_object.return_value = response_obj
        mock_chroma, mock_collection = self._mock_chroma()

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
                params={"collections": "decisions", "file": "episodic/ADR-430.md"},
            )

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "indexed"
        assert data["total_chunks"] > 0
        mock_collection.upsert.assert_called()

    def test_bulk_zero_chunks_still_200(self, client: TestClient) -> None:
        """Gate 3 — bulk mode (``?file`` absent) with 0 matching objects
        still 200s ``{status: indexed, total_chunks: 0}``: an admin
        whole-collection rebuild that legitimately finds nothing to
        rebuild is not a failure. Neuter: drop the ``single_file_mode
        and`` clause from the guard so it fires on ``total_chunks == 0``
        alone -> this test goes RED (expects 200, gets 422)."""
        mock_minio = MagicMock()
        mock_minio.list_objects.return_value = []
        mock_chroma, _mock_collection = self._mock_chroma()

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
            response = client.post("/memory/index", params={"collections": "decisions"})

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "indexed"
        assert data["total_chunks"] == 0

    def test_dry_run_zero_chunks_still_ok(self, client: TestClient) -> None:
        """Gate 4 — a dry-run single-file index that would have
        0-chunked (the same layer/collection mismatch as Gate 1, plus
        ``dry_run=true``) still 200s ``{status: dry_run}``: no write was
        ever intended, so the loud-fail guard must not fire."""
        mock_minio = MagicMock()
        mock_chroma, _mock_collection = self._mock_chroma()

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
                    "file": "procedural/x.md",
                    "dry_run": "true",
                },
            )

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "dry_run"
        assert data["total_chunks"] == 0

    def test_audit_still_emitted_on_zero_chunk_422(self, client: TestClient) -> None:
        """Gate 5 — the write ATTEMPT is audited (ADR-058) BEFORE the 422
        is raised: the same 0-chunk mismatch request that 422s still
        produces a ``memory_access`` write-audit row naming
        ``mode: single_file`` with ``total_chunks: 0`` in its
        ``detail_extra`` — the audit trail records the attempt even
        though the caller sees a loud failure, not a false success."""
        mock_minio = MagicMock()
        mock_chroma, _mock_collection = self._mock_chroma()

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
                params={"collections": "decisions", "file": "procedural/y.md"},
            )
        assert response.status_code == 422, response.text

        rows = client.get(
            "/interactions", params={"event_class": "memory_access"}
        ).json()["interactions"]
        writes = [
            r
            for r in rows
            if r["question"].startswith("op=write layer=semantic")
            and r.get("error_detail")
            and json.loads(r["error_detail"]).get("mode") == "single_file"
        ]
        assert writes, rows
        detail = json.loads(writes[-1]["error_detail"])
        assert detail["total_chunks"] == 0

    def test_single_file_blank_pdf_benign_zero_chunks_is_200(
        self, client: TestClient
    ) -> None:
        """Gate 6 (REVIEW REJECT 2026-08-07 fix) — a single-file index of a
        text-free AND image-free PDF page into ``ai_research_papers`` is
        NOT a mismatch: the PDF pipeline legitimately marks such a page
        ``ok=True, "benign, no warning", chunks_written=0``
        (memory_pdf/pipeline.py:511-513, the "truly empty page" branch).
        The object WAS routed to and processed by the ``ai_research_papers``
        indexer — it just had nothing to embed — so this must 200, not 422.

        Neuter: revert the guard's discriminator from ``not
        any(object_outcomes)`` (v3) back to ``total_chunks == 0`` (v1) ->
        this test goes RED (a benign 0-chunk PDF wrongly 422s, reproducing
        the reviewer's live repro `file=episodic/papers/blank.pdf&
        collections=ai_research_papers`)."""
        from unittest.mock import MagicMock, patch

        raw_bytes = b"%PDF-1.4 blank-page-stub"

        mock_minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = raw_bytes
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj

        mock_chroma, mock_collection = self._mock_chroma()

        rect_mock = MagicMock(x0=0.0, y0=0.0, x1=612.0, y1=792.0)
        fake_page = MagicMock()
        fake_page.get_text.return_value = ""  # text-free
        fake_page.rect = rect_mock
        fake_page.widgets.return_value = []
        fake_page.get_images.return_value = []  # image-free

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
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
            patch(
                "audittrace.routes.memory.get_memory_manifest_service",
                return_value=mock_manifest,
            ),
            patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
        ):
            response = client.post(
                "/memory/index",
                params={
                    "collections": "ai_research_papers",
                    "file": "episodic/papers/blank.pdf",
                    "details": "true",
                },
            )

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "indexed"
        assert data["total_chunks"] == 0
        # The object WAS routed to and processed by the PDF indexer — the
        # manifest write was attempted (proving dispatch happened) and the
        # ``?details=true`` per-document outcome shows ``ok: True`` with
        # ``chunks: 0``, the "benign, no warning" path, not a rejection.
        mock_manifest.upsert_pdf_metadata.assert_called_once()
        assert data["documents"] == [
            {
                "file": "episodic/papers/blank.pdf",
                "chunks": 0,
                "signature_status": data["documents"][0]["signature_status"],
                "page_count": 1,
                "extraction_warnings": [],
                "document_sha256": data["documents"][0]["document_sha256"],
                "pdf_title": None,
                "pdf_author": None,
                "pdf_creator": None,
                "pdf_creation_date": None,
                "pdfa_part": None,
                "pdfa_conformance": None,
                "ltv_data": None,
                "ok": True,
                "error": None,
            }
        ]
        mock_collection.upsert.assert_not_called()

    def test_single_file_extension_mismatch_is_422(self, client: TestClient) -> None:
        """Gate 7 (REVIEW REJECT #2 2026-08-07 fix) — a single-file index
        of a ``.pdf``-keyed object into a ``.md``-only collection
        (episodic layer -> ``decisions``) is a genuine extension
        mismatch: the object IS routed to ``_index_md_objects`` (the
        layer matches ``decisions``) but is REJECTED there on the
        ``.md`` suffix check (memory.py's extension guard) before any
        MinIO read. v2 wrongly 200d this — reviewer-proven live:
        ``?collections=decisions&file=episodic/foo.pdf`` — because its
        ``objects_attempted`` counter only measured "was the layer
        routed", not "was the object accepted". Must 422.

        Neuter: revert the guard's discriminator from ``not
        any(object_outcomes)`` back to v2's ``objects_attempted == 0``
        -> this test goes RED (expects 422, gets 200 — the object was
        routed, so v2's counter is nonzero even though the object was
        rejected on extension)."""
        mock_minio = MagicMock()
        mock_chroma, _mock_collection = self._mock_chroma()

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
                params={"collections": "decisions", "file": "episodic/foo.pdf"},
            )

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "episodic/foo.pdf" in detail
        assert "decisions" in detail
        # The extension check rejects the object before any MinIO read.
        mock_minio.get_object.assert_not_called()

    def test_single_file_corrupted_pdf_is_422(self, client: TestClient) -> None:
        """Gate 8 (REVIEW REJECT #2 2026-08-07 fix) — a single-file index
        of a corrupted PDF (pymupdf raises mid-parse) into
        ``ai_research_papers`` is a genuine processing failure: the
        object IS routed to ``_index_pdf_objects`` and read
        successfully, but the pipeline's outer ``except Exception``
        classifies the raise and flushes ``ok=False``
        (memory_pdf/pipeline.py's exception handler). v2 wrongly 200d
        this — reviewer-proven live:
        ``?collections=ai_research_papers&file=episodic/broken.pdf`` —
        for the same reason as Gate 7: routed-but-rejected still counted
        as "attempted" under v2. Must 422.

        Neuter: revert the guard's discriminator from ``not
        any(object_outcomes)`` back to v2's ``objects_attempted == 0``
        -> this test goes RED (expects 422, gets 200)."""
        raw_bytes = b"%PDF-1.4 garbage-pretending-to-be-pdf"

        mock_minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = raw_bytes
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj

        fake_pymupdf = MagicMock()
        fake_pymupdf.open.side_effect = RuntimeError("invalid xref offset")

        mock_chroma, mock_collection = self._mock_chroma()
        mock_manifest = MagicMock()
        mock_manifest.upsert_pdf_metadata = AsyncMock()

        with (
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
            patch(
                "audittrace.routes.memory.get_memory_manifest_service",
                return_value=mock_manifest,
            ),
            patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
        ):
            response = client.post(
                "/memory/index",
                params={
                    "collections": "ai_research_papers",
                    "file": "episodic/broken.pdf",
                },
            )

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "episodic/broken.pdf" in detail
        assert "ai_research_papers" in detail
        mock_collection.upsert.assert_not_called()


class TestIndexGap2AutoRoutePdf:
    """SPEC #387 Phase 1 (WU-4) — GAP-2 closure: a single-file
    ``/memory/index?file=`` call with NO ``?collections=`` auto-routes by
    content type instead of falling through to the bulk-mode ``.md``-only
    default (which either 400s here, since that default has 3 collections
    and single-file mode requires exactly 1, or — for a caller who *did*
    pick one of the three — silently accepts 0 chunks for a PDF).

    Neuter: revert the ``collection_for_key(file)`` branch in
    ``index_memory`` back to unconditionally defaulting to
    ``list(_DEFAULT_COLLECTIONS)`` -> both tests below go RED (400,
    "requires exactly one collection in ?collections=")."""

    @staticmethod
    def _mock_chroma() -> tuple[MagicMock, AsyncMock]:
        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])
        return mock_chroma, mock_collection

    def test_promoted_pdf_default_index_routes_to_ai_research_papers(
        self, client: TestClient
    ) -> None:
        """The exact zero-manual-touch shape: a promoted, scanned-clean
        PDF's key posted to ``/memory/index?file=...`` with NO
        ``?collections=`` at all — the plain operator default call."""
        from unittest.mock import MagicMock, patch

        raw_bytes = b"%PDF-1.4 fake-content"
        mock_minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = raw_bytes
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj

        mock_chroma, mock_collection = self._mock_chroma()

        rect_mock = MagicMock(x0=0.0, y0=0.0, x1=612.0, y1=792.0)
        fake_page = MagicMock()
        fake_page.get_text.return_value = "Body text of a promoted paper."
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
                "audittrace.routes.memory._get_minio_client",
                return_value=mock_minio,
            ),
            patch(
                "audittrace.routes.memory.get_chromadb",
                return_value=mock_chroma,
            ),
            patch(
                "audittrace.routes.memory.get_memory_manifest_service",
                return_value=mock_manifest,
            ),
            patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
        ):
            response = client.post(
                "/memory/index",
                params={"file": "episodic/papers/scan-1/report.pdf"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        # Routed to ai_research_papers — NOT the .md-only default set —
        # and it actually wrote chunks (not the silent 0-chunk no-op).
        assert body["collections"] == {"ai_research_papers": 1}
        assert body["total_chunks"] == 1
        mock_collection.upsert.assert_called_once()
        mock_manifest.upsert_pdf_metadata.assert_called_once()

    def test_md_file_default_index_still_routes_to_semantic(
        self, client: TestClient
    ) -> None:
        """The same auto-routing fix for a non-PDF key: falls to
        ``semantic`` (the general ``.md`` collection), not the 3-item
        bulk default — single-file mode requires exactly one collection
        either way, so this must 200, not 400."""
        from unittest.mock import MagicMock, patch

        mock_minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = b"# A note\n\nSome content."
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj

        mock_chroma, mock_collection = self._mock_chroma()

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
                params={"file": "episodic/note.md"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body["collections"]) == {"semantic"}
        assert body["total_chunks"] >= 1
        mock_collection.upsert.assert_called_once()


class TestIndexPrivateTier:
    """POST /memory/index single-file mode accepts private-tier keys
    (ADR-062 Phase B regression, #426).

    Since WU-B5, ``POST /memory/upload`` writes to the caller's PRIVATE
    tier and returns ``key = "{jwt.sub}/{layer}/{filename}"``. Tier is
    disambiguated by testing whether ``?file=`` starts with the TOKEN
    sub (never the caller-supplied value, per ADR-027 §1) — see the
    tier-disambiguation block at the top of ``index_memory``. Six
    falsifiable gates from SPEC #426, one test class member each.
    """

    @staticmethod
    def _mock_chroma() -> tuple[MagicMock, AsyncMock]:
        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])
        return mock_chroma, mock_collection

    def test_private_tier_index_accepted(self, client: TestClient) -> None:
        """Gate 1 — ``?file={token_sub}/episodic/foo.md`` against a
        private object indexes successfully (200, >=1 chunk) and the
        upserted vector is stamped ``user_id=<token_sub>``. Neuter: revert
        the tier-anchored parser to the old ``file.split("/", 1)`` ->
        400 -> RED."""
        mock_minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = b"# private note\nsome body"
        mock_minio.get_object.return_value = response_obj
        mock_chroma, mock_collection = self._mock_chroma()

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
                "sub": "user-abc",
                "scope": "memory:episodic:write",
            }
            response = client.post(
                "/memory/index",
                params={
                    "collections": "decisions",
                    "file": "user-abc/episodic/foo.md",
                },
                headers={"Authorization": "Bearer fake-token"},
            )

        assert response.status_code == 200, response.text
        assert response.json()["collections"]["decisions"] >= 1
        mock_collection.upsert.assert_called()
        call_kwargs = mock_collection.upsert.call_args.kwargs
        assert call_kwargs["metadatas"][0]["user_id"] == "user-abc"

    def test_private_tier_index_stamps_tier_metadata(self, client: TestClient) -> None:
        """ADR-059 fleet-recall gap (WU-1, 2026-08-07) — the token-derived
        tier resolved by ``index_memory`` (private, from the ``?file=``
        key-prefix disambiguation) must be STAMPED into every chunk's
        ChromaDB metadata, not just used for bucket routing.

        Before this fix, no ``tier`` key was ever written here. The
        backoffice discovery-merge builder
        (``_merge_semantic_with_chroma``) defaults an untagged row's
        surfaced tier to ``"corpus"``, and ``list_semantic``'s
        ``_filter_corpus_read_gate`` then drops any corpus-tagged item the
        caller lacks ``memory:corpus:<collection>:read`` for — so a
        caller's OWN genuinely-private single-file fold (e.g. the ADR-059
        fleet helper's ``log_deploy_record``) was silently misclassified
        as corpus and gated out.

        Neuter: drop the ``"tier": tier`` metadata field in
        ``_index_md_objects`` and this test goes RED."""
        mock_minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = b"# private note\nsome body"
        mock_minio.get_object.return_value = response_obj
        mock_chroma, mock_collection = self._mock_chroma()

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
                "sub": "user-abc",
                "scope": "memory:episodic:write",
            }
            response = client.post(
                "/memory/index",
                params={
                    "collections": "decisions",
                    "file": "user-abc/episodic/foo.md",
                },
                headers={"Authorization": "Bearer fake-token"},
            )

        assert response.status_code == 200, response.text
        call_kwargs = mock_collection.upsert.call_args.kwargs
        assert call_kwargs["metadatas"][0]["tier"] == "private", (
            "single-file private-tier index did not stamp tier metadata: "
            f"{call_kwargs['metadatas'][0]}"
        )

    def test_private_bucket_is_read_not_shared(self, client: TestClient) -> None:
        """Gate 2 — the private branch reads ``memory-private``, never
        ``memory-shared``. Neuter: force ``bucket = shared_bucket``
        unconditionally -> the object read targets the wrong bucket
        (object-not-found on a real MinIO) -> RED."""
        mock_minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = b"# body\ntext"
        mock_minio.get_object.return_value = response_obj
        mock_chroma, _mock_collection = self._mock_chroma()

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
                "sub": "user-abc",
                "scope": "memory:episodic:write",
            }
            response = client.post(
                "/memory/index",
                params={
                    "collections": "decisions",
                    "file": "user-abc/episodic/foo.md",
                },
                headers={"Authorization": "Bearer fake-token"},
            )

        assert response.status_code == 200, response.text
        assert mock_minio.get_object.call_count == 1
        call_args = mock_minio.get_object.call_args
        assert call_args.args[0] == "memory-private"
        assert call_args.args[1] == "user-abc/episodic/foo.md"

    def test_cross_user_private_key_rejected(self, client: TestClient) -> None:
        """Gate 3 (SECURITY, load-bearing) — a foreign sub in ``?file=``
        must NEVER be accepted as this caller's private tier. It falls to
        the corpus branch, where the foreign sub is not a valid
        MemoryLayer -> 400, and ``memory-private`` is never read for the
        victim. Neuter: take the private branch unconditionally (drop the
        ``file.startswith(token_prefix)`` test) -> cross-user read -> RED."""
        mock_minio = MagicMock()
        mock_chroma, _mock_collection = self._mock_chroma()

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
            # The caller ("attacker") holds a valid write scope for
            # episodic — the point of this gate is that scope is never
            # even reached, because the foreign sub fails layer
            # resolution first.
            mock_decode.return_value = {
                "sub": "attacker-sub",
                "scope": "memory:episodic:write",
            }
            response = client.post(
                "/memory/index",
                params={
                    "collections": "decisions",
                    "file": "victim-sub/episodic/secret.md",
                },
                headers={"Authorization": "Bearer fake-token"},
            )

        assert response.status_code == 400, response.text
        assert "known layer prefix" in response.json()["detail"]
        # The private bucket (or any bucket) must NEVER be touched when
        # tier resolution fails for a foreign sub.
        mock_minio.get_object.assert_not_called()

    def test_corpus_key_still_reads_shared_bucket(self, client: TestClient) -> None:
        """Gate 4 (backward-compat regression guard) — a legacy
        ``{layer}/{filename}`` key (no sub prefix) still resolves to the
        shared bucket, byte-identical to pre-#426 behaviour (admin bulk
        rebuild, the ``ai_research_papers`` PDF loop)."""
        mock_minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = b"# body\ntext"
        mock_minio.get_object.return_value = response_obj
        mock_chroma, mock_collection = self._mock_chroma()

        with (
            patch(
                "audittrace.routes.memory._get_minio_client", return_value=mock_minio
            ),
            patch("audittrace.routes.memory.get_chromadb", return_value=mock_chroma),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "decisions", "file": "episodic/ADR-007.md"},
            )

        assert response.status_code == 200, response.text
        mock_collection.upsert.assert_called()
        call_args = mock_minio.get_object.call_args
        assert call_args.args[0] == "memory-shared"
        assert call_args.args[1] == "episodic/ADR-007.md"

    def test_private_tier_scope_enforcement_preserved(self, client: TestClient) -> None:
        """Gate 5 — private-tier index still requires
        ``memory:<layer>:write`` (or admin); a token holding a
        DIFFERENT layer's write scope gets 403, matching the corpus
        path. Neuter: drop the ``_require_layer_write`` call -> RED."""
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
                "sub": "user-abc",
                "scope": "memory:procedural:write",
            }
            response = client.post(
                "/memory/index",
                params={
                    "collections": "decisions",
                    "file": "user-abc/episodic/foo.md",
                },
                headers={"Authorization": "Bearer fake-token"},
            )

        assert response.status_code == 403
        assert "memory:episodic:write" in response.json()["detail"]

    def test_private_tier_index_invalidates_episodic_cache(
        self, client: TestClient
    ) -> None:
        """Parity fix — cache invalidation is keyed off the RESOLVED
        layer (``layer_for_scope``), not the raw ``file`` prefix, so a
        private-tier index still self-heals the episodic list cache.
        Neuter: revert to ``file.startswith("episodic/")`` -> a private
        key never matches -> the cache is never invalidated -> RED."""
        mock_minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = b"# body\ntext"
        mock_minio.get_object.return_value = response_obj
        mock_chroma, _mock_collection = self._mock_chroma()
        mock_episodic_service = MagicMock()

        with (
            patch("audittrace.auth.get_settings") as mock_settings,
            patch("audittrace.auth._get_jwks_keys") as mock_jwks,
            patch("audittrace.auth._decode_jwt_with_allowed_issuers") as mock_decode,
            patch(
                "audittrace.routes.memory._get_minio_client", return_value=mock_minio
            ),
            patch("audittrace.routes.memory.get_chromadb", return_value=mock_chroma),
            patch(
                "audittrace.routes.memory.get_episodic_service",
                return_value=mock_episodic_service,
            ),
        ):
            mock_settings.return_value = MagicMock(
                auth_enabled=True, auth_required=True
            )
            mock_jwks.return_value = ["fake-key"]
            mock_decode.return_value = {
                "sub": "user-abc",
                "scope": "memory:episodic:write",
            }
            response = client.post(
                "/memory/index",
                params={
                    "collections": "decisions",
                    "file": "user-abc/episodic/foo.md",
                },
                headers={"Authorization": "Bearer fake-token"},
            )

        assert response.status_code == 200, response.text
        mock_episodic_service.invalidate_cache.assert_called_once()

    def test_private_tier_audit_row_carries_tier_field(
        self, client: TestClient
    ) -> None:
        """Parity fix — the write-audit event's ``detail_extra`` carries
        ``tier`` additively (audit schema frozen at 1.17.0: only fields
        ADDED, never removed/renamed). Uses bypass-mode auth (default
        ``client`` fixture) so the sentinel identity's admin scope can
        read the resulting row back via ``GET /interactions``."""
        from audittrace.identity import SENTINEL_SUBJECT

        mock_minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = b"# body\ntext"
        mock_minio.get_object.return_value = response_obj
        mock_chroma, _mock_collection = self._mock_chroma()

        private_key = f"{SENTINEL_SUBJECT}/episodic/foo.md"
        with (
            patch(
                "audittrace.routes.memory._get_minio_client", return_value=mock_minio
            ),
            patch("audittrace.routes.memory.get_chromadb", return_value=mock_chroma),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "decisions", "file": private_key},
            )
        assert response.status_code == 200, response.text

        rows = client.get(
            "/interactions", params={"event_class": "memory_access"}
        ).json()["interactions"]
        writes = [
            r
            for r in rows
            if r["question"].startswith("op=write layer=semantic")
            and r.get("error_detail")
            and json.loads(r["error_detail"]).get("mode") == "single_file"
        ]
        assert writes, rows
        detail = json.loads(writes[-1]["error_detail"])
        assert detail["tier"] == "private"


class TestPdfProvenance:
    """Per-chunk provenance schema (gap-inventory item #21).

    Asserts that every PDF chunk carries the full provenance set:
    bbox_x0/y0/x1/y1, text_source, extraction_confidence,
    document_hash (sha256 of raw bytes), signature_status (placeholder
    until #12 lands), user_id, ingestion_ts_ms.

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

        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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
        # `user_id` is the ONE ownership key across every Chroma writer.
        # It was `ingested_by_user_id` until 2026-07-21, which no reader ever
        # filtered on — ChromaSemanticService.search filters non-admin callers
        # on `user_id`, so these chunks were invisible to everyone but admins
        # (#372 / #374). If a distinct "who ingested this" field is ever
        # needed, ADD one; do not rename this back.
        assert isinstance(meta["user_id"], str)
        assert meta["user_id"]  # non-empty
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

        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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
            mock_collection = AsyncMock()
            mock_chroma = MagicMock()
            mock_chroma.get_or_create_collection = AsyncMock(
                return_value=mock_collection
            )
            mock_chroma.delete_collection = AsyncMock()
            mock_chroma.list_collections = AsyncMock(return_value=[])
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
            mock_collection = AsyncMock()
            mock_chroma = MagicMock()
            mock_chroma.get_or_create_collection = AsyncMock(
                return_value=mock_collection
            )
            mock_chroma.delete_collection = AsyncMock()
            mock_chroma.list_collections = AsyncMock(return_value=[])

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
            mock_collection = AsyncMock()
            mock_chroma = MagicMock()
            mock_chroma.get_or_create_collection = AsyncMock(
                return_value=mock_collection
            )
            mock_chroma.delete_collection = AsyncMock()
            mock_chroma.list_collections = AsyncMock(return_value=[])

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
            mock_collection = AsyncMock()
            mock_chroma = MagicMock()
            mock_chroma.get_or_create_collection = AsyncMock(
                return_value=mock_collection
            )
            mock_chroma.delete_collection = AsyncMock()
            mock_chroma.list_collections = AsyncMock(return_value=[])

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
            mock_collection = AsyncMock()
            mock_chroma = MagicMock()
            mock_chroma.get_or_create_collection = AsyncMock(
                return_value=mock_collection
            )
            mock_chroma.delete_collection = AsyncMock()
            mock_chroma.list_collections = AsyncMock(return_value=[])

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
        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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
            mock_collection = AsyncMock()
            mock_chroma = MagicMock()
            mock_chroma.get_or_create_collection = AsyncMock(
                return_value=mock_collection
            )
            mock_chroma.delete_collection = AsyncMock()
            mock_chroma.list_collections = AsyncMock(return_value=[])

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
        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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
            mock_collection = AsyncMock()
            mock_chroma = MagicMock()
            mock_chroma.get_or_create_collection = AsyncMock(
                return_value=mock_collection
            )
            mock_chroma.delete_collection = AsyncMock()
            mock_chroma.list_collections = AsyncMock(return_value=[])

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
    8-class status taxonomy (per ADR-052 §1: check_skipped /
    check_unavailable / check_failed / none / signed_valid /
    signed_invalid / signed_untrusted / signed_tampered). Plus a smoke
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
        """``valid=False`` is the signature-math-broken signal:
        wrong key, corrupted bytes, or weak-algorithm policy reject.
        Real audit signal — the auth claim is unverifiable.
        Distinct from ``signed_untrusted`` (configuration gap) per
        ADR-052 §1; distinct from ``signed_tampered`` (content
        provably modified) since the document bytes match what was
        signed but the math itself fails."""
        from audittrace.routes.memory import _pdf_signature_status

        fake_reader = MagicMock()
        fake_reader.embedded_signatures = [MagicMock()]
        # intact (content unchanged) + valid=False (sig math broken).
        # trusted=True isolates the valid=False path: post-ADR-052
        # trusted=False would route to signed_untrusted, so this mock
        # exercises the "math broken" branch directly.
        fake_status = MagicMock(intact=True, valid=False, trusted=True)

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

    def test_signed_with_untrusted_chain_returns_signed_untrusted(self) -> None:
        """Signature math valid + content intact but cert chain not
        trusted by the configured trust store, even when re-validated
        as-of self-reported signing time. Per ADR-052 §1 + ADR-054 §2
        this is ``signed_untrusted``: a configuration signal (we don't
        carry the issuing CA at any time), not a security signal
        (the math worked) and not an expiry signal (the chain doesn't
        validate at signing time either)."""
        from audittrace.routes.memory import _pdf_signature_status

        fake_emb = MagicMock()
        # No self-reported timestamp → retry is short-circuited;
        # falls straight through to signed_untrusted.
        fake_emb.self_reported_timestamp = None
        fake_reader = MagicMock()
        fake_reader.embedded_signatures = [fake_emb]
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
        assert status == "signed_untrusted"
        assert count == 1

    def test_signed_expired_returns_signed_expired(self) -> None:
        """ADR-054 §1 — chain doesn't validate at present (e.g. cert
        expired) but DOES validate as-of the self-reported signing
        time. Distinct from ``signed_untrusted`` (no confidence at
        any time) and from ``signed_valid`` (confidence at present).
        Surfaces "valid signature whose cert has aged out" as a
        usable audit signal."""
        from datetime import UTC, datetime

        from audittrace.routes.memory import _pdf_signature_status
        from audittrace.routes.memory_pdf import signature as _sig

        # Prime the trust-roots cache so the retry path can build a
        # second ValidationContext. Sentinel value — pyhanko's ctor
        # is mocked below, so the contents don't need to be a real
        # cert list.
        _sig._VC_TRUST_ROOTS = [MagicMock(name="trust-root-cert")]

        fake_emb = MagicMock()
        fake_emb.self_reported_timestamp = datetime(2025, 1, 15, tzinfo=UTC)
        fake_reader = MagicMock()
        fake_reader.embedded_signatures = [fake_emb]
        # First validation (current time): trusted=False (chain expired now).
        # Retry (as-of signing time): trusted=True (chain valid then).
        first_status = MagicMock(intact=True, valid=True, trusted=False)
        retry_status = MagicMock(intact=True, valid=True, trusted=True)

        with (
            patch(
                "pyhanko.pdf_utils.reader.PdfFileReader",
                return_value=fake_reader,
            ),
            patch(
                "pyhanko.sign.validation.validate_pdf_signature",
                side_effect=[first_status, retry_status],
            ),
            patch("pyhanko_certvalidator.ValidationContext"),
        ):
            status, count = _pdf_signature_status(
                b"%PDF-1.4 ignored", enabled=True, trust_store_path=""
            )
        assert status == "signed_expired"
        assert count == 1

    def test_signed_untrusted_when_retry_also_fails(self) -> None:
        """ADR-054 §2 — retry path: when the as-of-signing-time
        re-validation ALSO returns trusted=False (the chain doesn't
        validate at any time, not just now), classify as
        ``signed_untrusted``, not ``signed_expired``. Distinguishes
        "unknown CA" from "known CA with expired cert"."""
        from datetime import UTC, datetime

        from audittrace.routes.memory import _pdf_signature_status
        from audittrace.routes.memory_pdf import signature as _sig

        _sig._VC_TRUST_ROOTS = [MagicMock(name="trust-root-cert")]

        fake_emb = MagicMock()
        fake_emb.self_reported_timestamp = datetime(2025, 1, 15, tzinfo=UTC)
        fake_reader = MagicMock()
        fake_reader.embedded_signatures = [fake_emb]
        # First + retry both untrusted → signed_untrusted.
        first_status = MagicMock(intact=True, valid=True, trusted=False)
        retry_status = MagicMock(intact=True, valid=True, trusted=False)

        with (
            patch(
                "pyhanko.pdf_utils.reader.PdfFileReader",
                return_value=fake_reader,
            ),
            patch(
                "pyhanko.sign.validation.validate_pdf_signature",
                side_effect=[first_status, retry_status],
            ),
            patch("pyhanko_certvalidator.ValidationContext"),
        ):
            status, count = _pdf_signature_status(
                b"%PDF-1.4 ignored", enabled=True, trust_store_path=""
            )
        assert status == "signed_untrusted"
        assert count == 1

    def test_signed_untrusted_when_retry_path_crashes(self) -> None:
        """ADR-054 §2 — defensive guard: if the as-of-signing-time
        retry path itself crashes (e.g. ValidationContext ctor blows
        up on edge-case input), fall back to ``signed_untrusted``
        rather than masking with a worse class. The original outcome
        (trusted=False, no retry signal) is the safe interpretation."""
        from datetime import UTC, datetime

        from audittrace.routes.memory import _pdf_signature_status
        from audittrace.routes.memory_pdf import signature as _sig

        _sig._VC_TRUST_ROOTS = [MagicMock(name="trust-root-cert")]

        fake_emb = MagicMock()
        fake_emb.self_reported_timestamp = datetime(2025, 1, 15, tzinfo=UTC)
        fake_reader = MagicMock()
        fake_reader.embedded_signatures = [fake_emb]
        first_status = MagicMock(intact=True, valid=True, trusted=False)

        with (
            patch(
                "pyhanko.pdf_utils.reader.PdfFileReader",
                return_value=fake_reader,
            ),
            patch(
                "pyhanko.sign.validation.validate_pdf_signature",
                return_value=first_status,
            ),
            patch(
                "pyhanko_certvalidator.ValidationContext",
                side_effect=RuntimeError("crash inside retry"),
            ),
        ):
            status, count = _pdf_signature_status(
                b"%PDF-1.4 ignored", enabled=True, trust_store_path=""
            )
        assert status == "signed_untrusted"
        assert count == 1

    def test_signed_untrusted_takes_precedence_over_signed_expired(self) -> None:
        """ADR-054 §4 — multi-sig document with one expired sig and
        one untrusted sig flags as ``signed_untrusted``. ``untrusted``
        (no confidence at any time) outranks ``expired`` (confidence
        at signing time, past validity now) because the more cautious
        signal wins."""
        from datetime import UTC, datetime

        from audittrace.routes.memory import _pdf_signature_status
        from audittrace.routes.memory_pdf import signature as _sig

        _sig._VC_TRUST_ROOTS = [MagicMock(name="trust-root-cert")]

        sig_a = MagicMock()
        sig_a.self_reported_timestamp = datetime(2025, 1, 15, tzinfo=UTC)
        sig_b = MagicMock()
        sig_b.self_reported_timestamp = datetime(2025, 1, 15, tzinfo=UTC)
        fake_reader = MagicMock()
        fake_reader.embedded_signatures = [sig_a, sig_b]
        # Sig A: first untrusted, retry trusted → signed_expired.
        # Sig B: first untrusted, retry also untrusted → signed_untrusted.
        # Order pyhanko sees:
        #   sig_a first  → trusted=False
        #   sig_a retry  → trusted=True (would be expired alone)
        #   sig_b first  → trusted=False
        #   sig_b retry  → trusted=False (untrusted)
        statuses = [
            MagicMock(intact=True, valid=True, trusted=False),
            MagicMock(intact=True, valid=True, trusted=True),
            MagicMock(intact=True, valid=True, trusted=False),
            MagicMock(intact=True, valid=True, trusted=False),
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
            patch("pyhanko_certvalidator.ValidationContext"),
        ):
            status, count = _pdf_signature_status(
                b"%PDF-1.4 ignored", enabled=True, trust_store_path=""
            )
        assert status == "signed_untrusted"
        assert count == 2

    def test_signed_invalid_takes_precedence_over_signed_untrusted(self) -> None:
        """Multi-sig document with one ``valid=False`` sig and one
        ``trusted=False`` sig flags as ``signed_invalid`` — the
        worst-signal-wins precedence per ADR-052 §1
        (``tampered > invalid > untrusted > expired > valid``).
        Closes the single-bit-or-the-other ambiguity that
        ``signed_invalid`` had pre-ADR-052."""
        from audittrace.routes.memory import _pdf_signature_status

        fake_emb_a = MagicMock()
        fake_emb_a.self_reported_timestamp = None
        fake_emb_b = MagicMock()
        fake_emb_b.self_reported_timestamp = None
        fake_reader = MagicMock()
        fake_reader.embedded_signatures = [fake_emb_a, fake_emb_b]
        # First signature: math broken (signed_invalid).
        # Second signature: valid math but untrusted chain
        # (signed_untrusted; no signing-time so retry is skipped).
        statuses = [
            MagicMock(intact=True, valid=False, trusted=True),
            MagicMock(intact=True, valid=True, trusted=False),
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
        # signed_invalid wins because invalid > untrusted in the
        # precedence order (a real audit signal outranks a
        # configuration signal when both fire on the same document).
        assert status == "signed_invalid"
        assert count == 2

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

        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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
        from audittrace.routes.memory_pdf import signature as _sig

        _sig._VALIDATION_CONTEXT = None
        _sig._VC_TRUST_STORE_PATH = ""

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
        from audittrace.routes.memory_pdf import signature as _sig

        _sig._VALIDATION_CONTEXT = None
        _sig._VC_TRUST_STORE_PATH = ""

        from audittrace.routes.memory import _get_validation_context

        vc1 = _get_validation_context("")
        vc2 = _get_validation_context("/nonexistent/trust/store.pem")
        assert vc1 is not vc2

    def test_validation_context_uses_provider_pem_when_no_path_set(self) -> None:
        """Per ADR-052 §2: when ``pdf_signature_trust_store`` is empty
        (no operator-set file path), ``_get_validation_context`` calls
        the configured ``TrustStoreProvider`` to obtain the PEM bundle.
        A populated Provider drives the ValidationContext's
        ``trust_roots`` keyword argument.

        We patch ``ValidationContext`` itself so the test does not
        need a real X.509 cert — the asssertion is that the PEM bytes
        from the Provider land in the constructor's ``trust_roots``."""
        from audittrace.routes.memory_pdf import signature as _sig
        from audittrace.services.trust_store import (
            MockTrustStoreProvider,
            _bundle_from_pem,
        )

        fake_pem = (
            b"-----BEGIN CERTIFICATE-----\nfake-cert-bytes\n-----END CERTIFICATE-----\n"
        )
        provider = MockTrustStoreProvider()
        bundle = _bundle_from_pem(
            fake_pem,
            builder_id="test",
            source_url="test://x",
            cert_count=1,
        )
        provider.store(bundle)

        # Reset cache.
        _sig._VALIDATION_CONTEXT = None
        _sig._VC_TRUST_STORE_PATH = ""

        fake_vc = MagicMock(name="ValidationContext-instance")
        with (
            patch(
                "audittrace.dependencies.get_trust_store_provider",
                return_value=provider,
            ),
            patch(
                "pyhanko_certvalidator.ValidationContext",
                return_value=fake_vc,
            ) as mock_vc_cls,
        ):
            from audittrace.routes.memory import _get_validation_context

            # Empty trust_store_path → falls through to Provider.
            vc = _get_validation_context("")

        # The Provider's PEM bytes were parsed into asn1crypto
        # x509.Certificate objects before being passed to
        # ValidationContext (caught by live evidence 2026-05-09:
        # pyhanko_certvalidator's actual TrustRootList type is
        # Iterable[Union[x509.Certificate, TrustAnchor]]; the
        # docstring claiming raw bytes are accepted is misleading).
        # The fake PEM in this test is not a real cert, so the
        # parser logs a warning and returns an empty list — that's
        # fine for asserting the wiring; a real-cert end-to-end
        # test runs as live evidence.
        assert vc is fake_vc
        kwargs = mock_vc_cls.call_args.kwargs
        # trust_roots key may be omitted if the parser couldn't
        # extract any cert (the synthetic fake PEM here doesn't
        # parse). The relevant invariant is that ValidationContext
        # was called at all and the cache key encodes the Provider
        # metadata.
        assert "trust_roots" in kwargs
        # Cache key encodes the Provider's metadata sha256.
        assert bundle.metadata.sha256 in _sig._VC_TRUST_STORE_PATH

    def test_validation_context_invalidates_via_helper(self) -> None:
        """``_invalidate_validation_context`` (called by the admin
        refresh endpoint after a successful refresh) drops the
        in-process singleton so the next signature check rebuilds
        against the freshly-stored PEM (ADR-052 §5)."""
        from audittrace.routes.memory import _invalidate_validation_context
        from audittrace.routes.memory_pdf import signature as _sig

        # Prime the singleton.
        _sig._VALIDATION_CONTEXT = MagicMock()
        _sig._VC_TRUST_STORE_PATH = "primed-cache-key"

        _invalidate_validation_context()

        assert _sig._VALIDATION_CONTEXT is None
        assert _sig._VC_TRUST_STORE_PATH == ""

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

    def test_list_requires_authenticated_user(self, client: TestClient) -> None:
        """ADR-062 WU-A1: ``GET /memory/semantic`` must carry
        ``Depends(require_user)`` like every sibling list/read handler
        (closes the CS-4 anomaly). ``validate_jwt`` alone (the
        pre-existing ``Security(...)`` scope check) is gated on
        ``auth_enabled``, not ``auth_required`` — so we disable that
        gate and isolate ``require_user``'s own ``auth_required`` gate
        to prove THIS dependency, specifically, rejects an unauthenticated
        caller. Falsifiable: removing ``Depends(require_user)`` from
        ``list_semantic`` makes this 401 regress to 200."""
        with patch("audittrace.auth.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                auth_enabled=False, auth_required=True
            )
            response = client.get("/memory/semantic")
        assert response.status_code == 401


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


# ── ADR-062 Phase B (WU-B4 + WU-B5) ─────────────────────────────────────────
#
# WU-B4: backoffice per-user scoping (manifest `tier` column, owner-or-corpus
# predicate on list_*/read_*). WU-B5: default-write-private + corpus-write
# promote gate. Shared helper below swaps the bypass-mode admin sentinel for
# a plain non-admin identity with an explicit scope set — the shape every
# scope-gate test in this section needs.


def _override_identity(client: TestClient, user_id: str, scopes: tuple[str, ...]):
    """Swap ``require_user`` for a non-admin ``UserContext``. Caller MUST
    ``client.app.dependency_overrides.clear()`` in a ``finally`` block."""
    from audittrace.auth import require_user
    from audittrace.identity import UserContext

    identity = UserContext(
        user_id=user_id,
        username=user_id,
        agent_type="test",
        scopes=scopes,
        is_admin=False,
    )
    client.app.dependency_overrides[require_user] = lambda: identity
    return identity


class TestEpisodicProceduralCorpusPromoteRejected:
    """ADR-062 Phase B (WU-B5): episodic/procedural have no declared
    corpus-write scope (the ADR-062 §4 frozenset is exactly
    decisions/skills/semantic — ``TestCorpusScopeGovernance`` in
    ``test_chart_drift_guards.py`` pins it). A ``tier=corpus`` /
    ``?promote=corpus`` request on these two layers is therefore
    rejected (400 — "not supported", not "not authorized"), never
    silently accepted."""

    def test_create_episodic_default_write_is_private(self, client: TestClient) -> None:
        r = client.post(
            "/memory/episodic",
            json={"filename": "ADR-default.md", "content": "x"},
        )
        assert r.status_code == 200
        assert r.json()["tier"] == "private"

    def test_create_episodic_tier_corpus_in_body_rejected(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/memory/episodic",
            json={"filename": "ADR-nope.md", "content": "x", "tier": "corpus"},
        )
        assert r.status_code == 400

    def test_create_episodic_promote_query_rejected(self, client: TestClient) -> None:
        r = client.post(
            "/memory/episodic?promote=corpus",
            json={"filename": "ADR-nope2.md", "content": "x"},
        )
        assert r.status_code == 400

    def test_create_procedural_default_write_is_private(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/memory/procedural",
            json={"filename": "SKILL-default.md", "content": "x"},
        )
        assert r.status_code == 200
        assert r.json()["tier"] == "private"

    def test_create_procedural_tier_corpus_in_body_rejected(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/memory/procedural",
            json={"filename": "SKILL-nope.md", "content": "x", "tier": "corpus"},
        )
        assert r.status_code == 400

    def test_create_procedural_promote_query_rejected(self, client: TestClient) -> None:
        r = client.post(
            "/memory/procedural?promote=corpus",
            json={"filename": "SKILL-nope2.md", "content": "x"},
        )
        assert r.status_code == 400


class TestResolveRequestedTierHelper:
    """Direct unit coverage of ``_resolve_requested_tier`` — the shared
    body/query parser behind every WU-B5 promote path."""

    def test_no_signal_defaults_private(self) -> None:
        from audittrace.routes.memory import _resolve_requested_tier

        assert _resolve_requested_tier(None, None) == "private"
        assert _resolve_requested_tier({}, None) == "private"

    def test_body_tier_private_explicit(self) -> None:
        from audittrace.routes.memory import _resolve_requested_tier

        assert _resolve_requested_tier({"tier": "private"}, None) == "private"

    def test_body_tier_corpus(self) -> None:
        from audittrace.routes.memory import _resolve_requested_tier

        assert _resolve_requested_tier({"tier": "corpus"}, None) == "corpus"

    def test_promote_query_corpus(self) -> None:
        from audittrace.routes.memory import _resolve_requested_tier

        assert _resolve_requested_tier(None, "corpus") == "corpus"

    def test_agreeing_body_and_query_ok(self) -> None:
        from audittrace.routes.memory import _resolve_requested_tier

        assert _resolve_requested_tier({"tier": "corpus"}, "corpus") == "corpus"

    def test_conflicting_body_and_query_400(self) -> None:
        from fastapi import HTTPException

        from audittrace.routes.memory import _resolve_requested_tier

        with pytest.raises(HTTPException) as exc_info:
            _resolve_requested_tier({"tier": "private"}, "corpus")
        assert exc_info.value.status_code == 400

    def test_garbage_value_400(self) -> None:
        from fastapi import HTTPException

        from audittrace.routes.memory import _resolve_requested_tier

        with pytest.raises(HTTPException) as exc_info:
            _resolve_requested_tier({"tier": "public"}, None)
        assert exc_info.value.status_code == 400


class TestSanitizeSemanticMetadataHelper:
    """ADR-062 Phase B (WU-B5 review fix, 2026-08-04) — direct unit
    coverage of ``_sanitize_semantic_metadata``, the route-layer half of
    the metadata-forgery fix (the service-layer half is the
    unconditional stamp in ``ChromaSemanticService.upsert``, proven in
    ``tests/test_semantic_service.py::TestUpsertStampsFromContextUnconditionally``).

    FALSIFIABLE: this helper's own logic is trivially neutered by
    making it an identity function (``return metadata``) — every test
    below goes RED immediately since the forged keys would then survive."""

    def test_strips_tier_key(self) -> None:
        from audittrace.routes.memory import _sanitize_semantic_metadata

        out = _sanitize_semantic_metadata({"tier": "corpus", "source": "adr"})
        assert "tier" not in out
        assert out == {"source": "adr"}

    def test_strips_user_id_key(self) -> None:
        from audittrace.routes.memory import _sanitize_semantic_metadata

        out = _sanitize_semantic_metadata({"user_id": "victim", "title": "x"})
        assert "user_id" not in out
        assert out == {"title": "x"}

    def test_strips_both_keys_together(self) -> None:
        from audittrace.routes.memory import _sanitize_semantic_metadata

        out = _sanitize_semantic_metadata(
            {"tier": "corpus", "user_id": "victim", "title": "x"}
        )
        assert out == {"title": "x"}

    def test_none_passes_through(self) -> None:
        from audittrace.routes.memory import _sanitize_semantic_metadata

        assert _sanitize_semantic_metadata(None) is None

    def test_non_dict_passes_through_unchanged(self) -> None:
        """Malformed metadata isn't this helper's job to validate — the
        existing downstream error handling covers it."""
        from audittrace.routes.memory import _sanitize_semantic_metadata

        assert _sanitize_semantic_metadata("not-a-dict") == "not-a-dict"

    def test_input_dict_not_mutated(self) -> None:
        """Returns a new dict — the caller's original payload dict must
        not be mutated as a side effect."""
        from audittrace.routes.memory import _sanitize_semantic_metadata

        original = {"tier": "corpus", "k": "v"}
        _sanitize_semantic_metadata(original)
        assert original == {"tier": "corpus", "k": "v"}


class TestSemanticCorpusPromoteGate:
    """ADR-062 Phase B (WU-B5) — the promote-to-corpus scope gate on
    ``POST /memory/semantic``.

    FALSIFIABLE: neuter ``_require_corpus_scope`` (e.g. make it always
    return without raising) and ``test_promote_without_scope_is_denied``
    goes RED (a writer with no corpus scope promotes successfully);
    restore it and it goes green.
    ``test_promote_with_scope_succeeds``/``test_admin_bypasses_the_gate``
    pin the two "should still work" arms so a "fix" can't just reject
    everything.
    """

    def test_default_write_is_private(self, client: TestClient) -> None:
        writer = _override_identity(
            client, "writer-1", ("memory:semantic:write", "memory:semantic:read")
        )
        try:
            r = client.post(
                "/memory/semantic",
                json={"collection": "decisions", "document_id": "d-1", "text": "x"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["tier"] == "private"
        finally:
            client.app.dependency_overrides.clear()
        del writer

    def test_promote_without_scope_is_denied(self, client: TestClient) -> None:
        _override_identity(
            client, "writer-2", ("memory:semantic:write", "memory:semantic:read")
        )
        try:
            r = client.post(
                "/memory/semantic",
                json={
                    "collection": "decisions",
                    "document_id": "d-2",
                    "text": "x",
                    "tier": "corpus",
                },
            )
            assert r.status_code == 403
            assert "memory:corpus:decisions:write" in r.json()["detail"]
        finally:
            client.app.dependency_overrides.clear()

    def test_promote_via_query_param_without_scope_is_denied(
        self, client: TestClient
    ) -> None:
        _override_identity(
            client, "writer-2b", ("memory:semantic:write", "memory:semantic:read")
        )
        try:
            r = client.post(
                "/memory/semantic?promote=corpus",
                json={"collection": "decisions", "document_id": "d-2b", "text": "x"},
            )
            assert r.status_code == 403
        finally:
            client.app.dependency_overrides.clear()

    def test_promote_with_scope_succeeds(self, client: TestClient) -> None:
        _override_identity(
            client,
            "curator-1",
            (
                "memory:semantic:write",
                "memory:semantic:read",
                "memory:corpus:decisions:write",
            ),
        )
        try:
            r = client.post(
                "/memory/semantic",
                json={
                    "collection": "decisions",
                    "document_id": "d-3",
                    "text": "shared content",
                    "tier": "corpus",
                },
            )
            assert r.status_code == 200, r.text
            assert r.json()["tier"] == "corpus"
        finally:
            client.app.dependency_overrides.clear()

    def test_admin_bypasses_the_gate(self, client: TestClient) -> None:
        """The default ``client`` fixture identity is the admin sentinel
        — no explicit corpus scope needed."""
        r = client.post(
            "/memory/semantic",
            json={
                "collection": "decisions",
                "document_id": "d-admin",
                "text": "x",
                "tier": "corpus",
            },
        )
        assert r.status_code == 200
        assert r.json()["tier"] == "corpus"

    def test_wrong_collection_scope_does_not_grant_another(
        self, client: TestClient
    ) -> None:
        """Holding ``memory:corpus:skills:write`` must NOT authorize a
        promote into the ``decisions`` collection — the scope is
        per-collection, not a blanket corpus-write grant."""
        _override_identity(
            client,
            "curator-2",
            (
                "memory:semantic:write",
                "memory:semantic:read",
                "memory:corpus:skills:write",
            ),
        )
        try:
            r = client.post(
                "/memory/semantic",
                json={
                    "collection": "decisions",
                    "document_id": "d-4",
                    "text": "x",
                    "tier": "corpus",
                },
            )
            assert r.status_code == 403
        finally:
            client.app.dependency_overrides.clear()


class TestSemanticCorpusReadGate:
    """ADR-062 Phase B (WU-B4, D3) — reading CORPUS content through the
    ``/memory/semantic`` backoffice requires
    ``memory:corpus:<collection>:read``. The LLM recall path
    (``recall_semantic``) is untouched by this gate — see
    ``_filter_corpus_read_gate``'s docstring.

    FALSIFIABLE: neuter ``_filter_corpus_read_gate``/the ``read_semantic``
    scope check (e.g. make ``_has_corpus_scope`` always return ``True``)
    and ``test_reader_without_corpus_scope_cannot_read_corpus_doc`` /
    ``test_reader_without_corpus_scope_does_not_list_corpus_doc`` go RED;
    restore and they go green. ``test_reader_sees_own_private_doc``
    pins that the gate doesn't ALSO (wrongly) block a caller's own
    content.
    """

    @staticmethod
    def _seed_corpus_doc(client: TestClient) -> None:
        """Admin (sentinel) promotes one doc into the `decisions` corpus."""
        r = client.post(
            "/memory/semantic",
            json={
                "collection": "decisions",
                "document_id": "corpus-doc",
                "text": "shared decision",
                "tier": "corpus",
            },
        )
        assert r.status_code == 200, r.text

    def test_reader_without_corpus_scope_cannot_read_corpus_doc(
        self, client: TestClient
    ) -> None:
        self._seed_corpus_doc(client)
        _override_identity(client, "reader-1", ("memory:semantic:read",))
        try:
            r = client.get("/memory/semantic/decisions/corpus-doc")
            assert r.status_code == 403
        finally:
            client.app.dependency_overrides.clear()

    def test_reader_with_corpus_scope_can_read_corpus_doc(
        self, client: TestClient
    ) -> None:
        self._seed_corpus_doc(client)
        _override_identity(
            client,
            "curator-reader",
            ("memory:semantic:read", "memory:corpus:decisions:read"),
        )
        try:
            r = client.get("/memory/semantic/decisions/corpus-doc")
            assert r.status_code == 200
            assert r.json()["content"] == "shared decision"
        finally:
            client.app.dependency_overrides.clear()

    def test_reader_without_corpus_scope_does_not_list_corpus_doc(
        self, client: TestClient
    ) -> None:
        self._seed_corpus_doc(client)
        _override_identity(client, "reader-2", ("memory:semantic:read",))
        try:
            r = client.get("/memory/semantic?collection=decisions")
            keys = {i["key"] for i in r.json()["items"]}
            assert "decisions/corpus-doc" not in keys, f"leaked corpus doc: {keys}"
        finally:
            client.app.dependency_overrides.clear()

    def test_reader_with_corpus_scope_lists_corpus_doc(
        self, client: TestClient
    ) -> None:
        self._seed_corpus_doc(client)
        _override_identity(
            client,
            "curator-reader-2",
            ("memory:semantic:read", "memory:corpus:decisions:read"),
        )
        try:
            r = client.get("/memory/semantic?collection=decisions")
            keys = {i["key"] for i in r.json()["items"]}
            assert "decisions/corpus-doc" in keys
        finally:
            client.app.dependency_overrides.clear()

    def test_reader_sees_own_private_doc_without_corpus_scope(
        self, client: TestClient
    ) -> None:
        """The gate is corpus-specific — a caller's own private write is
        never blocked by it."""
        writer = _override_identity(
            client, "reader-3", ("memory:semantic:write", "memory:semantic:read")
        )
        try:
            r = client.post(
                "/memory/semantic",
                json={
                    "collection": "decisions",
                    "document_id": "own-doc",
                    "text": "mine",
                },
            )
            assert r.status_code == 200
            r = client.get("/memory/semantic/decisions/own-doc")
            assert r.status_code == 200
            assert r.json()["content"] == "mine"
        finally:
            client.app.dependency_overrides.clear()
        del writer


class TestSemanticMetadataForgeryClosed:
    """ADR-062 Phase B (WU-B5 review fix, 2026-08-04) — end-to-end proof
    that ``tier``/``user_id`` smuggled inside the nested ``metadata``
    field of ``POST``/``PUT /memory/semantic`` cannot bypass the
    promote gate or forge attribution.

    Exercised against the REAL ``ChromaSemanticService`` (swapped in via
    the ``real_semantic`` fixture below) rather than the default
    ``client`` fixture's ``MockSemanticService`` — the mock deliberately
    does not enforce ADR-062 isolation
    (``services/semantic.py::MockSemanticService`` docstring), so a
    "corpus-read stranger gets 404" assertion is only meaningful against
    the implementation that actually gates cross-user reads.

    Original hole (reviewer, 2026-08-04): ``create_semantic`` gated only
    the TOP-LEVEL body ``tier`` / ``?promote=`` signal via
    ``_require_corpus_scope``. A token with ONLY ``memory:semantic:write``
    (no corpus-write scope) could POST ``{"metadata": {"tier": "corpus"}}``
    with no top-level ``tier`` — the route believed it wrote "private",
    but ``ChromaSemanticService.upsert``'s ``dict.setdefault`` silently
    honoured the caller-supplied tag, so ChromaDB actually stored
    ``tier=corpus``. Every subsequent ``get_document`` then trusted that
    forged tag via ``_tier_authorized`` — any corpus-read-scoped
    stranger (never touched the write, not the owner) could read it.
    ``metadata.user_id`` was forgeable the same way (attribution
    forgery). ``update_semantic`` (PUT) had the identical hole.

    FALSIFIABLE: reverting EITHER the route-level
    ``_sanitize_semantic_metadata`` strip OR (more definitively, since
    the service is the authoritative choke point — see
    ``ChromaSemanticService.upsert``'s docstring) the unconditional
    stamp in ``ChromaSemanticService.upsert`` back to ``dict.setdefault``
    turns every test below RED (the "stranger" assertions start
    succeeding — i.e. the forged doc becomes readable).
    """

    @pytest.fixture
    def real_semantic(self, monkeypatch):
        """Swap the route layer's semantic service for the REAL
        ``ChromaSemanticService`` backed by the in-repo Chroma mock
        client (isolation-enforcing, unlike ``MockSemanticService``).
        ``ChromaSemanticService.upsert`` always vectorises on the nomic
        server (ADR-047) via its OWN ``embed_via_nomic`` import — stub
        it so this stays offline (mirrors
        ``test_semantic_service.py``'s ``_mock_nomic_embed`` fixture;
        the module-level ``_mock_nomic_embed`` autouse fixture in THIS
        file only patches ``routes.memory.embed_via_nomic``, a
        different call site)."""
        import asyncio

        from audittrace.db.factory import MockChromaDBFactory
        from audittrace.routes import memory as m
        from audittrace.services.semantic import ChromaSemanticService

        monkeypatch.setattr(
            "audittrace.services.semantic.embed_via_nomic",
            AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
        )
        factory = MockChromaDBFactory()
        chroma_client = asyncio.run(factory.get_client())
        service = ChromaSemanticService(
            client=chroma_client, default_collections=["decisions"]
        )
        monkeypatch.setattr(m, "get_semantic_service", lambda: service)
        return service

    def test_create_tier_forgery_via_metadata_is_ineffective(
        self, client: TestClient, real_semantic
    ) -> None:
        """A writer holding only ``memory:semantic:write`` cannot
        promote by hiding ``tier=corpus`` inside ``metadata`` instead of
        the top-level field/``?promote=``."""
        _override_identity(
            client, "attacker-1", ("memory:semantic:write", "memory:semantic:read")
        )
        try:
            r = client.post(
                "/memory/semantic",
                json={
                    "collection": "decisions",
                    "document_id": "forge-1",
                    "text": "attacker body",
                    "metadata": {"tier": "corpus"},
                },
            )
            assert r.status_code == 200, r.text
            assert r.json()["tier"] == "private"
        finally:
            client.app.dependency_overrides.clear()

        # A corpus-read-scoped STRANGER (never touched the write, not
        # the owner) must NOT be able to read it — bare 404, no
        # existence disclosure.
        _override_identity(
            client,
            "stranger-1",
            ("memory:semantic:read", "memory:corpus:decisions:read"),
        )
        try:
            r = client.get("/memory/semantic/decisions/forge-1")
            assert r.status_code == 404, (
                "forged metadata['tier']='corpus' leaked the doc to a "
                f"stranger: {r.status_code} {r.text}"
            )
        finally:
            client.app.dependency_overrides.clear()

    def test_create_user_id_forgery_via_metadata_is_ineffective(
        self, client: TestClient, real_semantic
    ) -> None:
        """A caller cannot attribute a private write to another user by
        forging ``metadata.user_id``."""
        _override_identity(
            client, "attacker-2", ("memory:semantic:write", "memory:semantic:read")
        )
        try:
            r = client.post(
                "/memory/semantic",
                json={
                    "collection": "decisions",
                    "document_id": "forge-2",
                    "text": "attacker body 2",
                    "metadata": {"user_id": "victim"},
                },
            )
            assert r.status_code == 200, r.text
        finally:
            client.app.dependency_overrides.clear()

        # The forged "owner" must NOT see it as their own — they never
        # wrote it, and it's private-tier (owned by the real attacker).
        _override_identity(client, "victim", ("memory:semantic:read",))
        try:
            r = client.get("/memory/semantic/decisions/forge-2")
            assert r.status_code == 404, (
                "forged metadata['user_id']='victim' let 'victim' read "
                f"attacker's private doc: {r.status_code}"
            )
        finally:
            client.app.dependency_overrides.clear()

    def test_update_tier_forgery_via_metadata_is_ineffective(
        self, client: TestClient, real_semantic
    ) -> None:
        """PUT /memory/semantic had the identical unvalidated
        ``metadata`` passthrough — same forgery, same fix."""
        writer = _override_identity(
            client, "attacker-3", ("memory:semantic:write", "memory:semantic:read")
        )
        try:
            r = client.post(
                "/memory/semantic",
                json={
                    "collection": "decisions",
                    "document_id": "forge-3",
                    "text": "v1",
                },
            )
            assert r.status_code == 200
            r = client.put(
                "/memory/semantic/decisions/forge-3",
                json={"text": "v2", "metadata": {"tier": "corpus"}},
            )
            assert r.status_code == 200, r.text
        finally:
            client.app.dependency_overrides.clear()
        del writer

        _override_identity(
            client,
            "stranger-3",
            ("memory:semantic:read", "memory:corpus:decisions:read"),
        )
        try:
            r = client.get("/memory/semantic/decisions/forge-3")
            assert r.status_code == 404, (
                f"PUT metadata['tier'] forgery leaked the doc: {r.status_code} {r.text}"
            )
        finally:
            client.app.dependency_overrides.clear()

    def test_update_user_id_forgery_via_metadata_is_ineffective(
        self, client: TestClient, real_semantic
    ) -> None:
        _override_identity(
            client, "attacker-4", ("memory:semantic:write", "memory:semantic:read")
        )
        try:
            r = client.post(
                "/memory/semantic",
                json={
                    "collection": "decisions",
                    "document_id": "forge-4",
                    "text": "v1",
                },
            )
            assert r.status_code == 200
            r = client.put(
                "/memory/semantic/decisions/forge-4",
                json={"text": "v2", "metadata": {"user_id": "victim2"}},
            )
            assert r.status_code == 200, r.text
        finally:
            client.app.dependency_overrides.clear()

        _override_identity(client, "victim2", ("memory:semantic:read",))
        try:
            r = client.get("/memory/semantic/decisions/forge-4")
            assert r.status_code == 404, (
                f"PUT metadata['user_id'] forgery leaked the doc: {r.status_code}"
            )
        finally:
            client.app.dependency_overrides.clear()


class TestBackofficeOwnerScopedManifest:
    """ADR-062 Phase B (WU-B4) — the manifest-tracked half of the
    owner-or-corpus predicate, proven through the real HTTP surface
    (``list_episodic``/``list_procedural``/``list_semantic`` +
    ``read_episodic``/``read_procedural``/``read_semantic``).

    FALSIFIABLE: neuter ``list_for_layer``'s predicate (e.g. hardcode
    the filter condition to ``True`` in
    ``MemoryManifestService``/``MockMemoryManifestService``) and
    ``test_list_episodic_hides_other_users_private_row`` (+ the
    procedural/semantic siblings) go RED — user B's list includes user
    A's private row; restore and they go green.
    """

    def test_list_episodic_hides_other_users_private_row(
        self, client: TestClient
    ) -> None:
        _override_identity(
            client, "alice", ("memory:episodic:write", "memory:episodic:read")
        )
        try:
            r = client.post(
                "/memory/episodic",
                json={"filename": "ADR-alice.md", "content": "alice's note"},
            )
            assert r.status_code == 200
        finally:
            client.app.dependency_overrides.clear()

        _override_identity(client, "bob", ("memory:episodic:read",))
        try:
            r = client.get("/memory/episodic")
            keys = {i["key"] for i in r.json()["items"]}
            assert "ADR-alice.md" not in keys, f"leaked alice's row to bob: {keys}"
        finally:
            client.app.dependency_overrides.clear()

        _override_identity(
            client, "alice", ("memory:episodic:write", "memory:episodic:read")
        )
        try:
            r = client.get("/memory/episodic")
            keys = {i["key"] for i in r.json()["items"]}
            assert "ADR-alice.md" in keys
        finally:
            client.app.dependency_overrides.clear()

    def test_list_episodic_shows_corpus_row_to_everyone(
        self, client: TestClient
    ) -> None:
        import asyncio

        from audittrace.dependencies import get_memory_manifest_service

        asyncio.run(
            get_memory_manifest_service().record_create(
                "episodic", "ADR-shared.md", "Shared", 1, "curator", tier="corpus"
            )
        )
        _override_identity(client, "bob", ("memory:episodic:read",))
        try:
            r = client.get("/memory/episodic")
            keys = {i["key"] for i in r.json()["items"]}
            assert "ADR-shared.md" in keys
        finally:
            client.app.dependency_overrides.clear()

    def test_list_procedural_hides_other_users_private_row(
        self, client: TestClient
    ) -> None:
        _override_identity(
            client, "alice", ("memory:procedural:write", "memory:procedural:read")
        )
        try:
            r = client.post(
                "/memory/procedural",
                json={"filename": "SKILL-alice.md", "content": "alice's skill"},
            )
            assert r.status_code == 200
        finally:
            client.app.dependency_overrides.clear()

        _override_identity(client, "bob", ("memory:procedural:read",))
        try:
            r = client.get("/memory/procedural")
            keys = {i["key"] for i in r.json()["items"]}
            assert "SKILL-alice.md" not in keys, f"leaked alice's row to bob: {keys}"
        finally:
            client.app.dependency_overrides.clear()

    def test_list_semantic_hides_other_users_private_row(
        self, client: TestClient
    ) -> None:
        _override_identity(
            client, "alice", ("memory:semantic:write", "memory:semantic:read")
        )
        try:
            r = client.post(
                "/memory/semantic",
                json={
                    "collection": "decisions",
                    "document_id": "alice-doc",
                    "text": "alice's vector",
                },
            )
            assert r.status_code == 200
        finally:
            client.app.dependency_overrides.clear()

        _override_identity(client, "bob", ("memory:semantic:read",))
        try:
            r = client.get("/memory/semantic?collection=decisions")
            keys = {i["key"] for i in r.json()["items"]}
            assert "decisions/alice-doc" not in keys, (
                f"leaked alice's row to bob: {keys}"
            )
        finally:
            client.app.dependency_overrides.clear()

    def test_read_episodic_hides_other_users_manifest_metadata(
        self, client: TestClient
    ) -> None:
        """ADR-062 Phase B (WU-B4) — ``_manifest_visible`` defense against
        the pre-existing (layer, key) global-uniqueness manifest model
        (see its docstring): engineer the filename-collision edge case
        directly (monkeypatch the manifest getter) since the S3 content
        lookup alone already prevents it in the common case."""
        from audittrace.identity import UserContext
        from audittrace.routes import memory as m
        from audittrace.services.memory_manifest import ManifestEntry

        alices_row = ManifestEntry(
            id="mi-1",
            layer="episodic",
            key="collide.md",
            title="alice's title",
            size_bytes=1,
            created_at_ms=1,
            modified_at_ms=1,
            created_by_user_id="alice",
            modified_by_user_id="alice",
            deleted_at_ms=None,
            deleted_by_user_id=None,
            tier="private",
        )

        class _FixedManifest:
            async def get(self, layer, key):
                return alices_row

        bob = UserContext(
            user_id="bob",
            username="bob",
            agent_type="test",
            scopes=("memory:episodic:read",),
            is_admin=False,
        )

        # Bob's OWN content lookup resolves normally (his private tier
        # has "collide.md" too — a genuine same-filename collision).
        get_episodic = m.get_episodic_service()
        get_episodic.add_document(
            content="bob's body",
            file="collide.md",
            tier="private",
            user_id="bob",
        )

        original_get_manifest = m.get_memory_manifest_service
        m.get_memory_manifest_service = lambda: _FixedManifest()  # type: ignore[assignment]
        try:
            import asyncio

            from fastapi import BackgroundTasks, Request

            bare_request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/memory/episodic/collide.md",
                    "headers": [],
                }
            )
            result = asyncio.run(
                m.read_episodic(
                    "collide.md",
                    _auth={},
                    user=bob,
                    background_tasks=BackgroundTasks(),
                    request=bare_request,
                )
            )
        finally:
            m.get_memory_manifest_service = original_get_manifest

        assert result["content"] == "bob's body"
        assert result["manifest"] is None, (
            "alice's manifest row (owner='alice') leaked to bob's read "
            f"of his OWN file: {result['manifest']}"
        )


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

    def test_all_known_caller_filter_prevents_key_collision_hiding_own_doc(
        self, client: TestClient
    ) -> None:
        """ADR-062 Phase B (WU-B4 review fix, 2026-08-04) — the
        episodic/procedural twin of the same ``_merge_semantic_with_
        chroma`` gap the reviewer flagged: ``_merge_layer_items_with_
        s3``'s ``all_known`` dedup lookup must ALSO be caller-scoped, or
        another user's manifest-tracked PRIVATE row under the SAME
        filename makes that key "already known" fleet-wide, hiding the
        caller's OWN legitimately-discoverable S3 object.

        FALSIFIABLE: drop ``caller=user`` from the ``all_known`` call in
        ``_merge_layer_items_with_s3`` and this goes RED — alice's own
        discovered file disappears because bob's colliding manifest row
        (a different key entirely, same filename) makes "collide.md"
        look already-tracked."""
        import asyncio

        from audittrace.dependencies import (
            get_episodic_service,
            get_memory_manifest_service,
        )

        # Bob's manifest-TRACKED row for "collide.md" — never actually
        # written to S3 in this test, only the manifest row exists
        # (mirrors the real (layer,key) global-uniqueness gap:
        # `_manifest_visible`'s docstring).
        asyncio.run(
            get_memory_manifest_service().record_create(
                "episodic", "collide.md", "bob's title", 1, "bob", tier="private"
            )
        )

        # Alice's own file is DISCOVERED (S3-only, no manifest row) —
        # deliberately the identical filename.
        get_episodic_service().add_document(
            content="alice's body",
            file="collide.md",
            tier="private",
            user_id="alice",
        )

        _override_identity(
            client, "alice", ("memory:episodic:write", "memory:episodic:read")
        )
        try:
            r = client.get("/memory/episodic")
            items = [i for i in r.json()["items"] if i["key"] == "collide.md"]
            assert items, (
                "alice's own discovered file was hidden by bob's colliding "
                f"manifest row: {r.json()['items']}"
            )
            assert items[0]["discovered"] is True
        finally:
            client.app.dependency_overrides.clear()

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


class TestListBlindSpotAndErgonomics:
    """Backlog #15 regression guards + list ergonomics (R5/R6).

    (a) a non-``ADR-`` named doc must appear in the list — the exact case
    that regressed (Defect A); (c) sort/order/limit/offset paging over a
    mixed manifest + discovered set; (R4) discovered entries carry real
    timestamps from the S3 object's last_modified.
    """

    def test_non_adr_named_doc_appears_in_episodic_list(
        self, client: TestClient
    ) -> None:
        """R6a: the blind spot — a ``decision-*.md`` (non-ADR) uploaded with
        no manifest row MUST surface in GET /memory/episodic."""
        from audittrace.dependencies import get_episodic_service

        get_episodic_service().add_document(
            content="# Decision 375\n\nbody",
            title="Decision 375",
            file="decision-2026-07-25-375.md",
        )
        r = client.get("/memory/episodic")
        assert r.status_code == 200
        keys = {i["key"] for i in r.json()["items"]}
        assert "decision-2026-07-25-375.md" in keys

    def test_discovered_entry_carries_real_timestamps(self, client: TestClient) -> None:
        """R4: a discovered entry with an S3 last_modified surfaces it as
        created_at_ms / modified_at_ms (so the set is uniformly sortable)."""
        from audittrace.dependencies import get_episodic_service

        get_episodic_service().add_document(
            content="# Decision\n\nbody",
            title="Decision",
            file="decision-ts.md",
            last_modified_ms=1_700_000_000_000,
        )
        r = client.get("/memory/episodic")
        row = next(i for i in r.json()["items"] if i["key"] == "decision-ts.md")
        assert row["created_at_ms"] == 1_700_000_000_000
        assert row["modified_at_ms"] == 1_700_000_000_000

    def test_response_echoes_limit_and_offset(self, client: TestClient) -> None:
        r = client.get("/memory/episodic?limit=5&offset=0")
        body = r.json()
        assert body["limit"] == 5
        assert body["offset"] == 0
        assert "total" in body

    def test_order_desc_returns_newest_first_and_paging(
        self, client: TestClient
    ) -> None:
        """R6c: order=desc newest-first + limit/offset paging over a mixed
        manifest + discovered set, no error on the sort."""
        from audittrace.dependencies import get_episodic_service

        ep = get_episodic_service()
        # Discovered entries with ascending timestamps.
        for i in range(1, 5):
            ep.add_document(
                content=f"# d{i}\n\nbody",
                title=f"d{i}",
                file=f"discovered-{i}.md",
                last_modified_ms=1_000 * i,
            )
        # A manifest row too (mixed set) — created via the POST endpoint.
        client.post(
            "/memory/episodic",
            json={"filename": "ADR-manifest.md", "content": "# manifest"},
        )

        r = client.get("/memory/episodic?sort=created_at&order=desc&limit=2&offset=0")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 5  # 4 discovered + >=1 manifest
        first_page = [i["key"] for i in body["items"]]
        assert len(first_page) == 2
        # The manifest row (created "now") is newest → first under desc.
        assert first_page[0] == "ADR-manifest.md"

        # Second page continues without overlap.
        r2 = client.get("/memory/episodic?sort=created_at&order=desc&limit=2&offset=2")
        second_page = [i["key"] for i in r2.json()["items"]]
        assert set(first_page).isdisjoint(second_page)

    def test_sort_by_key_ascending(self, client: TestClient) -> None:
        from audittrace.dependencies import get_episodic_service

        ep = get_episodic_service()
        for name in ("b.md", "a.md", "c.md"):
            ep.add_document(content="# x", title=name, file=name)
        r = client.get("/memory/episodic?sort=key&order=asc&limit=100")
        keys = [i["key"] for i in r.json()["items"]]
        assert keys == sorted(keys)

    def test_invalid_sort_value_is_422(self, client: TestClient) -> None:
        r = client.get("/memory/episodic?sort=bogus")
        assert r.status_code == 422

    def test_limit_over_max_is_422(self, client: TestClient) -> None:
        r = client.get("/memory/episodic?limit=999")
        assert r.status_code == 422

    def test_procedural_list_supports_sort_params(self, client: TestClient) -> None:
        from audittrace.dependencies import get_procedural_service

        get_procedural_service().add_document(
            content="# runbook", skill="runbook", file="runbook-notes.md"
        )
        r = client.get("/memory/procedural?sort=modified_at&order=asc&limit=10")
        assert r.status_code == 200
        keys = {i["key"] for i in r.json()["items"]}
        assert "runbook-notes.md" in keys


class TestSortAndPaginateHelper:
    """Unit coverage for _sort_and_paginate: total-order safety over a set
    that mixes real timestamps with None (defensive), every sort field, and
    the pagination slice."""

    @staticmethod
    def _items():
        return [
            {
                "key": "b.md",
                "created_at_ms": 200,
                "modified_at_ms": 5,
                "size_bytes": 30,
            },
            {
                "key": "a.md",
                "created_at_ms": 100,
                "modified_at_ms": 9,
                "size_bytes": 10,
            },
            # None timestamps must not blow up the sort (coalesced to 0).
            {
                "key": "c.md",
                "created_at_ms": None,
                "modified_at_ms": None,
                "size_bytes": None,
            },
        ]

    def test_created_at_desc_is_newest_first(self) -> None:
        from audittrace.routes.memory import _sort_and_paginate

        page, total = _sort_and_paginate(
            self._items(), sort="created_at", order="desc", limit=100, offset=0
        )
        assert total == 3
        assert [i["key"] for i in page] == ["b.md", "a.md", "c.md"]

    def test_none_timestamps_do_not_raise_and_sort_last_on_desc(self) -> None:
        from audittrace.routes.memory import _sort_and_paginate

        page, _ = _sort_and_paginate(
            self._items(), sort="modified_at", order="desc", limit=100, offset=0
        )
        assert page[-1]["key"] == "c.md"  # None → 0 → last under desc

    def test_key_and_size_sorts(self) -> None:
        from audittrace.routes.memory import _sort_and_paginate

        by_key, _ = _sort_and_paginate(
            self._items(), sort="key", order="asc", limit=100, offset=0
        )
        assert [i["key"] for i in by_key] == ["a.md", "b.md", "c.md"]
        by_size, _ = _sort_and_paginate(
            self._items(), sort="size", order="desc", limit=100, offset=0
        )
        assert [i["key"] for i in by_size][0] == "b.md"  # 30 is largest

    def test_pagination_slice_and_total(self) -> None:
        from audittrace.routes.memory import _sort_and_paginate

        page, total = _sort_and_paginate(
            self._items(), sort="key", order="asc", limit=1, offset=1
        )
        assert total == 3
        assert [i["key"] for i in page] == ["b.md"]


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

    # ── Populated paths ────────────────────────────────────────────────
    # The two tests above only ever exercised the empty-list and 404
    # branches, so the response-building code — every field mapping in both
    # handlers — was never executed by the suite. #364 surfaced that while
    # moving serialisation inside the session scope. These seed real rows
    # and assert the documented shape.

    @staticmethod
    async def _seed_session_with_interaction() -> None:
        from datetime import datetime

        from audittrace.db.models import InteractionRecord as InteractionRow
        from audittrace.db.models import SessionRecord as SessionRow
        from audittrace.dependencies import get_postgres_factory

        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            db.add(
                SessionRow(
                    id="sess-conv-1",
                    project="AuditTrace-AI",
                    date="2026-07-20",
                    summary="a summary",
                    key_points='["one","two"]',
                    model="qwen3.6",
                    user_id="sentinel-user",
                    summarized_at=datetime(2026, 7, 20, 12, 0, 0),
                )
            )
            db.add(
                InteractionRow(
                    project="AuditTrace-AI",
                    source="opencode",
                    question="q1",
                    answer="a1",
                    model="qwen3.6",
                    user_id="sentinel-user",
                    session_id="sess-conv-1",
                    timestamp="2026-07-20T12:00:00",
                    trace_id="a" * 32,
                )
            )
            await db.commit()

    @pytest.mark.asyncio
    async def test_list_returns_full_session_shape(self, client: TestClient) -> None:
        await self._seed_session_with_interaction()
        body = client.get("/memory/conversational").json()
        assert body["total"] >= 1
        item = next(i for i in body["items"] if i["id"] == "sess-conv-1")
        assert item["project"] == "AuditTrace-AI"
        assert item["summary"] == "a summary"
        assert item["key_points"] == '["one","two"]'
        assert item["model"] == "qwen3.6"
        assert item["user_id"] == "sentinel-user"
        # summarized_at is a DateTime column serialised to ISO-8601.
        assert item["summarized_at"] == "2026-07-20T12:00:00"

    @pytest.mark.asyncio
    async def test_read_returns_session_plus_interactions(
        self, client: TestClient
    ) -> None:
        await self._seed_session_with_interaction()
        body = client.get("/memory/conversational/sess-conv-1").json()

        assert body["session"]["id"] == "sess-conv-1"
        assert body["session"]["summary"] == "a summary"
        assert body["session"]["summarized_at"] == "2026-07-20T12:00:00"

        assert body["total"] == 1
        row = body["interactions"][0]
        # `timestamp` is a String column (migration 005) — passed through
        # verbatim rather than reformatted.
        assert row["timestamp"] == "2026-07-20T12:00:00"
        assert row["question"] == "q1"
        assert row["answer"] == "a1"
        assert row["session_id"] == "sess-conv-1"
        assert row["source"] == "opencode"
        assert row["trace_id"] == "a" * 32
        assert row["status"] == "success"

    @pytest.mark.asyncio
    async def test_read_summarised_null_serialises_as_none(
        self, client: TestClient
    ) -> None:
        """`summarized_at` is nullable — NULL must not blow up the isoformat."""
        from audittrace.db.models import SessionRecord as SessionRow
        from audittrace.dependencies import get_postgres_factory

        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            db.add(
                SessionRow(
                    id="sess-conv-2",
                    project="P",
                    date="2026-07-20",
                    summary="s",
                    key_points="[]",
                    model="m",
                    user_id="sentinel-user",
                    summarized_at=None,
                )
            )
            await db.commit()

        body = client.get("/memory/conversational/sess-conv-2").json()
        assert body["session"]["summarized_at"] is None
        assert body["interactions"] == []
        assert body["total"] == 0


# ── Tier-B: PDF robustness (ADR-050) ─────────────────────────────────────────


class TestExtractionWarningCodes:
    """Closed-set discipline on the JSONB ``extraction_warnings.code``
    enum. Adding a new code without an ADR-050 amendment is a quiet
    documentation drift; this test pins the set so the drift surfaces
    in CI."""

    def test_warning_codes_match_adr_050_closed_set(self) -> None:
        from audittrace.routes.memory import _PDF_WARNING_CODES

        # The exact set documented in ADR-050 §extraction_warnings (tier-A
        # / tier-B) + ADR-056 §2 (tier-C corruption + metadata classes).
        # Adding a code: bump the relevant ADR + add it here. Removing
        # one: same. CI fails the diff if these drift.
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
            # tier-B
            "encrypted",
            "no_text_layer",
            "ocr_low_confidence",
            "attachment",
            "attachment_quarantine_failed",
            "form_fields",
            # tier-C (ADR-056 #16 corrupted-file + #10 metadata)
            "pdf_corrupted_xref",
            "pdf_corrupted_structure",
            "pdf_metadata_parse_error",
        }
        assert _PDF_WARNING_CODES == expected


class TestSignatureStatusCodes:
    """Closed-set discipline on the ``signature_status`` taxonomy
    (per ADR-052 §1). Adding a new status value without an ADR
    amendment is a quiet audit-taxonomy drift; this test pins the
    set so the drift surfaces in CI. Mirrors
    :class:`TestExtractionWarningCodes` for the extraction-warnings
    codes."""

    def test_signature_status_codes_match_adr_052_closed_set(self) -> None:
        from audittrace.routes.memory import _SIGNATURE_STATUS_CODES

        # The exact 9 values documented in ADR-052 §1 + ADR-054 §1.
        # Adding a value: bump the ADR + add it here. Removing one:
        # same. CI fails the diff if these drift. The split across
        # operator/runtime conditions, structural, and verdict
        # categories is intentional — see the constant's docstring.
        expected = {
            # operator/runtime conditions
            "check_skipped",
            "check_unavailable",
            "check_failed",
            # structural (the document carries no signatures)
            "none",
            # verdicts (pyhanko produced a verdict for at least
            # one embedded signature)
            "signed_valid",
            "signed_invalid",
            "signed_untrusted",
            "signed_expired",  # ADR-054: valid as-of signing time, expired now
            "signed_tampered",
        }
        assert _SIGNATURE_STATUS_CODES == expected


class TestScanStatusCodes:
    """Closed-set discipline on the ``memory_items.scan_status`` enum
    (per ADR-048 §Failure modes). Adding a new status value without an
    ADR-048 amendment is a quiet audit-taxonomy drift; this test pins
    the set so the drift surfaces in CI. Mirrors
    :class:`TestSignatureStatusCodes`."""

    def test_scan_status_codes_match_adr_048_closed_set(self) -> None:
        from audittrace.routes.memory import _SCAN_STATUS_CODES

        # The exact 6 values documented in ADR-048 §Failure modes table.
        # Adding a value: bump the ADR + add it here. Removing one:
        # same. CI fails the diff if these drift.
        expected = {
            "pending_scan",
            "scanning",
            "scanned_clean",
            "rejected_malware",
            "scan_failed",
            "scan_unrecoverable",
        }
        assert _SCAN_STATUS_CODES == expected


class TestEventClassValues:
    """Closed-set discipline on the ``interactions.event_class`` enum.
    ADR-048 introduces ``security`` to distinguish content-control
    verdict rows from interaction rows so SOC tooling can alert on
    rejections without scanning every interaction row.

    Adding a new event class is a SOC-tooling-shape change — pinned
    here to force the conversation."""

    def test_event_class_values_match_adr_048_closed_set(self) -> None:
        from audittrace.routes.memory import _EVENT_CLASS_VALUES

        # ``interaction`` is the legacy implicit value (chat completions,
        # tool calls); ``security`` is added by ADR-048's verdict
        # consumer; ``assessment`` is added by ADR-058's recursive
        # self-audit (the recorder recording its own security review);
        # ``memory_access`` is added by ADR-062 §5 (WU-A4) — every
        # /memory/* read/list/write/delete emits a first-class audit row.
        expected = {"interaction", "security", "assessment", "memory_access"}
        assert _EVENT_CLASS_VALUES == expected


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

    async def test_encrypted_pdf_yields_zero_chunks_and_warning(
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
        manifest.upsert_pdf_metadata = AsyncMock()
        await _flush_pdf_manifest(
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
        # MagicMock the rest of the code path doesn't inspect. Patched
        # on ``memory_pdf.extraction`` — where ``_ocr_render_page``
        # actually lives and imports ``io`` itself (``routes/memory.py``
        # only re-exports the name; ADR-062 Phase B WU-B5 removed its
        # own now-unused ``import io``, so the old patch target no
        # longer resolves).
        with patch(
            "audittrace.routes.memory_pdf.extraction.io.BytesIO"
        ) as mock_bytesio:
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

        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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

        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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

    async def test_service_failure_is_swallowed(self) -> None:
        """Postgres outage during manifest write must not undo the
        ChromaDB chunk writes already committed. Per ADR-050 §#22."""
        from unittest.mock import MagicMock

        from audittrace.routes.memory import _flush_pdf_manifest

        manifest = MagicMock()
        manifest.upsert_pdf_metadata = AsyncMock(side_effect=RuntimeError("pg down"))
        # Should NOT raise.
        await _flush_pdf_manifest(
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


class TestFlushMdManifestHelper:
    """ADR-059 WU-1c — ``memory_md_manifest._flush_md_manifest`` resilience,
    mirroring ``TestPdfFlushManifest`` above for the ``.md`` fold path:
    missing service is a no-op; per-chunk service errors are logged +
    swallowed (a Postgres outage must not undo the ChromaDB chunks already
    committed by ``_index_md_objects``)."""

    async def test_none_service_is_silent_noop(self) -> None:
        from audittrace.routes.memory_md_manifest import _flush_md_manifest

        # Should not raise. No assertion needed beyond "did not crash".
        await _flush_md_manifest(
            None,
            keys=["decisions/abc123"],
            filename="x.md",
            sizes_bytes=[42],
            user_id="u",
            tier="private",
        )

    async def test_per_chunk_failure_is_swallowed_and_others_still_attempted(
        self,
    ) -> None:
        """One chunk's Postgres failure must not abort the remaining
        chunks in the same file — each chunk is an independent best-effort
        write."""
        from unittest.mock import AsyncMock, MagicMock

        from audittrace.routes.memory_md_manifest import _flush_md_manifest

        manifest = MagicMock()
        manifest.record_create = AsyncMock(side_effect=[RuntimeError("pg down"), None])
        # Should NOT raise, despite the first chunk's write failing.
        await _flush_md_manifest(
            manifest,
            keys=["decisions/chunk-0", "decisions/chunk-1"],
            filename="x.md",
            sizes_bytes=[10, 20],
            user_id="u",
            tier="private",
        )
        assert manifest.record_create.call_count == 2

    async def test_writes_one_row_per_chunk_with_matching_fields(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from audittrace.routes.memory_md_manifest import _flush_md_manifest

        manifest = MagicMock()
        manifest.record_create = AsyncMock()
        await _flush_md_manifest(
            manifest,
            keys=["decisions/chunk-0"],
            filename="deploy-record-canary.md",
            sizes_bytes=[57],
            user_id="fleet-service-0b0cdd4d",
            tier="private",
        )
        manifest.record_create.assert_awaited_once_with(
            layer="semantic",
            key="decisions/chunk-0",
            title="deploy-record-canary.md",
            size_bytes=57,
            user_id="fleet-service-0b0cdd4d",
            tier="private",
        )


# ── Tier-C: PDF document metadata + corruption taxonomy + per-doc audit (ADR-056) ──


class TestPdfMetadataParseDate:
    """Direct unit tests for ``_parse_pdf_date`` (ADR-056 #10)."""

    def test_full_pdf_date_with_offset(self) -> None:

        from audittrace.routes.memory import _parse_pdf_date

        dt = _parse_pdf_date("D:20260403103936+02'00'")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 4
        assert dt.day == 3
        assert dt.hour == 10
        assert dt.minute == 39
        assert dt.second == 36
        assert dt.utcoffset() is not None
        assert dt.tzinfo != UTC

    def test_zulu_form(self) -> None:

        from audittrace.routes.memory import _parse_pdf_date

        dt = _parse_pdf_date("D:20260403103936Z")
        assert dt is not None
        assert dt.tzinfo == UTC

    def test_short_form_year_only(self) -> None:
        from audittrace.routes.memory import _parse_pdf_date

        dt = _parse_pdf_date("D:2026")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 1
        assert dt.day == 1

    def test_empty_returns_none(self) -> None:
        from audittrace.routes.memory import _parse_pdf_date

        assert _parse_pdf_date("") is None
        assert _parse_pdf_date("   ") is None

    def test_garbage_returns_none(self) -> None:
        from audittrace.routes.memory import _parse_pdf_date

        assert _parse_pdf_date("not-a-date") is None
        assert _parse_pdf_date("D:99999999999999") is None

    def test_non_string_returns_none(self) -> None:
        from audittrace.routes.memory import _parse_pdf_date

        assert _parse_pdf_date(None) is None  # type: ignore[arg-type]
        assert _parse_pdf_date(12345) is None  # type: ignore[arg-type]


class TestPdfMetadataExtraction:
    """``_extract_pdf_metadata`` over fake pymupdf docs (ADR-056 #10)."""

    def test_full_metadata(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _extract_pdf_metadata

        doc = SimpleNamespace(
            metadata={
                "title": "Luis Research Proposal",
                "author": "Luis Filipe de Sousa",
                "creator": "Microsoft Word",
                "creationDate": "D:20260403103936+02'00'",
            }
        )
        title, author, creator, creation_date, codes = _extract_pdf_metadata(doc)
        assert title == "Luis Research Proposal"
        assert author == "Luis Filipe de Sousa"
        assert creator == "Microsoft Word"
        assert creation_date is not None
        assert creation_date.year == 2026
        assert codes == []

    def test_missing_metadata_returns_all_none(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _extract_pdf_metadata

        doc = SimpleNamespace(metadata={})
        title, author, creator, creation_date, codes = _extract_pdf_metadata(doc)
        assert title is None
        assert author is None
        assert creator is None
        assert creation_date is None
        assert codes == []

    def test_blank_string_collapses_to_none(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _extract_pdf_metadata

        doc = SimpleNamespace(metadata={"title": "  ", "author": "", "creator": "  "})
        title, author, creator, _, codes = _extract_pdf_metadata(doc)
        assert title is None
        assert author is None
        assert creator is None
        assert codes == []

    def test_garbage_creation_date_emits_metadata_parse_error(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _extract_pdf_metadata

        doc = SimpleNamespace(
            metadata={"title": "Doc", "creationDate": "totally not a date"}
        )
        title, _, _, creation_date, codes = _extract_pdf_metadata(doc)
        assert title == "Doc"
        assert creation_date is None
        assert "pdf_metadata_parse_error" in codes

    def test_oversize_string_truncated(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _extract_pdf_metadata

        long = "x" * 1000
        doc = SimpleNamespace(metadata={"title": long, "author": long})
        title, author, _, _, _ = _extract_pdf_metadata(doc)
        assert title is not None and len(title) == 255
        assert author is not None and len(author) == 255


class TestPdfCorruptionClassification:
    """``_classify_pdf_extraction_error`` covers the closed-set codes
    introduced in ADR-056 §2."""

    def test_xref_message_classified_as_xref(self) -> None:
        from audittrace.routes.memory import _classify_pdf_extraction_error

        assert (
            _classify_pdf_extraction_error(RuntimeError("invalid xref offset"))
            == "pdf_corrupted_xref"
        )

    def test_trailer_message_classified_as_xref(self) -> None:
        from audittrace.routes.memory import _classify_pdf_extraction_error

        assert (
            _classify_pdf_extraction_error(RuntimeError("trailer not found"))
            == "pdf_corrupted_xref"
        )

    def test_filedata_class_classified_as_structure(self) -> None:
        from audittrace.routes.memory import _classify_pdf_extraction_error

        class FileDataError(Exception):
            pass

        assert (
            _classify_pdf_extraction_error(FileDataError("bad data"))
            == "pdf_corrupted_structure"
        )

    def test_unknown_falls_through_to_structure(self) -> None:
        from audittrace.routes.memory import _classify_pdf_extraction_error

        assert (
            _classify_pdf_extraction_error(RuntimeError("something unexpected"))
            == "pdf_corrupted_structure"
        )


class TestPdfFlushManifestDetailsLog:
    """ADR-056 #24 — _flush_pdf_manifest appends a per-document outcome
    when ``details_log`` is provided."""

    async def test_details_log_appended_success(self) -> None:
        from audittrace.routes.memory import _flush_pdf_manifest

        log: list[dict] = []
        await _flush_pdf_manifest(
            manifest_service=None,
            layer="episodic",
            key="papers/foo.pdf",
            user_id="u",
            size_bytes=1024,
            page_count=23,
            signature_status="signed_expired",
            ocr_coverage_pct=0.0,
            attachment_count=0,
            form_field_count=0,
            extraction_warnings=[],
            document_sha256="49aadc5e",
            pdf_title="Doc Title",
            pdf_author="Doc Author",
            pdf_creator="MS Word",
            pdf_creation_date=None,
            chunks_written=46,
            ok=True,
            error=None,
            details_log=log,
        )
        assert len(log) == 1
        entry = log[0]
        assert entry["chunks"] == 46
        assert entry["signature_status"] == "signed_expired"
        assert entry["page_count"] == 23
        assert entry["ok"] is True
        assert entry["error"] is None
        assert entry["pdf_title"] == "Doc Title"
        assert entry["pdf_author"] == "Doc Author"
        assert entry["pdf_creator"] == "MS Word"

    async def test_details_log_failure_outcome(self) -> None:
        from audittrace.routes.memory import _flush_pdf_manifest

        log: list[dict] = []
        await _flush_pdf_manifest(
            manifest_service=None,
            layer="episodic",
            key="papers/corrupt.pdf",
            user_id="u",
            size_bytes=512,
            page_count=None,
            signature_status=None,
            ocr_coverage_pct=None,
            attachment_count=0,
            form_field_count=0,
            extraction_warnings=[{"code": "pdf_corrupted_xref", "page": None}],
            document_sha256=None,
            chunks_written=0,
            ok=False,
            error="invalid xref offset",
            details_log=log,
        )
        assert len(log) == 1
        entry = log[0]
        assert entry["ok"] is False
        assert entry["error"] == "invalid xref offset"
        assert entry["chunks"] == 0
        assert entry["extraction_warnings"] == [
            {"code": "pdf_corrupted_xref", "page": None}
        ]

    def test_details_log_none_skips_append(self) -> None:
        """When details_log is None, no list mutations happen — the
        legacy callers stay legacy."""
        from audittrace.routes.memory import _flush_pdf_manifest

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
            details_log=None,
        )


class TestLtvSummary:
    """ADR-056 #13 — `_summarize_ltv` audit-pivot summary of the DSS dict."""

    def test_garbage_bytes_returns_none(self) -> None:
        from audittrace.routes.memory import _summarize_ltv

        assert _summarize_ltv(b"definitely not a pdf") is None

    def test_unsigned_pdf_returns_none(self) -> None:
        from unittest.mock import MagicMock, patch

        from audittrace.routes.memory import _summarize_ltv

        fake_reader = MagicMock()
        fake_reader.embedded_signatures = []

        with patch("pyhanko.pdf_utils.reader.PdfFileReader", return_value=fake_reader):
            assert _summarize_ltv(b"%PDF-1.4 minimal") is None

    def test_signed_no_dss_returns_has_dss_false(self) -> None:
        from unittest.mock import MagicMock, patch

        from audittrace.routes.memory import _summarize_ltv

        fake_sig = MagicMock()
        fake_sig.sig_object = b"<<...>>"
        fake_reader = MagicMock()
        fake_reader.embedded_signatures = [fake_sig]
        fake_root = {"/Type": "/Catalog"}
        fake_trailer = {"/Root": fake_root}
        fake_reader.trailer_view = fake_trailer
        fake_reader.trailer = fake_trailer

        with patch("pyhanko.pdf_utils.reader.PdfFileReader", return_value=fake_reader):
            result = _summarize_ltv(b"%PDF-1.4 signed-no-ltv")
        assert result is not None
        assert result == {
            "has_dss": False,
            "ocsp_responses": 0,
            "crls": 0,
            "certs": 0,
            "timestamps": 0,
            "vri_keys": 0,
        }

    def test_doc_timestamp_signature_counted(self) -> None:
        from unittest.mock import MagicMock, patch

        from audittrace.routes.memory import _summarize_ltv

        regular_sig = MagicMock()
        regular_sig.sig_object = b"<</Type /Sig>>"
        ts_sig = MagicMock()
        ts_sig.sig_object = b"<</Type /DocTimeStamp>>"
        fake_reader = MagicMock()
        fake_reader.embedded_signatures = [regular_sig, ts_sig]
        fake_root = {"/Type": "/Catalog"}
        fake_trailer = {"/Root": fake_root}
        fake_reader.trailer_view = fake_trailer
        fake_reader.trailer = fake_trailer

        with patch("pyhanko.pdf_utils.reader.PdfFileReader", return_value=fake_reader):
            result = _summarize_ltv(b"%PDF-1.4 doctimestamped")
        assert result is not None
        assert result["timestamps"] == 1


class TestPdfMetadataNonStringCreationDate:
    """Tier-C #10 — non-string `creationDate` (e.g. a datetime object) emits
    a `pdf_metadata_parse_error` warning."""

    def test_non_string_date_emits_warning(self) -> None:
        from datetime import datetime
        from types import SimpleNamespace

        from audittrace.routes.memory import _extract_pdf_metadata

        doc = SimpleNamespace(
            metadata={"title": "Doc", "creationDate": datetime(2026, 4, 3)}
        )
        title, _, _, creation_date, codes = _extract_pdf_metadata(doc)
        assert title == "Doc"
        assert creation_date is None
        assert "pdf_metadata_parse_error" in codes


class TestTocIndexInvalidEntries:
    """ADR-056 #9 — `_build_toc_index` skips malformed TOC entries."""

    def test_malformed_entries_skipped(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _build_toc_index

        toc = [
            [1, "Valid", 2],
            ["bad", "missing-page"],  # IndexError on entry[2]
            [1, None, 3],  # title=None -> skipped via title_clean
            [1, "Bad page", "not-a-number"],  # ValueError on int()
            [1, "Zero page", 0],  # page <= 0 -> skipped
            [1, "Latest", 4],
        ]
        doc = SimpleNamespace(get_toc=lambda simple=True: toc, page_count=5)
        index = _build_toc_index(doc)
        assert index[2] == "Valid"
        assert index[3] == "Valid"
        assert index[4] == "Latest"
        assert index[5] == "Latest"


class TestPdfaConformanceExtraction:
    """ADR-056 #14 — `_extract_pdfa_conformance` parses XMP."""

    def test_pdfa_3b_attribute_form(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _extract_pdfa_conformance

        xmp = (
            '<?xpacket begin="..." id="W5M0MpCehiHzreSzNTczkc9d"?>'
            '<rdf:Description xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/" '
            'pdfaid:part="3" pdfaid:conformance="B"/>'
        )
        doc = SimpleNamespace(get_xml_metadata=lambda: xmp)
        part, conf = _extract_pdfa_conformance(doc)
        assert part == "3"
        assert conf == "B"

    def test_pdfa_2u_element_form(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _extract_pdfa_conformance

        xmp = (
            "<rdf:Description xmlns:pdfaid='http://www.aiim.org/pdfa/ns/id/'>"
            "<pdfaid:part>2</pdfaid:part>"
            "<pdfaid:conformance>U</pdfaid:conformance>"
            "</rdf:Description>"
        )
        doc = SimpleNamespace(get_xml_metadata=lambda: xmp)
        part, conf = _extract_pdfa_conformance(doc)
        assert part == "2"
        assert conf == "U"

    def test_no_xmp_returns_none(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _extract_pdfa_conformance

        doc = SimpleNamespace(get_xml_metadata=lambda: "")
        part, conf = _extract_pdfa_conformance(doc)
        assert part is None
        assert conf is None

    def test_non_pdfa_xmp_records_no_conformance(self) -> None:
        """A valid XMP packet with no ``pdfaid`` namespace must yield
        ``(None, None)``, not a partial or invented value.

        Most uploaded PDFs are ordinary (non-PDF/A) documents that still
        carry XMP — Dublin Core, XMP Basic, PDF producer info. If either
        regex mis-fired on that metadata the audit row would claim PDF/A
        conformance the file does not have, and an archival-compliance
        query (``WHERE pdfa_part = '3'``) would return documents that are
        not archivable.
        """
        from types import SimpleNamespace

        from audittrace.routes.memory import _extract_pdfa_conformance

        xmp = (
            '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
            '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:pdf="http://ns.adobe.com/pdf/1.3/" '
            'pdf:Producer="LibreOffice 7.4">'
            "<dc:title>Quarterly report</dc:title>"
            "</rdf:Description>"
        )
        doc = SimpleNamespace(get_xml_metadata=lambda: xmp)
        part, conf = _extract_pdfa_conformance(doc)
        assert part is None
        assert conf is None

    def test_part_without_conformance_records_part_only(self) -> None:
        """A packet declaring ``pdfaid:part`` but no ``pdfaid:conformance``
        must record the part and leave conformance NULL.

        The two values are independent columns precisely so a half-declared
        packet degrades to partial truth. Coupling them (e.g. dropping both
        when one is absent, or defaulting conformance to 'A') would either
        lose the part evidence we do have or fabricate a conformance level
        the document never claimed.
        """
        from types import SimpleNamespace

        from audittrace.routes.memory import _extract_pdfa_conformance

        xmp = (
            "<rdf:Description xmlns:pdfaid='http://www.aiim.org/pdfa/ns/id/'>"
            "<pdfaid:part>4</pdfaid:part>"
            "</rdf:Description>"
        )
        doc = SimpleNamespace(get_xml_metadata=lambda: xmp)
        part, conf = _extract_pdfa_conformance(doc)
        assert part == "4"
        assert conf is None

    def test_xmp_raise_swallowed(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _extract_pdfa_conformance

        def raises() -> str:
            raise RuntimeError("xmp not parseable")

        doc = SimpleNamespace(get_xml_metadata=raises)
        part, conf = _extract_pdfa_conformance(doc)
        assert part is None
        assert conf is None


class TestTocIndexBuilder:
    """ADR-056 #9 — `_build_toc_index` forward-fills TOC entries to pages."""

    def test_simple_two_section_toc(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _build_toc_index

        toc = [
            [1, "Introduction", 1],
            [1, "Methods", 5],
            [1, "Results", 12],
        ]
        doc = SimpleNamespace(get_toc=lambda simple=True: toc, page_count=15)
        index = _build_toc_index(doc)
        assert index[1] == "Introduction"
        assert index[4] == "Introduction"
        assert index[5] == "Methods"
        assert index[11] == "Methods"
        assert index[12] == "Results"
        assert index[15] == "Results"

    def test_empty_toc_returns_empty_dict(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _build_toc_index

        doc = SimpleNamespace(get_toc=lambda simple=True: [], page_count=5)
        assert _build_toc_index(doc) == {}

    def test_toc_raise_returns_empty_dict(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _build_toc_index

        def raises(simple: bool = True) -> list:
            raise RuntimeError("no toc")

        doc = SimpleNamespace(get_toc=raises, page_count=5)
        assert _build_toc_index(doc) == {}

    def test_toc_entries_after_first_page_leave_pre_pages_unmapped(self) -> None:
        from types import SimpleNamespace

        from audittrace.routes.memory import _build_toc_index

        toc = [[1, "First Section", 3]]
        doc = SimpleNamespace(get_toc=lambda simple=True: toc, page_count=5)
        index = _build_toc_index(doc)
        assert 1 not in index
        assert 2 not in index
        assert index[3] == "First Section"
        assert index[5] == "First Section"


class TestPdfIndexDryRun:
    """ADR-056 #23 — `?dry_run=true` skips ChromaDB writes + manifest writes
    but still surfaces per-doc outcomes via `?details=true`."""

    def test_dry_run_skips_chroma_upsert_and_manifest(self, client: TestClient) -> None:
        from unittest.mock import MagicMock, patch

        raw_bytes = b"%PDF-1.4 fake-content"

        mock_minio = MagicMock()
        mock_minio.list_objects.side_effect = lambda bucket, prefix="", **_: (
            [_mock_minio_object("episodic/clean.pdf")] if prefix == "episodic/" else []
        )
        response_obj = MagicMock()
        response_obj.read.return_value = raw_bytes
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj

        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

        rect_mock = MagicMock(x0=0.0, y0=0.0, x1=612.0, y1=792.0)
        fake_page = MagicMock()
        fake_page.get_text.return_value = "Body text."
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
        fake_doc.metadata = {"title": "Doc"}
        fake_doc.get_xml_metadata = MagicMock(return_value="")
        fake_doc.get_toc = MagicMock(return_value=[])

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
                params={
                    "collections": "ai_research_papers",
                    "dry_run": "true",
                    "details": "true",
                },
            )

        assert response.status_code == 200, response.text
        body = response.json()
        # Status flips to dry_run; dry_run=true is surfaced explicitly.
        assert body["status"] == "dry_run"
        assert body.get("dry_run") is True
        # No collection delete-and-recreate happened — existing chunks
        # are preserved.
        mock_chroma.delete_collection.assert_not_called()
        # No manifest write happened (Postgres is not touched in
        # dry-run).
        mock_manifest.upsert_pdf_metadata.assert_not_called()
        # documents array still present + per-doc outcome reported.
        assert "documents" in body
        docs = body["documents"]
        assert len(docs) == 1
        assert docs[0]["ok"] is True
        # Collection-level chunk count is zero because no upsert ran;
        # chunks_written on the per-doc outcome reports what *would*
        # have been written.
        assert body["total_chunks"] == 0
        assert docs[0]["chunks"] >= 1


class TestPdfIndexCorruptionExceptionPath:
    """ADR-056 #16 — the orchestrator's outer ``except Exception`` path.

    When pymupdf raises mid-document (corrupted xref, structural
    parse error), the orchestrator must:
    - classify the raise into a closed-set warning code via
      ``_classify_pdf_extraction_error``,
    - flush a per-doc manifest entry with ``ok=False``,
    - continue to the next file rather than aborting the batch.

    Pre-refactor (Phase 1 + Phase 2 lived in routes/memory.py) the
    coverage of this branch came from live-evidence tests through
    the route. Post-Phase-2 the orchestrator is its own module
    (memory_pdf/pipeline.py) and the per-file 90% gate needs a
    direct unit test to exercise the exception branch."""

    async def test_pymupdf_raise_is_classified_and_flushed(self) -> None:
        from unittest.mock import MagicMock, patch

        from audittrace.routes.memory_pdf.pipeline import _index_pdf_objects

        raw_bytes = b"%PDF-1.4 garbage-pretending-to-be-pdf"

        mock_minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = raw_bytes
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj

        # pymupdf.open raises on entry — this is the corrupt-file shape.
        fake_pymupdf = MagicMock()
        fake_pymupdf.open.side_effect = RuntimeError("invalid xref offset")

        mock_collection = AsyncMock()
        mock_manifest = MagicMock()
        mock_manifest.upsert_pdf_metadata = AsyncMock()

        details_log: list[dict] = []

        with patch.dict("sys.modules", {"pymupdf": fake_pymupdf}):
            chunks = await _index_pdf_objects(
                collection=mock_collection,
                minio_client=mock_minio,
                bucket="memory-shared",
                objects=[{"key": "episodic/corrupt.pdf", "filename": "corrupt.pdf"}],
                col_name="ai_research_papers",
                category="episodic",
                layer_prefix="episodic/",
                user_id="u1",
                ingestion_ts_ms=0,
                manifest_service=mock_manifest,
                details_log=details_log,
                dry_run=False,
            )

        # No chunks committed.
        assert chunks == 0
        # No ChromaDB upsert happened.
        mock_collection.upsert.assert_not_called()
        # Manifest write fired with ok=False + classified warning.
        assert mock_manifest.upsert_pdf_metadata.called
        kwargs = mock_manifest.upsert_pdf_metadata.call_args.kwargs
        warnings = kwargs["extraction_warnings"]
        # The classifier picked pdf_corrupted_xref because the raise
        # message contained "xref" — verifies the classifier was
        # invoked from the exception path.
        assert any(w.get("code") == "pdf_corrupted_xref" for w in warnings)
        # And the per-document outcome lands in details_log with
        # ok=False so /memory/index?details=true surfaces the failure.
        assert len(details_log) == 1
        assert details_log[0]["ok"] is False
        assert details_log[0]["chunks"] == 0


def _fake_pdf_page(text: str = "Body text."):
    """A pymupdf page stub with the surface the orchestrator touches."""
    from unittest.mock import MagicMock

    page = MagicMock()
    page.get_text.return_value = text
    page.rect = MagicMock(x0=0.0, y0=0.0, x1=612.0, y1=792.0)
    page.widgets.return_value = []
    page.get_images.return_value = []
    page.annots = MagicMock(return_value=[])
    return page


def _fake_pdf_doc(pages: list, *, metadata: dict, toc: list, page_count: int):
    """A pymupdf Document stub, opened as a context manager by the pipeline."""
    from unittest.mock import MagicMock

    doc = MagicMock()
    doc.__iter__.return_value = iter(pages)
    doc.__enter__.return_value = doc
    doc.__exit__.return_value = None
    doc.page_count = page_count
    doc.xref_length.return_value = 10
    doc.is_encrypted = False
    doc.needs_pass = False
    doc.embfile_count.return_value = 0
    doc.metadata = metadata
    doc.get_xml_metadata = MagicMock(return_value="")
    doc.get_toc = MagicMock(return_value=toc)
    return doc


class TestPdfIndexOrchestratorEdgePaths:
    """Per-document paths of ``_index_pdf_objects`` that the route-level
    happy-path tests never reach."""

    async def test_unreadable_object_is_skipped_without_a_manifest_row(self) -> None:
        """A MinIO read failure must skip the file with no manifest row and no
        ChromaDB write.

        The manifest is the audit record of what was ingested. Writing a row
        for bytes we never obtained would assert an ingestion that did not
        happen — the manifest would claim a document is indexed while ChromaDB
        holds nothing, and a reconstruction attempt from the manifest would
        dead-end with no trace of why.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from audittrace.routes.memory_pdf.pipeline import _index_pdf_objects

        mock_minio = MagicMock()
        # _read_minio_object swallows the raise and returns None.
        mock_minio.get_object.side_effect = OSError("connection reset by peer")

        fake_pymupdf = MagicMock()
        mock_collection = AsyncMock()
        mock_manifest = MagicMock()
        mock_manifest.upsert_pdf_metadata = AsyncMock()
        details_log: list[dict] = []

        with patch.dict("sys.modules", {"pymupdf": fake_pymupdf}):
            chunks = await _index_pdf_objects(
                collection=mock_collection,
                minio_client=mock_minio,
                bucket="memory-shared",
                objects=[
                    {"key": "episodic/unreadable.pdf", "filename": "unreadable.pdf"}
                ],
                col_name="ai_research_papers",
                category="episodic",
                layer_prefix="episodic/",
                user_id="u1",
                ingestion_ts_ms=0,
                manifest_service=mock_manifest,
                details_log=details_log,
                dry_run=False,
            )

        assert chunks == 0
        mock_collection.upsert.assert_not_called()
        mock_manifest.upsert_pdf_metadata.assert_not_called()
        assert details_log == []
        # The document was never even opened — the skip happens before parsing.
        fake_pymupdf.open.assert_not_called()

    async def test_toc_section_and_metadata_warning_reach_chunks_and_manifest(
        self,
    ) -> None:
        """A page covered by a TOC entry must keep ``toc_section`` on every
        chunk, and an unparseable ``creationDate`` must surface as a
        document-level extraction warning.

        ``toc_section`` is the field auditors filter chunks by when tracing a
        claim back to a section of a source document; it is stripped from
        metadata only when genuinely unknown, so a bug that dropped it
        unconditionally would silently remove that pivot. The metadata warning
        is the complementary signal: it records "we read a creation date and it
        was malformed", distinct from "the document had none" — which is what
        an auditor needs to distinguish a producer bug from an absent field.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from audittrace.routes.memory_pdf.pipeline import _index_pdf_objects

        raw_bytes = b"%PDF-1.4 fake-content"
        mock_minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = raw_bytes
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj

        fake_doc = _fake_pdf_doc(
            [_fake_pdf_page("Introduction body text.")],
            # creationDate is a non-empty string that refuses to parse.
            metadata={"title": "Signed Report", "creationDate": "D:not-a-real-date"},
            # TOC entry starting at page 1 ⇒ page 1 maps to "Introduction".
            toc=[[1, "Introduction", 1]],
            page_count=1,
        )
        fake_pymupdf = MagicMock()
        fake_pymupdf.open.return_value = fake_doc

        mock_collection = AsyncMock()
        mock_manifest = MagicMock()
        mock_manifest.upsert_pdf_metadata = AsyncMock()
        upsert_spy = AsyncMock()
        details_log: list[dict] = []

        with (
            patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
            patch("audittrace.routes.memory._upsert_in_batches", upsert_spy),
        ):
            chunks = await _index_pdf_objects(
                collection=mock_collection,
                minio_client=mock_minio,
                bucket="memory-shared",
                objects=[{"key": "episodic/report.pdf", "filename": "report.pdf"}],
                col_name="ai_research_papers",
                category="episodic",
                layer_prefix="episodic/",
                user_id="u1",
                ingestion_ts_ms=1234,
                manifest_service=mock_manifest,
                details_log=details_log,
                dry_run=False,
            )

        assert chunks >= 1
        # Every chunk of a TOC-covered page carries the section title.
        assert upsert_spy.await_count == 1
        metadatas = upsert_spy.await_args.args[3]
        assert metadatas, "the page must have produced at least one chunk"
        assert all(md["toc_section"] == "Introduction" for md in metadatas)

        # The malformed date is recorded as a document-level (page=None)
        # warning, and the title that DID parse still reaches the manifest.
        kwargs = mock_manifest.upsert_pdf_metadata.call_args.kwargs
        warnings = kwargs["extraction_warnings"]
        assert {"code": "pdf_metadata_parse_error", "page": None} in warnings
        assert kwargs["pdf_title"] == "Signed Report"
        # Malformed input degrades that one field, it does not fail the doc.
        assert kwargs["pdf_creation_date"] is None
        assert details_log[0]["ok"] is True

    async def test_zero_page_document_reports_no_ocr_coverage(self) -> None:
        """A document reporting ``page_count == 0`` must flush a manifest row
        with ``ocr_coverage_pct=None`` rather than divide by zero.

        Zero-page PDFs are real (structurally valid files whose page tree is
        empty, and a common shape from truncated/generated output). The OCR
        coverage percentage is ``ocr_pages / page_count``; an unguarded
        division would raise inside the per-file loop, abort the whole batch,
        and take down ingestion for every remaining document in the layer.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from audittrace.routes.memory_pdf.pipeline import _index_pdf_objects

        raw_bytes = b"%PDF-1.4 empty-page-tree"
        mock_minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = raw_bytes
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj

        fake_doc = _fake_pdf_doc([], metadata={"title": "Empty"}, toc=[], page_count=0)
        fake_pymupdf = MagicMock()
        fake_pymupdf.open.return_value = fake_doc

        mock_collection = AsyncMock()
        mock_manifest = MagicMock()
        mock_manifest.upsert_pdf_metadata = AsyncMock()
        details_log: list[dict] = []

        with patch.dict("sys.modules", {"pymupdf": fake_pymupdf}):
            chunks = await _index_pdf_objects(
                collection=mock_collection,
                minio_client=mock_minio,
                bucket="memory-shared",
                objects=[{"key": "episodic/empty.pdf", "filename": "empty.pdf"}],
                col_name="ai_research_papers",
                category="episodic",
                layer_prefix="episodic/",
                user_id="u1",
                ingestion_ts_ms=0,
                manifest_service=mock_manifest,
                details_log=details_log,
                dry_run=False,
            )

        assert chunks == 0
        mock_collection.upsert.assert_not_called()
        # The document is still recorded — "we processed it and it held
        # nothing" is an auditable outcome, not a silent skip.
        kwargs = mock_manifest.upsert_pdf_metadata.call_args.kwargs
        assert kwargs["page_count"] == 0
        assert kwargs["ocr_coverage_pct"] is None
        assert details_log[0]["ok"] is True


class TestPdfIndexDetailsResponseShape:
    """ADR-056 #24 — ``?details=true`` adds a ``documents`` array to the
    /memory/index response; legacy ``?details=false`` (default) keeps
    the existing shape."""

    def test_default_response_omits_documents(self, client: TestClient) -> None:
        from unittest.mock import MagicMock, patch

        raw_bytes = b"%PDF-1.4 fake-content"

        mock_minio = MagicMock()
        mock_minio.list_objects.side_effect = lambda bucket, prefix="", **_: (
            [_mock_minio_object("episodic/clean.pdf")] if prefix == "episodic/" else []
        )
        response_obj = MagicMock()
        response_obj.read.return_value = raw_bytes
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj

        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

        rect_mock = MagicMock(x0=0.0, y0=0.0, x1=612.0, y1=792.0)
        fake_page = MagicMock()
        fake_page.get_text.return_value = "Body text."
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

        with (
            patch(
                "audittrace.routes.memory._get_minio_client", return_value=mock_minio
            ),
            patch("audittrace.routes.memory.get_chromadb", return_value=mock_chroma),
            patch(
                "audittrace.routes.memory.get_memory_manifest_service",
                return_value=MagicMock(),
            ),
            patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "ai_research_papers"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        # Legacy keys must be present and unchanged.
        assert body["status"] == "indexed"
        assert "collections" in body
        assert "total_chunks" in body
        assert "duration_s" in body
        # ?details default false — no documents key.
        assert "documents" not in body

    def test_details_true_adds_per_document_array(self, client: TestClient) -> None:
        from unittest.mock import MagicMock, patch

        raw_bytes = b"%PDF-1.4 fake-content"

        mock_minio = MagicMock()
        mock_minio.list_objects.side_effect = lambda bucket, prefix="", **_: (
            [_mock_minio_object("episodic/clean.pdf")] if prefix == "episodic/" else []
        )
        response_obj = MagicMock()
        response_obj.read.return_value = raw_bytes
        response_obj.__enter__.return_value = response_obj
        mock_minio.get_object.return_value = response_obj

        mock_collection = AsyncMock()
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

        rect_mock = MagicMock(x0=0.0, y0=0.0, x1=612.0, y1=792.0)
        fake_page = MagicMock()
        fake_page.get_text.return_value = "Body text."
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
        # Real metadata dict so the tier-C #10 path runs end-to-end.
        fake_doc.metadata = {
            "title": "Clean Doc",
            "author": "Alice",
            "creator": "Microsoft Word",
            "creationDate": "D:20260403103936Z",
        }

        fake_pymupdf = MagicMock()
        fake_pymupdf.open.return_value = fake_doc

        with (
            patch(
                "audittrace.routes.memory._get_minio_client", return_value=mock_minio
            ),
            patch("audittrace.routes.memory.get_chromadb", return_value=mock_chroma),
            patch(
                "audittrace.routes.memory.get_memory_manifest_service",
                return_value=MagicMock(),
            ),
            patch.dict("sys.modules", {"pymupdf": fake_pymupdf}),
        ):
            response = client.post(
                "/memory/index",
                params={"collections": "ai_research_papers", "details": "true"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert "documents" in body
        docs = body["documents"]
        assert len(docs) == 1
        d = docs[0]
        # Per-document outcome shape (ADR-056 §3).
        assert d["ok"] is True
        assert d["error"] is None
        assert d["chunks"] >= 1
        assert d["page_count"] == 1
        assert d["pdf_title"] == "Clean Doc"
        assert d["pdf_author"] == "Alice"
        assert d["pdf_creator"] == "Microsoft Word"
        # Date is serialised as ISO-8601 string.
        assert isinstance(d["pdf_creation_date"], str)
        assert d["pdf_creation_date"].startswith("2026-04-03")


# ── ?hard=true privilege guard (#366 branch coverage) ──────────────────────
# `?hard=true` permanently destroys a memory item rather than soft-deleting
# it, so it is gated on audittrace:admin. Only the ALLOWED side of that guard
# was exercised; the 403 refusal path had no test at all. A regression here
# would let any caller holding the ordinary per-layer write scope irreversibly
# destroy audit-relevant records — the one deletion the manifest cannot undo.


class TestHardDeleteRequiresAdmin:
    @staticmethod
    def _as_non_admin(client: TestClient):
        """Swap the bypass-mode sentinel (which is admin) for a plain writer."""
        from dataclasses import replace

        from audittrace.auth import require_user
        from audittrace.identity import sentinel_user_context

        plain = replace(
            sentinel_user_context(),
            is_admin=False,
            scopes=("memory:procedural:write", "memory:semantic:write"),
        )
        client.app.dependency_overrides[require_user] = lambda: plain

    def test_procedural_hard_delete_denied_without_admin(
        self, client: TestClient
    ) -> None:
        self._as_non_admin(client)
        try:
            r = client.delete("/memory/procedural/SKILL-x.md?hard=true")
            assert r.status_code == 403
            assert "audittrace:admin" in r.json()["detail"]
        finally:
            client.app.dependency_overrides.clear()

    def test_semantic_hard_delete_denied_without_admin(
        self, client: TestClient
    ) -> None:
        self._as_non_admin(client)
        try:
            r = client.delete("/memory/semantic/audittrace/doc-1?hard=true")
            assert r.status_code == 403
            assert "audittrace:admin" in r.json()["detail"]
        finally:
            client.app.dependency_overrides.clear()

    def test_soft_delete_is_allowed_without_admin(self, client: TestClient) -> None:
        """The guard must gate ONLY the destructive variant.

        If it also blocked soft delete, ordinary writers could never retract
        an item — the guard would have turned a safety rail into an outage.
        """
        self._as_non_admin(client)
        try:
            r = client.delete("/memory/procedural/SKILL-x.md")
            assert r.status_code != 403
        finally:
            client.app.dependency_overrides.clear()


class TestConversationalListFilters:
    """Query filters on GET /memory/conversational.

    Each filter is a separate `if` that narrows the SELECT. An inverted or
    dropped filter silently returns the wrong slice of the audit record —
    the failure mode is a WRONG ANSWER to an auditor, not an error, so it
    needs a test that would notice.
    """

    @staticmethod
    async def _seed_two_projects() -> None:
        from datetime import datetime

        from audittrace.db.models import SessionRecord as SessionRow
        from audittrace.dependencies import get_postgres_factory

        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            db.add(
                SessionRow(
                    id="f-alpha",
                    project="Alpha",
                    date="2026-07-01",
                    summary="s",
                    key_points="[]",
                    model="m",
                    user_id="sentinel-user",
                    summarized_at=datetime(2026, 7, 1, 9, 0, 0),
                )
            )
            db.add(
                SessionRow(
                    id="f-beta",
                    project="Beta",
                    date="2026-07-20",
                    summary="s",
                    key_points="[]",
                    model="m",
                    user_id="sentinel-user",
                    summarized_at=None,
                )
            )
            await db.commit()

    @pytest.mark.asyncio
    async def test_project_filter_narrows_results(self, client: TestClient) -> None:
        await self._seed_two_projects()
        ids = {
            i["id"]
            for i in client.get("/memory/conversational?project=Alpha").json()["items"]
        }
        assert "f-alpha" in ids
        assert "f-beta" not in ids, "project filter did not exclude the other project"

    @pytest.mark.asyncio
    async def test_since_filter_excludes_older_sessions(
        self, client: TestClient
    ) -> None:
        await self._seed_two_projects()
        ids = {
            i["id"]
            for i in client.get("/memory/conversational?since=2026-07-10").json()[
                "items"
            ]
        }
        assert "f-beta" in ids
        assert "f-alpha" not in ids, "since filter did not exclude the older row"

    @pytest.mark.asyncio
    async def test_summarised_true_and_false_are_complementary(
        self, client: TestClient
    ) -> None:
        """summarised=true/false are separate branches — assert BOTH, and that
        they partition the set rather than both returning everything."""
        await self._seed_two_projects()
        yes = {
            i["id"]
            for i in client.get("/memory/conversational?summarised=true").json()[
                "items"
            ]
        }
        no = {
            i["id"]
            for i in client.get("/memory/conversational?summarised=false").json()[
                "items"
            ]
        }
        assert "f-alpha" in yes and "f-alpha" not in no
        assert "f-beta" in no and "f-beta" not in yes


# ── Indexing-pipeline helper branches (#366) ───────────────────────────────
# These helpers sit upstream of ChromaDB. A wrong branch here does not raise —
# it quietly puts the wrong thing in the semantic index, and every later
# `recall_semantic` inherits the error. That is a WRONG ANSWER failure mode,
# which is exactly what the branch gate exists to catch.


class TestChunkTextBranches:
    def test_whitespace_only_chunk_is_dropped(self) -> None:
        """A chunk of pure whitespace must never reach the embedder.

        It would be embedded into a meaningless vector and then returned as a
        recall hit — polluting the context the model reasons over. The
        `if chunk.strip()` guard is the only thing preventing that, and only
        its true side was exercised.
        """
        from audittrace.routes.memory import _chunk_text

        # Long enough to force chunking; the tail slice is pure whitespace.
        text = "A" * 1500 + " " * 400
        chunks = _chunk_text(text, chunk_size=1500, overlap=200)
        assert all(c.strip() for c in chunks), "an all-whitespace chunk survived"

    def test_short_text_is_returned_as_a_single_chunk(self) -> None:
        """Below the threshold the splitter must not fragment the document."""
        from audittrace.routes.memory import _chunk_text

        assert _chunk_text("short doc") == ["short doc"]


class TestListObjectsFromMinioBranches:
    class _Obj:
        def __init__(self, name):
            self.object_name = name

    def test_directory_marker_keys_are_skipped(self) -> None:
        """Keys ending in '/' are prefix markers, not documents.

        S3-compatible stores surface them as zero-byte objects. Indexing one
        would create a manifest row with an empty filename that no later
        lookup can match — a phantom entry in the audit record.
        """
        from audittrace.routes.memory import _list_objects_from_minio

        class _Client:
            def list_objects(self, bucket, prefix=None):
                return [
                    TestListObjectsFromMinioBranches._Obj("episodic/ADR-1.md"),
                    TestListObjectsFromMinioBranches._Obj("episodic/"),  # marker
                    TestListObjectsFromMinioBranches._Obj(None),  # defensive
                ]

        got = _list_objects_from_minio(_Client(), "bucket", "episodic/")
        assert [o["filename"] for o in got] == ["ADR-1.md"]

    def test_unprefixed_key_keeps_its_whole_name(self) -> None:
        """A key with no '/' is its own filename — the rsplit must not eat it."""
        from audittrace.routes.memory import _list_objects_from_minio

        class _Client:
            def list_objects(self, bucket, prefix=None):
                return [TestListObjectsFromMinioBranches._Obj("top-level.md")]

        got = _list_objects_from_minio(_Client(), "bucket", "")
        assert got == [{"key": "top-level.md", "filename": "top-level.md"}]


class TestPdfUploadRequiresScanPipeline:
    """A PDF must never be stored while the malware scanner is unavailable.

    ADR-003: uploads are quarantined and scanned before they land. If
    `scan_queue` is absent (pipeline disabled or not yet started) the route
    must refuse with 503 rather than fall through to the plain-text path —
    a fall-through would write an UNSCANNED PDF into the memory layer, which
    is the exact bypass the quarantine design exists to prevent.

    Only the pipeline-present side of this guard had coverage.
    """

    @staticmethod
    def _minimal_pdf() -> bytes:
        # %PDF- magic is what is_pdf_upload sniffs; content beyond it is
        # irrelevant because the request must be refused before any parsing.
        return b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n"

    @staticmethod
    def _register_fake_object_storage():
        """Bind a fake provider into the DI container.

        `_get_minio_client` resolves `container._instances["object_storage"]`
        and only constructs a provider when that key is missing. Registering
        here exercises the REAL factory lookup the production path uses,
        instead of monkeypatching the private helper out of the picture —
        and it covers the cache-hit arm of that lookup as a side effect.
        """
        from audittrace import dependencies as deps

        fake = MagicMock()
        deps.container._instances["object_storage"] = fake
        return fake

    def test_pdf_upload_refused_when_scan_queue_absent(
        self, client: TestClient
    ) -> None:
        # The test app starts with the scan pipeline disabled, so
        # app.state.scan_queue is unset — the condition under test.
        assert getattr(client.app.state, "scan_queue", None) is None

        store = self._register_fake_object_storage()
        r = client.post(
            "/memory/upload?layer=episodic&filename=doc.pdf",
            files={"file": ("doc.pdf", self._minimal_pdf(), "application/pdf")},
        )
        assert r.status_code == 503, "an unscanned PDF must not be accepted"
        assert "scan pipeline" in r.json()["detail"]
        # Refusal must happen BEFORE anything is written — otherwise the
        # unscanned bytes are already in the bucket when we say "no".
        store.put_object.assert_not_called()

    def test_non_pdf_upload_is_unaffected_by_the_guard(
        self, client: TestClient
    ) -> None:
        """The guard must gate PDFs only.

        If it fired on every upload, disabling the scan pipeline would take
        the whole memory layer offline rather than just the PDF path.
        """
        self._register_fake_object_storage()
        r = client.post(
            "/memory/upload?layer=episodic&filename=note.md",
            files={"file": ("note.md", b"# plain markdown\n", "text/markdown")},
        )
        assert r.status_code != 503


# ── Semantic manifest/ChromaDB merge (#366) ────────────────────────────────
# `_merge_semantic_with_chroma` reconciles two sources of truth: the manifest
# (tracked rows) and ChromaDB (what is actually indexed). Its dedup decides
# whether an auditor listing the semantic layer sees each document once, or
# sees phantom duplicates. Neither the dedup hit nor the collection-filter
# arm was covered.


class TestMergeSemanticWithChroma:
    class _Col:
        def __init__(self, payload):
            self._payload = payload

        async def get(self, **_kw):
            return self._payload

    class _Chroma:
        def __init__(self, payload, names=("audittrace_v2",)):
            self._payload = payload
            self._names = names
            self.opened: list[str] = []

        async def list_collections(self):
            return [type("C", (), {"name": n})() for n in self._names]

        async def get_or_create_collection(self, name, embedding_function=None):
            self.opened.append(name)
            return TestMergeSemanticWithChroma._Col(self._payload)

    @staticmethod
    def _entry(key: str):
        """A tracked manifest row for `key`."""
        from audittrace.services.memory_manifest import ManifestEntry

        return ManifestEntry(
            id="mi-1",
            layer="semantic",
            key=key,
            title=key,
            size_bytes=1,
            created_at_ms=1,
            modified_at_ms=1,
            created_by_user_id="u",
            modified_by_user_id="u",
            deleted_at_ms=None,
            deleted_by_user_id=None,
        )

    @pytest.mark.asyncio
    async def test_document_already_tracked_is_not_listed_twice(
        self, monkeypatch, user_context
    ) -> None:
        """A doc present in BOTH manifest and ChromaDB must appear once.

        Without the `key in known_keys` guard the same document is emitted
        twice — once as a tracked row and once as a discovered one — and an
        auditor counting the semantic layer gets an inflated number that no
        query can reconcile. Called as admin (``user_context`` sentinel) —
        this test is about dedup, not the ADR-062 Phase B ownership
        predicate (covered separately below).
        """
        from audittrace.routes import memory as m

        tracked = self._entry("audittrace_v2/doc-1")

        class _Manifest:
            async def list_for_layer(self, layer, include_deleted=False, caller=None):
                return [tracked]

        chroma = self._Chroma(
            {"ids": ["doc-1"], "documents": ["body"], "metadatas": [{}]}
        )
        monkeypatch.setattr(m, "get_memory_manifest_service", lambda: _Manifest())
        monkeypatch.setattr(m, "get_chromadb", lambda: chroma)

        items = await m._merge_semantic_with_chroma(
            [tracked], collection=None, user=user_context
        )
        keys = [i["key"] for i in items]
        assert keys.count("audittrace_v2/doc-1") == 1, f"duplicated: {keys}"

    @pytest.mark.asyncio
    async def test_untracked_document_is_discovered(
        self, monkeypatch, user_context
    ) -> None:
        """The complementary arm: indexed-but-untracked must still surface.

        These are documents in ChromaDB with no manifest row. Dropping them
        would hide real indexed content from the audit listing. Called as
        admin — see the ownership-predicate tests below for the Phase B
        filter itself.
        """
        from audittrace.routes import memory as m

        class _Manifest:
            async def list_for_layer(self, layer, include_deleted=False, caller=None):
                return []

        chroma = self._Chroma(
            {"ids": ["ghost"], "documents": ["body"], "metadatas": [{"title": "T"}]}
        )
        monkeypatch.setattr(m, "get_memory_manifest_service", lambda: _Manifest())
        monkeypatch.setattr(m, "get_chromadb", lambda: chroma)

        items = await m._merge_semantic_with_chroma(
            [], collection=None, user=user_context
        )
        assert [i["key"] for i in items] == ["audittrace_v2/ghost"]
        assert items[0]["title"] == "T"
        assert items[0]["id"] is None, "discovered rows carry no manifest id"

    @pytest.mark.asyncio
    async def test_named_collection_skips_discovery_listing(
        self, monkeypatch, user_context
    ) -> None:
        """?collection= must target ONE physical collection, not scan all.

        The unfiltered path caps at 5 collections; if the filter fell through
        to it, asking for a specific collection could silently return results
        from others — or miss it entirely past the cap. Also pins the ADR-047
        logical -> physical `_v2` resolution.
        """
        from audittrace.routes import memory as m

        class _Manifest:
            async def list_for_layer(self, layer, include_deleted=False, caller=None):
                return []

        chroma = self._Chroma(
            {"ids": [], "documents": [], "metadatas": []},
            names=("should_not_be_listed_v2",),
        )
        monkeypatch.setattr(m, "get_memory_manifest_service", lambda: _Manifest())
        monkeypatch.setattr(m, "get_chromadb", lambda: chroma)

        await m._merge_semantic_with_chroma(
            [], collection="audittrace", user=user_context
        )
        assert chroma.opened == ["audittrace_v2"], (
            "named collection must resolve to its _v2 form and be the only "
            f"one opened; opened={chroma.opened}"
        )

    # ── ADR-062 Phase B (WU-B3, §3 hole 2) — ownership predicate ─────────
    # The raw `col.get()` discovery scan used to enumerate every row with
    # no `where` at all. These tests are the FALSIFIABLE gate: neuter the
    # `if not (user.is_admin or ...): continue` guard in
    # `_merge_semantic_with_chroma` (e.g. hardcode it to `False`) and
    # `test_non_admin_discovery_excludes_other_users_private_row` goes RED
    # (the other user's row leaks through); restore it and it goes green
    # again. `test_non_admin_discovery_includes_corpus_row` and
    # `test_admin_discovery_sees_everything` pin the two non-leak arms so
    # a reviewer can't "fix" the leak by simply hiding everything.

    @pytest.mark.asyncio
    async def test_non_admin_discovery_excludes_other_users_private_row(
        self, monkeypatch, user_context
    ) -> None:
        from dataclasses import replace

        from audittrace.routes import memory as m

        class _Manifest:
            async def list_for_layer(self, layer, include_deleted=False, caller=None):
                return []

        chroma = self._Chroma(
            {
                "ids": ["mine", "theirs"],
                "documents": ["my body", "their body"],
                "metadatas": [
                    {"user_id": "user-alice", "tier": "private"},
                    {"user_id": "user-bob", "tier": "private"},
                ],
            }
        )
        monkeypatch.setattr(m, "get_memory_manifest_service", lambda: _Manifest())
        monkeypatch.setattr(m, "get_chromadb", lambda: chroma)

        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        items = await m._merge_semantic_with_chroma([], collection=None, user=alice)
        keys = [i["key"] for i in items]
        assert any("mine" in k for k in keys)
        assert not any("theirs" in k for k in keys), f"leaked bob's row: {keys}"

    @pytest.mark.asyncio
    async def test_non_admin_discovery_includes_corpus_row(
        self, monkeypatch, user_context
    ) -> None:
        """A corpus-tier row (someone else's `user_id`, `tier="corpus"`)
        IS visible to a non-admin caller — the deliberate shared-read half
        of D1, so the fix above isn't just "hide everything not mine"."""
        from dataclasses import replace

        from audittrace.routes import memory as m

        class _Manifest:
            async def list_for_layer(self, layer, include_deleted=False, caller=None):
                return []

        chroma = self._Chroma(
            {
                "ids": ["shared-adr"],
                "documents": ["corpus body"],
                "metadatas": [{"user_id": "user-operator", "tier": "corpus"}],
            }
        )
        monkeypatch.setattr(m, "get_memory_manifest_service", lambda: _Manifest())
        monkeypatch.setattr(m, "get_chromadb", lambda: chroma)

        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        items = await m._merge_semantic_with_chroma([], collection=None, user=alice)
        keys = [i["key"] for i in items]
        assert any("shared-adr" in k for k in keys), f"corpus row hidden: {keys}"

    @pytest.mark.asyncio
    async def test_admin_discovery_sees_everything(
        self, monkeypatch, user_context
    ) -> None:
        """Admin (the sentinel bypass) is unaffected by the Phase B
        predicate — sees both users' rows, no regression to Phase A."""
        from audittrace.routes import memory as m

        class _Manifest:
            async def list_for_layer(self, layer, include_deleted=False, caller=None):
                return []

        chroma = self._Chroma(
            {
                "ids": ["mine", "theirs"],
                "documents": ["my body", "their body"],
                "metadatas": [
                    {"user_id": "user-alice", "tier": "private"},
                    {"user_id": "user-bob", "tier": "private"},
                ],
            }
        )
        monkeypatch.setattr(m, "get_memory_manifest_service", lambda: _Manifest())
        monkeypatch.setattr(m, "get_chromadb", lambda: chroma)

        items = await m._merge_semantic_with_chroma(
            [], collection=None, user=user_context
        )
        keys = [i["key"] for i in items]
        assert any("mine" in k for k in keys)
        assert any("theirs" in k for k in keys)

    # ── ADR-062 Phase B (WU-B4 review fix, 2026-08-04) ────────────────────
    # The reviewer found the `caller=user` argument on the `all_known`
    # (dedup) `list_for_layer` lookup inside `_merge_semantic_with_chroma`
    # had zero falsifying tests — removing it caused no failures across the
    # existing suite. Root cause: `all_known`/`known_keys` only affects
    # DEDUP (is a key "already tracked", so skip re-emitting it as
    # "discovered"), never authorization (the discovery loop has its own
    # independent owner-or-corpus check on every Chroma-discovered row).
    # An UNFILTERED `all_known` is only OBSERVABLE in the manifest
    # (layer,key)-collision case: another user's manifest-tracked PRIVATE
    # row for the same key makes that key "known" fleet-wide, so the
    # caller's OWN legitimately-visible Chroma doc under the identical key
    # gets silently skipped (a false-negative — hidden content, not a
    # leak — which is why leak-shaped tests never caught it). This test
    # exercises exactly that collision.

    @pytest.mark.asyncio
    async def test_all_known_caller_filter_prevents_key_collision_hiding_own_doc(
        self, monkeypatch, user_context
    ) -> None:
        """FALSIFIABLE: drop ``caller=user`` from the ``all_known`` call
        in ``_merge_semantic_with_chroma`` and this goes RED — alice's
        own doc disappears because bob's colliding manifest key makes
        it look "already known"."""
        from dataclasses import replace

        from audittrace.routes import memory as m
        from audittrace.services.memory_manifest import ManifestEntry

        # Key must match the PHYSICAL collection name the discovery
        # loop actually scans (`_Chroma`'s default `names`), not the
        # logical "decisions" name — `_merge_semantic_with_chroma`
        # builds discovered keys as f"{col_name}/{doc_id}" using
        # whatever `chroma.list_collections()` returns.
        bobs_row = ManifestEntry(
            id="mi-bob",
            layer="semantic",
            key="audittrace_v2/collide",
            title="bob's",
            size_bytes=1,
            created_at_ms=1,
            modified_at_ms=1,
            created_by_user_id="bob",
            modified_by_user_id="bob",
            deleted_at_ms=None,
            deleted_by_user_id=None,
            tier="private",
        )

        class _CallerAwareManifest:
            """Mirrors the real owner-or-corpus predicate — respects
            ``caller`` the way ``MemoryManifestService.list_for_layer``
            does, unlike this test class's other bare ``_Manifest``
            fakes (which ignore ``caller`` entirely)."""

            async def list_for_layer(self, layer, include_deleted=False, caller=None):
                rows = [bobs_row]
                if caller is None or caller.is_admin:
                    return rows
                return [
                    r
                    for r in rows
                    if r.created_by_user_id == caller.user_id or r.tier == "corpus"
                ]

        chroma = self._Chroma(
            {
                "ids": ["collide"],
                "documents": ["alice's body"],
                "metadatas": [{"user_id": "alice", "tier": "private"}],
            }
        )
        monkeypatch.setattr(
            m, "get_memory_manifest_service", lambda: _CallerAwareManifest()
        )
        monkeypatch.setattr(m, "get_chromadb", lambda: chroma)

        alice = replace(user_context, user_id="alice", is_admin=False, scopes=())
        items = await m._merge_semantic_with_chroma([], collection=None, user=alice)
        keys = [i["key"] for i in items]
        assert any("collide" in k for k in keys), (
            f"alice's own doc was hidden by bob's colliding manifest key: {keys}"
        )

    # ── ADR-059 fleet-recall gap (WU-1, 2026-08-07) ───────────────────────
    # `scripts/deploy/memory.py::recall_deploy_lessons` reads this same
    # `list_semantic` -> `_merge_semantic_with_chroma` code path with a
    # SERVICE identity. Fleet lessons are folded PRIVATE-tier under that
    # identity into a large, shared physical collection (`decisions_v2`)
    # that also holds every other user's + admin's rows. The pre-fix
    # discovery scan pulled an UNFILTERED top-`_SEMANTIC_DISCOVERY_LIMIT`
    # slice and applied the owner-or-corpus gate AFTER the cap — so a
    # caller's own freshly-folded row, sorting anywhere in ChromaDB's raw
    # `get()` order, could simply never appear inside the cap. These tests
    # use a `where`-AWARE fake (unlike `_Col`/`_Chroma` above, which
    # ignore `where` and only prove the Python-side gate) so they exercise
    # the actual mechanism `_discover_rows_for_caller` now relies on.

    class _WhereAwareCol:
        """A ChromaDB collection double that ACTUALLY applies `where` and
        `limit` server-side (flat-equality only, mirroring the real
        in-repo ChromaDB test double `db.factory.MockCollection.get`).
        Records every `get()` call so a test can pin the exact `where`
        clause issued, not just the end result."""

        def __init__(self, ids, documents, metadatas):
            self._rows = list(zip(ids, documents, metadatas))
            self.get_calls: list[dict[str, Any]] = []

        async def get(self, where=None, limit=None, include=None, **_kw):
            self.get_calls.append({"where": where, "limit": limit})
            rows = self._rows
            if where:
                rows = [
                    r for r in rows if all(r[2].get(k) == v for k, v in where.items())
                ]
            if limit is not None:
                rows = rows[:limit]
            return {
                "ids": [r[0] for r in rows],
                "documents": [r[1] for r in rows],
                "metadatas": [r[2] for r in rows],
            }

    class _WhereAwareChroma:
        def __init__(self, col):
            self._col = col

        async def get_or_create_collection(self, name, embedding_function=None):
            return self._col

    @pytest.mark.asyncio
    async def test_callers_own_private_row_survives_the_discovery_cap(
        self, monkeypatch, user_context
    ) -> None:
        """FOREVER GUARD: a caller's OWN just-folded private-tier row must
        be recallable via `list_semantic` even when the physical
        collection holds MORE than `_SEMANTIC_DISCOVERY_LIMIT` other rows
        sorting ahead of it in ChromaDB's raw `get()` order — exactly the
        fleet's real shape.

        FALSIFIABLE: in `_discover_rows_for_caller`, drop the
        `where={"user_id": ...}` scan for non-admin callers (fall back to
        the single unfiltered `col.get()`, the pre-fix behaviour) and this
        test goes RED — alice's canary, parked past the cap, never
        surfaces."""
        from dataclasses import replace

        from audittrace.routes import memory as m

        class _Manifest:
            async def list_for_layer(self, layer, include_deleted=False, caller=None):
                return []

        noise = m._SEMANTIC_DISCOVERY_LIMIT
        ids = [f"noise-{i}" for i in range(noise)] + ["alice-canary"]
        documents = ["noise body"] * noise + [
            "the teal otter named Daronne recalls memory on Tuesdays"
        ]
        metadatas = [{"user_id": "someone-else"} for _ in range(noise)] + [
            {"user_id": "user-alice"}
        ]
        col = self._WhereAwareCol(ids, documents, metadatas)
        chroma = self._WhereAwareChroma(col)
        monkeypatch.setattr(m, "get_memory_manifest_service", lambda: _Manifest())
        monkeypatch.setattr(m, "get_chromadb", lambda: chroma)

        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        items = await m._merge_semantic_with_chroma(
            [], collection="decisions", user=alice
        )
        keys = [i["key"] for i in items]
        assert any("alice-canary" in k for k in keys), (
            f"alice's own just-folded canary was dropped by the discovery cap: {keys}"
        )
        # Pin the mechanism itself: the non-admin scan issued a
        # where={"user_id": ...} query — not just a lucky unfiltered slice.
        assert {"user_id": "user-alice"} in [c["where"] for c in col.get_calls]

    @pytest.mark.asyncio
    async def test_where_aware_discovery_still_isolates_cross_user_rows(
        self, monkeypatch, user_context
    ) -> None:
        """Cross-user isolation holds under the where-scoped scan too — the
        fix must never become an over-broad query that leaks. Uses the
        SAME `where`-aware fake as the forever-canary test above (not the
        older `_Col`/`_Chroma` fakes, which ignore `where` entirely)."""
        from dataclasses import replace

        from audittrace.routes import memory as m

        class _Manifest:
            async def list_for_layer(self, layer, include_deleted=False, caller=None):
                return []

        col = self._WhereAwareCol(
            ["mine", "theirs"],
            ["my body", "their body"],
            [{"user_id": "user-alice"}, {"user_id": "user-bob"}],
        )
        chroma = self._WhereAwareChroma(col)
        monkeypatch.setattr(m, "get_memory_manifest_service", lambda: _Manifest())
        monkeypatch.setattr(m, "get_chromadb", lambda: chroma)

        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        items = await m._merge_semantic_with_chroma(
            [], collection="decisions", user=alice
        )
        keys = [i["key"] for i in items]
        assert any("mine" in k for k in keys)
        assert not any("theirs" in k for k in keys), f"leaked bob's row: {keys}"


class TestADR059FleetRecallGap:
    """WU-1 (SPEC-recall-loop-and-telemetry, 2026-08-07) + WU-1b
    (SPEC-wu1b-fleet-recall-live-fix, 2026-08-07) — TRUE end-to-end
    through the real `GET /memory/semantic` route (the endpoint
    `scripts/deploy/memory.py::recall_deploy_lessons` reads) using the
    REAL in-repo ChromaDB test double (`db.factory.MockCollection`), not a
    hand-rolled fake. Closes the ADR-059 self-improvement loop: fleet
    lessons written by the fleet must be recallable BY the fleet.

    WU-1b note: WU-1's own forever-guard test used to hand-set
    ``metadata: {"tier": "private"}`` directly onto ``MockCollection.data``
    for the caller's own canary row — bypassing the real
    ``_index_md_objects`` tier-computation entirely. That is EXACTLY the
    shortcut the live symptom slipped through: the mock passed while the
    live pod returned zero. ``test_fleet_agent_recalls_its_own_just_folded_canary``
    below now folds the canary through the REAL ``POST /memory/upload`` ->
    ``POST /memory/index`` write path (what `log_deploy_record` drives), so
    the tier value it lists back is whatever that code path actually
    produces — not an assumption baked into the fixture."""

    def test_fleet_agent_recalls_its_own_just_folded_canary(
        self, client: TestClient, monkeypatch
    ) -> None:
        """#423 (SPEC-wu1b) — the REAL upload->index->list path, no hand-set
        tier fixture. Noise rows (representing OTHER users' pre-existing
        Chroma rows) are still seeded directly — they are not the caller's
        own content, so driving them through the real write path adds
        nothing to the assertion — but the fleet's OWN canary is folded via
        the actual HTTP routes end to end."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from audittrace.db.factory import MockChromaDBFactory, MockCollection
        from audittrace.routes import memory as m

        factory = MockChromaDBFactory()
        physical = "decisions_v2"
        collection = MockCollection(physical)
        # A well-used shared collection: `_SEMANTIC_DISCOVERY_LIMIT` noise
        # rows from OTHER users sort ahead of the fleet's freshly-folded
        # canary in ChromaDB's raw get() order — the WU-1 discovery-cap
        # scenario this test also still covers.
        collection.data = [
            {
                "id": f"noise-{i}",
                "document": "noise",
                "metadata": {"user_id": "some-other-user", "tier": "private"},
            }
            for i in range(m._SEMANTIC_DISCOVERY_LIMIT)
        ]
        factory.collections[physical] = collection
        chroma_client = asyncio.run(factory.get_client())
        monkeypatch.setattr(m, "get_chromadb", lambda: chroma_client)

        fleet_sub = "fleet-service-0b0cdd4d"
        _override_identity(
            client, fleet_sub, ("memory:episodic:write", "memory:semantic:read")
        )
        try:
            content = b"the teal otter named Daronne recalls memory on Tuesdays"
            with patch.object(m, "_get_minio_client", return_value=MagicMock()):
                up = client.post(
                    "/memory/upload",
                    params={"layer": "episodic", "filename": "deploy-record-canary.md"},
                    files={
                        "file": ("deploy-record-canary.md", content, "text/markdown")
                    },
                )
            assert up.status_code == 200, up.text
            key = up.json()["key"]
            assert key == f"{fleet_sub}/episodic/deploy-record-canary.md"
            assert up.json()["tier"] == "private"

            def get_object(bucket: str, k: str) -> MagicMock:
                assert (bucket, k) == ("memory-private", key), (bucket, k)
                response = MagicMock()
                response.__enter__.return_value = response
                response.__exit__.return_value = False
                response.read.return_value = content
                return response

            fake_minio = MagicMock()
            fake_minio.get_object.side_effect = get_object
            with (
                patch.object(m, "_get_minio_client", return_value=fake_minio),
                patch.object(
                    m,
                    "embed_via_nomic",
                    AsyncMock(
                        side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]
                    ),
                ),
            ):
                ix = client.post(
                    "/memory/index",
                    params={"file": key, "collections": "decisions"},
                )
            assert ix.status_code == 200, ix.text
            assert ix.json()["total_chunks"] == 1

            r = client.get("/memory/semantic?collection=decisions")
            assert r.status_code == 200, r.text
            titles = {i["title"] for i in r.json()["items"]}
            assert "deploy-record-canary.md" in titles, (
                "fleet's own just-folded canary is invisible to list_semantic "
                f"(the endpoint recall_deploy_lessons reads): {r.json()['items']}"
            )
        finally:
            client.app.dependency_overrides.clear()

    def test_fleet_agent_recalls_own_row_even_when_tier_reads_corpus(
        self, client: TestClient, monkeypatch
    ) -> None:
        """#423 hardening (SPEC-wu1b) — the robust fix: a caller's OWN row
        must be visible even when its ChromaDB tier reads (or mis-defaults
        to) "corpus" — exactly what a pre-tier-stamp legacy row, or any
        future tier-stamping regression, looks like. Deliberately NOT the
        "hand-set tier=private" antipattern the spec forbids: this
        constructs the FAILURE precondition (an owner-owned row whose tier
        is NOT private) and proves the new ownership exemption in
        `_filter_corpus_read_gate` — not the tier stamp — is what makes it
        visible. Neutering EITHER the exemption check in
        `_filter_corpus_read_gate` OR the `created_by_user_id` propagation
        in `_merge_semantic_with_chroma` turns this RED."""
        import asyncio

        from audittrace.db.factory import MockChromaDBFactory, MockCollection
        from audittrace.routes import memory as m

        factory = MockChromaDBFactory()
        physical = "decisions_v2"
        collection = MockCollection(physical)
        collection.data = [
            {
                "id": "legacy-owned-row",
                "document": "a lesson folded before the tier stamp existed",
                # No "tier" key at all: `_merge_semantic_with_chroma`
                # defaults this to "corpus" (`meta.get("tier", "corpus")`).
                "metadata": {"user_id": "fleet-service-0b0cdd4d"},
            }
        ]
        factory.collections[physical] = collection
        chroma_client = asyncio.run(factory.get_client())
        monkeypatch.setattr(m, "get_chromadb", lambda: chroma_client)

        _override_identity(client, "fleet-service-0b0cdd4d", ("memory:semantic:read",))
        try:
            r = client.get("/memory/semantic?collection=decisions")
            assert r.status_code == 200, r.text
            keys = {i["key"] for i in r.json()["items"]}
            assert any("legacy-owned-row" in k for k in keys), (
                "caller's own row was dropped by the corpus-read gate despite "
                f"being owned by the caller: {r.json()['items']}"
            )
        finally:
            client.app.dependency_overrides.clear()

    def test_non_owner_still_gated_from_corpus_defaulted_row(
        self, client: TestClient, monkeypatch
    ) -> None:
        """The WU-1b ownership exemption must not become an over-broad
        leak: a corpus-tier row NOT owned by the caller still requires
        `memory:corpus:<collection>:read` — proves the exemption checks
        ownership, not just presence of a `created_by_user_id` key."""
        import asyncio

        from audittrace.db.factory import MockChromaDBFactory, MockCollection
        from audittrace.routes import memory as m

        factory = MockChromaDBFactory()
        physical = "decisions_v2"
        collection = MockCollection(physical)
        collection.data = [
            {
                "id": "victim-corpus-row",
                "document": "victim's corpus-tier lesson",
                "metadata": {"user_id": "victim-service", "tier": "corpus"},
            }
        ]
        factory.collections[physical] = collection
        chroma_client = asyncio.run(factory.get_client())
        monkeypatch.setattr(m, "get_chromadb", lambda: chroma_client)

        _override_identity(client, "attacker-service", ("memory:semantic:read",))
        try:
            r = client.get("/memory/semantic?collection=decisions")
            assert r.status_code == 200, r.text
            keys = {i["key"] for i in r.json()["items"]}
            assert not any("victim-corpus-row" in k for k in keys), (
                f"non-owner leaked a corpus-tier row it doesn't own: {r.json()['items']}"
            )
        finally:
            client.app.dependency_overrides.clear()

    def test_fleet_agent_cannot_recall_another_services_private_canary(
        self, client: TestClient, monkeypatch
    ) -> None:
        """Cross-user isolation, end-to-end: the same fix must not leak
        another caller's private row to a different service identity."""
        import asyncio

        from audittrace.db.factory import MockChromaDBFactory, MockCollection
        from audittrace.routes import memory as m

        factory = MockChromaDBFactory()
        physical = "decisions_v2"
        collection = MockCollection(physical)
        collection.data = [
            {
                "id": "victim-row",
                "document": "victim's private lesson",
                "metadata": {"user_id": "victim-service", "tier": "private"},
            }
        ]
        factory.collections[physical] = collection
        chroma_client = asyncio.run(factory.get_client())
        monkeypatch.setattr(m, "get_chromadb", lambda: chroma_client)

        _override_identity(client, "attacker-service", ("memory:semantic:read",))
        try:
            r = client.get("/memory/semantic?collection=decisions")
            assert r.status_code == 200, r.text
            keys = {i["key"] for i in r.json()["items"]}
            assert not any("victim-row" in k for k in keys), (
                f"leaked victim-service's private row: {keys}"
            )
        finally:
            client.app.dependency_overrides.clear()


class TestWU1cManifestThreadedListingSurvivesDominantOwnerCap:
    """ADR-059 WU-1c (SPEC-wu1c-fleet-recall-discovery-cap, 2026-08-08) —
    the layer UNDER WU-1b. WU-1b fixed the tier stamp + the ownership
    exemption; a caller's own row was then visible IF it survived the
    ``_SEMANTIC_DISCOVERY_LIMIT`` cap. This class reproduces the residual
    live v1.20.3 gap WU-1b's own tests didn't cover: the DOMINANT-OWNER
    case, where the caller itself already owns MORE than
    ``_SEMANTIC_DISCOVERY_LIMIT`` rows (the fleet's real shape on the
    single-tenant laptop), so even the WU-1 owner-scoped
    ``where={"user_id": ...}`` discovery scan ALSO hits the cap and a
    fresh fold — added to the collection AFTER the 200 pre-existing rows —
    sorts outside ChromaDB's ``.get(limit=200)`` window (non-recency
    ordered doc ids).

    #423 non-negotiable: drives the REAL fold -> list path end to end
    (``POST /memory/upload`` -> ``POST /memory/index`` -> ``GET
    /memory/semantic``) with a precondition where the caller already owns
    201 rows (> the 200 cap) BEFORE the fresh fold — no fixture pre-seeds
    fewer than the cap, and no result is hand-ordered; the 201 legacy rows
    sit ahead of the fresh fold in ``MockCollection``'s insertion-ordered
    ``.get()`` exactly as ChromaDB's real non-recency doc-id order would
    strand a fresh write behind a large existing corpus.

    FALSIFIABLE: this test is RED without the WU-1c fix. Neutering the fix
    by not threading ``manifest_service`` into ``_index_md_objects`` (i.e.
    reverting the ``manifest_service=manifest_service`` kwarg at either
    ``index_memory`` call site, or gutting
    ``memory_md_manifest._flush_md_manifest`` to a no-op) leaves the fresh
    canary tracked ONLY via the capped, unordered ``.get()`` discovery
    scan — which the 201 pre-existing legacy (un-manifested) rows already
    fill past the cap, so the canary drops out of both the unfiltered AND
    the owner-scoped discovery slice and never reaches ``GET
    /memory/semantic``."""

    def test_fresh_fold_visible_when_caller_already_owns_gt_cap_rows(
        self, client: TestClient, monkeypatch
    ) -> None:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from audittrace.db.factory import MockChromaDBFactory, MockCollection
        from audittrace.routes import memory as m

        factory = MockChromaDBFactory()
        physical = "decisions_v2"
        collection = MockCollection(physical)
        # The dominant-owner precondition: the caller (not "some other
        # user") already owns MORE than `_SEMANTIC_DISCOVERY_LIMIT` rows,
        # inserted BEFORE the fresh fold below, and NONE of them carry a
        # manifest entry — exactly what every real .md fold looked like
        # before this WU-1c fix (the pre-fix `_index_md_objects` never
        # wrote a manifest row at all). This is what makes the caller's
        # OWN owner-scoped `where={"user_id": ...}` discovery scan ALSO
        # hit the cap, not just the unfiltered scan WU-1 already handled.
        fleet_sub = "fleet-service-0b0cdd4d"
        noise_count = m._SEMANTIC_DISCOVERY_LIMIT + 1
        collection.data = [
            {
                "id": f"legacy-{i}",
                "document": "a lesson folded before WU-1c",
                "metadata": {"user_id": fleet_sub, "tier": "private"},
            }
            for i in range(noise_count)
        ]
        factory.collections[physical] = collection
        chroma_client = asyncio.run(factory.get_client())
        monkeypatch.setattr(m, "get_chromadb", lambda: chroma_client)

        _override_identity(
            client, fleet_sub, ("memory:episodic:write", "memory:semantic:read")
        )
        try:
            content = (
                b"the teal otter named Daronne recalls memory past the discovery cap"
            )
            with patch.object(m, "_get_minio_client", return_value=MagicMock()):
                up = client.post(
                    "/memory/upload",
                    params={"layer": "episodic", "filename": "wu1c-canary.md"},
                    files={"file": ("wu1c-canary.md", content, "text/markdown")},
                )
            assert up.status_code == 200, up.text
            key = up.json()["key"]
            assert key == f"{fleet_sub}/episodic/wu1c-canary.md"

            def get_object(bucket: str, k: str) -> MagicMock:
                assert (bucket, k) == ("memory-private", key), (bucket, k)
                response = MagicMock()
                response.__enter__.return_value = response
                response.__exit__.return_value = False
                response.read.return_value = content
                return response

            fake_minio = MagicMock()
            fake_minio.get_object.side_effect = get_object
            with (
                patch.object(m, "_get_minio_client", return_value=fake_minio),
                patch.object(
                    m,
                    "embed_via_nomic",
                    AsyncMock(
                        side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]
                    ),
                ),
            ):
                ix = client.post(
                    "/memory/index",
                    params={"file": key, "collections": "decisions"},
                )
            assert ix.status_code == 200, ix.text
            assert ix.json()["total_chunks"] == 1

            # Sanity: the dominant-owner precondition really is in place —
            # the physical collection now holds > cap legacy rows owned by
            # the SAME caller, plus the fresh chunk.
            assert len(collection.data) == noise_count + 1

            r = client.get("/memory/semantic?collection=decisions")
            assert r.status_code == 200, r.text
            items = r.json()["items"]
            titles = {i["title"] for i in items}
            assert "wu1c-canary.md" in titles, (
                "fleet's own fold is invisible to list_semantic even though the "
                f"caller already owned {noise_count} (> the "
                f"{m._SEMANTIC_DISCOVERY_LIMIT}-row cap) rows before folding it: "
                f"{items}"
            )
            # The item that saved the canary must be the MANIFEST-tracked
            # one (real created_at_ms), not a lucky discovery-scan hit —
            # pins the actual mechanism, not just the end symptom.
            canary_item = next(i for i in items if i["title"] == "wu1c-canary.md")
            assert canary_item["created_at_ms"] is not None, (
                "canary surfaced via the capped/unordered discovery scan, not "
                f"the manifest — the WU-1c fix isn't actually wired: {canary_item}"
            )
            assert canary_item.get("discovered") is not True
        finally:
            client.app.dependency_overrides.clear()

    def test_legacy_unmanifested_row_still_falls_back_to_discovery(
        self, client: TestClient, monkeypatch
    ) -> None:
        """Backward-compat: a pre-WU-1c row that was written straight to
        ChromaDB with no manifest entry (simulating content indexed before
        this fix shipped) must still be discoverable through the existing
        ``.get()`` fallback — WU-1c demotes discovery to a fallback, it
        does not remove it. Stays well under the cap so this test isolates
        "legacy row still visible" from the dominant-owner cap mechanic
        covered above."""
        import asyncio

        from audittrace.db.factory import MockChromaDBFactory, MockCollection
        from audittrace.routes import memory as m

        factory = MockChromaDBFactory()
        physical = "decisions_v2"
        collection = MockCollection(physical)
        collection.data = [
            {
                "id": "pre-wu1c-legacy-row",
                "document": "folded before the manifest was threaded in",
                "metadata": {"user_id": "fleet-service-0b0cdd4d", "tier": "private"},
            }
        ]
        factory.collections[physical] = collection
        chroma_client = asyncio.run(factory.get_client())
        monkeypatch.setattr(m, "get_chromadb", lambda: chroma_client)

        _override_identity(client, "fleet-service-0b0cdd4d", ("memory:semantic:read",))
        try:
            r = client.get("/memory/semantic?collection=decisions")
            assert r.status_code == 200, r.text
            keys = {i["key"] for i in r.json()["items"]}
            assert any("pre-wu1c-legacy-row" in k for k in keys), (
                f"legacy un-manifested row lost visibility: {r.json()['items']}"
            )
        finally:
            client.app.dependency_overrides.clear()


class TestMemoryAccessAuditEvents:
    """ADR-062 §5 (WU-A4) falsifiable gate — every read/list/write/delete
    through the ``/memory/*`` backoffice emits a first-class, owner-scoped
    ``event_class="memory_access"`` audit row, reconstructable via
    ``GET /interactions`` under the caller's identity.

    Reviewer instruction (per the spec): neuter any of the
    ``emit_memory_audit_event`` / ``schedule_read_audit`` call sites this
    exercises in ``routes/memory.py`` — the corresponding test below must
    fail. Restore it — green again."""

    @staticmethod
    def _memory_access_rows(client: TestClient) -> list[dict[str, Any]]:
        r = client.get("/interactions", params={"event_class": "memory_access"})
        assert r.status_code == 200
        return r.json()["interactions"]

    def test_list_episodic_produces_audit_row(self, client: TestClient) -> None:
        client.get("/memory/episodic")
        rows = self._memory_access_rows(client)
        assert any(row["question"] == "op=list layer=episodic key=-" for row in rows), (
            rows
        )

    def test_read_episodic_produces_audit_row(self, client: TestClient) -> None:
        client.post(
            "/memory/episodic", json={"filename": "ADR-audit.md", "content": "x"}
        )
        client.get("/memory/episodic/ADR-audit.md")
        rows = self._memory_access_rows(client)
        assert any(
            row["question"] == "op=read layer=episodic key=ADR-audit.md" for row in rows
        ), rows

    def test_create_episodic_produces_audit_row(self, client: TestClient) -> None:
        client.post("/memory/episodic", json={"filename": "ADR-w.md", "content": "x"})
        rows = self._memory_access_rows(client)
        assert any(
            row["question"] == "op=write layer=episodic key=ADR-w.md" for row in rows
        ), rows

    def test_update_episodic_produces_audit_row(self, client: TestClient) -> None:
        client.post(
            "/memory/episodic", json={"filename": "ADR-upd.md", "content": "v1"}
        )
        client.put("/memory/episodic/ADR-upd.md", json={"content": "v2"})
        rows = self._memory_access_rows(client)
        writes = [
            r for r in rows if r["question"] == "op=write layer=episodic key=ADR-upd.md"
        ]
        assert len(writes) == 2  # one for POST (create), one for PUT (update)

    def test_delete_episodic_produces_audit_row(self, client: TestClient) -> None:
        client.post("/memory/episodic", json={"filename": "ADR-del.md", "content": "x"})
        client.delete("/memory/episodic/ADR-del.md")
        rows = self._memory_access_rows(client)
        assert any(
            row["question"] == "op=delete layer=episodic key=ADR-del.md" for row in rows
        ), rows

    def test_procedural_and_semantic_crud_produce_audit_rows(
        self, client: TestClient
    ) -> None:
        client.post(
            "/memory/procedural", json={"filename": "SKILL-audit.md", "content": "x"}
        )
        client.get("/memory/procedural")
        client.get("/memory/procedural/SKILL-audit.md")
        client.put("/memory/procedural/SKILL-audit.md", json={"content": "v2"})
        client.delete("/memory/procedural/SKILL-audit.md")

        client.post(
            "/memory/semantic",
            json={"collection": "decisions", "document_id": "d-audit", "text": "x"},
        )
        client.get("/memory/semantic")
        client.get("/memory/semantic/decisions/d-audit")
        client.put("/memory/semantic/decisions/d-audit", json={"text": "v2"})
        client.delete("/memory/semantic/decisions/d-audit")

        questions = {row["question"] for row in self._memory_access_rows(client)}
        expected = {
            "op=write layer=procedural key=SKILL-audit.md",
            "op=list layer=procedural key=-",
            "op=read layer=procedural key=SKILL-audit.md",
            "op=delete layer=procedural key=SKILL-audit.md",
            "op=write layer=semantic key=d-audit",
            "op=list layer=semantic key=-",
            "op=read layer=semantic key=d-audit",
            "op=delete layer=semantic key=d-audit",
        }
        assert expected.issubset(questions), expected - questions

    def test_bulk_index_produces_audit_row(self, client: TestClient) -> None:
        """Sentinel test identity is admin by construction (bypass mode),
        so bulk (no ``?file=``) /memory/index needs no extra override."""
        mock_minio = MagicMock()
        mock_minio.list_objects.return_value = []
        mock_chroma = MagicMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=AsyncMock())
        mock_chroma.delete_collection = AsyncMock()
        mock_chroma.list_collections = AsyncMock(return_value=[])

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
            r = client.post("/memory/index", params={"collections": "decisions"})
        assert r.status_code == 200, r.text
        rows = self._memory_access_rows(client)
        assert any(
            row["question"].startswith("op=write layer=semantic") for row in rows
        ), rows

    def test_hard_delete_is_audited_with_hard_flag(self, client: TestClient) -> None:
        client.post(
            "/memory/episodic", json={"filename": "ADR-hard.md", "content": "x"}
        )
        r = client.delete("/memory/episodic/ADR-hard.md?hard=true")
        assert r.status_code == 200
        deletes = [
            row
            for row in self._memory_access_rows(client)
            if row["question"] == "op=delete layer=episodic key=ADR-hard.md"
        ]
        assert len(deletes) == 1
        detail = json.loads(deletes[0]["error_detail"])
        assert detail["hard"] is True


class TestMemoryAuditWriteFailsClosed:
    """ADR-062 §5 read=fail-open / write=fail-closed decision (WU-A4):
    a write-path audit-emit failure must fail the REQUEST closed — the
    caller must not see a 200 for a mutation that could not be proven
    audited (see ``services/memory_audit.py`` module docstring for the
    full rationale)."""

    @staticmethod
    async def _boom(**_kwargs: Any) -> None:
        raise RuntimeError("audit store unavailable")

    def test_create_episodic_returns_500_when_audit_emit_fails(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from audittrace.routes import memory as m

        monkeypatch.setattr(m, "emit_memory_audit_event", self._boom)
        r = client.post(
            "/memory/episodic",
            json={"filename": "ADR-failclosed.md", "content": "x"},
        )
        assert r.status_code == 500

    def test_delete_episodic_returns_500_when_audit_emit_fails_but_soft_delete_lands(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 500 is a caller-visible signal demanding investigation/retry
        — it is NOT proof the underlying mutation was rolled back. Phase A
        deliberately excludes distributed-transaction/saga machinery (ADR-
        062 §9); the manifest soft-delete has already committed by the
        time the audit emit runs."""
        from audittrace.routes import memory as m

        client.post("/memory/episodic", json={"filename": "ADR-fc2.md", "content": "x"})

        with monkeypatch.context() as mp:
            mp.setattr(m, "emit_memory_audit_event", self._boom)
            r = client.delete("/memory/episodic/ADR-fc2.md")
        assert r.status_code == 500

        body = client.get("/memory/episodic?include_deleted=true").json()
        deleted = [i for i in body["items"] if i["key"] == "ADR-fc2.md"]
        assert deleted and deleted[0]["deleted_at_ms"] is not None


class TestMemoryAuditReadFailsOpen:
    """The complementary decision: a read must succeed even when its
    BACKGROUND audit emit fails — availability over completeness for the
    read path (reads vastly outnumber writes; every ``recall_*`` tool call
    is effectively a read)."""

    def test_list_episodic_succeeds_when_background_audit_emit_fails(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asserts via a patched module logger rather than ``caplog``: some
        other test in the full suite calls ``setup_logging`` (which does
        ``root.handlers.clear()``), detaching pytest's LogCaptureHandler —
        the same order-fragility documented in
        ``test_scan_verdict_consumer.py``. Patching the module logger
        directly is hermetic and survives test ordering."""

        async def _boom(**_kwargs: Any) -> None:
            raise RuntimeError("audit store unavailable")

        monkeypatch.setattr(
            "audittrace.services.memory_audit.emit_memory_audit_event", _boom
        )
        with patch("audittrace.services.memory_audit.logger") as mock_logger:
            r = client.get("/memory/episodic")
        assert r.status_code == 200
        assert mock_logger.exception.call_count == 1
        assert "memory_audit.read_emit_failed" in mock_logger.exception.call_args[0][0]


# ───────────────────────── Recall telemetry REST routes (WU-A + WU-B) ────────


class TestRecallTelemetryRestRoutes:
    """WU-A + WU-B non-vacuous proof (#423): every GET /memory/* read
    route must emit audittrace_recall_total{source=backoffice} AND
    a structured memory.read INFO log line. Neuter each → RED.

    The shared helper lives in :mod:`audittrace.services.recall_telemetry`;
    both the in-process tool recalls AND the REST routes call it."""

    def test_get_memory_semantic_increments_counter_with_backoffice_source(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GET /memory/semantic list call must increment
        audittrace_recall_total{source="backoffice", collection="semantic",
        hit=...}. Neuter the counter → RED."""
        from audittrace.services import recall_telemetry as telemetry_mod

        # Install a spy on the counter
        counter_spy: list[tuple[float, dict[str, Any]]] = []

        class _SpyCounter:
            def add(self, amount: float, labels: dict[str, Any] | None = None) -> None:
                counter_spy.append((amount, dict(labels or {})))

        real_counter = telemetry_mod._recall_counter
        telemetry_mod._recall_counter = _SpyCounter()
        try:
            r = client.get("/memory/semantic")
            assert r.status_code == 200
            # The counter must have been called once with source=backoffice
            assert len(counter_spy) >= 1
            last_call = counter_spy[-1]
            assert last_call[0] == 1
            assert last_call[1].get("source") == "backoffice"
            assert last_call[1].get("collection") in (
                "semantic",
                "decisions",
                "skills",
                "ai_research_papers",
            )
        finally:
            telemetry_mod._recall_counter = real_counter

    def test_get_memory_semantic_emits_memory_read_log_line(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GET /memory/semantic list call must emit a structured
        memory.read INFO log line with surface=backoffice.
        Neuter the logger → RED.

        The shared helper emits the log from its own module-scoped
        logger (audittrace.services.recall_telemetry), not from
        routes/memory.py."""
        with patch("audittrace.services.recall_telemetry.logger") as mock_logger:
            r = client.get("/memory/semantic")
            assert r.status_code == 200
            # Check that an INFO log with "memory.read" was emitted
            # The logger uses % formatting: logger.info("memory.read | ...", "backoffice", ...)
            info_calls = []
            for c in mock_logger.info.call_args_list:
                if c[0] and "memory.read" in str(c[0][0]):
                    info_calls.append(c)
            assert info_calls, (
                "GET /memory/semantic did not emit a 'memory.read' INFO log line"
            )
            # The format string is c[0][0], the args are c[0][1:]
            # First arg after format string should be surface ("backoffice")
            last_call = info_calls[-1]
            if last_call[0]:
                args = last_call[0][1:]  # skip format string
                assert args and args[0] == "backoffice", (
                    f"Expected surface='backoffice', got {args}"
                )

    def test_get_memory_episodic_increments_counter(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GET /memory/episodic list call must increment
        audittrace_recall_total{source="backoffice", collection="episodic"}."""
        from audittrace.services import recall_telemetry as telemetry_mod

        counter_spy: list[tuple[float, dict[str, Any]]] = []

        class _SpyCounter:
            def add(self, amount: float, labels: dict[str, Any] | None = None) -> None:
                counter_spy.append((amount, dict(labels or {})))

        real_counter = telemetry_mod._recall_counter
        telemetry_mod._recall_counter = _SpyCounter()
        try:
            r = client.get("/memory/episodic")
            assert r.status_code == 200
            assert len(counter_spy) >= 1
            last_call = counter_spy[-1]
            assert last_call[0] == 1
            assert last_call[1].get("source") == "backoffice"
            assert last_call[1].get("collection") == "episodic"
        finally:
            telemetry_mod._recall_counter = real_counter

    def test_get_memory_procedural_increments_counter(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GET /memory/procedural list call must increment
        audittrace_recall_total{source="backoffice", collection="procedural"}."""
        from audittrace.services import recall_telemetry as telemetry_mod

        counter_spy: list[tuple[float, dict[str, Any]]] = []

        class _SpyCounter:
            def add(self, amount: float, labels: dict[str, Any] | None = None) -> None:
                counter_spy.append((amount, dict(labels or {})))

        real_counter = telemetry_mod._recall_counter
        telemetry_mod._recall_counter = _SpyCounter()
        try:
            r = client.get("/memory/procedural")
            assert r.status_code == 200
            assert len(counter_spy) >= 1
            last_call = counter_spy[-1]
            assert last_call[0] == 1
            assert last_call[1].get("source") == "backoffice"
            assert last_call[1].get("collection") == "procedural"
        finally:
            telemetry_mod._recall_counter = real_counter

    def test_no_pii_in_rest_metric_labels(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REST route metric labels are {source, collection, hit, cache} ONLY —
        no user_id, no query, no content."""
        from audittrace.services import recall_telemetry as telemetry_mod

        counter_spy: list[tuple[float, dict[str, Any]]] = []

        class _SpyCounter:
            def add(self, amount: float, labels: dict[str, Any] | None = None) -> None:
                counter_spy.append((amount, dict(labels or {})))

        real_counter = telemetry_mod._recall_counter
        telemetry_mod._recall_counter = _SpyCounter()
        try:
            r = client.get("/memory/semantic")
            assert r.status_code == 200
            for _amount, labels in counter_spy:
                assert set(labels.keys()) == {"source", "collection", "hit", "cache"}, (
                    f"REST metric labels contain unexpected keys: {labels.keys()}"
                )
                # REST/backoffice reads must have cache="n/a".
                assert labels.get("cache") == "n/a", (
                    f"REST route cache label must be 'n/a', got {labels.get('cache')}"
                )
        finally:
            telemetry_mod._recall_counter = real_counter


# ─────── RECALL-METRIC-COVERAGE (2026-08-12) — fleet-vs-backoffice flip ──────


class TestRecallTelemetryFleetSourceFlip:
    """The fleet's own recalls hit these SAME REST routes (via
    ``scripts/deploy/memory.py::recall_deploy_lessons`` -> ``GET
    /memory/semantic`` and siblings) but were hardcoded ``source="backoffice"``
    at every ``emit_recall_telemetry`` call site — indistinguishable from a
    human front-door read, invisible on the fleet panels.

    Non-vacuous proof (#423): each test below asserts the label FLIPS to
    ``"fleet"`` when the request carries fleet attribution (``X-Source:
    opencode-*`` or ``X-Agent-Role``), and STAYS ``"backoffice"`` with no
    fleet header. Neuter any of the 6 emit sites back to the literal
    ``"backoffice"`` (undoing the ``classify_recall_source_from_request(...)``
    call) -> the fleet-header assertion below goes RED."""

    @staticmethod
    def _spy_counter(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, dict]]:
        from audittrace.services import recall_telemetry as telemetry_mod

        counter_spy: list[tuple[float, dict[str, Any]]] = []

        class _SpyCounter:
            def add(self, amount: float, labels: dict[str, Any] | None = None) -> None:
                counter_spy.append((amount, dict(labels or {})))

        monkeypatch.setattr(telemetry_mod, "_recall_counter", _SpyCounter())
        return counter_spy

    def test_get_memory_semantic_with_opencode_x_source_is_fleet(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        counter_spy = self._spy_counter(monkeypatch)
        r = client.get("/memory/semantic", headers={"X-Source": "opencode-builder"})
        assert r.status_code == 200
        assert counter_spy
        assert counter_spy[-1][1].get("source") == "fleet"

    def test_get_memory_semantic_with_x_agent_role_is_fleet(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        counter_spy = self._spy_counter(monkeypatch)
        r = client.get("/memory/semantic", headers={"X-Agent-Role": "reviewer"})
        assert r.status_code == 200
        assert counter_spy
        assert counter_spy[-1][1].get("source") == "fleet"

    def test_get_memory_semantic_with_no_fleet_header_stays_backoffice(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control: no fleet header -> unchanged human front-door
        default. This is what goes RED if the classifier is neutered to
        always return "fleet" (the flip side of the falsifiability proof)."""
        counter_spy = self._spy_counter(monkeypatch)
        r = client.get("/memory/semantic")
        assert r.status_code == 200
        assert counter_spy
        assert counter_spy[-1][1].get("source") == "backoffice"

    def test_get_memory_semantic_read_with_fleet_header_is_fleet(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The single-document read site (:2377, was :2355)."""
        client.post(
            "/memory/semantic",
            json={
                "collection": "decisions",
                "document_id": "fleet-read-doc",
                "text": "fleet read probe",
            },
        )
        counter_spy = self._spy_counter(monkeypatch)
        r = client.get(
            "/memory/semantic/decisions/fleet-read-doc",
            headers={"X-Source": "opencode-deployer"},
        )
        assert r.status_code == 200
        assert counter_spy
        assert counter_spy[-1][1].get("source") == "fleet"

    def test_get_memory_episodic_list_with_fleet_header_is_fleet(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        counter_spy = self._spy_counter(monkeypatch)
        r = client.get("/memory/episodic", headers={"X-Source": "opencode-builder"})
        assert r.status_code == 200
        assert counter_spy
        assert counter_spy[-1][1].get("source") == "fleet"

    def test_get_memory_episodic_read_with_fleet_header_is_fleet(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client.post(
            "/memory/episodic",
            json={"filename": "ADR-fleet-read.md", "content": "fleet read probe"},
        )
        counter_spy = self._spy_counter(monkeypatch)
        r = client.get(
            "/memory/episodic/ADR-fleet-read.md",
            headers={"X-Agent-Role": "builder"},
        )
        assert r.status_code == 200
        assert counter_spy
        assert counter_spy[-1][1].get("source") == "fleet"

    def test_get_memory_procedural_list_with_fleet_header_is_fleet(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        counter_spy = self._spy_counter(monkeypatch)
        r = client.get("/memory/procedural", headers={"X-Source": "opencode-reviewer"})
        assert r.status_code == 200
        assert counter_spy
        assert counter_spy[-1][1].get("source") == "fleet"

    def test_get_memory_procedural_read_with_fleet_header_is_fleet(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client.post(
            "/memory/procedural",
            json={"filename": "SKILL-fleet-read.md", "content": "fleet read probe"},
        )
        counter_spy = self._spy_counter(monkeypatch)
        r = client.get(
            "/memory/procedural/SKILL-fleet-read.md",
            headers={"X-Agent-Role": "curator"},
        )
        assert r.status_code == 200
        assert counter_spy
        assert counter_spy[-1][1].get("source") == "fleet"

    def test_no_pii_in_fleet_labels_either(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fleet flip is a new VALUE of the existing ``source`` label
        only — label KEYS stay exactly {source, collection, hit, cache},
        same invariant as the backoffice path."""
        counter_spy = self._spy_counter(monkeypatch)
        r = client.get("/memory/semantic", headers={"X-Source": "opencode-builder"})
        assert r.status_code == 200
        for _amount, labels in counter_spy:
            assert set(labels.keys()) == {"source", "collection", "hit", "cache"}
        assert counter_spy[-1][1].get("cache") == "n/a"

    def test_chat_tool_recall_source_still_tool_unaffected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The chat-tool surface (memory_handlers.py) never calls
        ``classify_recall_source_from_request`` — it stamps ``source="tool"``
        directly and is untouched by this change. Regression guard: emit
        the helper directly with source="tool" and confirm it passes the
        label through unchanged (the REST classifier is additive, not a
        replacement for the tool-surface literal)."""
        from audittrace.services.recall_telemetry import emit_recall_telemetry

        counter_spy = self._spy_counter(monkeypatch)
        emit_recall_telemetry("tool", "decisions", 3, cache="miss")
        assert counter_spy[-1][1].get("source") == "tool"
