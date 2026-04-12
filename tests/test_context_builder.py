"""Tests for ContextBuilderService — aggregates all 4 memory layers (ADR-018).

Phase 2 (DESIGN §15): every method takes ``user_context`` as first arg and
threads it down to all four layer services. The broken-layer tests override
layer methods with Phase-2 signatures (``user_context, query, ...``).
"""

import pytest

from sovereign_memory.services.context_builder import (
    ContextBuilderService,
    DefaultContextBuilder,
    MockContextBuilder,
)
from sovereign_memory.services.conversational import MockConversationalService
from sovereign_memory.services.episodic import MockEpisodicService
from sovereign_memory.services.procedural import MockProceduralService
from sovereign_memory.services.semantic import MockSemanticService

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def populated_builder(user_context):
    """ContextBuilder with all 4 mock layers populated."""
    episodic = MockEpisodicService()
    episodic.add_document(
        "KV cache compression reduces memory", title="ADR-009", file="ADR-009.md"
    )
    episodic.add_document(
        "ROCm GPU acceleration setup", title="ADR-001", file="ADR-001.md"
    )

    procedural = MockProceduralService()
    procedural.add_document(
        "OAuth2 OIDC JWT patterns", skill="IAM", file="SKILL-IAM.md"
    )
    procedural.add_document(
        "C4 model Structurizr", skill="ARCHITECTURE", file="SKILL-ARCH.md"
    )

    conversational = MockConversationalService()
    conversational.save_session(
        user_context, "AuditTrace", "KV cache compression enabled", ["ADR-009"]
    )
    conversational.save_session(
        user_context, "AuditTrace", "Phase 0 complete", ["DI container"]
    )

    semantic = MockSemanticService()
    semantic.add_document(
        "RAG doc about cache optimisation", source="ADR-009", collection="decisions"
    )
    semantic.add_document(
        "RAG doc about OAuth2 flows", source="SKILL-IAM", collection="skills"
    )

    return DefaultContextBuilder(
        episodic=episodic,
        procedural=procedural,
        conversational=conversational,
        semantic=semantic,
    )


@pytest.fixture
def empty_builder():
    """ContextBuilder with all empty mock layers."""
    return DefaultContextBuilder(
        episodic=MockEpisodicService(),
        procedural=MockProceduralService(),
        conversational=MockConversationalService(),
        semantic=MockSemanticService(),
    )


# ── DefaultContextBuilder tests ──────────────────────────────────────────────


