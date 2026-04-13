"""Identity primitives — Keycloak is the source of truth.

ADR-026 §15. Sovereign-memory-server holds NO
local users table; identity comes from Keycloak-issued JWTs validated
against the JWKS endpoint. The hot path is a Redis-backed token cache
keyed on ``sha256(token)``; the cold path validates the JWT against
JWKS and writes the resulting ``UserContext`` to the cache.

This module exports:

- ``UserContext`` — frozen dataclass passed explicitly to every memory
  service method as the first parameter (Phase 2 plumbing). Source-
  agnostic: it does not know whether it came from a JWT, the cache,
  or the bypass sentinel.
- ``hash_token`` — sha256 hex helper. Used as the cache key so the
  raw token never reaches Redis, logs, or memory dumps.
- ``is_admin_scope`` — checks whether a scope tuple grants admin.
- ``sentinel_user_context`` — bypass-mode UserContext for the
  ``SOVEREIGN_AUTH_REQUIRED=false`` migration window.
- ``TokenCache`` — Redis-backed sha256→UserContext store with TTL.
  Phase 8+ may swap the implementation for a multi-instance shared
  cluster; the interface stays the same.
- ``get_token_cache`` / ``reset_token_cache`` — module-level singleton
  accessors for the production cache instance.

The cache is intentionally per-deployment Redis (a dedicated
``sovereign-redis`` container in ``docker-compose.yml``). Sharing
Redis with Langfuse or other systems is not supported because the
key namespace ``sovereign:token:`` is unprotected from collisions
with other users of the same Redis instance.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis import Redis

logger = logging.getLogger(__name__)


# ─────────────────────────── Sentinel for bypass mode ────────────────────────
# A fixed Keycloak-shaped subject claim used by ``sentinel_user_context``.
# Phase 5 will remove the bypass mode entirely once cross-user isolation
# tests land; until then, this is the identity used for backwards-compat
# tests and dev workflows that pre-date multi-user.

SENTINEL_SUBJECT = "00000000-0000-0000-0000-000000000001"
SENTINEL_USERNAME = "default"


# ──────────────────────────────── UserContext ────────────────────────────────


@dataclass(frozen=True)
class UserContext:
    """Resolved identity for a single request, source-agnostic.

    Constructed by the auth middleware after JWT validation (cold path)
    or by the cache lookup (hot path). The frozen dataclass guarantees
    that no code downstream can mutate the identity once it has been
    resolved.
    """

    user_id: str
    """Keycloak ``sub`` claim — opaque UUID identifying the human."""

    username: str
    """Keycloak ``preferred_username`` claim, falling back to ``sub``."""

    agent_type: str
    """Best-effort detection from the request's User-Agent header.
    Recorded in audit rows for forensics; not an authorization gate."""

    scopes: tuple[str, ...]
    """OAuth2 scopes from the JWT ``scope`` claim, split on whitespace."""

    token_id: str | None = None
    """Keycloak ``jti`` claim — used to correlate audit rows back to a
    specific token issuance event."""

    is_admin: bool = False
    """Derived from ``scopes`` via ``is_admin_scope``."""

    extra: dict[str, str] = field(default_factory=dict)
    """Free-form extra claims (email, name, etc.) carried for display
    purposes only — never load-bearing for authorization."""


# ─────────────────────────────── Helpers ────────────────────────────────────


def hash_token(token: str) -> str:
    """Compute the hex sha256 of a token.

    Used as the cache key so the raw bearer token never reaches Redis,
    logs, or memory dumps. ``hash_token`` is deterministic — the same
    token always hashes to the same key.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_admin_scope(scopes: Iterable[str]) -> bool:
    """True iff any scope grants administrative access.

    Recognises both ``memory:admin`` and any ``admin:*`` namespace.
    Used to set ``UserContext.is_admin`` for downstream gates and
    the future Phase 4 RLS bypass policy.
    """
    return any(s == "memory:admin" or s.startswith("admin:") for s in scopes)


def sentinel_user_context(agent_type: str = "opencode") -> UserContext:
    """Build the UserContext returned by ``require_user`` in bypass mode.

    Has full admin scopes so existing tests and dev workflows that
    pre-date the multi-user feature continue to work end-to-end without
    per-call configuration. Production deployments must flip
    ``SOVEREIGN_AUTH_REQUIRED`` to ``true`` to take this path out of
    reach.
    """
    return UserContext(
        user_id=SENTINEL_SUBJECT,
        username=SENTINEL_USERNAME,
        agent_type=agent_type,
        scopes=("memory:admin", "memory:read", "session:read-own"),
        token_id=None,
        is_admin=True,
    )


# ─────────────────────────────── TokenCache ─────────────────────────────────


