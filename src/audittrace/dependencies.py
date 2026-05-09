"""Dependency injection for audittrace-server.

ADR-020: PostgreSQL replaces SQLite. ChromaDB is server-mode only.
No file-based databases from this point forward.
"""

import logging
from typing import Any, cast

from fastapi import Depends

from audittrace.auth import require_user
from audittrace.config import Settings, get_settings
from audittrace.db.factory import (
    ChromaDBFactory,
    HTTPChromaDBFactory,
    MockChromaDBFactory,
)
from audittrace.db.postgres import (
    InMemoryPostgresFactory,
    PostgresFactory,
    URLPostgresFactory,
)
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.services.context_builder import (
    ContextBuilderService,
    DefaultContextBuilder,
)
from audittrace.services.conversational import (
    ConversationalService,
    MockConversationalService,
    PostgresConversationalService,
)
from audittrace.services.episodic import (
    EpisodicService,
    MockEpisodicService,
    S3EpisodicService,
)
from audittrace.services.memory_manifest import (
    MemoryManifestService,
    MockMemoryManifestService,
)
from audittrace.services.procedural import (
    MockProceduralService,
    ProceduralService,
    S3ProceduralService,
)
from audittrace.services.trust_store import (
    CompositeTrustStoreBuilder,
    EuLotlTrustStoreBuilder,
    MockTrustStoreProvider,
    S3TrustStoreProvider,
    StaticTrustStoreBuilder,
    SwissTslTrustStoreBuilder,
    TrustStoreBuilder,
    TrustStoreProvider,
)

try:
    from minio import Minio
except ImportError:  # pragma: no cover - optional dep
    # The suppression below is required because mypy sees `Minio` as
    # `type[Minio]` from the try-branch import; the None fallback is
    # intentional for the optional-dep import-failure case (which only
    # happens in degenerate build environments — minio is a real
    # runtime dep).
    Minio = None  # type: ignore[assignment, misc]

from audittrace.services.semantic import (
    ChromaSemanticService,
    MockSemanticService,
    SemanticService,
    UserScopedSemanticService,
)

logger = logging.getLogger(__name__)


class DependencyContainer:
    """Container for dependency injection."""

    def __init__(self) -> None:
        self._factories: dict[str, ChromaDBFactory] = {}
        self._instances: dict[str, Any] = {}

    @log_call(logger=logger)
    def register_factory(self, name: str, factory: ChromaDBFactory) -> None:
        self._factories[name] = factory

    @log_call(logger=logger)
    def get_factory(self, name: str) -> ChromaDBFactory:
        if name not in self._factories:
            raise ValueError(f"Factory not registered: {name}")
        return self._factories[name]

    @log_call(logger=logger)
    def create_instance(self, name: str) -> Any:
        factory = self.get_factory(name)
        instance = factory.get_client()
        self._instances[name] = instance
        return instance

    @log_call(logger=logger)
    def get_instance(self, name: str) -> Any:
        if name not in self._instances:
            self._instances[name] = self.create_instance(name)
        return self._instances[name]


# Global dependency container
container = DependencyContainer()


@log_call(logger=logger)
def register_default_dependencies(settings: Settings | None = None) -> None:
    """Register default dependencies based on configuration.

    Skips registration if the container already has services (test mode).
    """
    if container._instances:
        logger.debug("Container already populated — skipping registration (test mode)")
        return

    if settings is None:
        settings = get_settings()

    # ChromaDB — server mode with optional token auth (ADR-020)
    container.register_factory(
        "chromadb",
        HTTPChromaDBFactory(settings.chroma_url, token=settings.chroma_token),
    )

    # PostgreSQL factory — real DB required in local/production
    if settings.database_url:
        pg_factory: PostgresFactory = URLPostgresFactory(settings.database_url)
    elif settings.env == "test":
        logger.info("Test environment — using in-memory database")
        pg_factory = InMemoryPostgresFactory()
    else:
        raise RuntimeError(
            f"AUDITTRACE_ENV={settings.env} requires a database. "
            "Set AUDITTRACE_POSTGRES_PASSWORD or AUDITTRACE_POSTGRES_URL."
        )
    container._instances["postgres_factory"] = pg_factory

    _register_memory_services(settings, pg_factory)


