"""Redis-backed per-session tool result cache (ADR-025 §Decision.8).

Mirrors the ``identity.TokenCache`` pattern: a thin class wrapping a
``redis.Redis`` client with deterministic key namespacing and graceful
degradation on Redis errors. The two caches share the same
``sovereign-redis`` container from DESIGN §15 but live under disjoint
key prefixes so they cannot collide:

  - ``sovereign:token:<sha256>``          — TokenCache (identity.py)
  - ``sovereign:tool-result:<cache_id>``  — ToolResultCache (this file)

TTL is read from ``settings.memory_tool_cache_ttl_seconds``. Setting
the TTL to ``0`` disables both ``get`` and ``put`` entirely — the
handler always runs and nothing is stored. That's the operator's
escape hatch if caching ever causes a correctness issue.

Resilience: every Redis call is wrapped so a cache miss, a malformed
payload, or a full Redis outage all degrade to "behave like a cache
miss". The chat path keeps working and the real handler fires.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redis import Redis

logger = logging.getLogger(__name__)


class ToolResultCache:
    """sha256-keyed ``cache_id`` → tool result dict, TTL-evicted via Redis.

    Cache key shape: ``sovereign:tool-result:<cache_id>`` where ``cache_id``
    is supplied by the caller (typically a hex sha256 of
    ``session_id|tool_name|canonical_args_json``). The class does not
    build the cache_id itself; the invoke helper in ``tools/__init__.py``
    owns that responsibility so the canonicalisation of tool arguments
    lives next to the handler dispatch logic.

    Write-on-success semantics: handler exceptions are never cached.
    That responsibility also sits with the invoke helper — this class
    treats every ``put`` call as a valid write.
    """

    KEY_PREFIX = "sovereign:tool-result:"

    def __init__(self, redis_client: Redis, default_ttl_seconds: int = 900):
        self._redis = redis_client
        self._default_ttl = default_ttl_seconds

    def _key(self, cache_id: str) -> str:
        return f"{self.KEY_PREFIX}{cache_id}"

    def get(self, cache_id: str) -> dict[str, Any] | None:
        """Return the cached result for ``cache_id`` or ``None``.

        ``None`` covers: TTL=0 (cache disabled), key missing, expired
        entry (Redis evicted), unparseable payload, or any Redis
        connectivity error. Callers treat ``None`` uniformly as "miss —
        execute the handler and re-cache".
        """
        if self._default_ttl <= 0:
            return None
        try:
            raw = self._redis.get(self._key(cache_id))
        except Exception as exc:
            logger.warning("ToolResultCache.get failed: %s", exc)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("ToolResultCache.get malformed payload: %s", exc)
            return None

    def put(self, cache_id: str, result: dict[str, Any]) -> None:
        """Store a handler result under ``cache_id`` for the default TTL.

        No-op when the cache is disabled (TTL=0). Swallows Redis errors
        with a warning so a cache-write failure never breaks a chat
        response — the result is returned to the caller regardless.
        """
        if self._default_ttl <= 0:
            return
        try:
            self._redis.setex(
                self._key(cache_id),
                self._default_ttl,
                json.dumps(result),
            )
        except Exception as exc:
            logger.warning("ToolResultCache.put failed: %s", exc)

    def clear(self) -> None:
        """Drop every cache entry under the ``sovereign:tool-result:`` prefix.

        Uses SCAN (not KEYS *) so the operation is non-blocking on a busy
        Redis. Keys under other prefixes (``sovereign:token:`` for the
        TokenCache) are untouched.
        """
        cursor = 0
        while True:
            try:
                cursor, keys = self._redis.scan(
                    cursor=cursor, match=f"{self.KEY_PREFIX}*", count=100
                )
            except Exception as exc:
                logger.warning("ToolResultCache.clear scan failed: %s", exc)
                return
            if keys:
                try:
                    self._redis.delete(*keys)
                except Exception as exc:  # pragma: no cover - resilience path
                    logger.warning("ToolResultCache.clear delete failed: %s", exc)
            if cursor == 0:
                break

    def size(self) -> int:
        """Approximate count of cached entries via SCAN. Returns 0 on Redis
        error so the observability endpoint degrades gracefully."""
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
        except Exception:
            return 0


# ─────────────────── Module-level singleton accessors ──────────────────────
# Lazy construction mirrors identity.get_token_cache: the Redis client is
# created on first access so imports stay cheap and tests can install a
# fakeredis-backed cache before production code reaches the accessor.


_tool_result_cache: ToolResultCache | None = None


def get_tool_result_cache() -> ToolResultCache:
    """Return the process-wide ToolResultCache, creating it on first call.

    Uses the same settings.redis_url as ``identity.TokenCache`` — the
    sovereign-redis container is shared between the two caches with
    disjoint key prefixes.
    """
    global _tool_result_cache
    if _tool_result_cache is not None:
        return _tool_result_cache

    from redis import Redis

    from sovereign_memory.config import get_settings

    settings = get_settings()
    client = Redis.from_url(
        settings.redis_url,
        password=settings.redis_password,
        decode_responses=True,
    )
    _tool_result_cache = ToolResultCache(
        client,
        default_ttl_seconds=settings.memory_tool_cache_ttl_seconds,
    )
    return _tool_result_cache


def set_tool_result_cache(cache: ToolResultCache) -> None:
    """Install a specific cache instance (test escape hatch).

    Used by tests that want to inject a fakeredis-backed cache without
    waiting for lazy construction. Production code must never call this.
    """
    global _tool_result_cache
    _tool_result_cache = cache


def reset_tool_result_cache() -> None:
    """Clear the singleton so the next ``get_tool_result_cache`` call
    rebuilds it. Used by test teardown to avoid state bleeding between
    test cases."""
    global _tool_result_cache
    _tool_result_cache = None
