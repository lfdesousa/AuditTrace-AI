"""Tests for the ``recall_attachments`` memory tool (WU-5, Sovereign-Attach
EPIC — 2026-09-06-SPEC-wu5-same-turn-session-recall.md, deliverable D2).

New file rather than an extension of ``test_memory_tool_handlers.py``
(already 2000+ LOC — PYTHON-ENGINEERING §11: "module LOC > 2000 -> stop
adding, new work goes in a sibling module").

Uses ``dependencies.create_test_container()``'s REAL
``PostgresSessionMemoryService`` (aiosqlite-backed, not a mock) so the
non-vacuity guards exercise the actual ``user_id``-filtered SQL path —
the same posture ``test_session_memory_service.py``'s Postgres suite
already takes, and the "REAL service, not mock" pattern
``tests/test_memory_promote_route.py::real_semantic_client`` uses for the
analogous ChromaDB read-scoping proof.

Non-vacuity guards covered (spec §6):

* isolation — ``TestRecallAttachmentsIsolation`` (guard 1);
* scope gate — ``TestRecallAttachmentsScopeGate`` (guard 2);
* content cap / truncation flag — ``TestRecallAttachmentsContentCap``
  (guard 3);
* ``has_more`` +1 probe — ``TestRecallAttachmentsPagination`` (guard 4).
"""

from __future__ import annotations

import importlib
from dataclasses import replace

import fakeredis
import pytest
import pytest_asyncio

# Side-effect import — running the module is what runs the
# @register_memory_tool decorators, including recall_attachments.
import audittrace.tools.memory_handlers as handlers_mod
from audittrace import dependencies
from audittrace.dependencies import create_test_container
from audittrace.identity import sentinel_user_context
from audittrace.tools import (
    get_tool_by_name,
    invoke_tool,
    reset_registry_for_tests,
    tools_visible_to,
)
from audittrace.tools.cache import (
    ToolResultCache,
    reset_tool_result_cache,
    set_tool_result_cache,
)

_SNIPPET_LIMIT = 400  # mirrors tools/memory_handlers.py::_SNIPPET_LIMIT


@pytest.fixture(autouse=True)
def _fresh_registry_with_handlers():
    """Reset the registry and re-run the decorator pass for each test
    (mirrors test_memory_tool_handlers.py's identical fixture)."""
    reset_registry_for_tests()
    importlib.reload(handlers_mod)
    yield
    reset_registry_for_tests()


@pytest_asyncio.fixture
async def _session_container():
    """A fresh test container wired with the REAL
    ``PostgresSessionMemoryService`` (create_test_container's default —
    see module docstring for why this matters over a mock)."""
    c = create_test_container()
    prior = dependencies.container
    dependencies.container = c
    yield c
    dependencies.container = prior


@pytest.fixture
def _fakeredis_cache():
    """Install a fakeredis-backed ToolResultCache as the global singleton
    for the duration of a test (mirrors test_memory_tool_handlers.py)."""
    client = fakeredis.FakeRedis(decode_responses=True)
    cache = ToolResultCache(client, default_ttl_seconds=900)
    set_tool_result_cache(cache)
    yield cache
    reset_tool_result_cache()


# ─────────────────────────── Canonical shape ────────────────────────────────


