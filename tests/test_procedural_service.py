"""Tests for ProceduralService — Layer 2 of the 4-layer memory architecture (ADR-018).

Phase 2 (DESIGN §15): every service method takes ``user_context`` as first
arg. The admin-sentinel fixture comes from ``conftest.py``.
"""

from pathlib import Path

import pytest

from audittrace.services.procedural import (
    FileProceduralService,
    MockProceduralService,
    ProceduralService,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def skill_dir(tmp_path: Path) -> Path:
    """Create a temp directory with sample SKILL files."""
    d = tmp_path / "procedural"
    d.mkdir()

    (d / "SKILL-IAM.md").write_text(
        "# IAM Skill\n\nOAuth2, OIDC, JWT validation, BFF pattern.\n"
    )
    (d / "SKILL-ARCHITECTURE.md").write_text(
        "# Architecture Skill\n\nC4 model, Structurizr DSL, EIP patterns.\n"
    )
    (d / "SKILL-memory-commands.md").write_text(
        "# Memory Commands\n\nCLI commands for memory indexing and query.\n"
    )
    return d


@pytest.fixture
def skill_dir_four(tmp_path: Path) -> Path:
    """4 SKILL files matching 'cloud' — no arbitrary cap test."""
    d = tmp_path / "procedural"
    d.mkdir()
    for name in [
        "CLOUD-STRATEGY",
        "CLOUD-APP-PATTERNS",
        "CLOUD-SECURITY",
        "CLOUD-MIGRATION",
    ]:
        (d / f"SKILL-{name}.md").write_text(
            f"# {name} Skill\n\nCloud architecture patterns and cloud migration.\n"
        )
    return d


# ── FileProceduralService tests ──────────────────────────────────────────────


class TestFileProceduralService:
    def test_load_returns_all_skills(self, skill_dir: Path, user_context):
        service = FileProceduralService(skill_dir=skill_dir)
        docs = service.load(user_context)
        assert len(docs) == 3

    def test_load_extracts_skill_name(self, skill_dir: Path, user_context):
        service = FileProceduralService(skill_dir=skill_dir)
        docs = service.load(user_context)
        skills = [d.metadata["skill"] for d in docs]
        assert "IAM" in skills
        assert "ARCHITECTURE" in skills
        assert "memory-commands" in skills

    def test_load_sets_metadata(self, skill_dir: Path, user_context):
        service = FileProceduralService(skill_dir=skill_dir)
        docs = service.load(user_context)
        for d in docs:
            assert d.metadata["source"] == "procedural"
            assert "file" in d.metadata
            assert d.metadata["file"].startswith("SKILL-")

    def test_load_empty_directory(self, tmp_path: Path, user_context):
        empty = tmp_path / "empty"
        empty.mkdir()
        service = FileProceduralService(skill_dir=empty)
        assert service.load(user_context) == []

    def test_load_missing_directory(self, tmp_path: Path, user_context):
        service = FileProceduralService(skill_dir=tmp_path / "nope")
        assert service.load(user_context) == []

    def test_search_filters_by_query(self, skill_dir: Path, user_context):
        service = FileProceduralService(skill_dir=skill_dir)
        results = service.search(user_context, "OAuth2 validation")
        assert len(results) >= 1
        assert any("IAM" in d.metadata["skill"] for d in results)

    def test_search_matches_skill_name(self, skill_dir: Path, user_context):
        service = FileProceduralService(skill_dir=skill_dir)
        results = service.search(user_context, "architecture patterns")
        assert any("ARCHITECTURE" in d.metadata["skill"] for d in results)

    def test_search_no_match_returns_empty(self, skill_dir: Path, user_context):
        service = FileProceduralService(skill_dir=skill_dir)
        assert service.search(user_context, "quantum physics") == []

    def test_search_no_arbitrary_cap(self, skill_dir_four: Path, user_context):
        """If 4 skills match, all 4 should be returned."""
        service = FileProceduralService(skill_dir=skill_dir_four)
        results = service.search(user_context, "cloud migration patterns")
        assert len(results) == 4

    def test_search_matches_content_beyond_first_200_chars(
        self, tmp_path: Path, user_context
    ):
        """Regression: keywords deep in the file must still match."""
        d = tmp_path / "procedural"
        d.mkdir()
        # 250 chars of filler then the keyword we need to find
        filler = "lorem ipsum " * 25  # ~300 chars
        (d / "SKILL-IAM.md").write_text(
            f"# IAM Skill\n\n{filler}\n\nDeep content with quantum keyword.\n"
        )
        service = FileProceduralService(skill_dir=d)
        results = service.search(user_context, "quantum")
        assert len(results) == 1
        assert results[0].metadata["skill"] == "IAM"

    def test_as_context_returns_formatted_string(self, skill_dir: Path, user_context):
        service = FileProceduralService(skill_dir=skill_dir)
        ctx = service.as_context(user_context, "memory commands")
        assert "Relevant Skills" in ctx
        assert "memory-commands" in ctx

    def test_as_context_empty_when_no_match(self, skill_dir: Path, user_context):
        service = FileProceduralService(skill_dir=skill_dir)
        assert service.as_context(user_context, "quantum") == ""


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
        """Queries with no keywords > 3 chars must return [] (line 71-72 branch)."""
        service = MockProceduralService()
        service.add_document("body", skill="X", file="SKILL-X.md")
        assert service.search(user_context, "hi a") == []

    def test_mock_as_context_renders_matched(self, user_context):
        """as_context with hits renders the section header + bullet list."""
        service = MockProceduralService()
        service.add_document(
            "OAuth2 implementation patterns", skill="IAM", file="SKILL-IAM.md"
        )
        out = service.as_context(user_context, "OAuth2")
        assert "## Relevant Skills" in out
        assert "IAM" in out
        assert "SKILL-IAM.md" in out

    def test_mock_as_context_no_match_returns_empty_string(self, user_context):
        """as_context returns "" when search yields nothing (defensive contract)."""
        service = MockProceduralService()
        service.add_document("body", skill="X", file="SKILL-X.md")
        assert service.as_context(user_context, "nothing-matches") == ""

    def test_file_procedural_search_short_query_returns_empty(
        self, tmp_path, user_context
    ):
        """File-backed service must also reject short-keyword queries."""
        d = tmp_path / "procedural"
        d.mkdir()
        (d / "SKILL-X.md").write_text("---\nskill: X\n---\nbody\n")
        service = FileProceduralService(skill_dir=str(d))
        assert service.search(user_context, "hi a") == []
