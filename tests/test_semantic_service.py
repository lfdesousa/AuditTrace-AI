"""Tests for SemanticService — Layer 4 of the 4-layer memory architecture (ADR-018).

Phase 2 (DESIGN §15): every service method takes ``user_context`` as the
first positional argument. ``ChromaSemanticService.search`` applies a
``where={"user_id": ...}`` filter when the caller is NOT admin — a preview
of the Phase 4 ChromaDB scoped wrapper. Admins see everything, which is
why the sentinel-backed ``user_context`` fixture (admin by construction)
keeps all legacy test data visible.
"""

from dataclasses import replace

import pytest

from audittrace.db.factory import MockChromaDBFactory
from audittrace.services.semantic import (
    ChromaSemanticService,
    MockSemanticService,
    SemanticService,
)

# ── ChromaSemanticService tests ──────────────────────────────────────────────


class TestChromaSemanticService:
    @pytest.fixture
    def service(self):
        factory = MockChromaDBFactory()
        client = factory.get_client()
        # Seed a collection with documents
        col = client.get_or_create_collection(name="decisions")
        col.add(
            ids=["doc1", "doc2", "doc3"],
            documents=[
                "KV cache compression reduces memory by 75%",
                "ROCm GPU acceleration for AMD hardware",
                "OAuth2 OIDC token validation patterns",
            ],
            metadatas=[
                {"source": "ADR-009", "project": "AuditTrace"},
                {"source": "ADR-001", "project": "AuditTrace"},
                {"source": "SKILL-IAM", "project": "AuditTrace"},
            ],
        )
        return ChromaSemanticService(client=client, default_collections=["decisions"])

    def test_search_returns_results(self, service, user_context):
        results = service.search(user_context, "cache compression", k=2)
        assert len(results) >= 1

    def test_search_returns_documents_with_metadata(self, service, user_context):
        results = service.search(user_context, "cache", k=2)
        for doc in results:
            assert doc.page_content
            assert "source" in doc.metadata

    def test_search_respects_k(self, service, user_context):
        results = service.search(user_context, "anything", k=1)
        assert len(results) <= 1

    def test_search_specific_collection(self, user_context):
        factory = MockChromaDBFactory()
        client = factory.get_client()
        col = client.get_or_create_collection(name="skills")
        col.add(
            ids=["s1"],
            documents=["Architecture patterns for cloud"],
            metadatas=[{"source": "SKILL-ARCH"}],
        )
        service = ChromaSemanticService(client=client, default_collections=["skills"])
        results = service.search(
            user_context, "architecture", k=4, collections=["skills"]
        )
        assert len(results) >= 1

    def test_search_empty_collection(self, user_context):
        factory = MockChromaDBFactory()
        client = factory.get_client()
        client.get_or_create_collection(name="empty")
        service = ChromaSemanticService(client=client, default_collections=["empty"])
        results = service.search(user_context, "anything", k=4)
        assert results == []

    def test_available_collections_graceful_when_unsupported(self):
        """MockChromaDBClient doesn't implement list_collections — should return []."""
        factory = MockChromaDBFactory()
        client = factory.get_client()
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )
        cols = service.available_collections()
        assert cols == []  # graceful degradation

    def test_search_across_multiple_collections(self, user_context):
        factory = MockChromaDBFactory()
        client = factory.get_client()
        col1 = client.get_or_create_collection(name="decisions")
        col1.add(ids=["d1"], documents=["ADR content"], metadatas=[{"source": "adr"}])
        col2 = client.get_or_create_collection(name="skills")
        col2.add(
            ids=["s1"], documents=["Skill content"], metadatas=[{"source": "skill"}]
        )
        service = ChromaSemanticService(
            client=client, default_collections=["decisions", "skills"]
        )
        results = service.search(user_context, "content", k=4)
        assert len(results) >= 2

    def test_search_non_admin_applies_user_id_filter(self, user_context):
        """Phase 4 preview: a non-admin UserContext restricts results to rows
        whose metadata ``user_id`` matches the caller. Rows tagged with a
        different user_id must be invisible."""
        factory = MockChromaDBFactory()
        client = factory.get_client()
        col = client.get_or_create_collection(name="decisions")
        col.add(
            ids=["mine", "theirs", "untagged"],
            documents=[
                "mine: private note about cache",
                "theirs: another user's note about cache",
                "untagged: legacy row without user_id",
            ],
            metadatas=[
                {"source": "n1", "user_id": "user-alice"},
                {"source": "n2", "user_id": "user-bob"},
                {"source": "n3"},  # no user_id — pre-Phase 2 row
            ],
        )
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )

        alice_ctx = replace(
            user_context, user_id="user-alice", is_admin=False, scopes=()
        )
        results = service.search(alice_ctx, "cache", k=10)
        contents = [d.page_content for d in results]
        assert any("mine" in c for c in contents)
        assert not any("theirs" in c for c in contents)

    def test_search_admin_bypasses_user_id_filter(self, user_context):
        """Admin sees every row regardless of ``user_id`` metadata."""
        factory = MockChromaDBFactory()
        client = factory.get_client()
        col = client.get_or_create_collection(name="decisions")
        col.add(
            ids=["mine", "theirs"],
            documents=[
                "mine: note about cache",
                "theirs: another note about cache",
            ],
            metadatas=[
                {"source": "n1", "user_id": "user-alice"},
                {"source": "n2", "user_id": "user-bob"},
            ],
        )
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )
        # user_context fixture is sentinel → is_admin=True
        results = service.search(user_context, "cache", k=10)
        assert len(results) == 2


