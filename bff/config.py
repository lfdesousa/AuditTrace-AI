"""Configuration for the LibreChat BFF (12-factor, env-parameterized).

Mirrors ``audittrace.config.Settings`` in shape and discipline — a
``pydantic_settings.BaseSettings`` subclass, laptop-safe defaults, real
values injected via env/Vault at deploy time — but this is a genuinely
separate deployable (its own image, own env prefix
``AUDITTRACE_BFF_``), not a code path inside ``src/audittrace``.

Portability invariant: every target-shaped value (Keycloak host, realm,
orchestrator base URL, confidential-client secret) sits behind config
with a laptop default. A cloud target overrides via env, never a
rewrite.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Skip .env loading when BFF_ENV=test, same discipline as
# ``audittrace.config`` — a developer's local .env (with a real client
# secret) must never leak into the test suite.
_ENV_FILE: str | None = (
    None if os.environ.get("AUDITTRACE_BFF_ENV") == "test" else ".env"
)


class Settings(BaseSettings):
    """Configuration for the LibreChat BFF sidecar."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="AUDITTRACE_BFF_",
        case_sensitive=False,
        extra="ignore",
    )

    # "local" (laptop k3s / dev), "production", "test" (set by conftest).
    env: str = "local"

    host: str = "0.0.0.0"
    port: int = 8766
    log_level: str = "INFO"

    # ── Orchestrator target (the frozen /v1 surface) ─────────────────────
    # Laptop default is the in-mesh Service DNS name the memory-server
    # chart already exposes port 8765 on; override per target (compose
    # profile, cloud rig) — never hardcode a second value in code.
    orchestrator_base_url: str = "http://localhost:8765"
    orchestrator_chat_path: str = "/v1/chat/completions"
    orchestrator_timeout_seconds: float = 120.0

    # ── Inbound-token validation (the token LibreChat forwards) ──────────
    # Same Keycloak realm as the orchestrator's own auth.py; the BFF
    # deliberately does NOT enforce a fixed audience on the inbound token
    # (that ambiguity — access vs id_token, wrong-audience — is exactly
    # what token-exchange resolves), only signature + issuer + expiry.
    keycloak_issuer: str = "http://localhost:8080/realms/audittrace"
    keycloak_issuer_extras: list[str] = []
    keycloak_jwks_url: str = (
        "http://localhost:8080/realms/audittrace/protocol/openid-connect/certs"
    )
    jwks_cache_ttl_seconds: int = 300

    # ── RFC 8693 token exchange (confidential client) ─────────────────────
    keycloak_token_url: str = (
        "http://localhost:8080/realms/audittrace/protocol/openid-connect/token"
    )
    exchange_client_id: str = "audittrace-librechat-bff"
    # Vault/env-sourced ONLY — never hard-coded, never defaulted to a real
    # value. No default means an unset secret fails Settings construction
    # (fail-closed at startup, not at the first request).
    exchange_client_secret: str
    # The client whose scope profile (`aud-audittrace-server` mapper +
    # `audittrace:query` default scope) the exchanged token should carry.
    # Reuses the WU-1 `audittrace-librechat` client's own protocol mappers
    # rather than declaring a second copy of the same audience/scope
    # wiring — see bff/README.md.
    exchange_audience: str = "audittrace-librechat"
    exchange_subject_token_type: str = "urn:ietf:params:oauth:token-type:access_token"
    exchange_requested_token_type: str = "urn:ietf:params:oauth:token-type:access_token"
    exchange_grant_type: str = "urn:ietf:params:oauth:grant-type:token-exchange"
    exchange_timeout_seconds: float = 30.0

    # X-Source stamped on every proxied request so orchestrator-side audit
    # rows / interactions.source attribute this channel distinctly from
    # webui/opencode (routes/chat.py::_detect_source).
    proxy_source_label: str = "librechat"

    # ── Outbound TLS trust (M3-WU-3) ──────────────────────────────────────
    # JWKS fetches, RFC 8693 token-exchange, and the `/v1` proxy all hit
    # ``orchestrator_base_url`` / ``keycloak_*_url`` over TLS. httpx's
    # default trust store is the certifi bundle, which does NOT include
    # the laptop front door's locally-minted cert (chart's
    # secret-tls.yaml, self-signed). Empty (default) = httpx's normal
    # certifi trust (correct for a real-CA front door, e.g. the cloud
    # rig's Let's Encrypt cert). Set to a mounted PEM path to trust an
    # additional local CA (the laptop's `~/.config/audittrace/ca.crt`,
    # same cert `scripts/install-audittrace-trust.sh` extracts — see
    # `bff/app.py::lifespan`). Portability invariant: env-parameterized,
    # no hardcoded target; mirrors `src/audittrace/ops/pod_reaper.py`'s
    # ``verify=str(_CA_PATH)`` pattern for the same class of problem.
    ca_bundle_path: str = ""


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings singleton.

    ``lru_cache`` mirrors ``audittrace.config.get_settings`` — constructed
    once, re-read only via ``get_settings.cache_clear()`` (tests use this
    to pick up monkeypatched env vars).
    """
    return Settings()  # type: ignore[call-arg]
