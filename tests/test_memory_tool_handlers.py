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


@pytest.fixture
def _populated_container():
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
    c._instances["conversational"].save_session(
        sentinel_user_context(),
        "AuditTrace",
        "Session about KV cache compression",
        ["ADR-009 accepted"],
        session_id="seed-kv-1",
    )
    c._instances["semantic"].add_document(
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
