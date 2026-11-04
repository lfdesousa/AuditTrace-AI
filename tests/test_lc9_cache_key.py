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

import pytest

import audittrace.tools.memory_handlers as handlers_mod
from audittrace import dependencies as deps_module
from audittrace.dependencies import create_test_container
from audittrace.identity import UserContext
from audittrace.tools import (
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
