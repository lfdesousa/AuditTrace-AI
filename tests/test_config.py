"""Tests for configuration module.

ADR-020: Removed sessions_db, chroma_persist_dir. Added chroma_token.
database_url now uses postgresql+psycopg2:// driver prefix.
"""

import os

from sovereign_memory.config import Settings


def test_settings_default_values():
    """Test default configuration values."""
    settings = Settings()
    assert settings.host == "0.0.0.0"
    assert settings.port == 8765
    assert settings.workers == 1
    assert settings.auth_enabled is False
    assert settings.langfuse_enabled is False
    assert settings.llama_url == "http://host.docker.internal:11435/v1"
    assert settings.chroma_url == "http://localhost:8000"


def test_settings_from_env():
    """Test configuration from environment variables."""
    os.environ["SOVEREIGN_PORT"] = "9999"
    os.environ["SOVEREIGN_AUTH_ENABLED"] = "true"
    os.environ["SOVEREIGN_LOG_LEVEL"] = "DEBUG"

    settings = Settings()
    assert settings.port == 9999
    assert settings.auth_enabled is True
    assert settings.log_level == "DEBUG"

    # Cleanup
    del os.environ["SOVEREIGN_PORT"]
    del os.environ["SOVEREIGN_AUTH_ENABLED"]
    del os.environ["SOVEREIGN_LOG_LEVEL"]


def test_settings_chroma_server_mode():
    """Test ChromaDB URL configuration (server mode only — ADR-020)."""
    settings = Settings(chroma_url="http://chromadb:8000")
    assert settings.chroma_url == "http://chromadb:8000"


def test_settings_chroma_token():
    """Test ChromaDB token authentication (ADR-020)."""
    settings = Settings(chroma_token="my-secret-token")
    assert settings.chroma_token == "my-secret-token"


def test_settings_chroma_token_defaults_none():
    """Test ChromaDB token defaults to None."""
    settings = Settings()
    assert settings.chroma_token is None


def test_database_url_property():
    """Test PostgreSQL URL construction with psycopg2 driver."""
    settings = Settings(
        postgres_user="sovereign",
        postgres_password="secret",
        postgres_host="postgres",
        postgres_port=5432,
        postgres_db="sovereign_ai",
    )

    url = settings.database_url
    assert url == "postgresql+psycopg2://sovereign:secret@postgres:5432/sovereign_ai"


def test_database_url_from_full_url():
    """Test using full postgres_url when provided."""
    custom_url = "postgresql+psycopg2://custom:pass@host:5432/custom_db"
    settings = Settings(postgres_url=custom_url)
    assert settings.database_url == custom_url


def test_database_url_none():
    """Test database_url returns None when not configured."""
    settings = Settings()
    assert settings.database_url is None


def test_langfuse_enabled_flag():
    """Test Langfuse enabled check."""
    settings = Settings(langfuse_enabled=True, langfuse_host="http://langfuse:3000")
    assert settings.langfuse_enabled_flag is False  # Missing keys

    settings = Settings(
        langfuse_enabled=True,
        langfuse_host="http://langfuse:3000",
        langfuse_public_key="key",
        langfuse_secret_key="secret",
    )
    assert settings.langfuse_enabled_flag is True


def test_auth_configured():
    """Test OAuth2 configuration check."""
    settings = Settings(auth_enabled=True)
    assert settings.auth_configured is False  # Missing Keycloak config

    settings = Settings(
        auth_enabled=True,
        keycloak_issuer="http://kc:8080/realms/sovereign",
        keycloak_jwks_url="http://kc:8080/realms/sovereign/protocol/openid-connect/certs",
    )
    assert settings.auth_configured is True


def test_settings_memory_configuration():
    """Test memory-specific configuration."""
    settings = Settings(
        memory_cache_ttl=7200,
        memory_max_context_turns=130000,
        memory_embedding_dim=1536,
    )
    assert settings.memory_cache_ttl == 7200
    assert settings.memory_max_context_turns == 130000
    assert settings.memory_embedding_dim == 1536


def test_settings_rate_limiting():
    """Test rate limiting configuration."""
    settings = Settings(
        rate_limit_requests=200,
        rate_limit_window=120,
    )
    assert settings.rate_limit_requests == 200
    assert settings.rate_limit_window == 120


