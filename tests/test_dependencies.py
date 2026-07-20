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

    Uses MockChromaDBFactory to avoid needing a live ChromaDB server. MinIO
    is mandatory since the 2026-05-03 sweep, so this test stubs the client
    creation with a fake — the FS fallback no longer exists.
    """
    from audittrace import dependencies as deps_module
    from audittrace.config import Settings
    from audittrace.db.postgres import InMemoryPostgresFactory

    monkeypatch.setattr(
        deps_module,
        "HTTPChromaDBFactory",
        lambda url, token=None: MockChromaDBFactory(),
    )
    # Object storage is mandatory (ADR-006 + ADR-027); stub the factory
    # so this test doesn't need a real backend. Patch the canonical name
    # AND the back-compat alias to be safe.
    monkeypatch.setattr(
        deps_module,
        "_create_object_storage_provider",
        lambda settings: object(),
    )
    monkeypatch.setattr(deps_module, "_create_minio_client", lambda settings: object())

    settings = Settings(chroma_url="http://localhost:8000")
    register_default_dependencies(settings)
    assert "chromadb" in deps_module.container._factories
    assert "postgres_factory" in deps_module.container._instances
    assert isinstance(
        deps_module.container._instances["postgres_factory"],
        InMemoryPostgresFactory,
    )


def test_register_default_dependencies_raises_when_minio_missing(monkeypatch):
    """The 2026-05-03 sweep removed the FS fallback for layers 1+2.
    A missing ``AUDITTRACE_MINIO_SECRET_KEY`` MUST surface as a startup-time
    ``RuntimeError`` instead of a silent filesystem fallback. See
    ``feedback_storage_always_s3``.
    """
    from audittrace import dependencies as deps_module
    from audittrace.config import Settings

    monkeypatch.setattr(
        deps_module,
        "HTTPChromaDBFactory",
        lambda url, token=None: MockChromaDBFactory(),
    )
    # Reset container so we exercise the registration path, not the cached
    # instances guard at the top of register_default_dependencies.
    deps_module.container = deps_module.DependencyContainer()

    settings = Settings(chroma_url="http://localhost:8000", minio_secret_key="")
    with pytest.raises(RuntimeError, match="AUDITTRACE_MINIO_SECRET_KEY"):
        register_default_dependencies(settings)


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


# ── Trust-store DI selection paths (#366 branch coverage) ──────────────────
# These branches are pure operator-configuration handling: which storage
# Provider and which sourcing Builder get wired at startup (ADR-052 §2/§3,
# ADR-053). Every untested outcome here was an error path — the ones that
# turn a typo in an env var into a clear message instead of a confusing
# failure three layers down. `_build_inner_builder`'s 'static' arm and the
# 'file' Provider arm had NO coverage at all before this.


def _bootable_settings(**overrides):
    """Settings valid enough for register_default_dependencies to reach the
    trust-store wiring at the end of the function.

    Object-storage credentials are validated FIRST and fail fast when empty
    (feedback_storage_always_s3 — the filesystem fallback was removed), so a
    bare Settings() never gets as far as the Provider/Builder selection.
    """
    from audittrace.config import Settings

    base = dict(env="test", minio_secret_key="test-secret")
    base.update(overrides)
    return Settings(**base)


class TestTrustStoreProviderSelection:
    """AUDITTRACE_PDF_TRUST_STORE_PROVIDER — the storage layer."""

    def test_file_provider_registers_mock_so_di_shape_stays_uniform(self) -> None:
        """`provider=file` is the pre-ADR-052 backwards-compat path.

        The operator points the validator at a Vault-Agent-mounted PEM, so
        the Provider is bypassed — but DI still needs *something* bound or
        every downstream lookup has to special-case a missing key. It
        registers a Mock whose load() raises, and the validator falls back
        to certifi. Had zero coverage before.
        """
        from audittrace.dependencies import _build_trust_store_provider
        from audittrace.services.trust_store import MockTrustStoreProvider

        provider = _build_trust_store_provider(
            _bootable_settings(pdf_trust_store_provider="file"),
            object_store=None,
            bucket="irrelevant",
        )
        assert isinstance(provider, MockTrustStoreProvider)

    def test_unknown_provider_fails_fast_with_the_allowed_values(self) -> None:
        """A typo must fail at startup naming the valid options.

        The alternative is a server that boots and then fails on the first
        signed-PDF upload, which is a far worse place to discover a typo.
        """

        from audittrace.dependencies import _build_trust_store_provider

        with pytest.raises(RuntimeError) as exc:
            _build_trust_store_provider(
                _bootable_settings(pdf_trust_store_provider="gcs"),
                object_store=None,
                bucket="irrelevant",
            )
        msg = str(exc.value)
        assert "gcs" in msg, "error must echo the offending value"
        assert "'s3'" in msg and "'file'" in msg, "error must list valid options"


class TestTrustStoreBuilderSelection:
    """AUDITTRACE_PDF_TRUST_STORE_BUILDER — the sourcing layer (ADR-053)."""

    def test_composite_with_blank_list_fails_fast(self) -> None:
        """composite + empty list would build a bundle with zero roots.

        That silently trusts nothing, so every signature validates as
        `signed_untrusted` — a taxonomy-wide false negative. Fail at boot.
        """

        from audittrace.dependencies import _build_trust_store_builder

        with pytest.raises(RuntimeError) as exc:
            _build_trust_store_builder(
                _bootable_settings(
                    pdf_trust_store_builder="composite",
                    pdf_trust_store_composite_builders="  ,  ,",
                )
            )
        assert "non-empty" in str(exc.value)

    def test_static_builder_requires_its_directory(self) -> None:
        """'static' without a directory has nothing to read.

        Same failure class as the blank composite list: it would produce an
        empty bundle rather than an error.
        """
        from audittrace.config import Settings
        from audittrace.dependencies import _build_inner_builder

        settings = Settings(pdf_trust_store_static_dir="")
        with pytest.raises(RuntimeError) as exc:
            _build_inner_builder("static", settings)
        assert "STATIC_DIR" in str(exc.value)

    def test_static_builder_is_constructed_when_directory_is_set(self) -> None:
        """The happy arm of the same branch — operator-supplied roots."""
        from audittrace.config import Settings
        from audittrace.dependencies import _build_inner_builder
        from audittrace.services.trust_store import StaticTrustStoreBuilder

        settings = Settings(pdf_trust_store_static_dir="/etc/roots")
        builder = _build_inner_builder("static", settings)
        assert isinstance(builder, StaticTrustStoreBuilder)

    def test_unknown_builder_name_lists_the_valid_set(self) -> None:
        from audittrace.config import Settings
        from audittrace.dependencies import _build_inner_builder

        with pytest.raises(RuntimeError) as exc:
            _build_inner_builder("uk_tsl", Settings())
        msg = str(exc.value)
        assert "uk_tsl" in msg
        assert "eu_lotl" in msg and "swiss_tsl" in msg and "static" in msg


# ── Remaining bootstrap branches (#366) ────────────────────────────────────
# Startup-path decisions: which Postgres factory, which object-storage
# backend, which trust-store builder. Every one of these is a place where a
# misconfigured deployment should fail loudly at boot rather than degrade
# silently once traffic arrives.


class TestContainerCoroutineResolution:
    """``create_instance`` must handle BOTH sync and async factories.

    ChromaDB factories are ``async def get_client`` (#263) while others are
    plain sync. The container API is synchronous, so it resolves coroutines
    itself — but it must not try to "resolve" a value that is already
    concrete, or every sync factory would break.
    """

    def test_sync_factory_result_is_stored_as_is(self) -> None:
        class _SyncFactory:
            def get_client(self):
                return {"kind": "sync-client"}

        container = DependencyContainer()
        container.register_factory("thing", _SyncFactory())
        assert container.create_instance("thing") == {"kind": "sync-client"}

    def test_async_factory_result_is_awaited_before_storing(self) -> None:
        class _AsyncFactory:
            async def get_client(self):
                return {"kind": "async-client"}

        container = DependencyContainer()
        container.register_factory("thing", _AsyncFactory())
        got = container.create_instance("thing")
        # A coroutine leaking through here would be stored and later awaited
        # by an unsuspecting sync caller -> "coroutine was never awaited".
        assert got == {"kind": "async-client"}
        assert container._instances["thing"] == {"kind": "async-client"}


class TestPostgresFactorySelection:
    """AUDITTRACE_POSTGRES_URL vs env=test vs neither."""

    @staticmethod
    def _register(monkeypatch, settings):
        """Register up to the PG selection, stubbing the networked tail.

        ``_register_memory_services`` builds ChromaDB/embedder clients and
        talks to them; the factory choice under test happens before it.
        """
        from audittrace import dependencies as deps

        monkeypatch.setattr(deps, "_register_memory_services", lambda *a, **k: None)
        deps.container = DependencyContainer()
        deps.register_default_dependencies(settings)
        return deps.container

    def test_explicit_database_url_wins(self, monkeypatch) -> None:
        from audittrace.db.postgres import URLPostgresFactory

        c = self._register(
            monkeypatch,
            # database_url is DERIVED from postgres_url (config.py:485),
            # so the URL has to be supplied through the real field.
            _bootable_settings(postgres_url="postgresql://u:p@h/db"),
        )
        assert isinstance(c._instances["postgres_factory"], URLPostgresFactory)

    def test_test_env_without_url_falls_back_to_in_memory(self, monkeypatch) -> None:
        from audittrace.db.postgres import InMemoryPostgresFactory

        c = self._register(monkeypatch, _bootable_settings(env="test", postgres_url=""))
        assert isinstance(c._instances["postgres_factory"], InMemoryPostgresFactory)

    def test_non_test_env_without_url_refuses_to_boot(self, monkeypatch) -> None:
        """No silent SQLite fallback (ADR-020).

        Booting a production pod against an in-memory DB would accept writes
        and lose every audit row on restart — the worst possible failure for
        a recorder. It must refuse instead.
        """
        with pytest.raises(RuntimeError) as exc:
            self._register(
                monkeypatch, _bootable_settings(env="production", postgres_url="")
            )
        assert "requires a database" in str(exc.value)


class TestObjectStorageBackendSelection:
    """AUDITTRACE_OBJECT_STORAGE_BACKEND — minio | aws, nothing else."""

    def test_aws_backend_requires_region_and_bucket(self) -> None:
        """IRSA supplies credentials, but never the region/bucket.

        Missing either yields a provider that cannot address anything; the
        failure would surface as a confusing 500 on the first upload.
        """
        from audittrace.config import Settings
        from audittrace.dependencies import _create_object_storage_provider

        with pytest.raises(RuntimeError) as exc:
            _create_object_storage_provider(
                Settings(object_storage_backend="aws", aws_region="", aws_bucket="")
            )
        assert "AWS_REGION" in str(exc.value) or "AWS_BUCKET" in str(exc.value)

    def test_unknown_backend_names_the_valid_set(self) -> None:
        from audittrace.config import Settings
        from audittrace.dependencies import _create_object_storage_provider

        with pytest.raises(RuntimeError) as exc:
            _create_object_storage_provider(
                Settings(object_storage_backend="azure-blob")
            )
        msg = str(exc.value)
        assert "azure-blob" in msg
        assert "minio" in msg and "aws" in msg


class TestCompositeBuilderHappyPath:
    def test_composite_wraps_each_named_inner_builder(self) -> None:
        """ADR-053: one refresh must cover EU + CH together.

        If the composite silently collapsed to a single builder, the bundle
        would cover one jurisdiction and every signature from the other would
        validate as `signed_untrusted`.
        """
        from audittrace.dependencies import _build_trust_store_builder
        from audittrace.services.trust_store import (
            CompositeTrustStoreBuilder,
            EuLotlTrustStoreBuilder,
            SwissTslTrustStoreBuilder,
        )

        builder = _build_trust_store_builder(
            _bootable_settings(
                pdf_trust_store_builder="composite",
                pdf_trust_store_composite_builders="eu_lotl,swiss_tsl",
            )
        )
        assert isinstance(builder, CompositeTrustStoreBuilder)
        assert [type(b) for b in builder._inner] == [
            EuLotlTrustStoreBuilder,
            SwissTslTrustStoreBuilder,
        ]

    def test_single_named_builder_skips_the_composite_wrapper(self) -> None:
        from audittrace.dependencies import _build_trust_store_builder
        from audittrace.services.trust_store import EuLotlTrustStoreBuilder

        builder = _build_trust_store_builder(
            _bootable_settings(pdf_trust_store_builder="eu_lotl")
        )
        assert isinstance(builder, EuLotlTrustStoreBuilder)
