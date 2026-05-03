"""Tests for S3-backed memory services and MinIO client creation (ADR-027).

Mock the minio.Minio client to test S3EpisodicService, S3ProceduralService,
and _create_minio_client without a real MinIO server. Validates load, search,
as_context, caching, and the shared-content AuthZ model.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from audittrace.identity import UserContext
from audittrace.services.episodic import S3EpisodicService
from audittrace.services.procedural import S3ProceduralService


def _make_user(user_id: str = "kc-test-001", is_admin: bool = False) -> UserContext:
    return UserContext(
        user_id=user_id,
        username="test",
        agent_type="opencode",
        scopes=("memory:admin",) if is_admin else ("memory:read",),
        is_admin=is_admin,
    )


def _mock_s3_object(name: str) -> MagicMock:
    """Create a mock S3 object from list_objects."""
    obj = MagicMock()
    obj.object_name = name
    return obj


def _mock_get_response(content: str) -> MagicMock:
    """Create a mock response from get_object."""
    resp = MagicMock()
    resp.read.return_value = content.encode("utf-8")
    resp.close = MagicMock()
    resp.release_conn = MagicMock()
    return resp


# ── S3EpisodicService ─────────────────────────────────────────────────────────


class TestS3EpisodicService:
    def _make_service(self, objects: dict[str, str]) -> S3EpisodicService:
        """Create service with mocked MinIO client."""
        client = MagicMock()
        client.list_objects.return_value = [_mock_s3_object(name) for name in objects]
        client.get_object.side_effect = lambda bucket, name: _mock_get_response(
            objects[name]
        )
        return S3EpisodicService(minio_client=client, bucket="memory-shared")

    def test_load_returns_adr_documents(self):
        svc = self._make_service(
            {
                "episodic/ADR-018-four-layer-memory-port.md": "# Four-Layer Memory\n\nContent here.",
                "episodic/ADR-025-memory-as-tools.md": "# Memory as Tools\n\nTools content.",
            }
        )
        user = _make_user()
        docs = svc.load(user)
        assert len(docs) == 2
        assert docs[0].metadata["file"] == "ADR-018-four-layer-memory-port.md"
        assert docs[0].metadata["title"] == "Four-Layer Memory"

    def test_load_ignores_non_adr_files(self):
        svc = self._make_service(
            {
                "episodic/ADR-018.md": "# ADR\n\nContent.",
                "episodic/agent-configuration.md": "Not an ADR.",
                "episodic/README.md": "Not an ADR.",
            }
        )
        docs = svc.load(_make_user())
        assert len(docs) == 1
        assert docs[0].metadata["file"] == "ADR-018.md"

    def test_search_filters_by_keyword(self):
        svc = self._make_service(
            {
                "episodic/ADR-009.md": "# KV Cache\n\nKV cache compression reduces memory by 75%.",
                "episodic/ADR-018.md": "# Memory Architecture\n\nFour-layer memory port.",
            }
        )
        user = _make_user()
        results = svc.search(user, "cache compression")
        assert len(results) == 1
        assert "KV cache" in results[0].page_content

    def test_as_context_formats_results(self):
        svc = self._make_service(
            {
                "episodic/ADR-009.md": "# KV Cache\n\nContent about caching.",
            }
        )
        ctx = svc.as_context(_make_user(), "cache")
        assert "## Architecture Decisions" in ctx
        assert "KV Cache" in ctx

    def test_load_caches_on_first_call(self):
        svc = self._make_service(
            {
                "episodic/ADR-018.md": "# ADR\n\nContent.",
            }
        )
        user = _make_user()
        docs1 = svc.load(user)
        docs2 = svc.load(user)
        assert docs1 == docs2
        # list_objects should only be called once (cached)
        assert svc._client.list_objects.call_count == 1

    def test_shared_content_no_user_prefix(self):
        """Shared bucket: list_objects uses 'episodic/' prefix, not user_id."""
        svc = self._make_service({})
        svc.load(_make_user(user_id="kc-john-001"))
        call_args = svc._client.list_objects.call_args
        assert call_args[0] == ("memory-shared",)
        # Should NOT contain user_id — shared content has no per-user prefix
        assert "kc-john" not in str(call_args)


# ── S3ProceduralService ───────────────────────────────────────────────────────


class TestS3ProceduralService:
    def _make_service(self, objects: dict[str, str]) -> S3ProceduralService:
        client = MagicMock()
        client.list_objects.return_value = [_mock_s3_object(name) for name in objects]
        client.get_object.side_effect = lambda bucket, name: _mock_get_response(
            objects[name]
        )
        return S3ProceduralService(minio_client=client, bucket="memory-shared")

    def test_load_returns_skill_documents(self):
        svc = self._make_service(
            {
                "procedural/SKILL-ARCHITECTURE.md": "Architecture skill content.",
                "procedural/SKILL-GENAI.md": "GenAI skill content about agents and RAG.",
            }
        )
        docs = svc.load(_make_user())
        assert len(docs) == 2
        assert docs[0].metadata["skill"] == "ARCHITECTURE"
        assert docs[1].metadata["skill"] == "GENAI"

    def test_load_ignores_non_skill_files(self):
        svc = self._make_service(
            {
                "procedural/SKILL-IAM.md": "IAM content.",
                "procedural/README.md": "Not a skill.",
            }
        )
        docs = svc.load(_make_user())
        assert len(docs) == 1

    def test_search_matches_skill_name_and_content(self):
        svc = self._make_service(
            {
                "procedural/SKILL-IAM.md": "OAuth2 OIDC JWT validation patterns.",
                "procedural/SKILL-GENAI.md": "Agent design and RAG patterns.",
            }
        )
        results = svc.search(_make_user(), "OAuth2 validation")
        assert len(results) == 1
        assert results[0].metadata["skill"] == "IAM"

    def test_as_context_formats_skill_list(self):
        svc = self._make_service(
            {
                "procedural/SKILL-ARCHITECTURE.md": "C4 model patterns.",
            }
        )
        ctx = svc.as_context(_make_user(), "architecture patterns")
        assert "## Relevant Skills" in ctx
        assert "ARCHITECTURE" in ctx

    def test_load_caches_results(self):
        svc = self._make_service(
            {
                "procedural/SKILL-IAM.md": "IAM content.",
            }
        )
        user = _make_user()
        svc.load(user)
        svc.load(user)
        assert svc._client.list_objects.call_count == 1


# ── _create_minio_client (dependencies.py) ────────────────────────────────────


class TestCreateMinioClient:
    """Tests for _create_minio_client in dependencies.py."""

    def test_raises_when_secret_key_empty(self):
        """Since the 2026-05-03 sweep, MinIO is mandatory — missing secret
        key surfaces as a startup-time RuntimeError, not a silent FS
        fallback. See ``feedback_storage_always_s3``."""
        from audittrace.dependencies import _create_minio_client

        settings = MagicMock()
        settings.minio_secret_key = ""
        with pytest.raises(RuntimeError, match="MinIO is required"):
            _create_minio_client(settings)

    def test_returns_client_when_configured(self):
        from audittrace.dependencies import _create_minio_client

        settings = MagicMock()
        settings.minio_url = "http://minio:9000"
        settings.minio_access_key = "minioadmin"
        settings.minio_secret_key = "secret123"

        with patch("audittrace.dependencies.Minio") as mock_minio:
            mock_minio.return_value = MagicMock()
            client = _create_minio_client(settings)
            assert client is not None
            mock_minio.assert_called_once_with(
                "minio:9000",
                access_key="minioadmin",
                secret_key="secret123",
                secure=False,
            )

    def test_raises_on_client_init_error(self):
        """Construction failure is surfaced as RuntimeError, not silently
        swallowed — same rule as the missing-secret-key branch."""
        from audittrace.dependencies import _create_minio_client

        settings = MagicMock()
        settings.minio_url = "http://minio:9000"
        settings.minio_access_key = "minioadmin"
        settings.minio_secret_key = "secret123"

        with patch(
            "audittrace.dependencies.Minio",
            side_effect=Exception("minio not installed"),
        ):
            with pytest.raises(
                RuntimeError, match="MinIO client initialisation failed"
            ):
                _create_minio_client(settings)

    def test_https_url_sets_secure_true(self):
        from audittrace.dependencies import _create_minio_client

        settings = MagicMock()
        settings.minio_url = "https://minio.prod:9000"
        settings.minio_access_key = "admin"
        settings.minio_secret_key = "secret"

        with patch("audittrace.dependencies.Minio") as mock_minio:
            mock_minio.return_value = MagicMock()
            _create_minio_client(settings)
            mock_minio.assert_called_once_with(
                "minio.prod:9000",
                access_key="admin",
                secret_key="secret",
                secure=True,
            )