class TestDefaultContextBuilder:
    def test_no_query_returns_profile_only(self, populated_builder, user_context):
        ctx = populated_builder.build_system_context(
            user_context, project="AuditTrace", query=None
        )
        assert "Profil" in ctx
        assert "Architecture Decisions" not in ctx
        assert "Relevant Skills" not in ctx
        assert "Recent Sessions" not in ctx

    def test_query_fires_all_four_layers(self, populated_builder, user_context):
        ctx = populated_builder.build_system_context(
            user_context, project="AuditTrace", query="cache compression OAuth2"
        )
        # Layer 1: Episodic
        assert "Architecture Decisions" in ctx
        assert "ADR-009" in ctx

        # Layer 2: Procedural
        assert "Relevant Skills" in ctx
        assert "IAM" in ctx

        # Layer 3: Conversational
        assert "Recent Sessions" in ctx

        # Layer 4: Semantic
        assert "Relevant Context" in ctx

    def test_profile_always_present(self, populated_builder, user_context):
        ctx = populated_builder.build_system_context(
            user_context, project="AuditTrace", query="anything"
        )
        assert "Luis Filipe" in ctx
        assert "AuditTrace" in ctx

    def test_query_with_no_matches(self, populated_builder, user_context):
        ctx = populated_builder.build_system_context(
            user_context, project="AuditTrace", query="quantum entanglement"
        )
        assert "Profil" in ctx
        # Episodic/procedural/semantic should return nothing for this query
        assert "Architecture Decisions" not in ctx
        # Conversational always returns for the project
        assert "Recent Sessions" in ctx

    def test_empty_layers_still_work(self, empty_builder, user_context):
        ctx = empty_builder.build_system_context(
            user_context, project="P", query="cache"
        )
        assert "Profil" in ctx
        assert "Architecture Decisions" not in ctx
        assert "Recent Sessions" not in ctx

    def test_no_arbitrary_caps(self, user_context):
        """No caps — all matching results returned."""
        episodic = MockEpisodicService()
        for i in range(10):
            episodic.add_document(
                f"Server configuration change {i}",
                title=f"ADR-{i:03d}",
                file=f"ADR-{i:03d}.md",
            )
        builder = DefaultContextBuilder(
            episodic=episodic,
            procedural=MockProceduralService(),
            conversational=MockConversationalService(),
            semantic=MockSemanticService(),
        )
        ctx = builder.build_system_context(
            user_context, project="P", query="server configuration"
        )
        adr_count = ctx.count("### ADR-")
        assert adr_count == 10

    def test_layer_stats_returned(self, populated_builder, user_context):
        ctx, stats = populated_builder.build_system_context_with_stats(
            user_context, project="AuditTrace", query="cache compression"
        )
        assert "episodic" in stats
        assert "procedural" in stats
        assert "conversational" in stats
        assert "semantic" in stats
        assert isinstance(stats["episodic"], int)

    def test_context_sections_separated(self, populated_builder, user_context):
        ctx = populated_builder.build_system_context(
            user_context, project="AuditTrace", query="cache"
        )
        assert "---" in ctx

    def test_exception_in_one_layer_doesnt_break_others(self, user_context):
        """If one layer throws, the others should still contribute."""

        class BrokenEpisodicService(MockEpisodicService):
            def search(self, user_context, query):
                raise RuntimeError("Episodic layer broken")

        builder = DefaultContextBuilder(
            episodic=BrokenEpisodicService(),
            procedural=MockProceduralService(),
            conversational=MockConversationalService(),
            semantic=MockSemanticService(),
        )
        # Should not raise
        ctx = builder.build_system_context(user_context, project="P", query="cache")
        assert "Profil" in ctx

    def test_project_none_still_works(self, populated_builder, user_context):
        ctx = populated_builder.build_system_context(
            user_context, project=None, query="cache"
        )
        assert "Profil" in ctx

    def test_procedural_layer_exception_is_swallowed(self, user_context):
        """Procedural layer exception must be swallowed; layer_stats=0.

        The contract is: one broken layer cannot break the whole context build.
        We assert on the layer_stats outcome rather than caplog because pytest's
        caplog has flaky propagation interactions with other tests in this suite.
        """

        class BrokenProcedural(MockProceduralService):
            def search(self, user_context, query):
                raise RuntimeError("Procedural broken")

        builder = DefaultContextBuilder(
            episodic=MockEpisodicService(),
            procedural=BrokenProcedural(),
            conversational=MockConversationalService(),
            semantic=MockSemanticService(),
        )
        ctx, stats = builder.build_system_context_with_stats(
            user_context, project="P", query="cache"
        )
        assert stats["procedural"] == 0
        assert "Profil" in ctx  # other layers still produced output

    def test_conversational_layer_exception_is_swallowed(self, user_context):
        """Conversational layer exception must be swallowed; layer_stats=0."""

        class BrokenConversational(MockConversationalService):
            def as_context(self, user_context, project):
                raise RuntimeError("Conversational broken")

        builder = DefaultContextBuilder(
            episodic=MockEpisodicService(),
            procedural=MockProceduralService(),
            conversational=BrokenConversational(),
            semantic=MockSemanticService(),
        )
        ctx, stats = builder.build_system_context_with_stats(
            user_context, project="P", query="cache"
        )
        assert stats["conversational"] == 0
        assert "Profil" in ctx

    def test_semantic_layer_exception_is_swallowed(self, user_context):
        """Semantic layer exception must be swallowed; layer_stats=0."""

        class BrokenSemantic(MockSemanticService):
            def search(self, user_context, query, k=4, collections=None):
                raise RuntimeError("Semantic broken")

        builder = DefaultContextBuilder(
            episodic=MockEpisodicService(),
            procedural=MockProceduralService(),
            conversational=MockConversationalService(),
            semantic=BrokenSemantic(),
        )
        ctx, stats = builder.build_system_context_with_stats(
            user_context, project="P", query="cache"
        )
        assert stats["semantic"] == 0
        assert "Profil" in ctx

    def test_semantic_layer_strips_path_prefix_from_source(self, user_context):
        """Sources containing '/' must be displayed as basename only (line 132-133)."""
        semantic = MockSemanticService()
        semantic.add_document(
            "RAG body about cache compression and KV optimisation",
            source="/abs/path/to/ADR-009.md",
            collection="decisions",
        )
        builder = DefaultContextBuilder(
            episodic=MockEpisodicService(),
            procedural=MockProceduralService(),
            conversational=MockConversationalService(),
            semantic=semantic,
        )
        ctx = builder.build_system_context(user_context, project="P", query="cache")
        # The basename should appear, the full path should not
        assert "ADR-009.md" in ctx
        assert "/abs/path/to/" not in ctx


# ── MockContextBuilder tests ─────────────────────────────────────────────────


