"""Dependency injection for audittrace-server.

ADR-020: PostgreSQL replaces SQLite. ChromaDB is server-mode only.
No file-based databases from this point forward.
"""

import asyncio
import inspect
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

# ADR-006 — direct ``minio.Minio`` import removed; the bare client is
# constructed inside the shared ``audittrace_object_storage`` package
# (MinIOObjectStorageProvider). dependencies.py only orchestrates the
# factory call.
from audittrace.services.semantic import (
    ChromaSemanticService,
    MockSemanticService,
    SemanticService,
    UserScopedSemanticService,
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

logger = logging.getLogger(__name__)


def _resolve_coroutine(coro: Any) -> Any:
    """Run an awaitable to completion from synchronous code.

    The DI container's ``create_instance`` is synchronous but the ChromaDB
    factories are ``async def`` (#263). With no running loop (test-container
    build) use ``asyncio.run``; if a loop IS running (FastAPI lifespan)
    offload to a fresh loop on a worker thread so the active loop isn't
    blocked.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


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
        # #263: ChromaDB factories are ``async def get_client``. Resolve the
        # coroutine to a concrete client so the synchronous container API
        # keeps returning a usable instance (callers run before/outside the
        # request event loop).
        if inspect.iscoroutine(instance):
            instance = _resolve_coroutine(instance)
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


def _create_object_storage_provider(settings: Settings) -> object:
    """Create the object-storage provider for episodic + procedural layers.

    Reads ``settings.object_storage_backend`` to dispatch to:

    - **MinIO** (default): ``settings.minio_*`` fields. ``minio_secret_key``
      MUST be non-empty (no filesystem fallback —
      ``feedback_storage_always_s3``, 2026-05-03).
    - **AWS S3**: ``settings.aws_region`` + ``aws_bucket`` required;
      ``aws_use_irsa=True`` (default) means boto3 picks up
      ``AWS_ROLE_ARN`` + ``AWS_WEB_IDENTITY_TOKEN_FILE`` from the EKS
      pod-identity webhook (no key plumbing).

    Wraps the resulting provider in
    :class:`QuarantineDenyingObjectStorageClient` (ADR-048 PR-B2) which
    refuses ``get_object`` calls whose key starts with ``quarantine/``.
    Application-layer half of the trust-boundary enforcement; the
    AWS-IAM / MinIO-IAM bucket-policy layer is the second half (PR-B7
    precedent).

    Raises :class:`RuntimeError` on misconfiguration. Startup-time
    failure is intentional — silent degradation would let a misrouted
    request hit anonymous storage.
    """
    from urllib.parse import urlparse  # noqa: E402

    from audittrace_object_storage import (  # noqa: E402
        ObjectStorageConfig,
        create_provider,
    )

    from audittrace.services.quarantine_denying_provider import (  # noqa: E402
        QuarantineDenyingObjectStorageClient,
    )

    backend = settings.object_storage_backend.lower()

    if backend == "minio":
        if not settings.minio_secret_key:
            raise RuntimeError(
                "MinIO backend selected but AUDITTRACE_MINIO_SECRET_KEY is empty. "
                "Either provide the key, or switch AUDITTRACE_OBJECT_STORAGE_BACKEND "
                "to 'aws'. Filesystem fallback removed in 2026-05-03 stabilization "
                "sweep — see feedback_storage_always_s3."
            )
        parsed = urlparse(settings.minio_url)
        config = ObjectStorageConfig(
            backend="minio",
            endpoint=(parsed.netloc or parsed.path),
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=(parsed.scheme == "https"),
        )
    elif backend == "aws":
        if not settings.aws_region or not settings.aws_bucket:
            raise RuntimeError(
                "AWS backend selected but AUDITTRACE_AWS_REGION or AUDITTRACE_AWS_BUCKET "
                "is empty. Set both. With aws_use_irsa=True (default) no other secrets "
                "are needed — boto3 resolves IRSA from the pod's ServiceAccount."
            )
        config = ObjectStorageConfig(
            backend="aws",
            region=settings.aws_region,
            endpoint_url=settings.aws_endpoint_url or None,
            use_irsa=settings.aws_use_irsa,
            access_key_id=settings.aws_access_key_id or None,
            secret_access_key=settings.aws_secret_access_key or None,
        )
    else:
        raise RuntimeError(
            f"unknown AUDITTRACE_OBJECT_STORAGE_BACKEND={backend!r}; "
            "expected 'minio' or 'aws'."
        )

    try:
        inner = create_provider(config)
        wrapped = QuarantineDenyingObjectStorageClient(inner)
        logger.info(
            "object-storage provider initialised — backend=%s, quarantine-deny=%s",
            backend,
            wrapped.quarantine_prefix,
        )
        return wrapped
    except Exception as exc:
        raise RuntimeError(
            f"object-storage provider initialisation failed (backend={backend}): {exc}. "
            "Layers 1+2 are S3-only — no filesystem fallback."
        ) from exc


# Backwards-compatibility alias for one release — same behaviour as
# the renamed function above. Drop in the follow-up PR after the grep
# audit confirms zero non-test callers.
_create_minio_client = _create_object_storage_provider


@log_call(logger=logger)
def _register_memory_services(settings: Settings, pg_factory: PostgresFactory) -> None:
    """Register all 4 memory layer services + context builder (ADR-018, ADR-027).

    Layers 1 + 2 are **always object-store-backed** — MinIO by default
    (laptop/homelab/k3s) or AWS S3 on EKS. The backend is selected by
    ``settings.object_storage_backend``; no filesystem fallback
    (``feedback_storage_always_s3``).
    """
    object_store = _create_object_storage_provider(settings)
    effective_bucket = (
        settings.aws_bucket
        if settings.object_storage_backend == "aws"
        else settings.minio_shared_bucket
    )
    container._instances["object_storage"] = object_store

    episodic: EpisodicService = S3EpisodicService(
        minio_client=object_store,
        bucket=effective_bucket,
        prefix="episodic/",
    )
    procedural: ProceduralService = S3ProceduralService(
        minio_client=object_store,
        bucket=effective_bucket,
        prefix="procedural/",
    )
    logger.info(
        "Memory layers 1+2: %s-backed (bucket=%s)",
        settings.object_storage_backend,
        effective_bucket,
    )

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
        # ADR-047 — vectors computed on the dedicated nomic embed server.
        embed_url=settings.embed_url,
        embed_model=settings.embed_model,
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
    container._instances["trust_store_provider"] = _build_trust_store_provider(
        settings, object_store, effective_bucket
    )
    container._instances["trust_store_builder"] = _build_trust_store_builder(settings)


def _build_trust_store_provider(
    settings: Settings, object_store: Any, bucket: str
) -> TrustStoreProvider:
    """Resolve ``AUDITTRACE_PDF_TRUST_STORE_PROVIDER`` to a Provider.

    Extracted from ``register_default_dependencies`` so the selection can be
    tested without booting the whole container — that function performs real
    network I/O (ChromaDB, embedder) before it reaches this point, which made
    these config branches unreachable from a unit test. Mirrors the shape of
    ``_build_inner_builder`` below.
    """
    if settings.pdf_trust_store_provider == "s3":
        return S3TrustStoreProvider(
            minio_client=object_store,
            bucket=bucket,
            pem_key=settings.pdf_trust_store_s3_key,
        )
    if settings.pdf_trust_store_provider == "file":
        # Pre-ADR-052 backwards-compat: when the operator points
        # AUDITTRACE_PDF_SIGNATURE_TRUST_STORE at a Vault-Agent-mounted
        # PEM file, the validator picks that up directly — the
        # Provider is effectively bypassed. We register the Mock
        # so DI shape is uniform; load() raises FileNotFoundError
        # when called, which the validator handles by falling back
        # to certifi.
        return MockTrustStoreProvider()
    raise RuntimeError(
        f"Unknown AUDITTRACE_PDF_TRUST_STORE_PROVIDER="
        f"{settings.pdf_trust_store_provider!r}; expected one of "
        f"'s3', 'file' (per ADR-052 §2)."
    )


def _build_trust_store_builder(settings: Settings) -> TrustStoreBuilder:
    """Resolve ``AUDITTRACE_PDF_TRUST_STORE_BUILDER`` to a Builder.

    Extracted alongside ``_build_trust_store_provider`` for the same reason.
    """
    builder_kind = settings.pdf_trust_store_builder
    if builder_kind != "composite":
        return _build_inner_builder(builder_kind, settings)

    # ADR-053 — compose multiple jurisdictional builders so the bundle
    # covers EU + CH (and any future programs) in one refresh cycle.
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
    return CompositeTrustStoreBuilder(
        [_build_inner_builder(n, settings) for n in names]
    )


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
