"""Tests for the four memory tool handlers (ADR-025 §Decision.1) and
the cache-aware ``invoke_tool`` entry point (§Decision.8).

The handlers wrap the existing four memory services and normalise their
results into the canonical ``{matches, total, truncated}`` schema.
``invoke_tool`` sits in front of the handler with the Redis-backed
``ToolResultCache`` — cache hits skip the handler and return
``was_cache_hit=True`` so the eventual tool-call loop can skip the
``ToolCall`` audit row (per §Decision.8).

These tests exercise the whole stack: registry decorator → invoke
helper → handler → underlying service, using the mock services from
``dependencies.create_test_container`` and fakeredis for the cache.
"""

from __future__ import annotations

from dataclasses import replace

import fakeredis
import pytest
import pytest_asyncio

# Side-effect import — running the module is what runs the @register_memory_tool
# decorators. Must happen before any test code dispatches through the registry.
import audittrace.tools.memory_handlers  # noqa: F401
from audittrace import dependencies
from audittrace.dependencies import create_test_container
from audittrace.identity import sentinel_user_context
from audittrace.tools import (
    MEMORY_TOOL_REGISTRY,
    get_tool_by_name,
    invoke_tool,
    reset_registry_for_tests,
)
from audittrace.tools.cache import (
    ToolResultCache,
    reset_tool_result_cache,
    set_tool_result_cache,
)

# ────────────────────────────── Fixtures ────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_registry_with_handlers():
    """Reset the registry and re-run the decorator pass for each test.

    Phase 1's autouse `_clean_registry` fixture wipes the registry; we
    need the four memory handlers re-registered between tests so this
    file's tests have something to dispatch.
    """
    reset_registry_for_tests()
    import importlib

    import audittrace.tools.memory_handlers as handlers_mod

    importlib.reload(handlers_mod)
    yield
    reset_registry_for_tests()


@pytest_asyncio.fixture
async def _populated_container():
    """A fresh test container with mocks seeded so the handlers have
    something real to query against."""
    c = create_test_container()
    # Seed each mock service with one representative doc/row.
    c._instances["episodic"].add_document(
        "KV cache compression reduces memory by 75%",
        title="ADR-009",
        file="ADR-009.md",
    )
    c._instances["procedural"].add_document(
        "OAuth2 OIDC JWT validation patterns",
        skill="IAM",
        file="SKILL-IAM.md",
    )
    await c._instances["conversational"].save_session(
        sentinel_user_context(),
        "AuditTrace",
        "Session about KV cache compression",
        ["ADR-009 accepted"],
        session_id="seed-kv-1",
    )
    await c._instances["semantic"].add_document(
        "RAG body about cache optimisation",
        source="ADR-009",
        collection="decisions",
    )
    # Swap global container so the get_*_service helpers see our seeded one.
    prior = dependencies.container
    dependencies.container = c
    yield c
    dependencies.container = prior


@pytest.fixture
def _fakeredis_cache():
    """Install a fakeredis-backed ToolResultCache as the global singleton
    for the duration of a test."""
    client = fakeredis.FakeRedis(decode_responses=True)
    cache = ToolResultCache(client, default_ttl_seconds=900)
    set_tool_result_cache(cache)
    yield cache
    reset_tool_result_cache()


@pytest.fixture
def _disabled_cache():
    """Install a TTL=0 cache so invoke_tool always executes the handler
    and never stores the result."""
    client = fakeredis.FakeRedis(decode_responses=True)
    cache = ToolResultCache(client, default_ttl_seconds=0)
    set_tool_result_cache(cache)
    yield cache
    reset_tool_result_cache()


# ─────────────────────────── Canonical shape ────────────────────────────────