class TokenCache:
    """Redis-backed sha256(token) → UserContext cache with TTL eviction.

    Cache keys are namespaced under ``sovereign:token:<hash>`` so that
    multiple consumers of the same Redis instance (none today, but
    designing for it) cannot collide.

    The raw bearer token is never stored — only its sha256 hash. Anyone
    who breaches the Redis instance gets a list of valid (hash → claims)
    bindings, which lets them verify an existing token but not forge
    new ones.

    Thread-safe via Redis itself; no in-process locks needed. FastAPI
    runs request handlers across a thread pool that all share a single
    ``redis.Redis[str]`` client.

    Resilience: ``get`` returns ``None`` on any Redis[str] error (treated as
    cache miss — the middleware falls through to JWT validation).
    ``put`` swallows Redis errors with a warning so cache failures
    never break requests.
    """

    KEY_PREFIX = "sovereign:token:"

    def __init__(self, redis_client: Redis[str], default_ttl_seconds: int = 300):
        self._redis = redis_client
        self._default_ttl = default_ttl_seconds

    def _key(self, token_hash: str) -> str:
        return f"{self.KEY_PREFIX}{token_hash}"

    def get(self, token_hash: str) -> UserContext | None:
        """Return the cached ``UserContext`` for this hash, or ``None``.

        ``None`` is returned for: missing key, expired entry (Redis[str]
        evicted), unparseable payload, or any Redis connectivity error.
        Treat ``None`` as "validate the JWT and re-cache".
        """
        try:
            raw = self._redis.get(self._key(token_hash))
        except Exception as exc:  # pragma: no cover - resilience path
            logger.warning("TokenCache.get failed: %s", exc)
            return None
        if raw is None:
            return None
        try:
            data = json.loads(str(raw))
            return UserContext(
                user_id=data["user_id"],
                username=data["username"],
                agent_type=data["agent_type"],
                scopes=tuple(data["scopes"]),
                token_id=data.get("token_id"),
                is_admin=data.get("is_admin", False),
                extra=data.get("extra", {}),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("TokenCache.get malformed payload: %s", exc)
            return None

    def put(
        self,
        token_hash: str,
        ctx: UserContext,
        ttl_seconds: int | None = None,
    ) -> None:
        """Write a ``UserContext`` under the token hash with TTL eviction.

        ``ttl_seconds`` overrides the default. Callers typically pass
        ``min(jwt.exp - now, default)`` so the cache never holds an
        entry beyond the underlying JWT's own expiry.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        if ttl <= 0:
            return  # nothing to cache — avoid Redis[str] SETEX with TTL=0
        payload = {
            "user_id": ctx.user_id,
            "username": ctx.username,
            "agent_type": ctx.agent_type,
            "scopes": list(ctx.scopes),
            "token_id": ctx.token_id,
            "is_admin": ctx.is_admin,
            "extra": ctx.extra,
        }
        try:
            self._redis.setex(self._key(token_hash), ttl, json.dumps(payload))
        except Exception as exc:  # pragma: no cover - resilience path
            logger.warning("TokenCache.put failed: %s", exc)

    def invalidate(self, token_hash: str) -> None:
        """Remove a token's cache entry. Used on logout/revocation."""
        try:
            self._redis.delete(self._key(token_hash))
        except Exception as exc:  # pragma: no cover - resilience path
            logger.warning("TokenCache.invalidate failed: %s", exc)

    def clear(self) -> None:
        """Drop every cache entry under the sovereign:token: prefix.

        Uses ``SCAN`` rather than ``KEYS *`` so the operation is
        non-blocking on a busy Redis. Used by tests and admin tools.
        """
        cursor = 0
        while True:
            cursor, keys = self._redis.scan(
                cursor=cursor, match=f"{self.KEY_PREFIX}*", count=100
            )
            if keys:
                self._redis.delete(*keys)
            if cursor == 0:
                break

    def size(self) -> int:
        """Approximate count of cached entries (via SCAN, not KEYS).

        Used for observability — exposed via the /metrics endpoint in
        a future phase. Returns 0 on Redis error.
        """
        count = 0
        cursor = 0
        try:
            while True:
                cursor, keys = self._redis.scan(
                    cursor=cursor, match=f"{self.KEY_PREFIX}*", count=100
                )
                count += len(keys)
                if cursor == 0:
                    break
            return count
        except Exception:  # pragma: no cover - resilience path
            return 0


# ──────────────── Module-level singleton accessors ──────────────────────────
# Lazy: the Redis connection is created on first access so module imports
# stay cheap and tests can monkey-patch ``_token_cache`` with a fakeredis-
# backed instance without ever touching production settings.

_token_cache: TokenCache | None = None
_redis_client: Redis[str] | None = None


def get_token_cache() -> TokenCache:
    """Return the process-wide TokenCache, creating it on first call.

    Lazy construction so importing this module has zero side effects;
    tests can override ``_token_cache`` before any code path reaches
    here.
    """
    global _token_cache, _redis_client
    if _token_cache is not None:
        return _token_cache

    from redis import Redis

    from sovereign_memory.config import get_settings

    settings = get_settings()
    _redis_client = Redis.from_url(
        settings.redis_url,
        password=settings.redis_password,
        decode_responses=True,
    )
    _token_cache = TokenCache(
        _redis_client,
        default_ttl_seconds=settings.token_cache_ttl_seconds,
    )
    return _token_cache


def reset_token_cache() -> None:
    """Reset the singleton. Used by tests after monkey-patching settings."""
    global _token_cache, _redis_client
    _token_cache = None
    _redis_client = None
