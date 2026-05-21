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
    """Create a mock response from get_object.

    Supports the context-manager protocol because the production code
    now uses ``with client.get_object(...) as response:`` for
    deterministic resource cleanup (per the PYTHON-ENGINEERING skill
    §1 and feedback_use_context_managers).
    """
    resp = MagicMock()
    resp.read.return_value = content.encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = None
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


class TestCreateObjectStorageProvider:
    """Tests for _create_object_storage_provider in dependencies.py.

    ADR-006 renamed _create_minio_client to _create_object_storage_provider.
    The old name remains as an alias for one release.
    """

    def _base_settings(self, backend: str = "minio") -> MagicMock:
        settings = MagicMock()
        settings.object_storage_backend = backend
        settings.minio_url = "http://minio:9000"
        settings.minio_access_key = "minioadmin"
        settings.minio_secret_key = "secret123"
        # AWS branch defaults — irrelevant unless backend == "aws"
        settings.aws_region = ""
        settings.aws_bucket = ""
        settings.aws_endpoint_url = ""
        settings.aws_use_irsa = True
        settings.aws_access_key_id = ""
        settings.aws_secret_access_key = ""
        return settings

    def test_minio_raises_when_secret_key_empty(self):
        """MinIO backend requires a secret key — startup-time RuntimeError,
        not silent fallback. See ``feedback_storage_always_s3``."""
        from audittrace.dependencies import _create_object_storage_provider

        settings = self._base_settings(backend="minio")
        settings.minio_secret_key = ""
        with pytest.raises(RuntimeError, match="AUDITTRACE_MINIO_SECRET_KEY"):
            _create_object_storage_provider(settings)

    def test_minio_returns_wrapped_provider_when_configured(self):
        """Happy-path MinIO: returns a QuarantineDenyingObjectStorageClient
        wrapping a MinIOObjectStorageProvider."""
        from audittrace.dependencies import _create_object_storage_provider
        from audittrace.services.quarantine_denying_provider import (
            QuarantineDenyingObjectStorageClient,
        )

        settings = self._base_settings(backend="minio")
        with patch("audittrace_object_storage.minio_backend.Minio"):
            wrapped = _create_object_storage_provider(settings)
        assert isinstance(wrapped, QuarantineDenyingObjectStorageClient)

    def test_minio_init_error_wrapped_as_runtime_error(self):
        """A Minio-construction failure surfaces as RuntimeError with the
        canonical error message."""
        from audittrace.dependencies import _create_object_storage_provider

        settings = self._base_settings(backend="minio")
        with patch(
            "audittrace_object_storage.minio_backend.Minio",
            side_effect=Exception("minio not installed"),
        ):
            with pytest.raises(
                RuntimeError,
                match="object-storage provider initialisation failed",
            ):
                _create_object_storage_provider(settings)

    def test_aws_raises_when_region_missing(self):
        from audittrace.dependencies import _create_object_storage_provider

        settings = self._base_settings(backend="aws")
        settings.aws_region = ""  # missing
        settings.aws_bucket = "test-bucket"
        with pytest.raises(RuntimeError, match="AUDITTRACE_AWS_REGION"):
            _create_object_storage_provider(settings)

    def test_aws_raises_when_bucket_missing(self):
        from audittrace.dependencies import _create_object_storage_provider

        settings = self._base_settings(backend="aws")
        settings.aws_region = "eu-central-2"
        settings.aws_bucket = ""  # missing
        with pytest.raises(RuntimeError, match="AUDITTRACE_AWS_REGION"):
            _create_object_storage_provider(settings)

    def test_unknown_backend_raises(self):
        from audittrace.dependencies import _create_object_storage_provider

        settings = self._base_settings(backend="swift")
        with pytest.raises(
            RuntimeError, match="unknown AUDITTRACE_OBJECT_STORAGE_BACKEND"
        ):
            _create_object_storage_provider(settings)

    def test_legacy_alias_still_works(self):
        """Backwards-compat: _create_minio_client is an alias."""
        from audittrace.dependencies import (
            _create_minio_client,
            _create_object_storage_provider,
        )

        assert _create_minio_client is _create_object_storage_provider