class TestCanonicalShape:
    @pytest.mark.asyncio
    async def test_recall_decisions_returns_canonical_shape(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")
        assert tool is not None
        result, was_cache_hit = await invoke_tool(
            user,
            tool,
            {"query": "cache compression"},
            session_id="sess-1",
        )
        assert was_cache_hit is False
        assert set(result.keys()) >= {"matches", "total", "truncated"}
        assert result["total"] == 1
        assert result["matches"][0]["title"] == "ADR-009"
        assert "cache" in result["matches"][0]["snippet"].lower()

    @pytest.mark.asyncio
    async def test_recall_skills_returns_canonical_shape(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_skills")
        result, _ = await invoke_tool(
            user, tool, {"query": "OAuth2"}, session_id="sess-1"
        )
        assert result["total"] == 1
        assert result["matches"][0]["title"] == "IAM"
        assert result["matches"][0]["source"] == "SKILL-IAM.md"

    @pytest.mark.asyncio
    async def test_recall_recent_sessions_returns_canonical_shape(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_recent_sessions")
        result, _ = await invoke_tool(
            user,
            tool,
            {"project": "AuditTrace", "n": 5},
            session_id="sess-1",
        )
        assert result["total"] == 1
        assert "cache" in result["matches"][0]["snippet"].lower()

    @pytest.mark.asyncio
    async def test_recall_semantic_returns_canonical_shape(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_semantic")
        result, _ = await invoke_tool(
            user,
            tool,
            {"query": "cache", "k": 4},
            session_id="sess-1",
        )
        assert result["total"] >= 1
        assert "cache" in result["matches"][0]["snippet"].lower()


# ─────────────────────── Handler threads UserContext ───────────────────────


class TestUserContextPropagation:
    @pytest.mark.asyncio
    async def test_recall_recent_sessions_respects_user_isolation(
        self, _populated_container, _fakeredis_cache
    ):
        """Phase 2 sessions were stored under the sentinel user_id.
        A different non-admin user should see ZERO of those sessions via
        the handler — proves the handler threads user_context into the
        underlying service's per-user filter."""
        alice = replace(
            sentinel_user_context(),
            user_id="user-alice",
            is_admin=False,
            scopes=("memory:conversational:read-own",),
        )
        tool = get_tool_by_name("recall_recent_sessions")
        result, _ = await invoke_tool(
            alice,
            tool,
            {"project": "AuditTrace", "n": 5},
            session_id="sess-1",
        )
        assert result["total"] == 0
        assert result["matches"] == []


# ─────────────────────── Arg validation / errors ────────────────────────────


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_missing_required_arg_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")
        result, was_cache_hit = await invoke_tool(user, tool, {}, session_id="sess-1")
        assert "error" in result
        assert was_cache_hit is False
        # And the cache is empty — errors are never cached
        assert _fakeredis_cache.size() == 0

    @pytest.mark.asyncio
    async def test_recall_skills_missing_query_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_skills")
        result, _ = await invoke_tool(user, tool, {}, session_id="sess-1")
        assert "error" in result
        assert "query" in result["error"]

    @pytest.mark.asyncio
    async def test_recall_recent_sessions_missing_project_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_recent_sessions")
        result, _ = await invoke_tool(user, tool, {}, session_id="sess-1")
        assert "error" in result
        assert "project" in result["error"]

    @pytest.mark.asyncio
    async def test_recall_recent_sessions_bad_n_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        """Non-integer n arg surfaces a dedicated error (not a crash).
        Covers the int() ValueError branch in recall_recent_sessions."""
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_recent_sessions")
        result, _ = await invoke_tool(
            user,
            tool,
            {"project": "AuditTrace", "n": "not-a-number"},
            session_id="sess-1",
        )
        assert "error" in result
        assert "integer" in result["error"]

    @pytest.mark.asyncio
    async def test_recall_semantic_missing_query_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_semantic")
        result, _ = await invoke_tool(user, tool, {}, session_id="sess-1")
        assert "error" in result
        assert "query" in result["error"]

    @pytest.mark.asyncio
    async def test_recall_semantic_bad_k_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        """Non-integer k arg surfaces a dedicated error."""
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_semantic")
        result, _ = await invoke_tool(
            user,
            tool,
            {"query": "cache", "k": "four"},
            session_id="sess-1",
        )
        assert "error" in result
        assert "integer" in result["error"]

    @pytest.mark.asyncio
    async def test_handler_exception_becomes_error_result(
        self, _populated_container, _fakeredis_cache, monkeypatch
    ):
        """A handler that raises unexpectedly must not crash the loop —
        the invoke helper catches and returns {'error': ExceptionType}.

        The error payload is the exception TYPE name only. str(exc) is
        intentionally dropped because it can carry user query content
        (SQL bind values, ChromaDB query strings) when an inner layer
        echoes parameters — and this payload flows into the LLM response,
        the audit row, and INFO logs, none of which may contain user
        content from a regulated-industry deployment."""
        tool = get_tool_by_name("recall_decisions")

        async def _exploding(user_context, args):
            # The message contains both an inner identifier ("episodic layer")
            # AND a user-query-shaped fragment ("cache"). Neither may appear
            # in the returned error payload.
            raise RuntimeError("episodic layer is on fire while running query=cache")

        # Re-register under the same name with the exploding handler.
        object.__setattr__(tool, "handler", _exploding)
        MEMORY_TOOL_REGISTRY[tool.name] = replace(tool, handler=_exploding)

        user = sentinel_user_context()
        result, was_cache_hit = await invoke_tool(
            user,
            get_tool_by_name("recall_decisions"),
            {"query": "cache"},
            session_id="sess-1",
        )
        assert result == {"error": "RuntimeError"}
        assert "episodic layer" not in result["error"]
        assert "cache" not in result["error"]
        assert was_cache_hit is False
        assert _fakeredis_cache.size() == 0  # errors never cached


# ──────────────────────── Cache hit / miss semantics ────────────────────────


class TestCacheSemantics:
    @pytest.mark.asyncio
    async def test_cache_miss_then_hit(self, _populated_container, _fakeredis_cache):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")

        # First call — cache miss
        result1, hit1 = await invoke_tool(
            user, tool, {"query": "cache compression"}, session_id="sess-1"
        )
        assert hit1 is False
        assert _fakeredis_cache.size() == 1

        # Second call, same args + session — cache hit, handler NOT re-run
        call_counter = {"n": 0}
        real_handler = tool.handler

        async def _counting_handler(uc, args):
            call_counter["n"] += 1
            return await real_handler(uc, args)

        MEMORY_TOOL_REGISTRY[tool.name] = replace(tool, handler=_counting_handler)

        result2, hit2 = await invoke_tool(
            user,
            get_tool_by_name("recall_decisions"),
            {"query": "cache compression"},
            session_id="sess-1",
        )
        assert hit2 is True
        assert result2 == result1
        assert call_counter["n"] == 0

    @pytest.mark.asyncio
    async def test_same_args_different_session_is_a_miss(
        self, _populated_container, _fakeredis_cache
    ):
        """The cache key includes session_id so two users in two sessions
        asking the same thing do not share a cached result."""
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")

        await invoke_tool(user, tool, {"query": "cache"}, session_id="sess-1")
        # Second call — different session
        _, hit = await invoke_tool(user, tool, {"query": "cache"}, session_id="sess-2")
        assert hit is False
        assert _fakeredis_cache.size() == 2  # two distinct entries

    @pytest.mark.asyncio
    async def test_ttl_zero_disables_caching(
        self, _populated_container, _disabled_cache
    ):
        """TTL=0 means: always execute, never store. Successive calls are
        both misses and nothing lands in Redis."""
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")

        _, hit1 = await invoke_tool(user, tool, {"query": "cache"}, session_id="sess-1")
        _, hit2 = await invoke_tool(user, tool, {"query": "cache"}, session_id="sess-1")
        assert hit1 is False
        assert hit2 is False
        assert _disabled_cache.size() == 0

    @pytest.mark.asyncio
    async def test_canonical_args_irrespective_of_key_order(
        self, _populated_container, _fakeredis_cache
    ):
        """Cache key must be insensitive to dict key ordering so the model
        calling with {'query': 'x', 'k': 4} and {'k': 4, 'query': 'x'}
        hits the same cache entry."""
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_semantic")

        _, hit1 = await invoke_tool(
            user, tool, {"query": "cache", "k": 4}, session_id="sess-1"
        )
        _, hit2 = await invoke_tool(
            user, tool, {"k": 4, "query": "cache"}, session_id="sess-1"
        )
        assert hit1 is False
        assert hit2 is True


# ─────────────────── read_decision / read_skill (Phase A.1) ─────────────────


class TestReadDecisionTool:
    @pytest.mark.asyncio
    async def test_read_decision_returns_full_content(
        self, _populated_container, _fakeredis_cache
    ):
        """Regression: returns full untruncated content (was bounded by
        `_SNIPPET_LIMIT = 400` in the recall_* tools).

        Seeds a 5-KB ADR via the mock service so the assertion meaningfully
        proves the 400-char limit is gone."""
        big = "# ADR-025: Memory as Tools\n\n" + ("body line.\n" * 1000)
        _populated_container._instances["episodic"].add_document(
            big, title="ADR-025: Memory as Tools", file="ADR-025.md"
        )
        user = sentinel_user_context()
        tool = get_tool_by_name("read_decision")
        assert tool is not None
        result, _ = await invoke_tool(
            user, tool, {"file": "ADR-025.md"}, session_id="sess-1"
        )
        assert "error" not in result
        assert result["file"] == "ADR-025.md"
        assert result["source"] == "episodic"
        assert result["title"] == "ADR-025: Memory as Tools"
        assert result["content"] == big
        assert len(result["content"]) > 5000  # well over the old 400-char cap

    @pytest.mark.asyncio
    async def test_read_decision_file_not_found_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("read_decision")
        result, _ = await invoke_tool(
            user, tool, {"file": "ADR-999-nope.md"}, session_id="sess-1"
        )
        assert result["error"] == "not_found"
        assert result["file"] == "ADR-999-nope.md"
        # Fix B: not_found carries an actionable hint so the model stops
        # guessing filename variations and pivots to recall_decisions.
        assert "recall_decisions" in result["detail"]
        assert "Do not retry" in result["detail"]

    @pytest.mark.asyncio
    async def test_read_decision_skill_filename_is_wrong_tool(
        self, _populated_container, _fakeredis_cache
    ):
        """Fix B: a SKILL-* filename to read_decision is a category error —
        redirect to read_skill rather than emit a bare not_found."""
        user = sentinel_user_context()
        tool = get_tool_by_name("read_decision")
        result, _ = await invoke_tool(
            user, tool, {"file": "SKILL-IAM.md"}, session_id="sess-1"
        )
        assert result["error"] == "wrong_tool"
        assert result["file"] == "SKILL-IAM.md"
        assert "read_skill" in result["detail"]

    @pytest.mark.asyncio
    async def test_read_decision_path_traversal_blocked(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("read_decision")
        for bad in ["../etc/passwd.md", "subdir/ADR-001.md", "..\\win.md"]:
            result, _ = await invoke_tool(
                user, tool, {"file": bad}, session_id="sess-1"
            )
            assert result["error"] == "not_found"

    @pytest.mark.asyncio
    async def test_read_decision_missing_file_arg_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("read_decision")
        result, _ = await invoke_tool(user, tool, {}, session_id="sess-1")
        assert "error" in result
        assert "file" in result["error"]


class TestReadSkillTool:
    @pytest.mark.asyncio
    async def test_read_skill_returns_full_content(
        self, _populated_container, _fakeredis_cache
    ):
        big = "# IAM Skill\n\n" + ("OAuth2 OIDC line.\n" * 1000)
        _populated_container._instances["procedural"].add_document(
            big, skill="IAM-big", file="SKILL-IAM-big.md"
        )
        user = sentinel_user_context()
        tool = get_tool_by_name("read_skill")
        assert tool is not None
        result, _ = await invoke_tool(
            user, tool, {"file": "SKILL-IAM-big.md"}, session_id="sess-1"
        )
        assert "error" not in result
        assert result["file"] == "SKILL-IAM-big.md"
        assert result["source"] == "procedural"
        assert result["title"] == "IAM-big"
        assert result["content"] == big
        assert len(result["content"]) > 5000

    @pytest.mark.asyncio
    async def test_read_skill_file_not_found_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("read_skill")
        result, _ = await invoke_tool(
            user, tool, {"file": "SKILL-NOPE.md"}, session_id="sess-1"
        )
        assert result["error"] == "not_found"
        # Fix B: actionable hint pointing at recall_skills.
        assert "recall_skills" in result["detail"]
        assert "Do not" in result["detail"]

    @pytest.mark.asyncio
    async def test_read_skill_adr_filename_is_wrong_tool(
        self, _populated_container, _fakeredis_cache
    ):
        """Fix B: an ADR-* filename to read_skill is the exact category error
        seen in the OpenCode trace (an ADR is a decision, not a skill) —
        redirect to read_decision."""
        user = sentinel_user_context()
        tool = get_tool_by_name("read_skill")
        result, _ = await invoke_tool(
            user, tool, {"file": "ADR-026-multi-user-identity.md"}, session_id="sess-1"
        )
        assert result["error"] == "wrong_tool"
        assert result["file"] == "ADR-026-multi-user-identity.md"
        assert "read_decision" in result["detail"]

    @pytest.mark.asyncio
    async def test_read_skill_missing_file_arg_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("read_skill")
        result, _ = await invoke_tool(user, tool, {}, session_id="sess-1")
        assert "error" in result
        assert "file" in result["error"]


class TestRecallRecordsIdentity:
    """#372 / ADR-060 — every recall_* match records a durable ``id`` and a
    stable ``source_ref`` (plus ``sha256``/``distance`` when available) so a
    ``tool_calls.result_summary`` row names the exact memory that shaped the
    answer, and a reconstruction query can fetch it back."""

    @pytest.mark.asyncio
    async def test_recall_decisions_match_has_id_and_source_ref(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")
        result, _ = await invoke_tool(
            user, tool, {"query": "cache compression"}, session_id="s"
        )
        m = result["matches"][0]
        # Keyword layer: no chunk_id, so id falls back to the durable file.
        assert m["id"] == "ADR-009.md"
        assert m["source_ref"] == "ADR-009.md"
        assert m["id"] and m["source_ref"]  # never blank
        assert m["sha256"] is None
        assert m["distance"] is None

    @pytest.mark.asyncio
    async def test_recall_skills_match_has_id_and_source_ref(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_skills")
        result, _ = await invoke_tool(user, tool, {"query": "OAuth2"}, session_id="s")
        m = result["matches"][0]
        assert m["id"] == "SKILL-IAM.md"
        assert m["source_ref"] == "SKILL-IAM.md"
        assert m["id"] and m["source_ref"]

    @pytest.mark.asyncio
    async def test_recall_semantic_surfaces_chunk_id_sha_and_distance(
        self, _populated_container, _fakeredis_cache
    ):
        """When the ChromaDB-backed metadata carries chunk_id + distance +
        document_sha256, the match surfaces all of them."""
        from langchain_core.documents import Document

        _populated_container._instances["semantic"]._docs.setdefault(
            "decisions", []
        ).append(
            Document(
                page_content="cache-rich chunk about optimisation",
                metadata={
                    "chunk_id": "decisions:ADR-050.md:3",
                    "distance": 0.2,
                    "document_sha256": "ab" * 32,
                    "source": "ADR-050.md",
                    "collection": "decisions",
                },
            )
        )
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_semantic")
        result, _ = await invoke_tool(
            user, tool, {"query": "cache", "k": 10}, session_id="s"
        )
        rich = [m for m in result["matches"] if m["id"] == "decisions:ADR-050.md:3"]
        assert rich, "expected the chunk_id-tagged match to surface"
        m = rich[0]
        assert m["source_ref"] == "ADR-050.md"
        assert m["sha256"] == "ab" * 32
        # Raw ChromaDB distance, honestly named (lower = closer).
        assert m["distance"] == 0.2

    @pytest.mark.asyncio
    async def test_recall_semantic_every_match_has_nonblank_id_and_source_ref(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_semantic")
        result, _ = await invoke_tool(
            user, tool, {"query": "cache", "k": 4}, session_id="s"
        )
        assert result["matches"]
        for m in result["matches"]:
            assert m["id"], "id must never be blank"
            assert m["source_ref"], "source_ref must never be blank"


class TestRecallIdentityHelper:
    """Unit coverage for ``_recall_identity_fields`` — the D1 stable-pointer
    fallback chain and the D1 re-index guarantee."""

    def test_source_ref_prefers_file_then_source_then_title(self):
        from audittrace.tools.memory_handlers import _recall_identity_fields

        assert (
            _recall_identity_fields({"file": "f.md", "source": "s", "title": "t"})[
                "source_ref"
            ]
            == "f.md"
        )
        assert (
            _recall_identity_fields({"source": "s", "title": "t"})["source_ref"] == "s"
        )
        assert _recall_identity_fields({"title": "t"})["source_ref"] == "t"

    def test_source_ref_falls_back_to_chunk_id_then_collection(self):
        from audittrace.tools.memory_handlers import _recall_identity_fields

        # No file/source/title → fall back to chunk_id …
        assert (
            _recall_identity_fields({"chunk_id": "c9", "collection": "decisions"})[
                "source_ref"
            ]
            == "c9"
        )
        # … then to collection when there's no chunk_id either.
        assert (
            _recall_identity_fields({"collection": "decisions"})["source_ref"]
            == "decisions"
        )

    def test_source_ref_is_never_blank_for_a_real_match(self):
        from audittrace.tools.memory_handlers import _recall_identity_fields

        # MINOR-2: a metadata dict with NONE of file/source/title must still
        # yield a non-empty source_ref (via chunk_id / collection). Every real
        # ChromaDB match carries a chunk_id and a collection, so this is the
        # never-blank guarantee in practice.
        fields = _recall_identity_fields(
            {"chunk_id": "decisions:x:0", "collection": "decisions"}
        )
        assert fields["source_ref"]  # non-empty

    def test_source_ref_empty_only_when_metadata_totally_bare(self):
        from audittrace.tools.memory_handlers import _recall_identity_fields

        # The helper is total: an entirely empty dict never raises, and only
        # then is source_ref "" (no real recall path produces this).
        assert _recall_identity_fields({})["source_ref"] == ""

    def test_id_uses_chunk_id_when_present_else_source_ref(self):
        from audittrace.tools.memory_handlers import _recall_identity_fields

        assert _recall_identity_fields({"chunk_id": "c1", "file": "f.md"})["id"] == "c1"
        assert _recall_identity_fields({"file": "f.md"})["id"] == "f.md"

    def test_sha256_reads_document_sha256_then_document_hash(self):
        from audittrace.tools.memory_handlers import _recall_identity_fields

        assert _recall_identity_fields({"document_sha256": "aa"})["sha256"] == "aa"
        # PDF pipeline writes the hash under ``document_hash``.
        assert _recall_identity_fields({"document_hash": "bb"})["sha256"] == "bb"
        assert _recall_identity_fields({})["sha256"] is None

    def test_reindex_changes_chunk_id_but_source_ref_is_stable(self):
        """The D1 guarantee: a re-index that re-mints ``chunk_id`` leaves
        ``source_ref`` (and thus the durable pointer to the artefact)
        untouched — the audit row still resolves after re-indexing."""
        from audittrace.tools.memory_handlers import _recall_identity_fields

        before = _recall_identity_fields(
            {"chunk_id": "decisions:ADR-050.md:3", "source": "ADR-050.md"}
        )
        after = _recall_identity_fields(
            {"chunk_id": "decisions:ADR-050.md:0", "source": "ADR-050.md"}
        )
        assert before["id"] != after["id"]  # chunk id churned
        assert before["source_ref"] == after["source_ref"] == "ADR-050.md"


class TestRecallSemanticUncap:
    """``recall_semantic`` previously truncated chunks at 400 chars. Chunks
    are bounded by the chunker so the second cap was hiding useful context."""

    @pytest.mark.asyncio
    async def test_recall_semantic_returns_untruncated_chunk(
        self, _populated_container, _fakeredis_cache
    ):
        big_chunk = "RAG chunk text — " + ("cache theme repeated line.\n" * 200)
        assert len(big_chunk) > 1000
        await _populated_container._instances["semantic"].add_document(
            big_chunk, source="ADR-025", collection="decisions"
        )
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_semantic")
        result, _ = await invoke_tool(
            user, tool, {"query": "cache", "k": 4}, session_id="sess-1"
        )
        long_snippets = [
            m["snippet"] for m in result["matches"] if len(m["snippet"]) > 400
        ]
        assert long_snippets, "expected at least one untruncated semantic chunk"