def _create_minio_client(settings: Settings) -> object:
    """Create a MinIO client. Required — there is no FS fallback (ADR-027,
    ``feedback_storage_always_s3``).

    Wraps the bare ``Minio`` instance in
    ``QuarantineDenyingMinioClient`` (ADR-048 PR-B2) which refuses
    ``get_object`` calls whose key starts with ``quarantine/``. This
    is the application-layer half of the trust-boundary enforcement;
    PR-B7 lands the MinIO IAM split that puts ``Effect: Deny`` on
    ``quarantine/*`` for the ``audittrace_app`` role at the
    bucket-policy layer. Two layers of enforcement on the same
    invariant (defense in depth).

    Raises ``RuntimeError`` if MinIO is not configured (missing secret key)
    or the client fails to initialise. Layers 1 + 2 are S3-only by design;
    a missing client is a startup-time error, not a silent degradation.
    """
    if not settings.minio_secret_key:
        raise RuntimeError(
            "MinIO is required for episodic + procedural memory layers. "
            "Set AUDITTRACE_MINIO_SECRET_KEY (and AUDITTRACE_MINIO_ACCESS_KEY / "
            "AUDITTRACE_MINIO_URL). Filesystem fallback removed in 2026-05-03 "
            "stabilization sweep — see feedback_storage_always_s3."
        )
    try:
        from urllib.parse import urlparse  # noqa: E402

        from audittrace.services.quarantine_guard import (  # noqa: E402
            QuarantineDenyingMinioClient,
        )

        parsed = urlparse(settings.minio_url)
        endpoint = parsed.netloc or parsed.path
        secure = parsed.scheme == "https"
        bare_client = Minio(
            endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=secure,
        )
        client = QuarantineDenyingMinioClient(bare_client)
        logger.info(
            "MinIO client initialised — endpoint=%s, quarantine-deny=%s",
            endpoint,
            client.quarantine_prefix,
        )
        return client
    except Exception as exc:
        raise RuntimeError(
            f"MinIO client initialisation failed: {exc}. "
            "Layers 1+2 are S3-only — no filesystem fallback."
        ) from exc


@log_call(logger=logger)
def _register_memory_services(settings: Settings, pg_factory: PostgresFactory) -> None:
    """Register all 4 memory layer services + context builder (ADR-018, ADR-027).

    Layers 1 + 2 are **always S3-backed** (MinIO). There is no filesystem
    fallback — see ``feedback_storage_always_s3``.
    """
    minio_client = _create_minio_client(settings)

    episodic: EpisodicService = S3EpisodicService(
        minio_client=minio_client,
        bucket=settings.minio_shared_bucket,
        prefix="episodic/",
    )
    procedural: ProceduralService = S3ProceduralService(
        minio_client=minio_client,
        bucket=settings.minio_shared_bucket,
        prefix="procedural/",
    )
    logger.info("Memory layers 1+2: S3-backed (MinIO)")

    conversational = PostgresConversationalService(
        session_factory=pg_factory.get_session_factory(),
    )

    # Semantic service needs ChromaDB client — lazy-resolve via container
    chroma_client = container.get_instance("chromadb")
    semantic = ChromaSemanticService(
        client=chroma_client,
        default_collections=[
            "decisions",
            "skills",
            "ai_research",  # legacy index-chromadb.py corpus (host-side script)
            "ai_research_papers",  # /memory/index?collections=ai_research_papers (PDF corpus, ADR-047 path)
            "scm_coursework",
        ],
    )

    # Memory-layer manifest (CRUD backoffice — migration 009 + the
    # /memory/<layer> REST endpoints). Postgres-backed; same session
    # factory as conversational since the table is in the same DB.
    memory_manifest = MemoryManifestService(
        session_factory=pg_factory.get_session_factory(),
    )

    context_builder = DefaultContextBuilder(
        episodic=episodic,
        procedural=procedural,
        conversational=conversational,
        semantic=semantic,
    )

    container._instances["episodic"] = episodic
    container._instances["procedural"] = procedural
    container._instances["conversational"] = conversational
    container._instances["semantic"] = semantic
    container._instances["memory_manifest"] = memory_manifest
    container._instances["context_builder"] = context_builder

    # PAdES trust store (ADR-052) — Provider/Builder pair, settings-
    # selected. The Provider is the storage layer (where the PEM
    # lives — default S3/MinIO); the Builder is the sourcing layer
    # (where the PEM comes from — default EU LOTL via pyhanko[etsi]).
    trust_store_provider: TrustStoreProvider
    if settings.pdf_trust_store_provider == "s3":
        trust_store_provider = S3TrustStoreProvider(
            minio_client=minio_client,
            bucket=settings.minio_shared_bucket,
            pem_key=settings.pdf_trust_store_s3_key,
        )
    elif settings.pdf_trust_store_provider == "file":
        # Pre-ADR-052 backwards-compat: when the operator points
        # AUDITTRACE_PDF_SIGNATURE_TRUST_STORE at a Vault-Agent-mounted
        # PEM file, the validator picks that up directly — the
        # Provider is effectively bypassed. We register the Mock
        # so DI shape is uniform; load() raises FileNotFoundError
        # when called, which the validator handles by falling back
        # to certifi.
        trust_store_provider = MockTrustStoreProvider()
    else:
        raise RuntimeError(
            f"Unknown AUDITTRACE_PDF_TRUST_STORE_PROVIDER="
            f"{settings.pdf_trust_store_provider!r}; expected one of "
            f"'s3', 'file' (per ADR-052 §2)."
        )

    trust_store_builder: TrustStoreBuilder
    builder_kind = settings.pdf_trust_store_builder
    if builder_kind == "composite":
        # ADR-053 — compose multiple jurisdictional builders so the
        # bundle covers EU + CH (and any future programs) in one
        # refresh cycle.
        names = [
            n.strip()
            for n in settings.pdf_trust_store_composite_builders.split(",")
            if n.strip()
        ]
        if not names:
            raise RuntimeError(
                "AUDITTRACE_PDF_TRUST_STORE_BUILDER=composite requires "
                "AUDITTRACE_PDF_TRUST_STORE_COMPOSITE_BUILDERS to be a "
                "non-empty comma-separated list (e.g. 'eu_lotl,swiss_tsl')."
            )
        inner_builders: list[TrustStoreBuilder] = [
            _build_inner_builder(n, settings) for n in names
        ]
        trust_store_builder = CompositeTrustStoreBuilder(inner_builders)
    else:
        trust_store_builder = _build_inner_builder(builder_kind, settings)

    container._instances["trust_store_provider"] = trust_store_provider
    container._instances["trust_store_builder"] = trust_store_builder