def test_settings_four_layer_memory_defaults():
    """Test 4-layer memory path defaults (ADR-018, ADR-020)."""
    settings = Settings()
    assert settings.adr_dir == "./memory/episodic"
    assert settings.skill_dir == "./memory/procedural"
    assert settings.llama_proxy_timeout == 120
    # sessions_db removed — PostgreSQL is the only path (ADR-020)
    assert (
        not hasattr(settings, "sessions_db")
        or "sessions_db" not in Settings.model_fields
    )


def test_settings_four_layer_memory_from_env():
    """Test 4-layer memory paths from environment variables."""
    os.environ["SOVEREIGN_ADR_DIR"] = "/data/adrs"
    os.environ["SOVEREIGN_SKILL_DIR"] = "/data/skills"
    os.environ["SOVEREIGN_LLAMA_PROXY_TIMEOUT"] = "60"

    settings = Settings()
    assert settings.adr_dir == "/data/adrs"
    assert settings.skill_dir == "/data/skills"
    assert settings.llama_proxy_timeout == 60

    del os.environ["SOVEREIGN_ADR_DIR"]
    del os.environ["SOVEREIGN_SKILL_DIR"]
    del os.environ["SOVEREIGN_LLAMA_PROXY_TIMEOUT"]


def test_no_file_based_db_fields():
    """ADR-020: Verify file-based database fields are removed."""
    field_names = set(Settings.model_fields.keys())
    assert "sessions_db" not in field_names
    assert "chroma_persist_dir" not in field_names


# ─────────────────── ADR-025: memory-as-tools settings ──────────────────────


def test_settings_memory_mode_defaults_to_inject():
    """ADR-025 §Decision.4: default mode is 'inject' during rollout so existing
    behaviour is unchanged until an operator flips the kill switch."""
    settings = Settings()
    assert settings.memory_mode == "inject"


def test_settings_memory_mode_accepts_tools():
    """ADR-025: SOVEREIGN_MEMORY_MODE=tools enables the tool-call loop path."""
    os.environ["SOVEREIGN_MEMORY_MODE"] = "tools"
    try:
        settings = Settings()
        assert settings.memory_mode == "tools"
    finally:
        del os.environ["SOVEREIGN_MEMORY_MODE"]


def test_settings_memory_tool_loop_max_iterations_default():
    """ADR-025 §Decision.2: default iteration cap is 5 per the brainstorm §5.6
    recommendation. Configurable for operators who observe cap-hits in prod."""
    settings = Settings()
    assert settings.memory_tool_loop_max_iterations == 5


def test_settings_memory_tool_loop_max_iterations_from_env():
    """ADR-025: operator can raise or lower the cap via env."""
    os.environ["SOVEREIGN_MEMORY_TOOL_LOOP_MAX_ITERATIONS"] = "12"
    try:
        settings = Settings()
        assert settings.memory_tool_loop_max_iterations == 12
    finally:
        del os.environ["SOVEREIGN_MEMORY_TOOL_LOOP_MAX_ITERATIONS"]


def test_settings_memory_tool_cache_ttl_default():
    """ADR-025 §Decision.8: default TTL for Redis-backed tool result cache is
    900 seconds (15 minutes)."""
    settings = Settings()
    assert settings.memory_tool_cache_ttl_seconds == 900


def test_settings_memory_tool_cache_ttl_zero_disables():
    """ADR-025: TTL=0 is the disable signal. The cache layer reads this and
    short-circuits both get and put so the handler always runs and nothing
    is stored."""
    os.environ["SOVEREIGN_MEMORY_TOOL_CACHE_TTL_SECONDS"] = "0"
    try:
        settings = Settings()
        assert settings.memory_tool_cache_ttl_seconds == 0
    finally:
        del os.environ["SOVEREIGN_MEMORY_TOOL_CACHE_TTL_SECONDS"]


def test_settings_tools_config_path_default():
    """ADR-025 §Decision.3: default config override path is repo-local
    tools.toml. Absent at runtime is not an error — the decorator-built
    registry is authoritative."""
    settings = Settings()
    assert settings.tools_config_path == "tools.toml"


def test_settings_tools_config_path_from_env():
    """ADR-025: immutable-image deployments override via env."""
    os.environ["SOVEREIGN_TOOLS_CONFIG_PATH"] = "/etc/sovereign/tools.toml"
    try:
        settings = Settings()
        assert settings.tools_config_path == "/etc/sovereign/tools.toml"
    finally:
        del os.environ["SOVEREIGN_TOOLS_CONFIG_PATH"]
