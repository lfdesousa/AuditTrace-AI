"""Tests for EpisodicService — Layer 1 of the 4-layer memory architecture (ADR-018).

Phase 2 (DESIGN §15): every service method takes ``user_context`` as the
first positional argument. The admin-sentinel fixture is defined in
``conftest.py`` and reused here — Episodic is filesystem-backed so the
parameter is pure plumbing.
"""

from pathlib import Path

import pytest

from sovereign_memory.services.episodic import (
    EpisodicService,
    FileEpisodicService,
    MockEpisodicService,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def adr_dir(tmp_path: Path) -> Path:
    """Create a temp directory with sample ADR markdown files."""
    d = tmp_path / "episodic"
    d.mkdir()

    (d / "ADR-001-use-rocm.md").write_text(
        "# ADR-001: Use ROCm for GPU Acceleration\n\n"
        "Date: 2026-03-01\n\n## Status\n\nAccepted\n\n"
        "## Context\n\nThe workstation uses AMD GPU requiring ROCm.\n\n"
        "## Decision\n\nWe use ROCm 7.2 with gfx1151 override.\n\n"
        "## Consequences\n\nGPU acceleration enabled.\n"
    )
    (d / "ADR-009-kv-cache-compression.md").write_text(
        "# ADR-009: KV Cache Compression\n\n"
        "Date: 2026-03-31\n\n## Status\n\nAccepted\n\n"
        "## Context\n\nKV cache consumes 16 GB with FP16.\n\n"
        "## Decision\n\nUse q4_0 cache compression to reduce to 4 GB.\n\n"
        "## Consequences\n\n75% memory reduction, 21% faster generation.\n"
    )
    (d / "ADR-016-bandwidth-optimisation.md").write_text(
        "# ADR-016: Memory Bus Bandwidth Optimisation\n\n"
        "Date: 2026-04-10\n\n## Status\n\nAccepted\n\n"
        "## Context\n\nThe 256-bit memory bus is saturated.\n\n"
        "## Decision\n\nReduce context to 65k, move embeddings to CPU.\n\n"
        "## Consequences\n\nGPU bus exclusive to Qwen.\n"
    )
    return d


@pytest.fixture
def adr_dir_five(tmp_path: Path) -> Path:
    """5 ADRs that all contain 'server' — no arbitrary cap test."""
    d = tmp_path / "episodic"
    d.mkdir()
    for i in range(1, 6):
        (d / f"ADR-{i:03d}-server-config-{i}.md").write_text(
            f"# ADR-{i:03d}: Server Configuration Part {i}\n\n"
            f"## Context\n\nThe server needs configuration change {i}.\n\n"
            f"## Decision\n\nApply server setting {i}.\n"
        )
    return d


# ── FileEpisodicService tests ────────────────────────────────────────────────


class TestFileEpisodicService:
    def test_load_returns_all_adrs(self, adr_dir: Path, user_context):
        service = FileEpisodicService(adr_dir=adr_dir)
        docs = service.load(user_context)
        assert len(docs) == 3

    def test_load_extracts_title_from_heading(self, adr_dir: Path, user_context):
        service = FileEpisodicService(adr_dir=adr_dir)
        docs = service.load(user_context)
        titles = [d.metadata["title"] for d in docs]
        assert "ADR-001: Use ROCm for GPU Acceleration" in titles
        assert "ADR-009: KV Cache Compression" in titles

    def test_load_sets_metadata(self, adr_dir: Path, user_context):
        service = FileEpisodicService(adr_dir=adr_dir)
        docs = service.load(user_context)
        for d in docs:
            assert d.metadata["source"] == "episodic"
            assert "file" in d.metadata
            assert d.metadata["file"].startswith("ADR-")

    def test_load_empty_directory(self, tmp_path: Path, user_context):
        empty = tmp_path / "empty"
        empty.mkdir()
        service = FileEpisodicService(adr_dir=empty)
        docs = service.load(user_context)
        assert docs == []

    def test_load_missing_directory(self, tmp_path: Path, user_context):
        nonexistent = tmp_path / "does_not_exist"
        service = FileEpisodicService(adr_dir=nonexistent)
        docs = service.load(user_context)
        assert docs == []

    def test_search_filters_by_query(self, adr_dir: Path, user_context):
        service = FileEpisodicService(adr_dir=adr_dir)
        results = service.search(user_context, "cache compression")
        # Should match ADR-009 (contains "cache" and "compression")
        assert len(results) >= 1
        titles = [d.metadata["title"] for d in results]
        assert any("Cache" in t for t in titles)

    def test_search_no_match_returns_empty(self, adr_dir: Path, user_context):
        service = FileEpisodicService(adr_dir=adr_dir)
        results = service.search(user_context, "quantum entanglement")
        assert results == []

    def test_search_no_arbitrary_cap(self, adr_dir_five: Path, user_context):
        """If 5 ADRs match, all 5 should be returned — no cap."""
        service = FileEpisodicService(adr_dir=adr_dir_five)
        results = service.search(user_context, "server configuration")
        assert len(results) == 5

    def test_as_context_returns_formatted_string(self, adr_dir: Path, user_context):
        service = FileEpisodicService(adr_dir=adr_dir)
        ctx = service.as_context(user_context, "cache")
        assert "Architecture Decisions" in ctx
        assert "KV Cache" in ctx

    def test_as_context_empty_when_no_match(self, adr_dir: Path, user_context):
        service = FileEpisodicService(adr_dir=adr_dir)
        ctx = service.as_context(user_context, "quantum entanglement")
        assert ctx == ""


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


class TestFileEpisodicServiceEdgeCases:
    """Branches in FileEpisodicService.load + search beyond the happy path."""

    def test_load_handles_adr_with_no_h1_header(self, tmp_path, user_context):
        """An ADR file without an `# ` H1 line should still load — title falls
        back to the file stem (covers the loop-completes-without-break branch)."""
        d = tmp_path / "episodic"
        d.mkdir()
        (d / "ADR-100-no-header.md").write_text(
            "Just body text, no H1 line at all.\n\nMore body.\n"
        )
        service = FileEpisodicService(adr_dir=str(d))
        docs = service.load(user_context)
        assert len(docs) == 1
        # Title falls back to the file stem when no `# ` heading is found
        assert docs[0].metadata["title"] == "ADR-100-no-header"

    def test_search_short_query_returns_empty(self, tmp_path, user_context):
        """File-backed service must also reject short-keyword queries."""
        d = tmp_path / "episodic"
        d.mkdir()
        (d / "ADR-001-x.md").write_text("# ADR-001: X\n\nbody\n")
        service = FileEpisodicService(adr_dir=str(d))
        assert service.search(user_context, "hi a") == []