# ── MockSemanticService tests ────────────────────────────────────────────────


class TestMockSemanticService:
    def test_mock_starts_empty(self, user_context):
        service = MockSemanticService()
        assert service.search(user_context, "anything") == []

    def test_mock_add_and_search(self, user_context):
        service = MockSemanticService()
        service.add_document(
            "KV cache content", source="ADR-009", collection="decisions"
        )
        results = service.search(user_context, "cache")
        assert len(results) == 1
        assert "cache" in results[0].page_content.lower()

    def test_mock_available_collections(self):
        service = MockSemanticService()
        service.add_document("test", source="s", collection="decisions")
        service.add_document("test", source="s", collection="skills")
        cols = service.available_collections()
        assert "decisions" in cols
        assert "skills" in cols

    def test_mock_reset(self, user_context):
        service = MockSemanticService()
        service.add_document("test", source="s", collection="decisions")
        service.reset()
        assert service.search(user_context, "test") == []
        assert service.available_collections() == []

    def test_abstract_interface(self):
        assert isinstance(MockSemanticService(), SemanticService)


# ──────────────── Phase 4 — UserScopedSemanticService wrapper ───────────────
# The wrapper binds a UserContext at construction time and ignores any
# user_context passed at call time. Isolation is then true by construction:
# a bug elsewhere in the code base that leaks an admin context into a
# non-admin user's request cannot bypass the filter because the wrapper's
# bound identity is the only one the underlying service ever sees.
#
# This is the ChromaDB half of DESIGN §16 Phase 4. The Postgres half
# lives in Alembic migration 005 (RLS policies).


class TestUserScopedSemanticService:
    """Contract for the Phase 4 request-scoped wrapper."""

    @pytest.fixture
    def _client_with_two_users(self):
        """Chroma client seeded with two users' docs + one untagged row."""
        factory = MockChromaDBFactory()
        client = factory.get_client()
        col = client.get_or_create_collection(name="decisions")
        col.add(
            ids=["alice1", "bob1", "legacy"],
            documents=[
                "alice private note about cache",
                "bob private note about cache",
                "untagged legacy row about cache",
            ],
            metadatas=[
                {"source": "n1", "user_id": "user-alice"},
                {"source": "n2", "user_id": "user-bob"},
                {"source": "n3"},  # no user_id
            ],
        )
        return client

    def test_wrapper_binds_user_at_construction(
        self, _client_with_two_users, user_context
    ):
        """A wrapper constructed with alice's UserContext delegates to the
        inner service with that context — regardless of what's passed at
        call time."""
        from dataclasses import replace

        from audittrace.services.semantic import (
            ChromaSemanticService,
            UserScopedSemanticService,
        )

        inner = ChromaSemanticService(
            client=_client_with_two_users, default_collections=["decisions"]
        )
        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        wrapper = UserScopedSemanticService(inner=inner, user_context=alice)

        results = wrapper.search(user_context, "cache", k=10)
        contents = [d.page_content for d in results]
        assert any("alice" in c for c in contents)
        assert not any("bob" in c for c in contents)

    def test_wrapper_ignores_per_call_user_context(
        self, _client_with_two_users, user_context
    ):
        """Even if the caller passes an ADMIN context at call time, the
        wrapper overrides it with the bound (non-admin alice) context.
        This is the 'isolation by construction' property."""
        from dataclasses import replace

        from audittrace.services.semantic import (
            ChromaSemanticService,
            UserScopedSemanticService,
        )

        inner = ChromaSemanticService(
            client=_client_with_two_users, default_collections=["decisions"]
        )
        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        wrapper = UserScopedSemanticService(inner=inner, user_context=alice)

        # user_context fixture is the admin sentinel — wrapper must IGNORE it
        assert user_context.is_admin is True
        results = wrapper.search(user_context, "cache", k=10)
        # Still only alice's row — admin context at call time is discarded
        assert len(results) == 1
        assert "alice" in results[0].page_content

    def test_wrapper_admin_binding_bypasses_filter(
        self, _client_with_two_users, user_context
    ):
        """A wrapper BOUND with an admin UserContext bypasses the where
        filter entirely, mirroring Phase 2 admin semantics. The binding
        is the authority — it's just pinned at construction time instead
        of being decided per call."""
        from audittrace.services.semantic import (
            ChromaSemanticService,
            UserScopedSemanticService,
        )

        inner = ChromaSemanticService(
            client=_client_with_two_users, default_collections=["decisions"]
        )
        # user_context fixture is admin-by-construction (sentinel)
        wrapper = UserScopedSemanticService(inner=inner, user_context=user_context)

        results = wrapper.search(user_context, "cache", k=10)
        # Admin sees everything: alice + bob + untagged legacy
        assert len(results) == 3

    def test_wrapper_available_collections_delegates(
        self, _client_with_two_users, user_context
    ):
        """available_collections is a pass-through; it doesn't touch the
        where filter."""
        from audittrace.services.semantic import (
            ChromaSemanticService,
            UserScopedSemanticService,
        )

        inner = ChromaSemanticService(
            client=_client_with_two_users, default_collections=["decisions"]
        )
        wrapper = UserScopedSemanticService(inner=inner, user_context=user_context)
        assert wrapper.available_collections() == inner.available_collections()

    def test_wrapper_is_semantic_service(self, user_context):
        """The wrapper must implement SemanticService so context_builder
        can inject it transparently as a drop-in replacement."""
        from audittrace.services.semantic import (
            MockSemanticService,
            UserScopedSemanticService,
        )

        wrapper = UserScopedSemanticService(
            inner=MockSemanticService(), user_context=user_context
        )
        assert isinstance(wrapper, SemanticService)