def _build_inner_builder(name: str, settings: Settings) -> TrustStoreBuilder:
    """Resolve a single builder name to a TrustStoreBuilder instance.
    Used both directly (when ``pdf_trust_store_builder`` is a single
    name) and from the composite resolution path (ADR-053)."""
    if name == "eu_lotl":
        return EuLotlTrustStoreBuilder()
    if name == "swiss_tsl":
        return SwissTslTrustStoreBuilder(
            tslo_cert_path=settings.pdf_trust_store_swiss_tslo_cert_path
        )
    if name == "static":
        if not settings.pdf_trust_store_static_dir:
            raise RuntimeError(
                "trust-store builder 'static' requires "
                "AUDITTRACE_PDF_TRUST_STORE_STATIC_DIR to be set "
                "(directory of operator-supplied .pem/.crt files)."
            )
        return StaticTrustStoreBuilder(settings.pdf_trust_store_static_dir)
    raise RuntimeError(
        f"Unknown trust-store builder name: {name!r}. Expected one of "
        f"{{'eu_lotl', 'swiss_tsl', 'static'}} (per ADR-052 §3 + ADR-053)."
    )


@log_call(logger=logger)
def get_chromadb() -> Any:
    """Get ChromaDB client instance (dependency injection)."""
    return cast(Any, container.get_instance("chromadb"))


@log_call(logger=logger)
def get_chromadb_factory() -> ChromaDBFactory:
    return cast(ChromaDBFactory, container.get_factory("chromadb"))


@log_call(logger=logger)
def get_postgres_factory() -> PostgresFactory:
    """Get PostgreSQL factory (dependency injection)."""
    return cast(PostgresFactory, container._instances["postgres_factory"])


@log_call(logger=logger)
def get_context_builder(
    user: UserContext = Depends(require_user),
) -> ContextBuilderService:
    """Return a per-request context builder with a user-scoped semantic layer.

    DESIGN §16 Phase 4 follow-up. The episodic, procedural and
    conversational services are shared singletons (their per-user
    isolation is already enforced at the service layer via the Phase 2
    `user_context` plumbing + Postgres RLS from migration 005). The
    semantic service is the one layer that needs the extra
    ``UserScopedSemanticService`` wrapper: ChromaDB has no native RLS
    equivalent, and the wrapper enforces the isolation property by
    construction — the bound UserContext cannot be overridden by the
    per-call argument, so a bug that leaks an admin context elsewhere
    in the code cannot bypass the filter.

    One new ``DefaultContextBuilder`` instance is built per request.
    The three shared services are referenced by identity (no copy);
    only the semantic slot gets a fresh wrapper.
    """
    episodic = container._instances["episodic"]
    procedural = container._instances["procedural"]
    conversational = container._instances["conversational"]
    shared_semantic = container._instances["semantic"]

    scoped_semantic = UserScopedSemanticService(
        inner=shared_semantic, user_context=user
    )
    return DefaultContextBuilder(
        episodic=episodic,
        procedural=procedural,
        conversational=conversational,
        semantic=scoped_semantic,
    )


@log_call(logger=logger)
def get_episodic_service() -> EpisodicService:
    """Get episodic memory service (dependency injection)."""
    return cast(EpisodicService, container._instances["episodic"])


@log_call(logger=logger)
def get_conversational_service() -> ConversationalService:
    """Get conversational memory service (dependency injection)."""
    return cast(ConversationalService, container._instances["conversational"])


