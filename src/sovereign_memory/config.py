import logging
import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Skip .env loading entirely when SOVEREIGN_ENV=test so a developer's local
# .env (with real credentials) cannot leak into the test suite.
_ENV_FILE: str | None = None if os.environ.get("SOVEREIGN_ENV") == "test" else ".env"


class Settings(BaseSettings):
    """Configuration for sovereign-memory-server using 12-factor principles."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="SOVEREIGN_",
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

    # LLM servers (external, on host machine)
    llama_url: str = "http://host.docker.internal:11435/v1"
    embed_url: str = "http://host.docker.internal:11436/v1"

    # ChromaDB configuration (server mode — ADR-020)
    chroma_url: str = "http://localhost:8000"
    chroma_collection: str = "sovereign_memory"
    chroma_token: str | None = None

    # PostgreSQL configuration (Phase 1+)
    postgres_url: str | None = None
    postgres_db: str = "sovereign_ai"
    postgres_user: str = "sovereign"
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
    keycloak_realm: str = "sovereign-ai"
    keycloak_issuer: str = ""
    keycloak_jwks_url: str = ""
    jwt_audience: str = "sovereign-memory-server"

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

    # Memory tiering configuration
    memory_cache_ttl: int = 3600  # seconds
    memory_max_context_turns: int = 117000  # ~131k - system prompt - output buffer
    memory_embedding_dim: int = 1024  # Nomic-embed-text default

    # 4-layer memory paths (ADR-018) — filesystem fallback for tests/local dev
    adr_dir: str = "./memory/episodic"
    skill_dir: str = "./memory/procedural"
    llama_proxy_timeout: int = 120  # seconds — timeout for llama-server proxy calls

    # MinIO / S3 object storage (ADR-027) — replaces filesystem bind mounts.
    # When minio_secret_key is non-empty, S3*Services activate and read from
    # MinIO buckets. When empty, File*Services use the filesystem paths above.
    minio_url: str = "http://localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = ""
    minio_shared_bucket: str = "memory-shared"
    minio_private_bucket: str = "memory-private"

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

    # Security
    cors_origins: list[str] = ["http://localhost:8765", "http://localhost:3000"]
    rate_limit_requests: int = 100
    rate_limit_window: int = 60  # seconds

    # OpenTelemetry (ADR-014.4)
    # When otlp_endpoint is empty, OTel runs in no-op mode (spans/metrics are
    # created but not exported). Set to a collector URL to activate export.
    otlp_endpoint: str = ""
    otel_service_name: str = "sovereign-memory-server"
    metrics_enabled: bool = True
    tracing_enabled: bool = True

    @property
    def database_url(self) -> str | None:
        """Construct PostgreSQL URL from components (SQLAlchemy format)."""
        if self.postgres_url:
            return self.postgres_url
        if self.postgres_password:
            return (
                f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            )
        return None

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