# ── upsert / delete_document / get_document (PR A — CRUD backoffice) ────────


class TestChromaSemanticServiceCrud:
    """Write-side tests via the MockChromaDBFactory."""

    @pytest.fixture
    def service(self):
        factory = MockChromaDBFactory()
        client = factory.get_client()
        return ChromaSemanticService(client=client, default_collections=["decisions"])

    def test_upsert_then_get(self, service, user_context):
        service.upsert(
            user_context,
            "decisions",
            "doc-7",
            "hello world",
            metadata={"source": "ADR-007"},
        )
        doc = service.get_document(user_context, "decisions", "doc-7")
        assert doc is not None
        assert doc.page_content == "hello world"
        # User-id stamping happened (sentinel admin user_id).
        assert "user_id" in doc.metadata

    def test_upsert_replaces_existing(self, service, user_context):
        service.upsert(user_context, "decisions", "doc-7", "v1")
        service.upsert(user_context, "decisions", "doc-7", "v2")
        doc = service.get_document(user_context, "decisions", "doc-7")
        assert doc.page_content == "v2"

    def test_get_missing_returns_none(self, service, user_context):
        assert service.get_document(user_context, "decisions", "nope") is None

    def test_delete_existing_returns_true(self, service, user_context):
        service.upsert(user_context, "decisions", "doc-d", "bye")
        assert service.delete_document(user_context, "decisions", "doc-d") is True
        assert service.get_document(user_context, "decisions", "doc-d") is None

    def test_delete_missing_returns_false(self, service, user_context):
        assert service.delete_document(user_context, "decisions", "never") is False


class TestMockSemanticServiceCrud:
    """In-memory variant — used by the route tests."""

    def test_upsert_get_delete(self, user_context):
        s = MockSemanticService()
        s.upsert(user_context, "col", "id-1", "first", metadata={"k": "v"})
        d = s.get_document(user_context, "col", "id-1")
        assert d is not None and d.page_content == "first"
        # Replace
        s.upsert(user_context, "col", "id-1", "second")
        d = s.get_document(user_context, "col", "id-1")
        assert d.page_content == "second"
        # Delete
        assert s.delete_document(user_context, "col", "id-1") is True
        assert s.get_document(user_context, "col", "id-1") is None
        assert s.delete_document(user_context, "col", "id-1") is False


class TestUserScopedSemanticServiceCrud:
    """The wrapper must forward upsert/get/delete to the inner service
    using the bound user (not the per-call argument)."""

    def test_wrapper_forwards_upsert(self, user_context):
        from audittrace.services.semantic import UserScopedSemanticService

        inner = MockSemanticService()
        wrapper = UserScopedSemanticService(inner=inner, user_context=user_context)
        # Use a different user_context as the per-call arg; wrapper should
        # ignore it and use the bound one.
        other = replace(user_context, user_id="some-other-user")
        wrapper.upsert(other, "col", "id-1", "text")
        assert inner.get_document(user_context, "col", "id-1") is not None

    def test_wrapper_forwards_delete_and_get(self, user_context):
        from audittrace.services.semantic import UserScopedSemanticService

        inner = MockSemanticService()
        inner.upsert(user_context, "col", "id-1", "x")
        wrapper = UserScopedSemanticService(inner=inner, user_context=user_context)
        assert wrapper.get_document(user_context, "col", "id-1") is not None
        assert wrapper.delete_document(user_context, "col", "id-1") is True
