"""Shared, cross-replica cache for memory-layer object listings (backlog #15).

Backlog #15 — Defect B: the episodic/procedural S3 services kept their
listing in a *per-process* ``self._cache``. The chart runs
``replicaCount: 3``, so a write on one pod invalidates only that pod's cache;
the other two keep serving a stale ``load()`` until they restart. No local
invalidation can keep the fleet coherent — a list request load-balanced to a
different pod still misses a just-uploaded object.

The fix is a *shared* cache behind a small ABC (:class:`LayerCacheStore`).
The production backend is Redis (already deployed + configured), so a single
``invalidate`` clears the entry for the whole fleet — which makes the
invalidate-on-write in ``routes/memory.py`` the primary correctness
mechanism rather than best-effort defence-in-depth.

Design rules (mirrors ``tools.cache.ToolResultCache`` / ``identity.TokenCache``):

* **Fail OPEN.** S3 is the source of truth (``feedback_storage_always_s3``);
  the cache is only an accelerator. Every Redis call is wrapped so a miss, a
  malformed payload, or a full outage degrades to "behave like a cache miss"
  → the service reads fresh from S3 and the request still succeeds. A cache
  problem must never fail a request.
* **TTL safety net.** ``set`` takes a TTL so a *missed* invalidation cannot
  pin staleness forever — the entry self-heals after the TTL window.
* **Deterministic namespaced keys.** ``audittrace:layer-cache:<layer>:list``,
  disjoint from the ``sovereign:token:`` / ``audittrace:tool-result:``
  namespaces so the shared Redis container's caches cannot collide.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redis import Redis

logger = logging.getLogger(__name__)

KEY_PREFIX = "audittrace:layer-cache:"


def layer_list_cache_key(layer: str) -> str:
    """Deterministic shared-cache key for a layer's object listing.

    One key per layer (``episodic`` / ``procedural``) so an invalidation on
    a write to one layer never evicts the other.
    """
    return f"{KEY_PREFIX}{layer}:list"


class LayerCacheStore(ABC):
    """Abstract shared cache for a layer's object listing.

    Values are JSON-serialisable ``list[dict]`` rows (the loaded layer
    content). Implementations MUST fail open — never raise from ``get`` /
    ``set`` / ``invalidate`` — so the caller can always fall back to a fresh
    S3 read.
    """

    @abstractmethod
    def get(self, key: str) -> list[dict[str, Any]] | None:
        """Return the cached rows for ``key`` or ``None`` on miss/error."""

    @abstractmethod
    def set(self, key: str, value: list[dict[str, Any]], ttl_seconds: int) -> None:
        """Store ``value`` under ``key`` for ``ttl_seconds`` (no-op if ttl<=0)."""

    @abstractmethod
    def invalidate(self, key: str) -> None:
        """Drop ``key`` so the next ``get`` misses and a fresh read repopulates."""


class RedisLayerCacheStore(LayerCacheStore):
    """Redis-backed shared cache — coherent across all replicas.

    One invalidation clears the entry for the whole fleet. Every call fails
    open: a Redis error is logged and treated as a miss / dropped write, so
    the enumeration path degrades to a fresh S3 read rather than a 5xx.
    """

    def __init__(self, redis_client: Redis[str]) -> None:
        self._redis = redis_client

    def get(self, key: str) -> list[dict[str, Any]] | None:
        try:
            raw: Any = self._redis.get(key)
        except Exception as exc:  # fail open — behave like a miss
            logger.warning("LayerCache.get failed (fail-open to S3): %s", exc)
            return None
        if raw is None:
            return None
        try:
            data = json.loads(str(raw))
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(
                "LayerCache.get malformed payload (treating as miss): %s", exc
            )
            return None
        if not isinstance(data, list):
            return None
        return [dict(row) for row in data]

    def set(self, key: str, value: list[dict[str, Any]], ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        try:
            self._redis.setex(key, ttl_seconds, json.dumps(value))
        except Exception as exc:  # fail open — dropped write is harmless
            logger.warning("LayerCache.set failed (ignored): %s", exc)

    def invalidate(self, key: str) -> None:
        try:
            self._redis.delete(key)
        except Exception as exc:  # fail open — TTL is the safety net
            logger.warning("LayerCache.invalidate failed (ignored): %s", exc)


class InMemoryLayerCacheStore(LayerCacheStore):
    """Process-local dict-backed store — for unit tests + direct construction.

    NOT multi-process coherent (that is Redis's job); it stands in for the
    :class:`LayerCacheStore` interface so services are unit-testable without a
    live Redis, and preserves the old per-process caching semantics when a
    service is constructed without an injected shared store.
    """

    def __init__(self) -> None:
        self._store: dict[str, list[dict[str, Any]]] = {}

    def get(self, key: str) -> list[dict[str, Any]] | None:
        rows = self._store.get(key)
        return [dict(row) for row in rows] if rows is not None else None

    def set(self, key: str, value: list[dict[str, Any]], ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        self._store[key] = [dict(row) for row in value]

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)


# ─────────────────── Module-level singleton accessor ──────────────────────
# Lazy construction mirrors ``tools.cache.get_tool_result_cache``: the Redis
# client is built on first access so imports stay cheap and tests can install
# a fake before production code reaches the accessor. ``Redis.from_url`` is
# lazy (connects on first command), so constructing it never fails at startup.

_layer_cache: LayerCacheStore | None = None


def get_layer_cache() -> LayerCacheStore:
    """Return the process-wide shared LayerCacheStore, creating it on first call.

    Uses the same ``settings.redis_url`` as the TokenCache / ToolResultCache —
    the shared Redis container backs all three under disjoint key prefixes.
    """
    global _layer_cache
    if _layer_cache is not None:
        return _layer_cache

    from redis import Redis

    from audittrace.config import get_settings

    settings = get_settings()
    client = Redis.from_url(
        settings.redis_url,
        password=settings.redis_password,
        decode_responses=True,
    )
    _layer_cache = RedisLayerCacheStore(client)
    return _layer_cache


def set_layer_cache(cache: LayerCacheStore) -> None:
    """Install a specific cache instance (test escape hatch).

    Used by tests that want to inject a fakeredis/in-memory store without
    waiting for lazy construction. Production code must never call this.
    """
    global _layer_cache
    _layer_cache = cache


def reset_layer_cache() -> None:
    """Clear the singleton so the next ``get_layer_cache`` rebuilds it.

    Used by test teardown to avoid state bleeding between test cases.
    """
    global _layer_cache
    _layer_cache = None