@log_call(logger=logger)
def get_procedural_service() -> ProceduralService:
    """Get procedural memory service (dependency injection). Added by
    ADR-025 Phase 2 so the ``recall_skills`` memory tool handler can
    resolve the service without touching container internals."""
    return cast(ProceduralService, container._instances["procedural"])


@log_call(logger=logger)
def get_semantic_service() -> SemanticService:
    """Get semantic memory service (dependency injection). Added by
    ADR-025 Phase 2 so the ``recall_semantic`` memory tool handler can
    resolve the service without touching container internals."""
    return cast(SemanticService, container._instances["semantic"])


@log_call(logger=logger)
def get_memory_manifest_service() -> MemoryManifestService:
    """Get memory-layer manifest service (CRUD backoffice — migration 009).
    Backs the /memory/<layer> REST endpoints' authorship + timestamps +
    soft-delete bookkeeping."""
    return cast(MemoryManifestService, container._instances["memory_manifest"])


@log_call(logger=logger)
def get_trust_store_provider() -> TrustStoreProvider:
    """Get the PAdES trust-store Provider (ADR-052 §2). Default
    ``S3TrustStoreProvider`` reads/writes a PEM bundle from MinIO at
    the configured ``pdf_trust_store_s3_key``. Tests use
    ``MockTrustStoreProvider``."""
    return cast(TrustStoreProvider, container._instances["trust_store_provider"])


@log_call(logger=logger)
def get_trust_store_builder() -> TrustStoreBuilder:
    """Get the PAdES trust-store Builder (ADR-052 §3). Default
    ``EuLotlTrustStoreBuilder`` walks the EU LOTL via pyhanko[etsi].
    ``StaticTrustStoreBuilder`` (operator-supplied PEM directory) is
    the offline alternative."""
    return cast(TrustStoreBuilder, container._instances["trust_store_builder"])


@log_call(logger=logger)
def set_test_mode() -> None:
    """Set container to test mode with mock dependencies."""
    container.register_factory("chromadb", MockChromaDBFactory())
    container._instances.clear()
    _register_mock_memory_services()


@log_call(logger=logger)
def _register_mock_memory_services() -> None:
    """Register mock memory services for testing (ADR-018)."""
    episodic = MockEpisodicService()
    procedural = MockProceduralService()
    conversational = MockConversationalService()
    semantic = MockSemanticService()
    memory_manifest = MockMemoryManifestService()
    context_builder = DefaultContextBuilder(
        episodic=episodic,
        procedural=procedural,
        conversational=conversational,
        semantic=semantic,
    )
    container._instances["episodic"] = episodic
    container._instances["procedural"] = procedural
    container._instances["conversational"] = conversational
    container._instances["semantic"] = semantic
    container._instances["memory_manifest"] = memory_manifest
    container._instances["context_builder"] = context_builder
    # ADR-052 — Mock trust store + Static builder pointed at a
    # non-existent dir for unit-test isolation (integration tests
    # override these with explicit fixtures).
    container._instances["trust_store_provider"] = MockTrustStoreProvider()
    container._instances["trust_store_builder"] = StaticTrustStoreBuilder(
        directory="/tmp/audittrace-test-trust-store-empty"
    )


@log_call(logger=logger)
def reset_container() -> None:
    """Reset container state (useful for tests)."""
    container._factories.clear()
    container._instances.clear()


def create_test_container() -> DependencyContainer:
    """Create a fresh test container with mock dependencies."""
    test_container = DependencyContainer()
    test_container.register_factory("chromadb", MockChromaDBFactory())
    # Register mock memory services on the test container
    episodic = MockEpisodicService()
    procedural = MockProceduralService()
    conversational = MockConversationalService()
    semantic = MockSemanticService()
    memory_manifest = MockMemoryManifestService()
    context_builder = DefaultContextBuilder(
        episodic=episodic,
        procedural=procedural,
        conversational=conversational,
        semantic=semantic,
    )
    test_container._instances["episodic"] = episodic
    test_container._instances["procedural"] = procedural
    test_container._instances["conversational"] = conversational
    test_container._instances["semantic"] = semantic
    test_container._instances["memory_manifest"] = memory_manifest
    test_container._instances["context_builder"] = context_builder
    # ADR-052 — Mock trust store + Static builder pointed at a
    # non-existent dir (tests that exercise the refresh path
    # override these with explicit fixtures).
    test_container._instances["trust_store_provider"] = MockTrustStoreProvider()
    test_container._instances["trust_store_builder"] = StaticTrustStoreBuilder(
        directory="/tmp/audittrace-test-trust-store-empty"
    )
    # In-memory PostgreSQL factory so persistence side-effects work in tests
    test_container._instances["postgres_factory"] = InMemoryPostgresFactory()
    return test_container