class TestRecallAttachmentsCanonicalShape:
    async def test_returns_canonical_shape(self, _session_container, _fakeredis_cache):
        user = sentinel_user_context()
        session_memory = dependencies.get_session_memory_service()
        await session_memory.write(user, "notes.txt", "first upload")
        await session_memory.write(user, "log.txt", "second upload")

        tool = get_tool_by_name("recall_attachments")
        assert tool is not None
        result, was_cache_hit = await invoke_tool(user, tool, {}, session_id="sess-1")

        assert was_cache_hit is False
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
        assert result["total"] == 2
        assert result["sort"] == "recency"
        assert result["order"] == "desc"
        # most-recent write first
        assert result["matches"][0]["title"] == "log.txt"
        assert result["matches"][0]["snippet"] == "second upload"
        assert result["matches"][1]["title"] == "notes.txt"

    async def test_empty_when_no_uploads(self, _session_container, _fakeredis_cache):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_attachments")
        result, _ = await invoke_tool(user, tool, {}, session_id="sess-1")
        assert result["total"] == 0
        assert result["matches"] == []
        assert result["has_more"] is False

    async def test_default_n_is_five(self, _session_container, _fakeredis_cache):
        user = sentinel_user_context()
        session_memory = dependencies.get_session_memory_service()
        for i in range(7):
            await session_memory.write(user, f"file-{i}.txt", f"content {i}")
        tool = get_tool_by_name("recall_attachments")
        result, _ = await invoke_tool(user, tool, {}, session_id="sess-1")
        assert result["limit"] == 5
        assert len(result["matches"]) == 5


# ─────────────────────────────── Scope gate ─────────────────────────────────


class TestRecallAttachmentsScopeGate:
    """Non-vacuity guard 2 (spec §6.2): ``required_scope`` gates tool
    visibility. Neuter ``required_scope`` (or ``tools_visible_to``'s scope
    check) and a non-holder starts seeing ``recall_attachments``."""

    def test_required_scope_is_session_read_own(self):
        tool = get_tool_by_name("recall_attachments")
        assert tool is not None
        assert tool.required_scope == "memory:session:read-own"

    def test_visible_to_scope_holder(self):
        holder = replace(
            sentinel_user_context(),
            is_admin=False,
            scopes=("memory:session:read-own",),
        )
        names = {t["function"]["name"] for t in tools_visible_to(holder)}
        assert "recall_attachments" in names

    def test_hidden_from_non_holder(self):
        non_holder = replace(
            sentinel_user_context(),
            is_admin=False,
            scopes=("memory:conversational:read-own",),
        )
        names = {t["function"]["name"] for t in tools_visible_to(non_holder)}
        assert "recall_attachments" not in names

    def test_admin_bypasses_gate(self):
        admin = replace(sentinel_user_context(), is_admin=True, scopes=())
        names = {t["function"]["name"] for t in tools_visible_to(admin)}
        assert "recall_attachments" in names


# ────────────────────────────── Isolation ───────────────────────────────────


class TestRecallAttachmentsIsolation:
    """Non-vacuity guard 1 (spec §6.1 / acceptance (c)): user A's upload
    NEVER appears for user B. Neuter the ``user_id`` filter in
    ``SessionMemoryService.list_own`` and this test goes RED."""

    async def test_cross_user_upload_never_leaks(
        self, _session_container, _fakeredis_cache
    ):
        alice = replace(
            sentinel_user_context(), user_id="user-alice-ra", is_admin=False
        )
        bob = replace(sentinel_user_context(), user_id="user-bob-ra", is_admin=False)
        session_memory = dependencies.get_session_memory_service()
        await session_memory.write(alice, "alice-secret.txt", "alice's content")

        tool = get_tool_by_name("recall_attachments")
        bob_result, _ = await invoke_tool(bob, tool, {}, session_id="sess-bob")
        assert bob_result["total"] == 0
        assert bob_result["matches"] == [], (
            "user B's recall_attachments returned user A's upload — the "
            "isolation wall is broken"
        )

        alice_result, _ = await invoke_tool(alice, tool, {}, session_id="sess-alice")
        assert alice_result["total"] == 1
        assert alice_result["matches"][0]["snippet"] == "alice's content"


# ───────────────────────────── Content cap ──────────────────────────────────


