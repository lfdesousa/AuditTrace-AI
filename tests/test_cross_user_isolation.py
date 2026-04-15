"""End-to-end per-user isolation across every memory layer.

DESIGN §16 Phase 5a — the consolidated "does the system enforce
cross-user isolation?" test. Cross-user tests exist scattered across
six files today:

  - test_postgres_conversational.py::test_cross_user_isolation
  - test_conversational_service.py::test_mock_isolates_by_user
  - test_semantic_service.py::test_search_non_admin_applies_user_id_filter
  - test_semantic_service.py::test_wrapper_binds_user_at_construction
  - test_memory_tool_handlers.py::test_recall_recent_sessions_respects_user_isolation
  - test_rls_isolation.py::test_alice_sees_only_alice_rows

Each of them proves ONE property in isolation. This file is the one
place a reviewer can come to answer "does the whole stack enforce
per-user isolation end-to-end" without threading through six files.

Each test seeds data under alice's UserContext AND bob's UserContext,
then asserts each user only sees their own rows when reading. Layers
exercised:

  1. Episodic — shared filesystem corpus (plumbing-only, both users
     see the same ADRs; the test proves the plumbing doesn't crash
     and produces identical results).
  2. Procedural — same shape.
  3. Conversational — SQL WHERE user_id filter (MockConversationalService
     and PostgresConversationalService both honour it).
  4. Semantic — ChromaDB `where={"user_id"}` metadata filter for
     non-admin callers.
  5. UserScopedSemanticService — the Phase 4 wrapper's bound identity
     overrides any per-call context.
  6. ContextBuilder — the aggregator threads UserContext through all
     four layers per request.
  7. `_compute_session_id` — includes user_id in the hash so two users
     with identical (source, date, first_message) produce distinct
     session ids.
  8. `recall_*` memory tools — the Phase 3 scope-filtered handlers
     also carry the per-user filter through.
  9. `get_context_builder(user)` — the Phase 4 follow-up per-request
     wiring wraps the semantic layer with the caller's identity.

Phase 5a does NOT yet flip `SOVEREIGN_AUTH_REQUIRED=true`. That's
Phase 5b, which depends on Phase 7 Keycloak operator setup.
"""

from __future__ import annotations

import pytest

# Import memory-handler module so @register_memory_tool decorators
# populate the registry before the tool-layer tests run.
import sovereign_memory.tools.memory_handlers  # noqa: F401
from sovereign_memory import dependencies as deps_module
from sovereign_memory.dependencies import create_test_container, get_context_builder
from sovereign_memory.identity import UserContext
from sovereign_memory.routes.chat import _compute_session_id
from sovereign_memory.services.semantic import (
    ChromaSemanticService,
    UserScopedSemanticService,
)
from sovereign_memory.tools import (
    get_tool_by_name,
    invoke_tool,
    reset_registry_for_tests,
)
from sovereign_memory.tools.cache import (
    ToolResultCache,
    reset_tool_result_cache,
    set_tool_result_cache,
)

# ─────────────────────────── Two-user fixtures ──────────────────────────────


@pytest.fixture
def alice() -> UserContext:
    return UserContext(
        user_id="user-alice",
        username="alice",
        agent_type="curl",
        scopes=(
            "memory:episodic:read",
            "memory:procedural:read",
            "memory:conversational:read-own",
            "memory:semantic:read",
        ),
        is_admin=False,
    )


@pytest.fixture
def bob() -> UserContext:
    return UserContext(
        user_id="user-bob",
        username="bob",
        agent_type="curl",
        scopes=(
            "memory:episodic:read",
            "memory:procedural:read",
            "memory:conversational:read-own",
            "memory:semantic:read",
        ),
        is_admin=False,
    )


@pytest.fixture
def container():
    """Fresh test container for each test so state doesn't leak."""
    c = create_test_container()
    prior = deps_module.container
    deps_module.container = c
    yield c
    deps_module.container = prior


