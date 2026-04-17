"""Tests for dependency injection.

ADR-020: PostgreSQL-only path. No SQLite fallback.
"""

import pytest

from audittrace.db.factory import MockChromaDBFactory
from audittrace.dependencies import (
    DependencyContainer,
    create_test_container,
    register_default_dependencies,
    set_test_mode,
)


def test_dependency_container():
    """Test DependencyContainer basic operations."""
    container = DependencyContainer()

    # Register factory
    factory = MockChromaDBFactory()
    container.register_factory("test", factory)

    # Get factory
    retrieved_factory = container.get_factory("test")
    assert retrieved_factory is factory

    # Create instance
    instance = container.create_instance("test")
    assert instance is not None

    # Get cached instance
    cached_instance = container.get_instance("test")
    assert cached_instance is instance


def test_dependency_container_missing_factory():
    """Test that missing factory raises error."""
    container = DependencyContainer()

    with pytest.raises(ValueError, match="Factory not registered"):
        container.get_factory("nonexistent")


def test_test_container():
    """Test create_test_container helper."""
    test_container = create_test_container()

    # Should have mock factory registered
    assert "chromadb" in test_container._factories

    # Should create a mock client instance (not the factory itself)
    instance = test_container.create_instance("chromadb")
    assert instance is not None
    assert hasattr(instance, "get_or_create_collection")


def test_reset_container():
    """Test that clearing instances forces new client creation."""
    container = DependencyContainer()
    factory = MockChromaDBFactory()
    container.register_factory("test", factory)

    instance1 = container.create_instance("test")
    container._instances.clear()
    instance2 = container.create_instance("test")
    # MockChromaDBFactory.get_client returns a fresh _MockChromaDBClient each call
    assert instance1 is not instance2


def test_register_default_dependencies_no_pg(monkeypatch):
    """Test default registration with no database_url falls back to in-memory PG.

    Uses MockChromaDBFactory to avoid needing a live ChromaDB server.
    """
    from audittrace import dependencies as deps_module
    from audittrace.config import Settings
    from audittrace.db.postgres import InMemoryPostgresFactory

    # Patch the factory constructor to avoid real ChromaDB connection
    monkeypatch.setattr(
        deps_module,
        "HTTPChromaDBFactory",
        lambda url, token=None: MockChromaDBFactory(),
    )

    settings = Settings(chroma_url="http://localhost:8000")
    register_default_dependencies(settings)
    assert "chromadb" in deps_module.container._factories
    assert "postgres_factory" in deps_module.container._instances
    assert isinstance(
        deps_module.container._instances["postgres_factory"],
        InMemoryPostgresFactory,
    )


def test_set_test_mode():
    """Test setting test mode registers a mock factory on the global container."""
    from audittrace import dependencies as deps_module

    set_test_mode()
    factory = deps_module.container.get_factory("chromadb")
    assert isinstance(factory, MockChromaDBFactory)


# ───────────────── DESIGN §16 Phase 4 — per-request wrapper wiring ──────────
# get_context_builder(user) is a per-request FastAPI dependency that wraps
# the shared semantic service in a UserScopedSemanticService bound to the
# caller's UserContext. The wrapper's "isolation by construction" property
# then applies at the chat path.


class TestPerRequestContextBuilder:
    def test_context_builder_wraps_semantic_with_user_scope(
        self, test_container, user_context
    ):
        """Phase 4 follow-up: get_context_builder(user) must return a
        DefaultContextBuilder whose semantic layer is a
        UserScopedSemanticService bound to the request's user."""
        from audittrace import dependencies as deps_module
        from audittrace.dependencies import get_context_builder
        from audittrace.services.context_builder import DefaultContextBuilder
        from audittrace.services.semantic import UserScopedSemanticService

        # Swap global container so the dependency reads test services.
        deps_module.container = test_container
        builder = get_context_builder(user_context)

        assert isinstance(builder, DefaultContextBuilder)
        # Inner semantic is the wrapper, not the raw mock.
        assert isinstance(builder._semantic, UserScopedSemanticService)
        # Wrapper is bound to the caller's user_context identity.
        assert builder._semantic._bound_user.user_id == user_context.user_id

    def test_context_builder_episodic_procedural_conversational_unchanged(
        self, test_container, user_context
    ):
        """The wrapper is applied only to the semantic layer; the other
        three services are shared singletons from the container."""
        from audittrace import dependencies as deps_module
        from audittrace.dependencies import get_context_builder

        deps_module.container = test_container
        builder = get_context_builder(user_context)

        # These must be IS-identical to the shared container instances.
        assert builder._episodic is test_container._instances["episodic"]
        assert builder._procedural is test_container._instances["procedural"]
        assert builder._conversational is test_container._instances["conversational"]

    def test_two_users_get_distinct_wrappers(self, test_container, user_context):
        """Two different users hitting the endpoint in the same session
        get two distinct wrapper instances, each bound to the right
        identity."""
        from dataclasses import replace

        from audittrace import dependencies as deps_module
        from audittrace.dependencies import get_context_builder

        deps_module.container = test_container
        alice = replace(user_context, user_id="user-alice", is_admin=False)
        bob = replace(user_context, user_id="user-bob", is_admin=False)

        builder_a = get_context_builder(alice)
        builder_b = get_context_builder(bob)

        assert builder_a is not builder_b
        assert builder_a._semantic._bound_user.user_id == "user-alice"
        assert builder_b._semantic._bound_user.user_id == "user-bob"
