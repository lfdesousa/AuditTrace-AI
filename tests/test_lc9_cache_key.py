"""The tool-result cache id covers the request context (LC-9 WP-CK, layer U).

The cache id of ``invoke_tool`` is derived from the caller's identity, the
caller's scope set and admin flag, the session, the tool and the canonical
arguments. This module drives the real ``invoke_tool`` and the real
``ToolResultCache`` end to end.

Doubles declared (EVERY one; nothing else is faked):

- ``fakeredis.FakeRedis`` stands in for the Redis server. It really stores
  and expires entries; it is not faked out.
- ``create_test_container()`` instantiates the in-memory set
  ``InMemoryPostgresFactory`` (SQLite behind the Postgres factory),
  ``MockChromaDBFactory``, ``MockConversationalService``,
  ``MockEpisodicService``, ``MockMemoryManifestService``,
  ``MockProceduralService``, ``MockSemanticService`` and
  ``MockTrustStoreProvider``. Each is pinned by its own suite and is a
  collaborator here, not the subject. The mock conversational store really
  filters rows per user.

The registered handler ``recall_recent_sessions`` is the real one.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

import audittrace.tools.memory_handlers as handlers_mod
from audittrace import dependencies as deps_module
from audittrace.dependencies import create_test_container
from audittrace.identity import UserContext, is_admin_scope, sentinel_user_context
from audittrace.tools import (
    MemoryTool,
    _canonical_cache_id,
    get_tool_by_name,
    invoke_tool,
    reset_registry_for_tests,
)
from audittrace.tools.cache import (
    ToolResultCache,
    reset_tool_result_cache,
    set_tool_result_cache,
)

_READ_SCOPES = (
    "memory:episodic:read",
    "memory:procedural:read",
    "memory:conversational:read-own",
    "memory:semantic:read",
)


@pytest.fixture
def alice() -> UserContext:
    return UserContext(
        user_id="user-alice",
        username="alice",
        agent_type="curl",
        scopes=_READ_SCOPES,
        is_admin=False,
    )


@pytest.fixture
def bob() -> UserContext:
    return UserContext(
        user_id="user-bob",
        username="bob",
        agent_type="curl",
        scopes=_READ_SCOPES,
        is_admin=False,
    )


@pytest.fixture
def container():
    """Fresh container per test."""
    c = create_test_container()
    prior = deps_module.container
    deps_module.container = c
    yield c
    deps_module.container = prior


@pytest.fixture
def fakeredis_cache():
    """Install a fakeredis-backed ToolResultCache."""
    import fakeredis

    client = fakeredis.FakeRedis(decode_responses=True)
    cache = ToolResultCache(client, default_ttl_seconds=900)
    set_tool_result_cache(cache)
    yield cache
    reset_tool_result_cache()


@pytest.fixture(autouse=True)
def fresh_handlers_registry():
    """Re-register the memory tool handlers for every test."""
    reset_registry_for_tests()
    importlib.reload(handlers_mod)
    yield
    reset_registry_for_tests()


async def test_invoke_tool_result_cache_covers_the_caller_identity(
    container, alice, bob, fakeredis_cache
):
    """Same session, same tool, same args, different caller: a miss.

    The cache id includes the caller's identity, so bob's first call
    executes the handler and returns bob's own single row; bob's second
    call is a hit on bob's own entry.
    """
    conv = container._instances["conversational"]
    await conv.save_session(
        alice, "project-p", "alice KV cache session", ["x"], session_id="a-1"
    )
    await conv.save_session(
        bob, "project-p", "bob OAuth2 session", ["y"], session_id="b-1"
    )

    tool = get_tool_by_name("recall_recent_sessions")
    assert tool is not None
    args = {"project": "project-p", "n": 10}
    session_id = "shared-session"

    alice_result, alice_hit = await invoke_tool(alice, tool, args, session_id)
    assert alice_hit is False
    assert alice_result["total"] == 1
    assert "alice" in alice_result["matches"][0]["snippet"]

    bob_result, bob_hit = await invoke_tool(bob, tool, args, session_id)
    assert bob_hit is False
    assert bob_result["total"] == 1
    assert "bob" in bob_result["matches"][0]["snippet"]
    assert "alice" not in str(bob_result)

    _, bob_again_hit = await invoke_tool(bob, tool, args, session_id)
    assert bob_again_hit is True


# ───────────────────────── observer tool helpers ─────────────────────────────

_EXTRA_READ = ("memory:corpus:ai_research_papers:read",)


def _ctx(
    sub: str,
    scopes: tuple[str, ...] = _READ_SCOPES,
    is_admin: bool | None = None,
) -> UserContext:
    """``is_admin=None`` derives it as ``require_user`` does; a bool forces it."""
    admin = is_admin_scope(scopes) if is_admin is None else is_admin
    return UserContext(
        user_id=sub,
        username=sub,
        agent_type="curl",
        scopes=scopes,
        is_admin=admin,
    )


def _observer_tool(calls: list[tuple[str, tuple[str, ...], bool]]) -> MemoryTool:
    """A tool whose handler records the context it was invoked with."""

    async def handler(uc: UserContext, args: dict[str, Any]) -> dict[str, Any]:
        calls.append((uc.user_id, tuple(uc.scopes), uc.is_admin))
        return {"matches": [{"owner": uc.user_id}], "total": 1}

    return MemoryTool(
        name="recall_attachments",
        description="observer",
        parameters_schema={"type": "object"},
        required_scope="memory:conversational:read-own",
        handler=handler,
    )


def _raising_tool() -> MemoryTool:
    async def handler(uc: UserContext, args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom")

    return MemoryTool(
        name="recall_attachments",
        description="raiser",
        parameters_schema={"type": "object"},
        required_scope="memory:conversational:read-own",
        handler=handler,
    )


# ─────────────────────────────────── T2 ──────────────────────────────────────


async def test_cache_identity_is_the_authenticated_context_not_the_arguments(
    fakeredis_cache,
):
    """An identity-looking argument never stands in for the caller."""
    calls: list[tuple[str, tuple[str, ...], bool]] = []
    tool = _observer_tool(calls)
    bob = _ctx("user-bob")
    alice = _ctx("user-alice")
    args = {"n": 5, "user_id": "user-bob"}

    _, bob_hit = await invoke_tool(bob, tool, args, "S")
    _, alice_hit = await invoke_tool(alice, tool, args, "S")

    assert bob_hit is False
    assert alice_hit is False
    assert [c[0] for c in calls] == ["user-bob", "user-alice"]


# ─────────────────────────────────── T3 ──────────────────────────────────────


async def test_empty_identity_is_not_cached(fakeredis_cache, caplog):
    calls: list[tuple[str, tuple[str, ...], bool]] = []
    tool = _observer_tool(calls)
    empty = _ctx("")

    with caplog.at_level(logging.WARNING, logger="audittrace.tools"):
        _, hit1 = await invoke_tool(empty, tool, {"n": 5}, "S")
        _, hit2 = await invoke_tool(empty, tool, {"n": 5}, "S")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [hit1, hit2] == [False, False]
    assert len(calls) == 2
    assert fakeredis_cache.size() == 0
    assert len(warnings) == 2
    for record in warnings:
        message = record.getMessage()
        assert "recall_attachments" in message
        assert "user" not in message.lower()


async def test_empty_identity_with_a_raising_handler_keeps_the_error_shape(
    fakeredis_cache,
):
    result, hit = await invoke_tool(_ctx(""), _raising_tool(), {"n": 5}, "S")

    assert result == {"error": "RuntimeError"}
    assert hit is False
    assert fakeredis_cache.size() == 0


# ─────────────────────────────────── T4 ──────────────────────────────────────

_BASE: dict[str, Any] = {
    "user_id": "u",
    "scopes": _READ_SCOPES,
    "is_admin": False,
    "session_id": "s",
    "tool_name": "t",
    "args": {"a": 1, "b": 2},
}


def _id(**overrides: Any) -> str:
    return _canonical_cache_id(**{**_BASE, **overrides})


def test_canonical_cache_id_properties():
    base = _id()
    # (a) every context field changes the id
    assert _id(user_id="v") != base
    assert _id(session_id="s2") != base
    assert _id(tool_name="t2") != base
    # (b) argument order does not
    assert _id(args={"b": 2, "a": 1}) == base
    # (c) a separator character inside a field cannot alias two tuples
    assert _id(session_id="x|t", tool_name="y") != _id(session_id="x", tool_name="t|y")
    # (d) scopes alone change the id
    assert _id(scopes=_READ_SCOPES + _EXTRA_READ) != base
    # (e) the admin flag alone changes the id
    assert _id(is_admin=True) != base
    # (f) membership matters, order and duplicates do not
    assert _id(scopes=tuple(reversed(_READ_SCOPES)) + (_READ_SCOPES[0],)) == base


# ─────────────────────────────────── T5 ──────────────────────────────────────


async def test_sentinel_identity_is_cached_like_any_other(fakeredis_cache):
    calls: list[tuple[str, tuple[str, ...], bool]] = []
    tool = _observer_tool(calls)

    _, hit1 = await invoke_tool(sentinel_user_context(), tool, {"n": 5}, "S")
    _, hit2 = await invoke_tool(sentinel_user_context(), tool, {"n": 5}, "S")

    assert [hit1, hit2] == [False, True]
    assert len(calls) == 1


# ────────────────────────────── T6 / T6b / T6c ───────────────────────────────


async def test_same_subject_admin_and_non_admin_tokens_do_not_share_results(
    container, fakeredis_cache, monkeypatch
):
    """One subject, two tokens: the admin-scoped result is not replayed."""
    from audittrace.services.semantic import ChromaSemanticService

    monkeypatch.setattr(
        "audittrace.services.semantic.embed_via_nomic",
        AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
    )
    chroma = await container._factories["chromadb"].get_client()
    col = await chroma.get_or_create_collection(name="audittrace_v2")
    await col.add(
        ids=["a1", "b1"],
        documents=["carol cache note", "dave cache note"],
        metadatas=[{"user_id": "user-carol"}, {"user_id": "user-dave"}],
    )
    container._instances["semantic"] = ChromaSemanticService(
        client=chroma, default_collections=["audittrace"]
    )
    tool = get_tool_by_name("recall_semantic")
    assert tool is not None
    args = {"query": "cache", "k": 10}
    admin = _ctx("user-x", _READ_SCOPES + ("audittrace:admin",))
    plain = _ctx("user-x", _READ_SCOPES)
    assert admin.is_admin is True
    assert plain.is_admin is False

    direct = await tool.handler(plain, args)
    assert direct["total"] == 0

    admin_result, admin_hit = await invoke_tool(admin, tool, args, "S")
    assert admin_hit is False
    assert admin_result["total"] == 2

    plain_result, plain_hit = await invoke_tool(plain, tool, args, "S")
    assert plain_hit is False
    assert plain_result["total"] == 0
    assert "carol" not in str(plain_result)
    assert "dave" not in str(plain_result)

    _, plain_again_hit = await invoke_tool(plain, tool, args, "S")
    assert plain_again_hit is True


async def test_cache_identity_includes_the_scope_set(fakeredis_cache):
    calls: list[tuple[str, tuple[str, ...], bool]] = []
    tool = _observer_tool(calls)

    _, hit1 = await invoke_tool(_ctx("user-x", _READ_SCOPES), tool, {"n": 5}, "S")
    _, hit2 = await invoke_tool(
        _ctx("user-x", _READ_SCOPES + _EXTRA_READ), tool, {"n": 5}, "S"
    )

    assert [hit1, hit2] == [False, False]
    assert len(calls) == 2


async def test_cache_identity_includes_is_admin(fakeredis_cache):
    calls: list[tuple[str, tuple[str, ...], bool]] = []
    tool = _observer_tool(calls)

    _, hit1 = await invoke_tool(
        _ctx("user-x", _READ_SCOPES, is_admin=False), tool, {"n": 5}, "S"
    )
    _, hit2 = await invoke_tool(
        _ctx("user-x", _READ_SCOPES, is_admin=True), tool, {"n": 5}, "S"
    )

    assert [hit1, hit2] == [False, False]
    assert len(calls) == 2
