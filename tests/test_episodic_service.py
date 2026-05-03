"""Tests for EpisodicService — Layer 1 of the 4-layer memory architecture (ADR-018).

The service is **always S3-backed** in production (MinIO) — there is no
filesystem implementation. Tests here exercise ``S3EpisodicService`` against a
fake MinIO client and ``MockEpisodicService`` directly. See
``feedback_storage_always_s3``.

Phase 2 (DESIGN §15): every service method takes ``user_context`` as the first
positional argument. The admin-sentinel fixture is defined in ``conftest.py``
and reused here — Episodic is shared content, so the parameter is plumbing.
"""

from __future__ import annotations

from typing import Any

import pytest

from audittrace.services.episodic import (
    EpisodicService,
    MockEpisodicService,
    S3EpisodicService,
)

# ── Fake MinIO client ────────────────────────────────────────────────────────


class _FakeS3Error(Exception):
    """Stand-in for ``minio.error.S3Error`` — only the ``code`` attribute matters."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class _FakeObject:
    def __init__(self, object_name: str) -> None:
        self.object_name = object_name


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.closed = False
        self.released = False

    def read(self) -> bytes:
        return self._content

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class _FakeMinio:
    """Minimal MinIO-client double covering ``list_objects`` + ``get_object``."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        # Keys are full S3 keys including the prefix (e.g. ``episodic/ADR-001.md``)
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
def fake_bucket_objects() -> dict[str, bytes]:
    """Three sample ADR-*.md objects under the ``episodic/`` prefix."""
    return {
        "episodic/ADR-001-use-rocm.md": (
            b"# ADR-001: Use ROCm for GPU Acceleration\n\n"
            b"Date: 2026-03-01\n\n## Status\n\nAccepted\n\n"
            b"## Context\n\nThe workstation uses AMD GPU requiring ROCm.\n"
        ),
        "episodic/ADR-009-kv-cache-compression.md": (
            b"# ADR-009: KV Cache Compression\n\n"
            b"## Decision\n\nUse q4_0 cache compression to reduce to 4 GB.\n"
        ),
        "episodic/ADR-016-bandwidth-optimisation.md": (
            b"# ADR-016: Memory Bus Bandwidth Optimisation\n\n"
            b"## Decision\n\nReduce context to 65k.\n"
        ),
    }


@pytest.fixture
def s3_episodic(fake_bucket_objects: dict[str, bytes]) -> S3EpisodicService:
    return S3EpisodicService(
        minio_client=_FakeMinio(fake_bucket_objects),
        bucket="memory-shared",
        prefix="episodic/",
    )


# ── S3EpisodicService tests ──────────────────────────────────────────────────