class TestRecallAttachmentsContentCap:
    """Non-vacuity guard 3 (spec §6.3): content is capped at
    ``_SNIPPET_LIMIT`` and the response carries the ``truncated`` flag."""

    async def test_long_upload_snippet_is_capped_and_flagged(
        self, _session_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        session_memory = dependencies.get_session_memory_service()
        long_content = "x" * (_SNIPPET_LIMIT + 250)
        await session_memory.write(user, "big.txt", long_content)

        tool = get_tool_by_name("recall_attachments")
        result, _ = await invoke_tool(user, tool, {}, session_id="sess-1")

        assert len(result["matches"][0]["snippet"]) == _SNIPPET_LIMIT
        assert result["matches"][0]["snippet"] == long_content[:_SNIPPET_LIMIT]
        assert "truncated" in result

    async def test_short_upload_snippet_is_not_truncated(
        self, _session_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        session_memory = dependencies.get_session_memory_service()
        short_content = "short note"
        await session_memory.write(user, "small.txt", short_content)

        tool = get_tool_by_name("recall_attachments")
        result, _ = await invoke_tool(user, tool, {}, session_id="sess-1")

        assert result["matches"][0]["snippet"] == short_content


# ───────────────────────────── Pagination ───────────────────────────────────


class TestRecallAttachmentsPagination:
    """Non-vacuity guard 4 (spec §6.4): a full page reports
    ``has_more=True`` when more exist (the "+1 probe"). Neuter the probe
    (revert to a plain ``LIMIT limit`` fetch) and a fully-saturated page
    would report ``has_more=False`` even though more rows exist."""

    async def test_has_more_true_when_more_exist(
        self, _session_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        session_memory = dependencies.get_session_memory_service()
        for i in range(6):
            await session_memory.write(user, f"file-{i}.txt", f"content {i}")

        tool = get_tool_by_name("recall_attachments")
        result, _ = await invoke_tool(user, tool, {"n": 5}, session_id="sess-1")

        assert result["total"] == 6
        assert len(result["matches"]) == 5
        assert result["has_more"] is True
        assert result["truncated"] is True

    async def test_has_more_false_when_exhausted(
        self, _session_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        session_memory = dependencies.get_session_memory_service()
        for i in range(3):
            await session_memory.write(user, f"file-{i}.txt", f"content {i}")

        tool = get_tool_by_name("recall_attachments")
        result, _ = await invoke_tool(user, tool, {"n": 5}, session_id="sess-1")

        assert result["total"] == 3
        assert len(result["matches"]) == 3
        assert result["has_more"] is False
        assert result["truncated"] is False

    async def test_offset_pages_forward(self, _session_container, _fakeredis_cache):
        user = sentinel_user_context()
        session_memory = dependencies.get_session_memory_service()
        for i in range(6):
            await session_memory.write(user, f"file-{i}.txt", f"content {i}")

        tool = get_tool_by_name("recall_attachments")
        page1, _ = await invoke_tool(
            user, tool, {"n": 5, "offset": 0}, session_id="sess-1"
        )
        page2, _ = await invoke_tool(
            user, tool, {"n": 5, "offset": 5}, session_id="sess-1"
        )
        assert len(page1["matches"]) == 5
        assert len(page2["matches"]) == 1
        assert page2["has_more"] is False


# ─────────────────────────── Error handling ─────────────────────────────────


class TestRecallAttachmentsErrorHandling:
    async def test_bad_n_returns_error(self, _session_container, _fakeredis_cache):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_attachments")
        result, _ = await invoke_tool(
            user, tool, {"n": "not-an-int"}, session_id="sess-1"
        )
        assert "error" in result

    async def test_bad_offset_returns_error(self, _session_container, _fakeredis_cache):
        user = sentinel_user_context()
        tool = get_tool_by_name("recall_attachments")
        result, _ = await invoke_tool(user, tool, {"offset": -1}, session_id="sess-1")
        assert "error" in result


# ─────────────────────────────── Telemetry ──────────────────────────────────


class TestRecallAttachmentsToolToCollectionMapping:
    def test_tool_to_collection_maps_to_session(self):
        from audittrace.tools import _TOOL_TO_COLLECTION

        assert _TOOL_TO_COLLECTION["recall_attachments"] == "session"
