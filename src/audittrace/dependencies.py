"""Dependency injection for sovereign-memory-server.

ADR-020: PostgreSQL replaces SQLite. ChromaDB is server-mode only.
No file-based databases from this point forward.
"""

import logging
from pathlib import Path
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
    FileEpisodicService,
    MockEpisodicService,
    S3EpisodicService,
)
from audittrace.services.procedural import (
    FileProceduralService,
    MockProceduralService,
    ProceduralService,
    S3ProceduralService,
)

try:
    from minio import Minio
except ImportError:  # pragma: no cover - optional dep
    Minio = None

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


def _create_minio_client(settings: Settings) -> object | None:
    """Create a MinIO client if credentials are configured (ADR-027)."""
    if not settings.minio_secret_key:
        return None
    try:
        from urllib.parse import urlparse  # noqa: E402

        parsed = urlparse(settings.minio_url)
        endpoint = parsed.netloc or parsed.path
        secure = parsed.scheme == "https"
        client = Minio(
            endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=secure,
        )
        logger.info("MinIO client initialised — endpoint=%s", endpoint)
        return client  # type: ignore[no-any-return]
    except Exception as exc:
        logger.warning("MinIO client init failed, falling back to filesystem: %s", exc)
        return None


@log_call(logger=logger)
def _register_memory_services(settings: Settings, pg_factory: PostgresFactory) -> None:
    """Register all 4 memory layer services + context builder (ADR-018, ADR-027).

    When MinIO credentials are configured, S3-backed services replace the
    filesystem services for Layers 1+2. Otherwise falls back to File* services.
    """
    minio_client = _create_minio_client(settings)

    if minio_client is not None:
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
    else:
        episodic = FileEpisodicService(adr_dir=Path(settings.adr_dir))
        procedural = FileProceduralService(skill_dir=Path(settings.skill_dir))
        logger.info("Memory layers 1+2: filesystem-backed (fallback)")

    conversational = PostgresConversationalService(
        session_factory=pg_factory.get_session_factory(),
    )

    # Semantic service needs ChromaDB client — lazy-resolve via container
    chroma_client = container.get_instance("chromadb")
    semantic = ChromaSemanticService(
        client=chroma_client,
        default_collections=["decisions", "skills", "ai_research", "scm_coursework"],
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
    container._instances["context_builder"] = context_builder


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
    container._instances["context_builder"] = context_builder


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
    test_container._instances["context_builder"] = context_builder
    # In-memory PostgreSQL factory so persistence side-effects work in tests
    test_container._instances["postgres_factory"] = InMemoryPostgresFactory()
    return test_container
