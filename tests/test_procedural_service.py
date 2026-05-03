"""Tests for ProceduralService — Layer 2 of the 4-layer memory architecture (ADR-018).

The service is **always S3-backed** in production (MinIO) — there is no
filesystem implementation. Tests here exercise ``S3ProceduralService`` against
a fake MinIO client and ``MockProceduralService`` directly. See
``feedback_storage_always_s3``.
"""

from __future__ import annotations

from typing import Any

import pytest

from audittrace.services.procedural import (
    MockProceduralService,
    ProceduralService,
    S3ProceduralService,
)

# ── Fake MinIO client ────────────────────────────────────────────────────────


class _FakeS3Error(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class _FakeObject:
    def __init__(self, object_name: str) -> None:
        self.object_name = object_name


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def read(self) -> bytes:
        return self._content

    def close(self) -> None:
        return None

    def release_conn(self) -> None:
        return None


class _FakeMinio:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = dict(objects)

    def list_objects(
        self, bucket: str, prefix: str = "", **kwargs: Any
    ) -> list[_FakeObject]:
        del bucket, kwargs
        return [_FakeObject(k) for k in self._objects if k.startswith(prefix)]

    def get_object(self, bucket: str, key: str) -> _FakeResponse:
        del bucket
        if key not in self._objects:
            raise _FakeS3Error("NoSuchKey", f"Object does not exist: {key}")
        return _FakeResponse(self._objects[key])


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_skill_objects() -> dict[str, bytes]:
    """Three sample SKILL-*.md objects under the ``procedural/`` prefix."""
    return {
        "procedural/SKILL-IAM.md": (
            b"# IAM Skill\n\nOAuth2, OIDC, JWT validation, BFF pattern.\n"
        ),
        "procedural/SKILL-ARCHITECTURE.md": (
            b"# Architecture Skill\n\nC4 model, Structurizr DSL, EIP patterns.\n"
        ),
        "procedural/SKILL-memory-commands.md": (
            b"# Memory Commands\n\nCLI commands for memory indexing and query.\n"
        ),
    }


@pytest.fixture
def s3_procedural(fake_skill_objects: dict[str, bytes]) -> S3ProceduralService:
    return S3ProceduralService(
        minio_client=_FakeMinio(fake_skill_objects),
        bucket="memory-shared",
        prefix="procedural/",
    )


# ── S3ProceduralService tests ────────────────────────────────────────────────


class TestS3ProceduralService:
    def test_load_returns_all_skills(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        docs = s3_procedural.load(user_context)
        assert len(docs) == 3

    def test_load_extracts_skill_name(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        docs = s3_procedural.load(user_context)
        skills = [d.metadata["skill"] for d in docs]
        assert "IAM" in skills
        assert "ARCHITECTURE" in skills
        assert "memory-commands" in skills

    def test_load_sets_metadata(self, s3_procedural: S3ProceduralService, user_context):
        docs = s3_procedural.load(user_context)
        for d in docs:
            assert d.metadata["source"] == "procedural"
            assert d.metadata["file"].startswith("SKILL-")

    def test_load_skips_non_skill_keys(self, user_context):
        client = _FakeMinio(
            {
                "procedural/README.md": b"# readme\n",
                "procedural/SKILL-X.md": b"# X\n\nbody\n",
            }
        )
        service = S3ProceduralService(client, bucket="b", prefix="procedural/")
        files = [d.metadata["file"] for d in service.load(user_context)]
        assert files == ["SKILL-X.md"]

    def test_load_handles_empty_bucket(self, user_context):
        service = S3ProceduralService(_FakeMinio({}), bucket="b", prefix="procedural/")
        assert service.load(user_context) == []

    def test_load_handles_client_exception(self, user_context):
        class _Broken:
            def list_objects(self, *a: Any, **kw: Any) -> list[_FakeObject]:
                raise RuntimeError("connection refused")

        service = S3ProceduralService(_Broken(), bucket="b", prefix="procedural/")
        assert service.load(user_context) == []

    def test_search_filters_by_query(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        results = s3_procedural.search(user_context, "OAuth2 validation")
        assert len(results) >= 1
        assert any("IAM" in d.metadata["skill"] for d in results)

    def test_search_matches_skill_name(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        results = s3_procedural.search(user_context, "architecture patterns")
        assert any("ARCHITECTURE" in d.metadata["skill"] for d in results)

    def test_search_no_match_returns_empty(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        assert s3_procedural.search(user_context, "quantum physics") == []

    def test_search_no_arbitrary_cap(self, user_context):
        """If 4 skills match, all 4 should be returned."""
        objs = {
            f"procedural/SKILL-CLOUD-{n}.md": (
                f"# CLOUD-{n} Skill\n\nCloud architecture and cloud migration.\n"
            ).encode()
            for n in ("STRATEGY", "APP-PATTERNS", "SECURITY", "MIGRATION")
        }
        service = S3ProceduralService(
            _FakeMinio(objs), bucket="b", prefix="procedural/"
        )
        results = service.search(user_context, "cloud migration patterns")
        assert len(results) == 4

    def test_search_short_query_returns_empty(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        assert s3_procedural.search(user_context, "hi a") == []

    def test_search_matches_content_beyond_first_200_chars(self, user_context):
        """Regression: keywords deep in the file must still match."""
        filler = "lorem ipsum " * 25
        client = _FakeMinio(
            {
                "procedural/SKILL-IAM.md": (
                    f"# IAM Skill\n\n{filler}\n\nDeep content with quantum keyword.\n"
                ).encode()
            }
        )
        service = S3ProceduralService(client, bucket="b", prefix="procedural/")
        results = service.search(user_context, "quantum")
        assert len(results) == 1
        assert results[0].metadata["skill"] == "IAM"

    def test_as_context_returns_formatted_string(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        ctx = s3_procedural.as_context(user_context, "memory commands")
        assert "Relevant Skills" in ctx
        assert "memory-commands" in ctx

    def test_as_context_empty_when_no_match(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        assert s3_procedural.as_context(user_context, "quantum") == ""


class TestS3ProceduralServiceRead:
    """``read(file)`` — full-content fetch by exact filename (Phase A.1)."""

    def test_read_existing_file_returns_full_content(
        self,
        s3_procedural: S3ProceduralService,
        fake_skill_objects: dict[str, bytes],
        user_context,
    ):
        doc = s3_procedural.read(user_context, "SKILL-IAM.md")
        assert doc is not None
        expected = fake_skill_objects["procedural/SKILL-IAM.md"].decode("utf-8")
        assert doc.page_content == expected
        assert doc.metadata["file"] == "SKILL-IAM.md"
        assert doc.metadata["skill"] == "IAM"
        assert doc.metadata["source"] == "procedural"

    def test_read_missing_file_returns_none(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        assert s3_procedural.read(user_context, "SKILL-NOPE.md") is None

    def test_read_rejects_path_traversal(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        for bad in ["../passwd.md", "subdir/SKILL-IAM.md", "..\\win.md"]:
            assert s3_procedural.read(user_context, bad) is None

    def test_read_rejects_non_md(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        assert s3_procedural.read(user_context, "SKILL-IAM") is None
        assert s3_procedural.read(user_context, "SKILL-IAM.txt") is None

    def test_read_handles_unexpected_exception(self, user_context):
        class _Broken:
            def get_object(self, *a: Any, **kw: Any) -> _FakeResponse:
                raise RuntimeError("connection reset")

        service = S3ProceduralService(_Broken(), bucket="b", prefix="procedural/")
        assert service.read(user_context, "SKILL-X.md") is None

    def test_read_returns_full_untruncated_content(self, user_context):
        big = ("# IAM Skill\n\n" + ("body line.\n" * 5000)).encode()
        client = _FakeMinio({"procedural/SKILL-IAM.md": big})
        service = S3ProceduralService(client, bucket="b", prefix="procedural/")
        doc = service.read(user_context, "SKILL-IAM.md")
        assert doc is not None
        assert len(doc.page_content) == len(big.decode())
        assert len(doc.page_content) > 5000


# ── MockProceduralService tests ──────────────────────────────────────────────


class TestMockProceduralService:
    def test_mock_starts_empty(self, user_context):
        service = MockProceduralService()
        assert service.load(user_context) == []

    def test_mock_add_and_load(self, user_context):
        service = MockProceduralService()
        service.add_document("OAuth2 patterns", skill="IAM", file="SKILL-IAM.md")
        docs = service.load(user_context)
        assert len(docs) == 1
        assert docs[0].metadata["skill"] == "IAM"

    def test_mock_search_filters(self, user_context):
        service = MockProceduralService()
        service.add_document("OAuth2 JWT", skill="IAM", file="SKILL-IAM.md")
        service.add_document("C4 model", skill="ARCHITECTURE", file="SKILL-ARCH.md")
        results = service.search(user_context, "OAuth2")
        assert len(results) == 1

    def test_mock_reset(self, user_context):
        service = MockProceduralService()
        service.add_document("test", skill="T", file="T.md")
        service.reset()
        assert service.load(user_context) == []

    def test_abstract_interface(self):
        assert isinstance(MockProceduralService(), ProceduralService)

    def test_mock_search_short_query_returns_empty(self, user_context):
        service = MockProceduralService()
        service.add_document("body", skill="X", file="SKILL-X.md")
        assert service.search(user_context, "hi a") == []

    def test_mock_as_context_renders_matched(self, user_context):
        service = MockProceduralService()
        service.add_document(
            "OAuth2 implementation patterns", skill="IAM", file="SKILL-IAM.md"
        )
        out = service.as_context(user_context, "OAuth2")
        assert "## Relevant Skills" in out
        assert "IAM" in out
        assert "SKILL-IAM.md" in out

    def test_mock_as_context_no_match_returns_empty_string(self, user_context):
        service = MockProceduralService()
        service.add_document("body", skill="X", file="SKILL-X.md")
        assert service.as_context(user_context, "nothing-matches") == ""

    def test_mock_read_returns_matching_document(self, user_context):
        service = MockProceduralService()
        service.add_document("body", skill="IAM", file="SKILL-IAM.md")
        doc = service.read(user_context, "SKILL-IAM.md")
        assert doc is not None
        assert doc.page_content == "body"

    def test_mock_read_returns_none_when_missing(self, user_context):
        service = MockProceduralService()
        service.add_document("body", skill="IAM", file="SKILL-IAM.md")
        assert service.read(user_context, "SKILL-NOPE.md") is None

    def test_mock_read_rejects_path_traversal(self, user_context):
        service = MockProceduralService()
        service.add_document("body", skill="IAM", file="SKILL-IAM.md")
        assert service.read(user_context, "../passwd.md") is None