class TestMockContextBuilder:
    def test_mock_returns_static_context(self, user_context):
        mock = MockContextBuilder(static_context="Mock memory context")
        ctx = mock.build_system_context(user_context, project="P", query="anything")
        assert ctx == "Mock memory context"

    def test_mock_returns_empty_stats(self, user_context):
        mock = MockContextBuilder()
        _, stats = mock.build_system_context_with_stats(
            user_context, project="P", query="q"
        )
        assert stats == {}

    def test_abstract_interface(self):
        assert isinstance(MockContextBuilder(), ContextBuilderService)


# ─────────────────── ADR-025 — ambient context generator ───────────────────
# The ambient context is the minimal always-injected system message for
# `memory_mode=tools`. It must contain identity, project, date, and a
# short hint enumerating the available memory tools. Hard budget: 200
# tokens approximated as 200 words (pessimistic — real tokens are
# shorter than words for English).


class TestAmbientContext:
    def test_profile_includes_username_and_admin_flag(self, user_context):
        from sovereign_memory.services.context_builder import build_ambient_context

        ctx = build_ambient_context(
            user_context,
            project="AuditTrace",
            tools_visible=[],
        )
        assert user_context.username in ctx
        # Sentinel is admin-by-construction (Phase 2). ADR-025
        # §Decision.4 Q4: admin status is exposed in the profile so
        # the model can reason about tool-selection.
        assert "admin" in ctx.lower()

    def test_profile_includes_project_and_date(self, user_context):
        from datetime import date

        from sovereign_memory.services.context_builder import build_ambient_context

        ctx = build_ambient_context(
            user_context,
            project="AuditTrace",
            tools_visible=[],
        )
        assert "AuditTrace" in ctx
        assert date.today().isoformat() in ctx

    def test_profile_handles_missing_project(self, user_context):
        from sovereign_memory.services.context_builder import build_ambient_context

        ctx = build_ambient_context(
            user_context,
            project=None,
            tools_visible=[],
        )
        # Some explicit marker that no project was supplied
        assert ctx  # not empty
        assert "unspecified" in ctx.lower() or "non spécifié" in ctx.lower()

    def test_tool_hints_listed_when_tools_visible(self, user_context):
        """When the caller is authorised to see memory tools, the ambient
        context enumerates them by name so the LLM knows what is available."""
        from sovereign_memory.services.context_builder import build_ambient_context

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "recall_decisions",
                    "description": "Recall ADRs.",
                    "parameters": {},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "recall_skills",
                    "description": "Recall skills.",
                    "parameters": {},
                },
            },
        ]
        ctx = build_ambient_context(user_context, project="P", tools_visible=tools)
        assert "recall_decisions" in ctx
        assert "recall_skills" in ctx

    def test_no_tool_hints_when_empty(self, user_context):
        """Zero visible tools → no enumeration section (happens when a
        non-admin user has no memory scopes at all)."""
        from sovereign_memory.services.context_builder import build_ambient_context

        ctx = build_ambient_context(user_context, project="P", tools_visible=[])
        # The header line still appears but no bullet list of tools
        assert "recall_" not in ctx

    def test_ambient_context_stays_within_budget(self, user_context):
        """ADR-025 §Decision.1: ambient context ≤ 200 token budget
        approximated as ≤ 200 whitespace-split words. All four tools
        included; this is the worst case."""
        from sovereign_memory.services.context_builder import build_ambient_context

        four_tools = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": (
                        "Recall something with a somewhat longer description "
                        "that approximates what the real tool definitions will "
                        "carry in production so the budget test is realistic."
                    ),
                    "parameters": {},
                },
            }
            for name in (
                "recall_decisions",
                "recall_skills",
                "recall_recent_sessions",
                "recall_semantic",
            )
        ]
        ctx = build_ambient_context(
            user_context, project="AuditTrace", tools_visible=four_tools
        )
        word_count = len(ctx.split())
        assert word_count <= 200, (
            f"ambient context is {word_count} words, over the 200 budget:\n{ctx}"
        )

    def test_non_admin_profile_does_not_say_admin(self, user_context):
        """A non-admin UserContext does not see the word 'admin' in its
        profile — the flag is an honest reflection of the caller's
        authority, not boilerplate."""
        from dataclasses import replace

        from sovereign_memory.services.context_builder import build_ambient_context

        non_admin = replace(user_context, is_admin=False, scopes=())
        ctx = build_ambient_context(non_admin, project="P", tools_visible=[])
        # The profile line must not claim admin status for a non-admin
        assert "admin" not in ctx.lower() or "not admin" in ctx.lower()