@pytest.fixture
def _fakeredis_cache():
    """Install a fakeredis-backed ToolResultCache for the tool-layer tests."""
    import fakeredis

    client = fakeredis.FakeRedis(decode_responses=True)
    cache = ToolResultCache(client, default_ttl_seconds=900)
    set_tool_result_cache(cache)
    yield cache
    reset_tool_result_cache()


@pytest.fixture(autouse=True)
def _fresh_handlers_registry():
    """Every test starts with the four @register_memory_tool handlers
    freshly re-registered so the tool-layer tests have something to
    dispatch against."""
    reset_registry_for_tests()
    import importlib

    import sovereign_memory.tools.memory_handlers as handlers_mod

    importlib.reload(handlers_mod)
    yield
    reset_registry_for_tests()


# ──────────────────────── Layer 1 — Episodic ────────────────────────────────


class TestCrossUserIsolation:
    """Consolidated end-to-end per-user isolation proof."""

    def test_episodic_is_shared_corpus_not_per_user(self, container, alice, bob):
        """ADRs are shared architectural knowledge — both users see
        the whole corpus. Per-user filtering is NOT meaningful here.
        The test proves the plumbing doesn't crash and both users
        get consistent (identical) results."""
        episodic = container._instances["episodic"]
        episodic.add_document(
            "KV cache compression reduces memory by 75%",
            title="ADR-009",
            file="ADR-009.md",
        )
        alice_matches = episodic.search(alice, "cache compression")
        bob_matches = episodic.search(bob, "cache compression")
        assert len(alice_matches) == 1
        assert len(bob_matches) == 1
        assert alice_matches[0].metadata["title"] == bob_matches[0].metadata["title"]

    def test_procedural_is_shared_corpus_not_per_user(self, container, alice, bob):
        """SKILL files are shared procedural knowledge — same semantics
        as episodic. Both users see the full corpus."""
        procedural = container._instances["procedural"]
        procedural.add_document(
            "OAuth2 OIDC JWT patterns", skill="IAM", file="SKILL-IAM.md"
        )
        alice_matches = procedural.search(alice, "OAuth2")
        bob_matches = procedural.search(bob, "OAuth2")
        assert len(alice_matches) == 1
        assert len(bob_matches) == 1

    # ──────────────────── Layer 3 — Conversational ────────────────────

    def test_conversational_alice_cannot_read_bobs_sessions(
        self, container, alice, bob
    ):
        """Alice writes a session, Bob writes a session, they're in
        the same project — each one's `load_sessions` returns only
        their own row. Proves the SQL `WHERE user_id =` filter from
        Phase 2."""
        conv = container._instances["conversational"]

        conv.save_session(
            alice,
            "shared-project",
            "alice: KV cache notes",
            ["a1"],
            session_id="alice-kv-1",
        )
        conv.save_session(
            bob,
            "shared-project",
            "bob: OAuth2 notes",
            ["b1"],
            session_id="bob-oauth-1",
        )

        alice_sessions = conv.load_sessions(alice, "shared-project", n=10)
        bob_sessions = conv.load_sessions(bob, "shared-project", n=10)

        assert len(alice_sessions) == 1
        assert "alice" in alice_sessions[0]["summary"]
        assert len(bob_sessions) == 1
        assert "bob" in bob_sessions[0]["summary"]

        # And the as_context() helper respects the same filter.
        alice_ctx = conv.as_context(alice, "shared-project")
        bob_ctx = conv.as_context(bob, "shared-project")
        assert "alice" in alice_ctx
        assert "bob" not in alice_ctx
        assert "bob" in bob_ctx
        assert "alice" not in bob_ctx

    def test_conversational_save_persists_user_id(self, container, alice):
        """Side-effect confirmation: the row actually lands with
        `user_id=alice.user_id`, not NULL or sentinel."""
        conv = container._instances["conversational"]
        conv.save_session(alice, "P", "A", ["k"], session_id="alice-persist-1")
        # Mock stores it in self._sessions with user_id field
        assert conv._sessions[-1]["user_id"] == alice.user_id

    # ────────────────────── Layer 4 — Semantic ────────────────────────

    def test_semantic_chroma_filter_isolates_non_admin_users(
        self, container, alice, bob
    ):
        """Non-admin callers get a `where={"user_id"}` filter in the
        Chroma query. Alice's docs are tagged with her user_id, Bob's
        with his; searching as alice returns only alice's rows."""
        client = container._factories["chromadb"].get_client()
        col = client.get_or_create_collection(name="sovereign_memory")
        col.add(
            ids=["a1", "b1", "legacy"],
            documents=[
                "alice private note about KV cache compression",
                "bob private note about KV cache compression",
                "legacy untagged note about KV cache",
            ],
            metadatas=[
                {"source": "a", "user_id": "user-alice"},
                {"source": "b", "user_id": "user-bob"},
                {"source": "legacy"},  # no user_id — pre-Phase-2 row
            ],
        )
        semantic = ChromaSemanticService(
            client=client, default_collections=["sovereign_memory"]
        )

        alice_results = semantic.search(alice, "cache", k=10)
        bob_results = semantic.search(bob, "cache", k=10)

        alice_contents = [d.page_content for d in alice_results]
        bob_contents = [d.page_content for d in bob_results]

        assert any("alice" in c for c in alice_contents)
        assert not any("bob" in c for c in alice_contents)
        assert any("bob" in c for c in bob_contents)
        assert not any("alice" in c for c in bob_contents)

    def test_semantic_wrapper_enforces_binding_by_construction(
        self, container, alice, bob
    ):
        """UserScopedSemanticService binds a user at construction time
        and ignores whatever user_context is passed at call time.
        Proves the Phase 4 'isolation by construction' property."""
        client = container._factories["chromadb"].get_client()
        col = client.get_or_create_collection(name="sovereign_memory")
        col.add(
            ids=["a1", "b1"],
            documents=[
                "alice's cache note",
                "bob's cache note",
            ],
            metadatas=[
                {"source": "a", "user_id": "user-alice"},
                {"source": "b", "user_id": "user-bob"},
            ],
        )
        inner = ChromaSemanticService(
            client=client, default_collections=["sovereign_memory"]
        )
        alice_wrapper = UserScopedSemanticService(inner=inner, user_context=alice)

        # Call with bob's context — the wrapper must ignore it and
        # use its bound (alice) identity.
        results = alice_wrapper.search(bob, "cache", k=10)
        contents = [d.page_content for d in results]
        assert any("alice" in c for c in contents)
        assert not any("bob" in c for c in contents)

    # ──────────────────── Session id uniqueness ──────────────────────

    def test_session_id_is_distinct_for_two_users_with_same_message(self, alice, bob):
        """Two users asking the same question on the same day via the
        same agent must produce distinct session ids. Prevents a
        cross-user session collision in Langfuse / the interactions
        table."""
        alice_sid = _compute_session_id("opencode", "what about cache?", alice.user_id)
        bob_sid = _compute_session_id("opencode", "what about cache?", bob.user_id)
        assert alice_sid != bob_sid

    # ────────────────────── Context builder ───────────────────────────

    def test_context_builder_per_request_aggregates_per_user(
        self, container, alice, bob
    ):
        """get_context_builder(user) returns a per-request builder
        whose semantic layer is bound to the caller. Aggregated
        context includes the caller's conversational data but not
        the other user's."""
        conv = container._instances["conversational"]
        conv.save_session(
            alice, "shared-project", "alice KV cache", [], session_id="alice-builder-1"
        )
        conv.save_session(
            bob, "shared-project", "bob OAuth2", [], session_id="bob-builder-1"
        )

        alice_builder = get_context_builder(alice)
        bob_builder = get_context_builder(bob)

        # Builders are distinct instances, each with their own
        # semantic wrapper bound to the correct identity.
        assert alice_builder is not bob_builder
        assert alice_builder._semantic._bound_user.user_id == alice.user_id
        assert bob_builder._semantic._bound_user.user_id == bob.user_id

        # And the aggregated `build_system_context_with_stats` output
        # carries each user's conversational data separately.
        alice_ctx, _ = alice_builder.build_system_context_with_stats(
            alice, project="shared-project", query="cache"
        )
        bob_ctx, _ = bob_builder.build_system_context_with_stats(
            bob, project="shared-project", query="cache"
        )
        assert "alice KV cache" in alice_ctx
        assert "bob OAuth2" not in alice_ctx
        assert "bob OAuth2" in bob_ctx
        assert "alice KV cache" not in bob_ctx

    # ──────────── Phase 3 memory-tools handler path ───────────────

    @pytest.mark.asyncio
    async def test_recall_recent_sessions_handler_isolates_users(
        self, container, alice, bob, _fakeredis_cache
    ):
        """The `recall_recent_sessions` tool handler wraps
        ConversationalService.load_sessions and therefore inherits
        the per-user filter. Proves the Phase 3 tool-handler layer
        honours isolation end-to-end through invoke_tool."""
        conv = container._instances["conversational"]
        conv.save_session(
            alice,
            "shared-project",
            "alice KV cache session",
            ["x"],
            session_id="alice-handler-1",
        )
        conv.save_session(
            bob,
            "shared-project",
            "bob OAuth2 session",
            ["y"],
            session_id="bob-handler-1",
        )

        tool = get_tool_by_name("recall_recent_sessions")
        assert tool is not None

        alice_result, _ = await invoke_tool(
            alice,
            tool,
            {"project": "shared-project", "n": 10},
            session_id="sess-alice",
        )
        bob_result, _ = await invoke_tool(
            bob,
            tool,
            {"project": "shared-project", "n": 10},
            session_id="sess-bob",
        )

        assert alice_result["total"] == 1
        assert "alice" in alice_result["matches"][0]["snippet"]
        assert bob_result["total"] == 1
        assert "bob" in bob_result["matches"][0]["snippet"]

    # ──────────── Admin bypass — sentinel sees everything ───────────

    def test_admin_sentinel_sees_everything(self, container, alice, bob):
        """The sentinel bypass UserContext (is_admin=True) should
        bypass every per-user filter. This is the behaviour
        `SOVEREIGN_AUTH_REQUIRED=false` depends on — existing tests
        and dev workflows that pre-date multi-user keep working."""
        from sovereign_memory.identity import sentinel_user_context

        admin = sentinel_user_context()
        assert admin.is_admin is True

        # Conversational: admin's load_sessions is scoped to its OWN
        # user_id (sentinel) — not a god view. The invariant is
        # "no admin god read"; admin admin-ness is about scopes, not
        # data.
        conv = container._instances["conversational"]
        conv.save_session(alice, "shared", "alice", [], session_id="alice-admin-1")
        conv.save_session(bob, "shared", "bob", [], session_id="bob-admin-1")
        conv.save_session(admin, "shared", "admin's own", [], session_id="admin-own-1")
        admin_sessions = conv.load_sessions(admin, "shared", n=10)
        assert len(admin_sessions) == 1
        assert admin_sessions[0]["summary"] == "admin's own"

        # Semantic: the Chroma filter has an explicit admin bypass
        # per ADR-025 §Decision so the 4-layer context dump in
        # inject mode still works. Seed tagged rows and verify admin
        # sees them all.
        client = container._factories["chromadb"].get_client()
        col = client.get_or_create_collection(name="sovereign_memory")
        col.add(
            ids=["a1", "b1"],
            documents=["alice cache", "bob cache"],
            metadatas=[
                {"user_id": "user-alice"},
                {"user_id": "user-bob"},
            ],
        )
        semantic = ChromaSemanticService(
            client=client, default_collections=["sovereign_memory"]
        )
        admin_results = semantic.search(admin, "cache", k=10)
        assert len(admin_results) == 2  # admin bypass returns both
