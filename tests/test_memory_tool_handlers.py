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
from typing import Any
from unittest.mock import AsyncMock

import fakeredis
import pytest
import pytest_asyncio
from langchain_core.documents import Document

# Side-effect import — running the module is what runs the @register_memory_tool
# decorators. Must happen before any test code dispatches through the registry.
import audittrace.tools.memory_handlers  # noqa: F401
from audittrace import dependencies
from audittrace.db.factory import MockChromaDBFactory
from audittrace.dependencies import create_test_container
from audittrace.identity import UserContext, sentinel_user_context
from audittrace.services.semantic import ChromaSemanticService, SemanticService
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


class _SpySemanticService(SemanticService):
    """Records every ``search`` call and returns a canned document list.

    Lets a test assert exactly HOW ``recall_decisions`` / ``recall_skills``
    now call the semantic service (which collections, which user_context, which
    k) — the #383 contract — without standing up a real ChromaDB.
    """

    def __init__(self, docs: list[Document]):
        self.calls: list[dict[str, Any]] = []
        self._docs = docs

    async def search(
        self,
        user_context: UserContext,
        query: str,
        k: int = 4,
        collections: list[str] | None = None,
    ) -> list[Document]:
        self.calls.append(
            {
                "user_context": user_context,
                "query": query,
                "k": k,
                "collections": collections,
            }
        )
        return self._docs

    async def available_collections(self) -> list[str]:
        return []

    async def upsert(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("recall_* must not upsert")

    async def delete_document(self, *args: Any, **kwargs: Any) -> bool:
        raise AssertionError("recall_* must not delete")  # pragma: no cover

    async def get_document(self, *args: Any, **kwargs: Any) -> Document | None:
        raise AssertionError("recall_* must not get_document")  # pragma: no cover


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
    # #383 — recall_decisions / recall_skills now read the ChromaDB VECTOR
    # store via the semantic service, not the S3 keyword layers. Seed the
    # semantic mock with a decisions doc and a skills doc, each stamped the way
    # the real indexer stamps them (``source`` = the ``.md`` filename;
    # ``skill`` = the skill name), so the recall→read chaining contract is
    # exercised end to end. Direct-append (not add_document) so the metadata is
    # under our control — the mock's add_document takes only source/collection.
    from langchain_core.documents import Document as _Doc

    _sem = c._instances["semantic"]
    _sem._docs.setdefault("decisions", []).append(
        _Doc(
            page_content="KV cache compression reduces memory by 75%",
            metadata={
                "source": "ADR-009.md",
                "collection": "decisions",
                "user_id": sentinel_user_context().user_id,
            },
        )
    )
    _sem._docs.setdefault("skills", []).append(
        _Doc(
            page_content="OAuth2 OIDC JWT validation patterns",
            metadata={
                "source": "SKILL-IAM.md",
                "skill": "IAM",
                "collection": "skills",
                "user_id": sentinel_user_context().user_id,
            },
        )
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
        # #383 — no ``title`` is stamped on .md vector docs, so the preview
        # title falls back to the ``.md`` filename (the indexer's ``source``).
        assert result["matches"][0]["title"] == "ADR-009.md"
        assert result["matches"][0]["source"] == "ADR-009.md"
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


class TestRecallReadsVectorStore383:
    """#383 — recall_decisions / recall_skills read the ChromaDB VECTOR store
    via the semantic service (with the per-user ``where``), NOT the S3 keyword
    layer. These tests are falsifiable: reverting either handler to the S3
    episodic/procedural service fails at least one of them.
    """

    @pytest.mark.asyncio
    async def test_recall_decisions_queries_semantic_decisions_collection(
        self, _populated_container, _fakeredis_cache
    ):
        """recall_decisions calls the semantic service with
        ``collections=["decisions"]``, the caller's own ``user_context`` (the
        input the per-user ``where`` is derived from) and the discovery ``k`` —
        and shapes the match with the ``.md`` filename + a non-null distance."""
        spy = _SpySemanticService(
            [
                Document(
                    page_content="KV cache compression decision body",
                    metadata={
                        "source": "ADR-050.md",
                        "chunk_id": "decisions:ADR-050.md:3",
                        "distance": 0.2,
                        "collection": "decisions",
                        "user_id": "alice",
                    },
                )
            ]
        )
        _populated_container._instances["semantic"] = spy
        alice = replace(
            sentinel_user_context(),
            user_id="alice",
            is_admin=False,
            scopes=("memory:episodic:read",),
        )
        tool = get_tool_by_name("recall_decisions")
        result, _ = await invoke_tool(alice, tool, {"query": "kv cache"}, "sess-1")

        assert len(spy.calls) == 1
        assert spy.calls[0]["collections"] == ["decisions"]
        assert spy.calls[0]["user_context"] is alice  # threaded → per-user where
        assert spy.calls[0]["k"] == 5

        m = result["matches"][0]
        assert m["source"] == "ADR-050.md"  # read-follow-up filename …
        assert m["id"] == "decisions:ADR-050.md:3"  # … not the chunk id
        assert m["source_ref"] == "ADR-050.md"
        assert m["distance"] == 0.2  # vector store → non-null distance

    @pytest.mark.asyncio
    async def test_recall_skills_queries_semantic_skills_collection(
        self, _populated_container, _fakeredis_cache
    ):
        spy = _SpySemanticService(
            [
                Document(
                    page_content="OAuth2 BFF pattern skill body",
                    metadata={
                        "source": "SKILL-IAM.md",
                        "skill": "IAM",
                        "chunk_id": "skills:SKILL-IAM.md:0",
                        "distance": 0.11,
                        "collection": "skills",
                        "user_id": "alice",
                    },
                )
            ]
        )
        _populated_container._instances["semantic"] = spy
        alice = replace(
            sentinel_user_context(),
            user_id="alice",
            is_admin=False,
            scopes=("memory:procedural:read",),
        )
        tool = get_tool_by_name("recall_skills")
        result, _ = await invoke_tool(alice, tool, {"query": "oauth2"}, "sess-1")

        assert len(spy.calls) == 1
        assert spy.calls[0]["collections"] == ["skills"]
        assert spy.calls[0]["user_context"] is alice
        assert spy.calls[0]["k"] == 5

        m = result["matches"][0]
        assert m["title"] == "IAM"  # skill name preferred for the preview title
        assert m["source"] == "SKILL-IAM.md"  # read-follow-up filename
        assert m["id"] == "skills:SKILL-IAM.md:0"
        assert m["distance"] == 0.11

    @pytest.mark.asyncio
    async def test_recall_decisions_does_not_touch_s3_episodic_layer(
        self, _populated_container, _fakeredis_cache
    ):
        """Falsifiability: if recall_decisions still queried the S3 episodic
        keyword layer, this exploding stand-in would surface as an error. It
        must serve the request purely from the semantic decisions seed."""

        class _Boom:
            async def search(self, *args: Any, **kwargs: Any):
                raise AssertionError("recall_decisions must NOT call S3 episodic")

        _populated_container._instances["episodic"] = _Boom()
        tool = get_tool_by_name("recall_decisions")
        result, _ = await invoke_tool(
            sentinel_user_context(), tool, {"query": "cache compression"}, "sess-1"
        )
        assert "error" not in result
        assert result["total"] == 1  # from the semantic "decisions" seed
        assert result["matches"][0]["source"] == "ADR-009.md"

    @pytest.mark.asyncio
    async def test_recall_skills_does_not_touch_s3_procedural_layer(
        self, _populated_container, _fakeredis_cache
    ):
        class _Boom:
            async def search(self, *args: Any, **kwargs: Any):
                raise AssertionError("recall_skills must NOT call S3 procedural")

        _populated_container._instances["procedural"] = _Boom()
        tool = get_tool_by_name("recall_skills")
        result, _ = await invoke_tool(
            sentinel_user_context(), tool, {"query": "oauth2"}, "sess-1"
        )
        assert "error" not in result
        assert result["total"] == 1  # from the semantic "skills" seed
        assert result["matches"][0]["source"] == "SKILL-IAM.md"

    @pytest.mark.asyncio
    async def test_recall_decisions_per_user_where_isolation_and_distance(
        self, _fakeredis_cache, monkeypatch
    ):
        """End-to-end through the REAL ``ChromaSemanticService`` over a mock
        ChromaDB whose ``query`` honours the ``where`` filter: a non-admin
        caller sees ONLY their own indexed decision (the per-user ``where`` is
        exercised), the surfaced ``source`` is the ``.md`` filename (read-
        follow-up), the ``id`` is the durable chunk id, and ``distance`` is
        non-null — the #383 signal that the fix landed."""
        monkeypatch.setattr(
            "audittrace.services.semantic.embed_via_nomic",
            AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
        )
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        col = await client.get_or_create_collection(name="decisions_v2")
        await col.add(
            ids=["decisions:ADR-060.md:0", "decisions:ADR-061.md:0"],
            documents=["alice cache decision", "bob cache decision"],
            metadatas=[
                {"source": "ADR-060.md", "user_id": "alice"},
                {"source": "ADR-061.md", "user_id": "bob"},
            ],
        )
        service = ChromaSemanticService(
            client=client,
            default_collections=["decisions"],
            embed_url="",
            embed_model="nomic-embed-text",
        )
        c = create_test_container()
        c._instances["semantic"] = service
        prior = dependencies.container
        dependencies.container = c
        try:
            alice = replace(
                sentinel_user_context(),
                user_id="alice",
                is_admin=False,
                scopes=("memory:episodic:read",),
            )
            tool = get_tool_by_name("recall_decisions")
            result, _ = await invoke_tool(alice, tool, {"query": "cache"}, "sess-1")
        finally:
            dependencies.container = prior

        sources = {m["source"] for m in result["matches"]}
        assert sources == {"ADR-060.md"}  # bob's row hidden by the per-user where
        m = result["matches"][0]
        assert m["source"] == "ADR-060.md"  # read-follow-up filename …
        assert m["id"] == "decisions:ADR-060.md:0"  # … durable chunk id for audit
        assert m["distance"] is not None  # vector store → non-null distance


# ───────────────────────── Recall telemetry (WU-2.1) ────────────────────────


class _SpyInstrument:
    """Minimal OTel counter/histogram double — records every ``add``/
    ``record`` call as ``(amount, labels)`` so a test can assert exactly
    what :func:`audittrace.tools.memory_handlers._emit_recall_telemetry`
    emitted, without standing up a real MeterProvider/exporter."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, dict[str, Any]]] = []

    def add(self, amount: float, attributes: dict[str, Any] | None = None) -> None:
        self.calls.append((amount, dict(attributes or {})))

    def record(self, amount: float, attributes: dict[str, Any] | None = None) -> None:
        self.calls.append((amount, dict(attributes or {})))


@pytest.fixture
def _recall_telemetry_spy(monkeypatch):
    """Swap the module-level counter/histogram in the shared
    :mod:`audittrace.services.recall_telemetry` for spies for the
    duration of one test.  Must be installed AFTER the autouse
    ``_fresh_registry_with_handlers`` fixture's ``importlib.reload``
    (fixture ordering: autouse fixtures run first), so it patches the
    live instances the handlers actually call."""
    from audittrace.services import recall_telemetry as telemetry_mod

    counter = _SpyInstrument()
    histogram = _SpyInstrument()
    monkeypatch.setattr(telemetry_mod, "_recall_counter", counter)
    monkeypatch.setattr(telemetry_mod, "_recall_results_histogram", histogram)
    return counter, histogram


class TestRecallTelemetry:
    """WU-2.1 non-vacuous proof (#423): the counter/histogram increment with
    ``hit="true"`` on a non-empty recall and ``hit="false"`` on an empty one.
    Neutering the ``_emit_recall_telemetry(...)`` call in any ``recall_*``
    handler turns these RED — that was verified by hand during the build
    (call temporarily commented out, ``pytest`` re-run, restored) and is the
    forever-guard against a silent regression of the metric wiring."""

    @pytest.mark.asyncio
    async def test_recall_decisions_hit_true_increments_with_hit_true_label(
        self, _populated_container, _fakeredis_cache, _recall_telemetry_spy
    ):
        counter, histogram = _recall_telemetry_spy
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")
        result, _ = await invoke_tool(
            user, tool, {"query": "cache compression"}, "sess-1"
        )
        assert result["total"] >= 1
        assert counter.calls == [
            (
                1,
                {
                    "source": "tool",
                    "collection": "decisions",
                    "hit": "true",
                    "cache": "miss",
                },
            )
        ]
        assert histogram.calls == [
            (
                result["total"],
                {
                    "source": "tool",
                    "collection": "decisions",
                    "hit": "true",
                    "cache": "miss",
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_recall_decisions_hit_false_increments_with_hit_false_label(
        self, _populated_container, _fakeredis_cache, _recall_telemetry_spy
    ):
        counter, histogram = _recall_telemetry_spy
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")
        result, _ = await invoke_tool(
            user, tool, {"query": "zzz-no-keyword-matches-anything"}, "sess-1"
        )
        assert result["total"] == 0
        assert counter.calls == [
            (
                1,
                {
                    "source": "tool",
                    "collection": "decisions",
                    "hit": "false",
                    "cache": "miss",
                },
            )
        ]
        assert histogram.calls == [
            (
                0,
                {
                    "source": "tool",
                    "collection": "decisions",
                    "hit": "false",
                    "cache": "miss",
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_recall_skills_hit_true_uses_skills_collection_label(
        self, _populated_container, _fakeredis_cache, _recall_telemetry_spy
    ):
        counter, _histogram = _recall_telemetry_spy
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_skills")
        result, _ = await invoke_tool(user, tool, {"query": "OAuth2"}, "sess-1")
        assert result["total"] >= 1
        assert counter.calls == [
            (
                1,
                {
                    "source": "tool",
                    "collection": "skills",
                    "hit": "true",
                    "cache": "miss",
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_recall_semantic_hit_false_uses_semantic_collection_label(
        self, _populated_container, _fakeredis_cache, _recall_telemetry_spy
    ):
        counter, _histogram = _recall_telemetry_spy
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_semantic")
        result, _ = await invoke_tool(
            user, tool, {"query": "zzz-no-keyword-matches-anything"}, "sess-1"
        )
        assert result["total"] == 0
        assert counter.calls == [
            (
                1,
                {
                    "source": "tool",
                    "collection": "semantic",
                    "hit": "false",
                    "cache": "miss",
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_recall_recent_sessions_hit_true_uses_sessions_collection_label(
        self, _populated_container, _fakeredis_cache, _recall_telemetry_spy
    ):
        counter, histogram = _recall_telemetry_spy
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_recent_sessions")
        result, _ = await invoke_tool(
            user, tool, {"project": "AuditTrace", "n": 5}, "sess-1"
        )
        assert result["total"] >= 1
        assert counter.calls == [
            (
                1,
                {
                    "source": "tool",
                    "collection": "sessions",
                    "hit": "true",
                    "cache": "miss",
                },
            )
        ]
        assert histogram.calls == [
            (
                result["total"],
                {
                    "source": "tool",
                    "collection": "sessions",
                    "hit": "true",
                    "cache": "miss",
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_recall_recent_sessions_hit_false_uses_sessions_collection_label(
        self, _fakeredis_cache, _recall_telemetry_spy
    ):
        counter, histogram = _recall_telemetry_spy
        c = create_test_container()
        c._instances["conversational"] = _OrderedConversationalService([])
        prior = dependencies.container
        dependencies.container = c
        try:
            user = sentinel_user_context()
            tool = get_tool_by_name("recall_recent_sessions")
            result, _ = await invoke_tool(
                user, tool, {"project": "AuditTrace", "n": 5}, "sess-1"
            )
        finally:
            dependencies.container = prior
        assert result["total"] == 0
        assert counter.calls == [
            (
                1,
                {
                    "source": "tool",
                    "collection": "sessions",
                    "hit": "false",
                    "cache": "miss",
                },
            )
        ]
        assert histogram.calls == [
            (
                0,
                {
                    "source": "tool",
                    "collection": "sessions",
                    "hit": "false",
                    "cache": "miss",
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_no_pii_in_labels(
        self, _populated_container, _fakeredis_cache, _recall_telemetry_spy
    ):
        """Labels are ``source`` + ``collection`` + ``hit`` + ``cache`` ONLY —
        no user_id, no query text, matching the spec's non-negotiable
        NO-PII-in-labels rule."""
        counter, histogram = _recall_telemetry_spy
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")
        await invoke_tool(user, tool, {"query": "a secret query about alice"}, "sess-1")
        for _amount, labels in counter.calls + histogram.calls:
            assert set(labels.keys()) == {"source", "collection", "hit", "cache"}
            assert "source" in labels
            assert labels["source"] == "tool"
            assert user.user_id not in labels.values()
            assert "secret" not in str(labels.values())

    @pytest.mark.asyncio
    async def test_invoke_tool_cache_hit_emits_cache_hit_label(
        self, _populated_container, _fakeredis_cache, _recall_telemetry_spy
    ):
        """M1: a cache-hit (second invoke_tool call with same args) must emit
        telemetry with ``cache="hit"`` — the handler is NOT called on hit,
        so the telemetry comes from invoke_tool itself. The collection is
        derived from the tool name."""
        counter, histogram = _recall_telemetry_spy
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")

        # First call — cache miss, handler runs, emits cache="miss"
        result1, hit1 = await invoke_tool(
            user, tool, {"query": "cache compression"}, "sess-1"
        )
        assert hit1 is False
        # One miss emission from the handler
        miss_calls = [c for c in counter.calls if c[1].get("cache") == "miss"]
        assert len(miss_calls) == 1
        assert miss_calls[0][1]["collection"] == "decisions"

        # Second call — cache hit, handler NOT called, invoke_tool emits cache="hit"
        result2, hit2 = await invoke_tool(
            user, tool, {"query": "cache compression"}, "sess-1"
        )
        assert hit2 is True
        assert result2 == result1
        # Now we should have one miss + one hit
        hit_calls = [c for c in counter.calls if c[1].get("cache") == "hit"]
        assert len(hit_calls) == 1
        assert hit_calls[0][1]["collection"] == "decisions"
        assert hit_calls[0][1]["source"] == "tool"
        # Total from cache hit should match the cached result
        assert hit_calls[0][0] == result1.get("total", 0)

    @pytest.mark.asyncio
    async def test_cache_hit_and_miss_both_counted(
        self, _populated_container, _fakeredis_cache, _recall_telemetry_spy
    ):
        """M1: the recall total counter must include BOTH cache=hit and cache=miss
        entries so the panel query (cache=~"hit|miss") has data to work with."""
        counter, _ = _recall_telemetry_spy
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")

        # Two cache-miss calls (different args → different cache keys)
        await invoke_tool(user, tool, {"query": "cache compression"}, "sess-1")
        await invoke_tool(user, tool, {"query": "OAuth2 OIDC"}, "sess-2")
        # One cache-hit call
        await invoke_tool(user, tool, {"query": "cache compression"}, "sess-1")

        total_calls = len(counter.calls)
        assert total_calls == 3
        cache_values = {c[1]["cache"] for c in counter.calls}
        assert cache_values == {"miss", "hit"}, (
            f"Expected both 'miss' and 'hit' in cache values, got {cache_values}"
        )


# ── Pagination (backlog #15 residual, #375 / RECALL-PAGINATION-20260803) ────


class TestRecallToolPaginationShape:
    """R2: every recall tool's response gains
    ``limit``/``offset``/``sort``/``order``/``has_more``, and ``truncated``
    becomes a DEPRECATED alias of ``has_more`` (no longer hardcoded False)."""

    @pytest.mark.asyncio
    async def test_recall_decisions_response_has_pagination_fields(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")
        result, _ = await invoke_tool(
            user, tool, {"query": "cache compression"}, session_id="sess-1"
        )
        assert set(result.keys()) >= {
            "matches",
            "total",
            "limit",
            "offset",
            "sort",
            "order",
            "has_more",
            "truncated",
        }
        assert result["limit"] == 5  # _DISCOVERY_K
        assert result["offset"] == 0
        assert result["sort"] == "relevance"
        assert result["order"] == "asc"
        assert result["has_more"] is False
        assert result["truncated"] == result["has_more"]

    @pytest.mark.asyncio
    async def test_recall_skills_response_has_pagination_fields(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_skills")
        result, _ = await invoke_tool(
            user, tool, {"query": "OAuth2"}, session_id="sess-1"
        )
        assert result["limit"] == 5
        assert result["offset"] == 0
        assert result["truncated"] == result["has_more"]

    @pytest.mark.asyncio
    async def test_recall_semantic_response_has_pagination_fields(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_semantic")
        result, _ = await invoke_tool(
            user, tool, {"query": "cache", "k": 4}, session_id="sess-1"
        )
        assert result["limit"] == 4
        assert result["offset"] == 0
        assert result["truncated"] == result["has_more"]

    @pytest.mark.asyncio
    async def test_recall_recent_sessions_response_has_pagination_fields(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_recent_sessions")
        result, _ = await invoke_tool(
            user, tool, {"project": "AuditTrace", "n": 5}, session_id="sess-1"
        )
        assert result["limit"] == 5
        assert result["offset"] == 0
        assert result["sort"] == "recency"
        assert result["order"] == "desc"
        assert result["truncated"] == result["has_more"]


class TestRecallToolPaginationValidation:
    """Bad offset/sort/order args return an ``{"error": ...}`` dict — same
    convention as the pre-existing query/k/n/project validation."""

    @pytest.mark.asyncio
    async def test_recall_decisions_bad_offset_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")
        result, _ = await invoke_tool(
            user, tool, {"query": "cache", "offset": "not-a-number"}, "sess-1"
        )
        assert "error" in result
        assert "offset" in result["error"]

    @pytest.mark.asyncio
    async def test_recall_decisions_negative_offset_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")
        result, _ = await invoke_tool(
            user, tool, {"query": "cache", "offset": -1}, "sess-1"
        )
        assert "error" in result
        assert "offset" in result["error"]

    @pytest.mark.asyncio
    async def test_recall_decisions_bad_sort_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")
        result, _ = await invoke_tool(
            user, tool, {"query": "cache", "sort": "bogus"}, "sess-1"
        )
        assert "error" in result
        assert "sort" in result["error"]

    @pytest.mark.asyncio
    async def test_recall_decisions_bad_order_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_decisions")
        result, _ = await invoke_tool(
            user, tool, {"query": "cache", "order": "sideways"}, "sess-1"
        )
        assert "error" in result
        assert "order" in result["error"]

    @pytest.mark.asyncio
    async def test_recall_skills_bad_offset_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_skills")
        result, _ = await invoke_tool(
            user, tool, {"query": "oauth2", "offset": "nope"}, "sess-1"
        )
        assert "error" in result
        assert "offset" in result["error"]

    @pytest.mark.asyncio
    async def test_recall_skills_bad_sort_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_skills")
        result, _ = await invoke_tool(
            user, tool, {"query": "oauth2", "sort": "bogus"}, "sess-1"
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_recall_semantic_bad_offset_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_semantic")
        result, _ = await invoke_tool(
            user, tool, {"query": "cache", "offset": "nope"}, "sess-1"
        )
        assert "error" in result
        assert "offset" in result["error"]

    @pytest.mark.asyncio
    async def test_recall_semantic_bad_sort_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_semantic")
        result, _ = await invoke_tool(
            user, tool, {"query": "cache", "sort": "bogus"}, "sess-1"
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_recall_semantic_bad_order_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_semantic")
        result, _ = await invoke_tool(
            user, tool, {"query": "cache", "order": "bogus"}, "sess-1"
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_recall_recent_sessions_bad_offset_returns_error(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_recent_sessions")
        result, _ = await invoke_tool(
            user,
            tool,
            {"project": "AuditTrace", "offset": "nope"},
            "sess-1",
        )
        assert "error" in result
        assert "offset" in result["error"]


class TestRecallToolSchemasAdvertisePagination:
    """R3: the LLM must be able to learn it can page from the schema alone
    — the offset/sort/order params and their docs are advertised, not
    just accepted."""

    def test_recall_decisions_schema_advertises_offset_sort_order(self):
        tool = get_tool_by_name("recall_decisions")
        props = tool.parameters_schema["properties"]
        assert "offset" in props
        assert "sort" in props
        assert "order" in props
        assert "has_more" in props["offset"]["description"]
        assert set(props["sort"]["enum"]) == {"relevance", "recency", "id"}
        assert set(props["order"]["enum"]) == {"asc", "desc"}

    def test_recall_skills_schema_advertises_offset_sort_order(self):
        tool = get_tool_by_name("recall_skills")
        props = tool.parameters_schema["properties"]
        assert {"offset", "sort", "order"} <= props.keys()

    def test_recall_semantic_schema_advertises_offset_sort_order(self):
        tool = get_tool_by_name("recall_semantic")
        props = tool.parameters_schema["properties"]
        assert {"offset", "sort", "order"} <= props.keys()

    def test_recall_recent_sessions_schema_advertises_offset_only(self):
        """recall_recent_sessions is always recency-ordered at the service
        layer (R2 scoping decision, documented in the handler docstring) —
        it advertises ``offset`` but deliberately no ``sort``/``order``."""
        tool = get_tool_by_name("recall_recent_sessions")
        props = tool.parameters_schema["properties"]
        assert "offset" in props
        assert "sort" not in props
        assert "order" not in props


class _OrderedConversationalService:
    """Test double whose ``load_sessions`` returns a FIXED,
    already-recency-descending list truncated to ``n`` — mirrors
    ``PostgresConversationalService``'s ``ORDER BY date DESC LIMIT n``
    stable-prefix property (a bigger ``n`` is a stable EXTENSION of a
    smaller one). ``MockConversationalService``'s ``filtered[-n:]``
    slicing does NOT guarantee that property (each ``n`` re-anchors from
    the END of the full list, so a bigger window can shift EVERY item,
    not just add trailing ones) — harmless for the pre-existing single-call
    usage, but it makes ``MockConversationalService`` unsuitable for
    testing the offset+growing-window pagination added by this change. This
    double isolates the handler's own offset/window/slice arithmetic from
    that mock-only quirk (services/conversational.py is out of this
    change's scope — see the spec's file list)."""

    def __init__(self, sessions: list[dict[str, Any]]):
        self._sessions = sessions  # already newest-first

    async def load_sessions(
        self, user_context: Any, project: str, n: int = 5
    ) -> list[dict[str, Any]]:
        del user_context, project
        return self._sessions[:n]


class TestRecallRecentSessionsPagination:
    @pytest_asyncio.fixture
    async def _many_sessions_container(self):
        c = create_test_container()
        sessions = [
            {
                "id": f"sess-{i}",
                "date": f"2026-08-{i:02d}",
                "summary": f"session summary {i}",
                "key_points": ["decided X"] if i % 2 == 0 else [],
                "synthetic": False,
            }
            for i in range(5)
        ]
        c._instances["conversational"] = _OrderedConversationalService(sessions)
        prior = dependencies.container
        dependencies.container = c
        yield c
        dependencies.container = prior

    @pytest.mark.asyncio
    async def test_offset_pages_past_first_n_sessions(
        self, _many_sessions_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_recent_sessions")

        page0, _ = await invoke_tool(
            user, tool, {"project": "AuditTrace", "n": 2, "offset": 0}, "sess-1"
        )
        page1, _ = await invoke_tool(
            user, tool, {"project": "AuditTrace", "n": 2, "offset": 2}, "sess-2"
        )
        assert len(page0["matches"]) == 2
        assert len(page1["matches"]) == 2
        assert [m["title"] for m in page0["matches"]] == ["sess-0", "sess-1"]
        assert [m["title"] for m in page1["matches"]] == ["sess-2", "sess-3"]
        # "+1 probe" semantics (same honest-lower-bound rationale as
        # ChromaSemanticService.search_page): total is the number of
        # sessions actually fetched within the probe window, not
        # necessarily the full 5-session corpus — but has_more is always
        # correct because a saturated window always fetches at least one
        # more than the page itself.
        assert page0["total"] == 3  # window = min(0+2+1, 500) = 3
        assert page0["has_more"] is True
        assert page1["total"] == 5  # window = min(2+2+1, 500) = 5 (all of it)
        assert page1["has_more"] is True  # sess-4 still unseen

    @pytest.mark.asyncio
    async def test_offset_beyond_total_returns_empty_page_and_no_more(
        self, _many_sessions_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_recent_sessions")
        result, _ = await invoke_tool(
            user,
            tool,
            {"project": "AuditTrace", "n": 5, "offset": 100},
            "sess-1",
        )
        assert result["matches"] == []
        assert result["has_more"] is False

    @pytest.mark.asyncio
    async def test_key_points_and_synthetic_flag_render_in_snippet(
        self, _many_sessions_container, _fakeredis_cache
    ):
        """Covers the key_points-present branch (session 0, even index)
        alongside the existing no-key-points coverage from
        TestCanonicalShape."""
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_recent_sessions")
        result, _ = await invoke_tool(
            user, tool, {"project": "AuditTrace", "n": 5, "offset": 0}, "sess-1"
        )
        snippets = " ".join(m["snippet"] for m in result["matches"])
        assert "Key points: decided X" in snippets

    @pytest.mark.asyncio
    async def test_synthetic_session_flag_prefixes_snippet(self, _fakeredis_cache):
        """ADR-030 Part 1: a draft (not-yet-summarised) session is flagged
        inline so the LLM treats it as lower confidence."""
        c = create_test_container()
        c._instances["conversational"] = _OrderedConversationalService(
            [
                {
                    "id": "sess-draft",
                    "date": "2026-08-03",
                    "summary": "draft session",
                    "key_points": [],
                    "synthetic": True,
                }
            ]
        )
        prior = dependencies.container
        dependencies.container = c
        try:
            user = sentinel_user_context()
            tool = get_tool_by_name("recall_recent_sessions")
            result, _ = await invoke_tool(
                user, tool, {"project": "AuditTrace"}, "sess-1"
            )
        finally:
            dependencies.container = prior
        assert result["matches"][0]["snippet"].startswith(
            "[draft — not yet summarised]"
        )


class TestRecallDecisionsPaginationEndToEnd:
    """Through the TOOL layer (invoke_tool), against a REAL
    ChromaSemanticService over MockChromaDBFactory — the DoD scenario: a
    marker seeded past the discovery ``k`` is unreachable at ``offset=0``
    but reachable once the caller pages forward."""

    @pytest.mark.asyncio
    async def test_offset_reaches_marker_beyond_discovery_k(
        self, _fakeredis_cache, monkeypatch
    ):
        monkeypatch.setattr(
            "audittrace.services.semantic.embed_via_nomic",
            AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
        )
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        col = await client.get_or_create_collection(name="decisions_v2")
        ids = [f"d{i:03d}" for i in range(20)]
        documents = [f"filler decision {i}" for i in range(20)]
        documents[12] = "MARKER-20260803 pagination reachability decision"
        metadatas = [{"source": f"ADR-{i:03d}.md"} for i in range(20)]
        await col.add(ids=ids, documents=documents, metadatas=metadatas)
        service = ChromaSemanticService(
            client=client,
            default_collections=["decisions"],
            embed_url="",
            embed_model="nomic-embed-text",
        )
        c = create_test_container()
        c._instances["semantic"] = service
        prior = dependencies.container
        dependencies.container = c
        try:
            user = sentinel_user_context()
            tool = get_tool_by_name("recall_decisions")

            page0, _ = await invoke_tool(
                user, tool, {"query": "filler", "offset": 0}, "sess-1"
            )
            page1, _ = await invoke_tool(
                user, tool, {"query": "filler", "offset": 12}, "sess-2"
            )
        finally:
            dependencies.container = prior

        assert not any("MARKER" in m["snippet"] for m in page0["matches"])
        assert page0["has_more"] is True
        assert any("MARKER" in m["snippet"] for m in page1["matches"])