class TestS3EpisodicService:
    def test_load_returns_all_adrs(self, s3_episodic: S3EpisodicService, user_context):
        docs = s3_episodic.load(user_context)
        assert len(docs) == 3

    def test_load_extracts_title_from_heading(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        docs = s3_episodic.load(user_context)
        titles = [d.metadata["title"] for d in docs]
        assert "ADR-001: Use ROCm for GPU Acceleration" in titles
        assert "ADR-009: KV Cache Compression" in titles

    def test_load_sets_metadata(self, s3_episodic: S3EpisodicService, user_context):
        docs = s3_episodic.load(user_context)
        for d in docs:
            assert d.metadata["source"] == "episodic"
            assert d.metadata["file"].startswith("ADR-")
            assert d.metadata["file"].endswith(".md")

    def test_load_skips_non_adr_keys(self, user_context):
        """Objects that don't match ADR-*.md must be ignored."""
        client = _FakeMinio(
            {
                "episodic/README.md": b"# Just a readme\n",
                "episodic/ADR-001-x.md": b"# ADR-001\n\nbody\n",
            }
        )
        service = S3EpisodicService(client, bucket="b", prefix="episodic/")
        docs = service.load(user_context)
        files = [d.metadata["file"] for d in docs]
        assert files == ["ADR-001-x.md"]

    def test_load_handles_empty_bucket(self, user_context):
        service = S3EpisodicService(_FakeMinio({}), bucket="b", prefix="episodic/")
        assert service.load(user_context) == []

    def test_load_handles_client_exception(self, user_context):
        """An unexpected client error logs + returns []. No exception bubbles."""

        class _Broken:
            def list_objects(self, *a: Any, **kw: Any) -> list[_FakeObject]:
                raise RuntimeError("connection refused")

        service = S3EpisodicService(_Broken(), bucket="b", prefix="episodic/")
        assert service.load(user_context) == []

    def test_search_filters_by_query(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        results = s3_episodic.search(user_context, "cache compression")
        assert len(results) >= 1
        titles = [d.metadata["title"] for d in results]
        assert any("Cache" in t for t in titles)

    def test_search_no_match_returns_empty(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        assert s3_episodic.search(user_context, "quantum entanglement") == []

    def test_search_no_arbitrary_cap(self, user_context):
        """If 5 ADRs match, all 5 should be returned — no cap."""
        objs = {
            f"episodic/ADR-{i:03d}-server-config-{i}.md": (
                f"# ADR-{i:03d}: Server Config Part {i}\n\n"
                f"## Decision\n\nApply server setting {i}.\n"
            ).encode()
            for i in range(1, 6)
        }
        service = S3EpisodicService(_FakeMinio(objs), bucket="b", prefix="episodic/")
        results = service.search(user_context, "server configuration")
        assert len(results) == 5

    def test_search_short_query_returns_empty(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        """Short keywords (≤3 chars) yield nothing — avoids spam matches."""
        assert s3_episodic.search(user_context, "hi a") == []

    def test_as_context_returns_formatted_string(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        ctx = s3_episodic.as_context(user_context, "cache")
        assert "Architecture Decisions" in ctx
        assert "KV Cache" in ctx

    def test_as_context_empty_when_no_match(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        assert s3_episodic.as_context(user_context, "quantum entanglement") == ""

    def test_load_handles_adr_with_no_h1_header(self, user_context):
        """An ADR file without a `# ` H1 line still loads — title is the stem."""
        client = _FakeMinio(
            {"episodic/ADR-100-no-header.md": b"Just body text, no H1 line.\n"}
        )
        service = S3EpisodicService(client, bucket="b", prefix="episodic/")
        docs = service.load(user_context)
        assert len(docs) == 1
        assert docs[0].metadata["title"] == "ADR-100-no-header"


class TestS3EpisodicServiceRead:
    """``read(file)`` — full-content fetch by exact filename (Phase A.1)."""

    def test_read_existing_file_returns_full_content(
        self,
        s3_episodic: S3EpisodicService,
        fake_bucket_objects: dict[str, bytes],
        user_context,
    ):
        doc = s3_episodic.read(user_context, "ADR-009-kv-cache-compression.md")
        assert doc is not None
        expected = fake_bucket_objects[
            "episodic/ADR-009-kv-cache-compression.md"
        ].decode("utf-8")
        assert doc.page_content == expected
        assert doc.metadata["file"] == "ADR-009-kv-cache-compression.md"
        assert doc.metadata["title"] == "ADR-009: KV Cache Compression"
        assert doc.metadata["source"] == "episodic"

    def test_read_missing_file_returns_none(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        assert s3_episodic.read(user_context, "ADR-999-nope.md") is None

    def test_read_rejects_path_traversal(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        for bad in [
            "../etc/passwd.md",
            "ADR-001/../../secret.md",
            "subdir/ADR-001.md",
            "..\\windows.md",
        ]:
            assert s3_episodic.read(user_context, bad) is None

    def test_read_rejects_non_md(self, s3_episodic: S3EpisodicService, user_context):
        assert s3_episodic.read(user_context, "ADR-001") is None
        assert s3_episodic.read(user_context, "ADR-001.txt") is None

    def test_read_rejects_empty_or_non_string(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        assert s3_episodic.read(user_context, "") is None
        assert s3_episodic.read(user_context, None) is None  # type: ignore[arg-type]

    def test_read_handles_unexpected_exception(self, user_context):
        """Non-NoSuchKey errors log + return None — caller never sees a raise."""

        class _Broken:
            def get_object(self, *a: Any, **kw: Any) -> _FakeResponse:
                raise RuntimeError("connection reset")

        service = S3EpisodicService(_Broken(), bucket="b", prefix="episodic/")
        assert service.read(user_context, "ADR-001.md") is None

    def test_read_returns_full_untruncated_content(self, user_context):
        """Regression for the ADR-025 bug: full content, no 400-char limit."""
        big = ("# ADR-025\n\n" + ("body line.\n" * 5000)).encode()
        client = _FakeMinio({"episodic/ADR-025.md": big})
        service = S3EpisodicService(client, bucket="b", prefix="episodic/")
        doc = service.read(user_context, "ADR-025.md")
        assert doc is not None
        assert len(doc.page_content) == len(big.decode())
        assert len(doc.page_content) > 5000  # well over the old 400-char cap


# ── MockEpisodicService tests ────────────────────────────────────────────────


class TestMockEpisodicService:
    def test_mock_starts_empty(self, user_context):
        service = MockEpisodicService()
        assert service.load(user_context) == []
        assert service.search(user_context, "anything") == []

    def test_mock_add_and_load(self, user_context):
        service = MockEpisodicService()
        service.add_document(
            "ADR content about cache", title="ADR-009", file="ADR-009.md"
        )
        docs = service.load(user_context)
        assert len(docs) == 1
        assert docs[0].metadata["title"] == "ADR-009"

    def test_mock_search_filters(self, user_context):
        service = MockEpisodicService()
        service.add_document("KV cache compression", title="ADR-009", file="ADR-009.md")
        service.add_document("ROCm GPU setup", title="ADR-001", file="ADR-001.md")
        results = service.search(user_context, "cache")
        assert len(results) == 1
        assert results[0].metadata["title"] == "ADR-009"

    def test_mock_reset(self, user_context):
        service = MockEpisodicService()
        service.add_document("test", title="T", file="T.md")
        service.reset()
        assert service.load(user_context) == []

    def test_abstract_interface(self):
        """Verify MockEpisodicService is a valid EpisodicService."""
        service = MockEpisodicService()
        assert isinstance(service, EpisodicService)

    def test_mock_as_context_renders_matched(self, user_context):
        """as_context with results renders the section header + content slice."""
        service = MockEpisodicService()
        service.add_document(
            "Detailed body about KV cache compression",
            title="ADR-009",
            file="ADR-009.md",
        )
        out = service.as_context(user_context, "compression")
        assert "## Architecture Decisions" in out
        assert "ADR-009" in out
        assert "compression" in out

    def test_mock_search_short_query_returns_empty(self, user_context):
        """Queries with no keywords > 3 chars must return [] (no spam matches)."""
        service = MockEpisodicService()
        service.add_document("anything", title="T", file="T.md")
        assert service.search(user_context, "hi a") == []

    def test_mock_as_context_no_match_returns_empty_string(self, user_context):
        """as_context returns "" when search yields nothing."""
        service = MockEpisodicService()
        service.add_document("body", title="T", file="T.md")
        assert service.as_context(user_context, "nothing-matches-here") == ""

    def test_mock_read_returns_matching_document(self, user_context):
        service = MockEpisodicService()
        service.add_document("contents", title="ADR-007", file="ADR-007.md")
        doc = service.read(user_context, "ADR-007.md")
        assert doc is not None
        assert doc.page_content == "contents"

    def test_mock_read_returns_none_when_missing(self, user_context):
        service = MockEpisodicService()
        service.add_document("contents", title="ADR-007", file="ADR-007.md")
        assert service.read(user_context, "ADR-999.md") is None

    def test_mock_read_rejects_path_traversal(self, user_context):
        service = MockEpisodicService()
        service.add_document("contents", title="ADR-007", file="ADR-007.md")
        assert service.read(user_context, "../etc/passwd.md") is None
