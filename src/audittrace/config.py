import logging
import os
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def _as_async_url(url: str) -> str:
    """Normalise a SQLAlchemy URL to its async driver (asyncpg / aiosqlite)."""
    if url.startswith("postgresql+psycopg2://"):
        return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


def _as_sync_url(url: str) -> str:
    """Normalise a SQLAlchemy URL to its sync driver (psycopg2 / sqlite).

    Used for Alembic + the RLS oracle test, which run out-of-band of the
    request event loop.
    """
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    if url.startswith("sqlite+aiosqlite://"):
        return url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    return url


# Skip .env loading entirely when AUDITTRACE_ENV=test so a developer's local
# .env (with real credentials) cannot leak into the test suite.
_ENV_FILE: str | None = None if os.environ.get("AUDITTRACE_ENV") == "test" else ".env"


class Settings(BaseSettings):
    """Configuration for audittrace-server using 12-factor principles."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="AUDITTRACE_",
        case_sensitive=False,
        extra="ignore",
    )

    # Environment: "local" (Docker Compose), "production", "test" (mocks — set by conftest)
    env: str = "local"

    # Server configuration
    host: str = "0.0.0.0"
    port: int = 8765
    workers: int = 1
    log_level: str = "INFO"
    log_format: Literal["json", "plain"] = "json"
    """
    Structured-log emission shape. "json" (the default since
    2026-05-16) is the production-correct mode — every log line is a
    single JSON object carrying ``trace_id`` + ``span_id`` + ``service``
    at top level, so Loki's ``{...} | json | trace_id="..."`` selector
    works directly. "plain" is the legacy human-readable mode for
    interactive dev only. The reconstructibility walkthrough
    (docs/reconstructibility-walkthrough.md, Hop 5) hard-depends on
    the "json" shape; flipping this back to "plain" breaks the
    documented operator drill.
    """

    # LLM servers (external, on host machine)
    llama_url: str = "http://host.docker.internal:11435/v1"
    embed_url: str = "http://host.docker.internal:11436/v1"

    # ChromaDB configuration (server mode — ADR-020)
    chroma_url: str = "http://localhost:8000"
    chroma_collection: str = "audittrace"
    chroma_token: str | None = None

    # PostgreSQL configuration (Phase 1+)
    postgres_url: str | None = None
    postgres_db: str = "audittrace"
    postgres_user: str = "audittrace"
    postgres_password: str | None = None
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # Langfuse observability
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_enabled: bool = False

    # OAuth2 / Keycloak configuration (ADR-022, ADR-023, DESIGN §15)
    auth_enabled: bool = False
    keycloak_url: str = "http://localhost:8080"
    keycloak_realm: str = "audittrace"
    keycloak_issuer: str = ""
    keycloak_jwks_url: str = ""
    # ADR-032: additional ``iss`` values to accept on inbound JWTs.
    # Primary ``keycloak_issuer`` is the docker-network-internal URL
    # that service-account flows (client_credentials, client-JWT) mint
    # tokens against. Human-facing Device Flow tokens arrive via the
    # Traefik-exposed hostname and carry a different ``iss`` even
    # though they're signed by the same Keycloak. Putting the
    # externally-resolvable URL here lets both token families pass
    # validation. Empty list keeps the existing single-issuer
    # behaviour — no behaviour change for deployments that do not
    # enable Device Flow.
    keycloak_issuer_extras: list[str] = []
    jwt_audience: str = "audittrace-server"

    # Multi-user identity gate (ADR-026 §15).
    # When False (default during the migration), require_user returns a
    # sentinel UserContext with admin scopes for backwards compatibility.
    # Phase 5 will flip this to True after cross-user isolation tests land.
    auth_required: bool = False

    # Redis-backed token cache (DESIGN §15.4 / §15.4a). The cache holds
    # validated JWT claims keyed on sha256(token) so the hot path skips
    # JWKS validation entirely.
    redis_url: str = "redis://localhost:6379/0"
    redis_password: str | None = None
    token_cache_ttl_seconds: int = 300

    # In-memory JWKS cache TTL. JWKS keys rotate slowly; refetching on
    # every request would beat up Keycloak. 5 minutes is the same TTL
    # most OIDC clients pick by default, and falls inside Keycloak's
    # default key rotation interval (~12 h) by 144x — fast enough for
    # rotation pickup, slow enough to absorb startup races.
    jwks_cache_ttl_seconds: int = 300

    # Memory tiering configuration
    memory_cache_ttl: int = 3600  # seconds
    memory_max_context_turns: int = 117000  # ~131k - system prompt - output buffer
    memory_embedding_dim: int = 1024  # Nomic-embed-text default

    # 4-layer memory: layers 1+2 are S3-only (MinIO) — no filesystem paths.
    # See feedback_storage_always_s3 for the durable rule (2026-05-03 sweep).
    llama_proxy_timeout: int = 120  # seconds — DEPRECATED: use llama_chunk_timeout
    llama_chunk_timeout: int = 600  # seconds — per-chunk idle timeout (ADR-034).
    # 600s accommodates first-chunk delay on 27B Q4 + consumer GPU,
    # where prompt eval for ~5K-token prompts (e.g. OpenCode's tool-laden
    # system prompt) routinely exceeds 120s before the first token streams.
    # Once tokens flow, idle resets per chunk, so this only bounds initial
    # prompt eval + any genuine upstream hang.
    sse_keepalive_interval: int = 15  # seconds — SSE keep-alive interval (ADR-034)

    # Object storage (ADR-027 + ADR-006) — replaces filesystem bind mounts.
    #
    # ``object_storage_backend`` selects the backend at startup; the factory
    # in ``audittrace.dependencies`` dispatches to either
    # :class:`MinIOObjectStorageProvider` (default, laptop/homelab/k3s) or
    # :class:`AWSObjectStorageProvider` (EKS, IRSA-native).
    #
    # MinIO path: requires ``minio_secret_key`` (else fail fast at startup).
    # AWS path: requires ``aws_region`` + ``aws_bucket``;
    # ``aws_use_irsa=True`` (default) means boto3 reads
    # ``AWS_ROLE_ARN`` + ``AWS_WEB_IDENTITY_TOKEN_FILE`` from the
    # EKS pod-identity webhook — no key plumbing needed.
    #
    # No filesystem fallback (``feedback_storage_always_s3``, 2026-05-03).
    object_storage_backend: str = "minio"  # "minio" | "aws"
    # MinIO fields (legacy names preserved for backwards compatibility)
    minio_url: str = "http://localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = ""
    minio_shared_bucket: str = "memory-shared"
    minio_private_bucket: str = "memory-private"
    # AWS S3 fields (used only when object_storage_backend = "aws")
    aws_region: str = ""
    aws_bucket: str = ""  # When set, overrides minio_shared_bucket for AWS path
    aws_endpoint_url: str = ""  # Optional — for S3-compatible non-AWS endpoints
    aws_use_irsa: bool = True
    aws_access_key_id: str = ""  # Only when aws_use_irsa=False
    aws_secret_access_key: str = ""  # Only when aws_use_irsa=False

    # ─────────────── ADR-025 — memory-as-tools ──────────────────────────────
    # Kill switch (§Decision.4). During the rollout the default is the legacy
    # inject path so production behaviour is unchanged until an operator flips
    # the env var. After the Phase 7 canary week the default will flip to
    # "tools".
    memory_mode: str = "inject"  # "inject" | "tools"

    # Hard iteration cap for the proxy-internal tool-call loop (§Decision.2).
    # A misbehaving model could otherwise loop forever on tool_calls; the cap
    # short-circuits with a WARNING log and returns whatever text accumulated.
    # Operator-tunable in case a cap-hit is observed in production.
    memory_tool_loop_max_iterations: int = 5

    # Redis-backed tool result cache TTL (§Decision.8). Non-zero enables the
    # ToolResultCache; 0 disables both get and put so the handler always runs
    # and nothing is stored. Cache keys are namespaced under
    # "sovereign:tool-result:" — disjoint from the TokenCache namespace.
    memory_tool_cache_ttl_seconds: int = 900

    # Optional override file for the memory tool registry (§Decision.3).
    # Decorators at import time populate the base registry; this TOML file —
    # if present — overlays per-tool config (disable, retune scope, rename,
    # override description). Cannot add new handlers. Absent file at runtime
    # is not an error; the decorator-built registry is authoritative.
    tools_config_path: str = "tools.toml"

    # ─────────────── ADR-030 — session summariser ────────────────────────────
    # Dedicated llama-server endpoint for background session summarisation.
    # Separate port so summarisation never contends with the interactive
    # tool-loop on the Qwen slot. Falls back to llama_url when the dedicated
    # Mistral endpoint is not running so the feature degrades rather than
    # crashes.
    summarizer_url: str = "http://host.docker.internal:11437/v1"
    summarizer_model: str = "mistral-7b-summarizer"

    # Optional dedicated database URL for the summariser worker. The
    # main memory-server connects as the non-superuser ``audittrace_app``
    # role so RLS policies apply to every proxy query (ADR-026 §16
    # Phase 4). The summariser is an admin-grade batch worker that
    # must read across every user's interactions to build audit-time
    # summaries; that requires the owner role so
    # ``SET LOCAL row_security = off`` (in the read transaction)
    # actually bypasses RLS. When unset, falls back to the main
    # ``database_url`` (useful for tests where RLS is a no-op).
    summarizer_postgres_url: str | None = None

    # Kill switch. When False the background task is never started, even if
    # the server is otherwise configured correctly.
    summarizer_enabled: bool = True

    # Idle window — a session becomes eligible for summarisation once its
    # most recent interaction is older than this threshold. Short enough
    # that today's sessions get summarised before tomorrow's work starts,
    # long enough that we do not summarise mid-conversation.
    summarizer_idle_minutes: int = 15

    # Wake cadence for the background loop.
    summarizer_interval_minutes: int = 5

    # Upper bound on sessions processed per wake cycle. Protects against a
    # first-run spike when there are thousands of unsummarised sessions.
    summarizer_max_per_cycle: int = 10

    # Backlog #10 — pre-flight ctx-window guard.
    # llama-server's ``--ctx-size`` for the summariser model. When the rendered
    # transcript would exceed this minus headroom, the summariser truncates
    # oldest turns until it fits, rather than letting llama-server reject the
    # request with HTTP 400 and re-trying every 5 min indefinitely (the
    # 2026-04-22 incident, see project_summarizer_400.md). Match the value
    # baked into scripts/start-summarizer-llama.sh; 32768 since 2026-04-24.
    summarizer_ctx_tokens: int = 32768
    # Output-token reservation: max_tokens=600 in the request; pad with a
    # small safety margin against tokenizer drift.
    summarizer_ctx_reserve_tokens: int = 700

    # ─────────────── PDF ingestion bomb defenses (gap-inventory #18) ──────
    # Defense in depth — pymupdf does not bound resource usage by default,
    # and a 2 KiB malformed PDF can specify enormous page counts, deeply
    # nested resources, or content streams that decompress to gigabytes.
    # See docs/architecture/pdf-ingestion-gaps.md §2.5 for the threat
    # model. Multi-layer because pymupdf does not expose stream
    # decompression-ratio at the API level — we can't predict a single
    # ratio, so we cap shape (size, pages, xrefs) AND content (per-page
    # extracted text) AND time (parse timeout). Each layer catches a
    # different bomb shape; together they bound the worst case.
    pdf_max_size_mb: int = 200
    pdf_max_pages: int = 2000
    # Total declared object count in the PDF cross-reference table.
    # Benign documents are typically O(10²-10⁴); bombs use O(10⁶+).
    pdf_max_xref_count: int = 100_000
    # Total wall-clock budget for one PDF's parse + extract loop. The
    # check fires at page-boundary granularity (signal.alarm doesn't
    # work in FastAPI's worker-thread pool), so individual pymupdf
    # calls can still spike past this — but the total stays bounded
    # to ~timeout + one-page latency.
    pdf_parse_timeout_seconds: int = 300
    # Per-page extracted text byte cap. The decompression-ratio defense:
    # if a single page yields >10 MB of text, that's bomb-shaped (a
    # benign 50-page paper has ~50-500 KB total text). The check fires
    # AFTER ``page.get_text()`` returns, so a single page can still
    # transiently allocate up to this size — but the file as a whole
    # cannot decompress to gigabytes through repeated small streams.
    pdf_max_page_text_bytes: int = 10_000_000

    # ─────────────── PDF redaction policy (gap-inventory #8) ──────────────
    # Unflattened redaction annotations carry the redacted text in the
    # underlying content stream; indexing them would expose data the
    # author intended to remove. This is a confidentiality bug, not a
    # feature gap. See docs/architecture/pdf-ingestion-gaps.md §2.2.
    #
    # "reject" (default) — refuse the whole document on first
    # redaction-bearing page. Strict + safe; the right default.
    #
    # "clip-extract" — for advanced operators who explicitly accept
    # the residual risk (the underlying stream may still leak through
    # other paths): extract block-level text and drop blocks whose
    # bbox intersects any redaction rect. Pages without redactions
    # index normally; pages with redactions emit ``redaction_status =
    # "clipped"`` so auditors can see what was filtered.
    pdf_redaction_policy: str = "reject"

    # ─────────────── PDF signature validation (gap-inventory #12) ─────────
    # Detect-and-record only in v1 — every chunk metadata carries a
    # ``signature_status`` field; auditors can query for invalid /
    # tampered / unsigned content. Future revisions can flip a flag
    # to reject on invalid (defense-in-depth for high-assurance
    # corpora). See docs/architecture/pdf-ingestion-gaps.md §2.3.
    pdf_signature_check_enabled: bool = True
    # Filesystem path to a PEM bundle of additional trust-anchor
    # certificates (e.g. operator's internal CA). Empty string =
    # consult the configured TrustStoreProvider (default S3 +
    # MinIO) at first signature check; falls through to certifi
    # + OS trust store if the Provider has no bundle stored yet.
    # Pre-ADR-052 deployments that mount a PEM via Vault Agent at
    # ``/etc/audittrace/trust-store.pem`` continue to work — set
    # this to the mount path; takes precedence over the Provider.
    # ADR-052 §2.
    pdf_signature_trust_store: str = ""
    # ADR-052 §2 — Trust-store Provider/Builder selection.
    # ``pdf_trust_store_provider`` chooses the storage layer; ``s3``
    # is the default (MinIO at the bucket + key below). ``file``
    # uses ``pdf_signature_trust_store`` directly (pre-ADR-052
    # backwards-compat — the Provider is essentially read-only,
    # store() is a no-op). VaultTrustStoreProvider documented in
    # the ADR but not implemented in PR 3.
    pdf_trust_store_provider: str = "s3"
    # ``pdf_trust_store_builder`` chooses the sourcing layer.
    # ``eu_lotl`` walks the EU List of Trusted Lists via
    # pyhanko[etsi] (EU eIDAS qualified TSPs across all 27
    # member states). ``swiss_tsl`` walks the Swiss federal
    # Trusted List published by OFCOM/BAKOM (ADR-053 — adds
    # Swiss-jurisdiction qualified TSPs incl. SwissSign +
    # Swisscom Trust Services). ``composite`` runs both (and any
    # future jurisdictional builder) and concatenates the
    # bundles — the recommended default for any deployment
    # serving signed-document workflows in either jurisdiction.
    # ``static`` concatenates a directory of operator-supplied
    # PEMs (test/dev + air-gapped customers).
    pdf_trust_store_builder: str = "composite"
    # When ``pdf_trust_store_builder == "composite"`` this is the
    # comma-separated list of inner builders (in order). Each entry
    # must be one of {``eu_lotl``, ``swiss_tsl``, ``static``}.
    # Default ``eu_lotl,swiss_tsl,static`` covers the EU + CH
    # jurisdictions plus operator-vendored roots (Backlog #13
    # closed 2026-05-15: SwissSign 2020-2 root extracted from
    # `main_signed.pdf` PAdES chain and shipped via the chart).
    pdf_trust_store_composite_builders: str = "eu_lotl,swiss_tsl,static"
    # Filesystem path to the Swiss TSLO (Trust List Operator)
    # signing certificate, vendored in the chart at
    # ``charts/audittrace/trust-store/swiss-federal-tsl/CH-TL-cert.der``
    # and mounted into memory-server via ConfigMap. Read by the
    # SwissTslTrustStoreBuilder to validate the Swiss TSL's XAdES
    # signature before registering any TSP. SHA-1 fingerprint
    # ``e8638362 5130bdf0 1e42a317 6501e079 261b137f`` was OOB-
    # verified against
    # https://uri.tsl-switzerland.ch/TrstSvc/TrustedList/schemerules/CH/index.html
    # on 2026-05-09. ADR-053 §4.
    pdf_trust_store_swiss_tslo_cert_path: str = (
        "/etc/audittrace/swiss-federal-tsl/CH-TL-cert.der"
    )
    # S3-Provider object location: bucket re-uses the existing
    # ``minio_shared_bucket`` (memory-shared by default — ADR-027
    # §2 — public to all authenticated users since trust roots
    # are public CAs); key is single-object under
    # ``trust-store/`` to mirror the ``episodic/``, ``procedural/``
    # prefix convention.
    pdf_trust_store_s3_key: str = "trust-store/eu-lotl-bundle.pem"
    # Static-Builder source directory. Operator-supplied PEMs
    # under this path are concatenated by
    # ``StaticTrustStoreBuilder``. Default points at the chart-
    # mounted ConfigMap (`configmap-static-roots.yaml`), which
    # ships the SwissSign 2020-2 root by default (Backlog #13).
    # Empty string disables the static builder.
    pdf_trust_store_static_dir: str = "/etc/audittrace/static-roots"

    # ─────────────── PDF OCR (gap-inventory #1, ADR-050 tier-B) ───────────
    # Tesseract-backed OCR fallback for raster-only pages
    # (`page.get_text() == ""` AND `page.get_images()` non-empty).
    # The actual ``tesseract`` binary + language packs ship in the
    # Dockerfile (apt). Locally, missing binary triggers graceful
    # degradation: pages without a text layer get a
    # ``no_text_layer`` extraction-warning and zero chunks rather
    # than crashing. Operator can disable OCR entirely (e.g. to
    # keep the index path fast on a corpus known to be all-native-text)
    # by setting AUDITTRACE_PDF_OCR_ENABLED=false.
    pdf_ocr_enabled: bool = True
    # Languages Tesseract loads. Plus-separated; matches Tesseract's
    # CLI ``-l`` argument shape. Default = English + the three CH
    # national languages (de, fr, it). Each language pack adds
    # ~10 MB to the image; keep the default minimal.
    pdf_ocr_languages: str = "eng+deu+fra+ita"
    # DPI for page-to-PNG rasterisation before Tesseract reads. 300
    # is Tesseract's recommended sweet spot for accuracy (>200 quality
    # rises noticeably; >400 the curve flattens). Per-page memory at
    # 300 DPI A4 ≈ 3 MB, freed before the next page.
    pdf_ocr_dpi: int = 300

    # ─────────────── ADR-046 — async chat persistence ─────────────────────
    # Opt-in (`X-Persist-Mode: async` request header) Redis-Streams-backed
    # async write of the InteractionRecord. Each pod runs one consumer in
    # the `async_persist_group` consumer group; multi-pod safe by Redis
    # consumer-group routing semantics.
    #
    # Default OFF — toggled to ON in chart values once live evidence
    # captures the multi-pod behaviour (`feedback_test_and_evidence`).
    # While OFF: the producer never sees the async branch even if the
    # caller sends `X-Persist-Mode: async`; the consumer worker is not
    # started in lifespan. Zero runtime impact.
    async_persist_enabled: bool = False
    async_persist_stream: str = "audittrace:persist:stream"
    async_persist_dlq: str = "audittrace:persist:dlq"
    async_persist_group: str = "audittrace-persisters"
    # XREADGROUP BLOCK timeout (ms). Long enough that the consumer
    # parks gracefully when idle, short enough that shutdown drains in
    # under one BLOCK window after cancellation.
    async_persist_block_ms: int = 5000
    # Max entries pulled per XREADGROUP iteration.
    async_persist_batch_size: int = 10
    # Threshold (delivery_count from XPENDING) at which a stuck message
    # is moved to the DLQ stream and XACKed off the main stream. Must
    # be > 1 so transient errors get at least one retry.
    async_persist_max_deliveries: int = 5
    # XPENDING IDLE window (ms): on consumer iteration, any message
    # that has been pending longer than this without an XACK is
    # eligible for re-claim by THIS consumer (the prior owner is
    # presumed dead — pod restart, OOM, network partition).
    async_persist_pending_idle_ms: int = 60000

    # ─────────────── ADR-048 — ingestion content-control ─────────────────
    # PR-B3: PDF uploads land in a quarantine prefix and trigger an
    # AMQP scan-request. Markdown / non-PDF uploads keep the existing
    # synchronous direct-PUT path. Default OFF so the service starts
    # cleanly without RabbitMQ in dev / unit tests; the chart sets
    # this true once the broker subchart (PR-B2.5) is up.
    scan_pipeline_enabled: bool = False
    # AMQP connection URL (aio_pika.connect_robust). Same broker as
    # the topology bootstrap Job created in PR-B2.5.
    scan_amqp_url: str = ""
    # Topic exchange + routing-key prefix. Mirrors content-control's
    # Settings — must match on both sides or the bound queue won't
    # route. Closed-set in cross-repo `contracts/v1.yaml`.
    scan_request_exchange: str = "audittrace.scan"
    scan_request_routing_key: str = "scan.requested"
    # Quarantine MinIO key prefix. Memory-server PUTs here; content-
    # control reads here (and only here). Bucket-policy enforcement
    # of the read-side denylist lands in PR-B7.
    scan_quarantine_prefix: str = "quarantine"
    # Producer drain interval — how long the publisher's background
    # task waits for new ScanRequest entries before re-checking the
    # outbox. Short enough that 202 → broker delivery is sub-second
    # under load; long enough that an idle service doesn't burn CPU.
    scan_publisher_drain_interval_ms: int = 100
    # Janitor query interval — how often the background task scans
    # `memory_items WHERE published_at_ms IS NULL AND created_at_ms
    # < NOW()-grace`. The grace window must be > the publisher's
    # typical drain latency so a healthy publish-in-flight isn't
    # double-published. 60s grace + 30s interval handles that.
    scan_janitor_interval_seconds: int = 30
    scan_janitor_grace_seconds: int = 60

    # Security
    cors_origins: list[str] = ["http://localhost:8765", "http://localhost:3000"]
    rate_limit_requests: int = 100
    rate_limit_window: int = 60  # seconds

    # OpenTelemetry (ADR-014.4)
    # When otlp_endpoint is empty, OTel runs in no-op mode (spans/metrics are
    # created but not exported). Set to a collector URL to activate export.
    otlp_endpoint: str = ""
    otel_service_name: str = "audittrace-server"
    metrics_enabled: bool = True
    tracing_enabled: bool = True

    @property
    def database_url(self) -> str | None:
        """Async PostgreSQL URL (asyncpg) — the runtime engine driver.

        memory-server runs its data layer on asyncio (AsyncSession) so the
        event loop is never blocked on DB I/O under load. Alembic + the
        sync RLS oracle test use ``database_url_sync`` instead.
        """
        if self.postgres_url:
            return _as_async_url(self.postgres_url)
        if self.postgres_password:
            return (
                f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            )
        return None

    @property
    def database_url_sync(self) -> str | None:
        """Sync PostgreSQL URL (psycopg2) — Alembic migrations + the RLS
        isolation oracle test, which run out-of-band of the request loop."""
        url = self.database_url
        return _as_sync_url(url) if url else None

    @property
    def summarizer_database_url(self) -> str | None:
        """URL for the summariser's Postgres connection.

        Prefers the explicit ``summarizer_postgres_url`` (owner-role
        credentials) so the RLS-bypass ``SET LOCAL row_security = off``
        in the read transaction actually takes effect. Falls back to
        the main ``database_url`` when unset — acceptable for tests
        (SQLite has no RLS) and single-tenant dev deployments.
        """
        if self.summarizer_postgres_url:
            return self.summarizer_postgres_url
        return self.database_url

    @property
    def langfuse_enabled_flag(self) -> bool:
        """Check if Langfuse is properly configured."""
        return (
            self.langfuse_enabled
            and bool(self.langfuse_host)
            and bool(self.langfuse_public_key)
        )

    @property
    def auth_configured(self) -> bool:
        """Check if OAuth2 is properly configured."""
        return (
            self.auth_enabled
            and bool(self.keycloak_issuer)
            and bool(self.keycloak_jwks_url)
        )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance (12-factor pattern)."""
    settings = Settings()
    logger.debug(
        "Loaded settings: host=%s port=%s log_level=%s auth_enabled=%s "
        "otlp_endpoint=%s metrics_enabled=%s",
        settings.host,
        settings.port,
        settings.log_level,
        settings.auth_enabled,
        settings.otlp_endpoint or "<disabled>",
        settings.metrics_enabled,
    )
    return settings
