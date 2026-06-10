"""Tests for ContextBuilderService — aggregates all 4 memory layers (ADR-018).

Phase 2 (DESIGN §15): every method takes ``user_context`` as first arg and
threads it down to all four layer services. The broken-layer tests override
layer methods with Phase-2 signatures (``user_context, query, ...``).
"""

import pytest
import pytest_asyncio

from audittrace.services.context_builder import (
    PROFILE_SECTION_HEADER,
    ContextBuilderService,
    DefaultContextBuilder,
    MockContextBuilder,
)
from audittrace.services.conversational import MockConversationalService
from audittrace.services.episodic import MockEpisodicService
from audittrace.services.procedural import MockProceduralService
from audittrace.services.semantic import MockSemanticService

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def populated_builder(user_context):
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
    await conversational.save_session(
        user_context,
        "AuditTrace",
        "KV cache compression enabled",
        ["ADR-009"],
        session_id="cb-kv-1",
    )
    await conversational.save_session(
        user_context,
        "AuditTrace",
        "Phase 0 complete",
        ["DI container"],
        session_id="cb-phase0-1",
    )

    semantic = MockSemanticService()
    await semantic.add_document(
        "RAG doc about cache optimisation", source="ADR-009", collection="decisions"
    )
    await semantic.add_document(
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
    async def test_no_query_returns_profile_only(self, populated_builder, user_context):
        ctx = await populated_builder.build_system_context(
            user_context, project="AuditTrace", query=None
        )
        assert PROFILE_SECTION_HEADER in ctx
        assert "Architecture Decisions" not in ctx
        assert "Relevant Skills" not in ctx
        assert "Recent Sessions" not in ctx

    async def test_query_fires_all_four_layers(self, populated_builder, user_context):
        ctx = await populated_builder.build_system_context(
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

    async def test_profile_always_present(self, populated_builder, user_context):
        ctx = await populated_builder.build_system_context(
            user_context, project="AuditTrace", query="anything"
        )
        assert "Solutions Architect" in ctx
        assert "AuditTrace" in ctx

    async def test_query_with_no_matches(self, populated_builder, user_context):
        ctx = await populated_builder.build_system_context(
            user_context, project="AuditTrace", query="quantum entanglement"
        )
        assert PROFILE_SECTION_HEADER in ctx
        # Episodic/procedural/semantic should return nothing for this query
        assert "Architecture Decisions" not in ctx
        # Conversational always returns for the project
        assert "Recent Sessions" in ctx

    async def test_empty_layers_still_work(self, empty_builder, user_context):
        ctx = await empty_builder.build_system_context(
            user_context, project="P", query="cache"
        )
        assert PROFILE_SECTION_HEADER in ctx
        assert "Architecture Decisions" not in ctx
        assert "Recent Sessions" not in ctx

    async def test_no_arbitrary_caps(self, user_context):
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
        ctx = await builder.build_system_context(
            user_context, project="P", query="server configuration"
        )
        adr_count = ctx.count("### ADR-")
        assert adr_count == 10

    async def test_layer_stats_returned(self, populated_builder, user_context):
        ctx, stats = await populated_builder.build_system_context_with_stats(
            user_context, project="AuditTrace", query="cache compression"
        )
        assert "episodic" in stats
        assert "procedural" in stats
        assert "conversational" in stats
        assert "semantic" in stats
        assert isinstance(stats["episodic"], int)

    async def test_context_sections_separated(self, populated_builder, user_context):
        ctx = await populated_builder.build_system_context(
            user_context, project="AuditTrace", query="cache"
        )
        assert "---" in ctx

    async def test_exception_in_one_layer_doesnt_break_others(self, user_context):
        """If one layer throws, the others should still contribute."""

        class BrokenEpisodicService(MockEpisodicService):
            async def search(self, user_context, query):
                raise RuntimeError("Episodic layer broken")

        builder = DefaultContextBuilder(
            episodic=BrokenEpisodicService(),
            procedural=MockProceduralService(),
            conversational=MockConversationalService(),
            semantic=MockSemanticService(),
        )
        # Should not raise
        ctx = await builder.build_system_context(
            user_context, project="P", query="cache"
        )
        assert PROFILE_SECTION_HEADER in ctx

    async def test_project_none_still_works(self, populated_builder, user_context):
        ctx = await populated_builder.build_system_context(
            user_context, project=None, query="cache"
        )
        assert PROFILE_SECTION_HEADER in ctx

    async def test_procedural_layer_exception_is_swallowed(self, user_context):
        """Procedural layer exception must be swallowed; layer_stats=0.

        The contract is: one broken layer cannot break the whole context build.
        We assert on the layer_stats outcome rather than caplog because pytest's
        caplog has flaky propagation interactions with other tests in this suite.
        """

        class BrokenProcedural(MockProceduralService):
            async def search(self, user_context, query):
                raise RuntimeError("Procedural broken")

        builder = DefaultContextBuilder(
            episodic=MockEpisodicService(),
            procedural=BrokenProcedural(),
            conversational=MockConversationalService(),
            semantic=MockSemanticService(),
        )
        ctx, stats = await builder.build_system_context_with_stats(
            user_context, project="P", query="cache"
        )
        assert stats["procedural"] == 0
        assert PROFILE_SECTION_HEADER in ctx  # always-on layer survived broken sibling

    async def test_conversational_layer_exception_is_swallowed(self, user_context):
        """Conversational layer exception must be swallowed; layer_stats=0."""

        class BrokenConversational(MockConversationalService):
            async def as_context(self, user_context, project):
                raise RuntimeError("Conversational broken")

        builder = DefaultContextBuilder(
            episodic=MockEpisodicService(),
            procedural=MockProceduralService(),
            conversational=BrokenConversational(),
            semantic=MockSemanticService(),
        )
        ctx, stats = await builder.build_system_context_with_stats(
            user_context, project="P", query="cache"
        )
        assert stats["conversational"] == 0
        assert PROFILE_SECTION_HEADER in ctx

    async def test_semantic_layer_exception_is_swallowed(self, user_context):
        """Semantic layer exception must be swallowed; layer_stats=0."""

        class BrokenSemantic(MockSemanticService):
            async def search(self, user_context, query, k=4, collections=None):
                raise RuntimeError("Semantic broken")

        builder = DefaultContextBuilder(
            episodic=MockEpisodicService(),
            procedural=MockProceduralService(),
            conversational=MockConversationalService(),
            semantic=BrokenSemantic(),
        )
        ctx, stats = await builder.build_system_context_with_stats(
            user_context, project="P", query="cache"
        )
        assert stats["semantic"] == 0
        assert PROFILE_SECTION_HEADER in ctx

    async def test_semantic_layer_strips_path_prefix_from_source(self, user_context):
        """Sources containing '/' must be displayed as basename only (line 132-133)."""
        semantic = MockSemanticService()
        await semantic.add_document(
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
        ctx = await builder.build_system_context(
            user_context, project="P", query="cache"
        )
        # The basename should appear, the full path should not
        assert "ADR-009.md" in ctx
        assert "/abs/path/to/" not in ctx


# ── MockContextBuilder tests ─────────────────────────────────────────────────


class TestMockContextBuilder:
    async def test_mock_returns_static_context(self, user_context):
        mock = MockContextBuilder(static_context="Mock memory context")
        ctx = await mock.build_system_context(
            user_context, project="P", query="anything"
        )
        assert ctx == "Mock memory context"

    async def test_mock_returns_empty_stats(self, user_context):
        mock = MockContextBuilder()
        _, stats = await mock.build_system_context_with_stats(
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
        from audittrace.services.context_builder import build_ambient_context

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

        from audittrace.services.context_builder import build_ambient_context

        ctx = build_ambient_context(
            user_context,
            project="AuditTrace",
            tools_visible=[],
        )
        assert "AuditTrace" in ctx
        assert date.today().isoformat() in ctx

    def test_profile_handles_missing_project(self, user_context):
        from audittrace.services.context_builder import build_ambient_context

        ctx = build_ambient_context(
            user_context,
            project=None,
            tools_visible=[],
        )
        # Some explicit marker that no project was supplied
        assert ctx  # not empty
        assert "unspecified" in ctx.lower() or "unspecified" in ctx.lower()

    def test_tool_hints_listed_when_tools_visible(self, user_context):
        """When the caller is authorised to see memory tools, the ambient
        context enumerates them by name so the LLM knows what is available."""
        from audittrace.services.context_builder import build_ambient_context

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
        from audittrace.services.context_builder import build_ambient_context

        ctx = build_ambient_context(user_context, project="P", tools_visible=[])
        # The header line still appears but no bullet list of tools
        assert "recall_" not in ctx

    def test_ambient_context_stays_within_budget(self, user_context):
        """ADR-025 §Decision.1: ambient context ≤ 280 token budget
        approximated as ≤ 280 whitespace-split words. All four tools
        included; this is the worst case."""
        from audittrace.services.context_builder import build_ambient_context

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
        from audittrace.services.context_builder import _AMBIENT_BUDGET_WORDS

        ctx = build_ambient_context(
            user_context, project="AuditTrace", tools_visible=four_tools
        )
        word_count = len(ctx.split())
        assert word_count <= _AMBIENT_BUDGET_WORDS, (
            f"ambient context is {word_count} words, over the "
            f"{_AMBIENT_BUDGET_WORDS} budget:\n{ctx}"
        )

    def test_ambient_context_carries_confidentiality_directive(self, user_context):
        """F-L6 (OWASP-LLM LLM06): the always-injected ambient context must lead
        with the confidentiality directive so a 'paste your system prompt'
        reframe is instructed-against. Regression guard for the Round-3 finding
        where the model echoed its ambient context verbatim."""
        from audittrace.services.context_builder import (
            CONFIDENTIALITY_NOTE,
            build_ambient_context,
        )

        ctx = build_ambient_context(
            user_context, project="AuditTrace", tools_visible=[]
        )
        assert "## Confidentiality" in ctx
        assert CONFIDENTIALITY_NOTE in ctx
        # It must come BEFORE the profile/identity it is protecting.
        assert ctx.index("## Confidentiality") < ctx.index(PROFILE_SECTION_HEADER)
        # And explicitly cover the reframe vectors that defeated the blunt refusal.
        low = ctx.lower()
        assert "never reveal" in low
        for framing in ("debugging", "role-play"):
            assert framing in low

    def test_non_admin_profile_does_not_say_admin(self, user_context):
        """A non-admin UserContext does not see the word 'admin' in its
        profile — the flag is an honest reflection of the caller's
        authority, not boilerplate."""
        from dataclasses import replace

        from audittrace.services.context_builder import build_ambient_context

        non_admin = replace(user_context, is_admin=False, scopes=())
        ctx = build_ambient_context(non_admin, project="P", tools_visible=[])
        # The profile line must not claim admin status for a non-admin
        assert "admin" not in ctx.lower() or "not admin" in ctx.lower()


class TestNamingConventionInjection:
    """ADR-035 amendment 2026-05-01 — the rename mapping must appear in
    every assembled system prompt so the LLM never repeats stale
    SOVEREIGN_* / sovereign_memory names from legacy doc retrievals.
    Pinned in tests so a future edit to context_builder doesn't silently
    drop it."""

    async def test_inject_mode_includes_naming_note(self, user_context):
        """build_system_context_with_stats (inject mode) carries the
        naming note even when no query is provided — so a /context
        request without a query still warns the LLM about old names."""
        empty = DefaultContextBuilder(
            episodic=MockEpisodicService(),
            procedural=MockProceduralService(),
            conversational=MockConversationalService(),
            semantic=MockSemanticService(),
        )
        ctx, _ = await empty.build_system_context_with_stats(
            user_context, project="P", query=None
        )
        assert "SOVEREIGN_*" in ctx and "AUDITTRACE_*" in ctx
        assert "src/sovereign_memory/" in ctx
        assert "sovereign.component" in ctx  # explicit "kept as-is" exception

    def test_ambient_mode_includes_naming_note(self, user_context):
        """build_ambient_context (tools mode, the default) carries the
        same mapping. Tools-mode is the path most users hit."""
        from audittrace.services.context_builder import build_ambient_context

        ctx = build_ambient_context(user_context, project="P", tools_visible=[])
        assert "SOVEREIGN_*" in ctx and "AUDITTRACE_*" in ctx
        assert "src/sovereign_memory/" in ctx
        assert "sovereign.component" in ctx

    def test_naming_note_does_not_blow_ambient_budget(self, user_context):
        """Note + 4-tool ambient context still under the 280-word budget
        (ADR-025 §Decision.1). If a future expansion bumps this, also
        bump _AMBIENT_BUDGET_WORDS in lockstep."""
        from audittrace.services.context_builder import build_ambient_context

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
        from audittrace.services.context_builder import _AMBIENT_BUDGET_WORDS

        assert len(ctx.split()) <= _AMBIENT_BUDGET_WORDS, (
            f"naming note pushed ambient context over {_AMBIENT_BUDGET_WORDS} "
            f"words: {len(ctx.split())}"
        )
