"""Tests for ToolResultCache (ADR-025 §Decision.8).

Redis-backed per-session tool result cache. Mirrors the TokenCache
pattern from identity.py — the two caches share the same audittrace-redis
container but live under disjoint key prefixes so they cannot collide.

All tests use fakeredis so nothing touches a live Redis instance. The
dev dependency is already pinned in pyproject.toml.
"""

from __future__ import annotations

import json

import fakeredis
import pytest

from audittrace.tools.cache import ToolResultCache


@pytest.fixture
def redis_client():
    """Fresh in-memory Redis for each test."""
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def cache(redis_client):
    """Default cache with 900s TTL — the production default."""
    return ToolResultCache(redis_client, default_ttl_seconds=900)


# ─────────────────────────── Basic get / put ────────────────────────────────


class TestGetPut:
    def test_miss_returns_none(self, cache):
        assert cache.get("unknown-key") is None

    def test_put_then_get_returns_payload(self, cache):
        payload = {"matches": [{"title": "ADR-009"}], "total": 1, "truncated": False}
        cache.put("key-1", payload)
        assert cache.get("key-1") == payload

    def test_put_stores_json_encoded_value(self, cache, redis_client):
        """Verify the on-wire format so a crash dump is human-readable."""
        payload = {"matches": [], "total": 0, "truncated": False}
        cache.put("key-1", payload)
        raw = redis_client.get(f"{ToolResultCache.KEY_PREFIX}key-1")
        assert raw is not None
        assert json.loads(raw) == payload

    def test_key_prefix_is_tool_result(self, cache, redis_client):
        """Namespace must be 'audittrace:tool-result:' so TokenCache keys
        (prefix 'audittrace:token:') cannot collide."""
        cache.put("abc123", {"matches": [], "total": 0, "truncated": False})
        keys = list(redis_client.scan_iter(match="audittrace:tool-result:*"))
        assert len(keys) == 1
        # And NO entry ended up under the token namespace
        token_keys = list(redis_client.scan_iter(match="audittrace:token:*"))
        assert token_keys == []

    def test_get_malformed_payload_returns_none(self, cache, redis_client):
        """Corrupt or unparseable cache value is treated as a miss so the
        caller simply re-executes the handler."""
        redis_client.set(
            f"{ToolResultCache.KEY_PREFIX}key-1",
            "not valid json {{",
        )
        assert cache.get("key-1") is None


# ───────────────────────── TTL = 0 disables the cache ──────────────────────


class TestDisabled:
    def test_ttl_zero_get_always_misses(self, redis_client):
        cache = ToolResultCache(redis_client, default_ttl_seconds=0)
        # Seed something at the cache key directly — the disabled cache
        # must not return it regardless.
        redis_client.set(
            f"{ToolResultCache.KEY_PREFIX}key-1",
            json.dumps({"matches": [], "total": 0, "truncated": False}),
        )
        assert cache.get("key-1") is None

    def test_ttl_zero_put_is_noop(self, redis_client):
        cache = ToolResultCache(redis_client, default_ttl_seconds=0)
        cache.put("key-1", {"matches": [], "total": 0, "truncated": False})
        keys = list(redis_client.scan_iter(match="audittrace:tool-result:*"))
        assert keys == []


# ────────────────────── Resilience on Redis errors ──────────────────────────


class TestResilience:
    def test_get_returns_none_on_redis_error(self, monkeypatch):
        """A Redis outage must degrade gracefully: get returns None (treated
        as a miss by the caller, who will re-execute the handler)."""

        class BrokenRedis:
            def get(self, key):
                raise RuntimeError("redis down")

        cache = ToolResultCache(BrokenRedis(), default_ttl_seconds=900)
        assert cache.get("key-1") is None

    def test_put_swallows_redis_errors(self):
        """put() must never raise — a Redis hiccup cannot break the chat
        response path. The write is simply dropped."""

        class BrokenRedis:
            def setex(self, key, ttl, value):
                raise RuntimeError("redis down")

        cache = ToolResultCache(BrokenRedis(), default_ttl_seconds=900)
        # Must not raise
        cache.put("key-1", {"matches": [], "total": 0, "truncated": False})


# ─────────────────────────── clear + size ───────────────────────────────────


class TestHousekeeping:
    def test_clear_removes_all_entries(self, cache, redis_client):
        for i in range(3):
            cache.put(f"k-{i}", {"matches": [], "total": i, "truncated": False})
        assert cache.size() == 3
        cache.clear()
        assert cache.size() == 0
        # And only OUR namespace was touched — a sibling token entry survives
        redis_client.set("audittrace:token:xyz", "preserved")
        cache.clear()
        assert redis_client.get("audittrace:token:xyz") == "preserved"

    def test_size_returns_zero_on_redis_error(self):
        class BrokenRedis:
            def scan(self, cursor, match, count):
                raise RuntimeError("redis down")

        cache = ToolResultCache(BrokenRedis(), default_ttl_seconds=900)
        assert cache.size() == 0

    def test_clear_returns_silently_on_scan_error(self):
        """A Redis outage during clear() must not raise — the cache is a
        best-effort layer, never a correctness gate."""

        class BrokenScanRedis:
            def scan(self, cursor, match, count):
                raise RuntimeError("redis down")

        cache = ToolResultCache(BrokenScanRedis(), default_ttl_seconds=900)
        cache.clear()  # must not raise


# ─────────────────── Singleton accessor lazy construction ──────────────────


class TestSingleton:
    def test_get_tool_result_cache_lazy_builds_once(self, monkeypatch):
        """The first get_tool_result_cache() call must construct a real
        ToolResultCache from settings; subsequent calls return the same
        instance. set_tool_result_cache + reset round-trip covers the test
        escape hatch in one shot."""
        from audittrace.tools import cache as cache_mod

        cache_mod.reset_tool_result_cache()

        # Monkey-patch Redis.from_url so we don't touch a real Redis
        # instance during the lazy-build path.
        import redis

        fake_client = fakeredis.FakeRedis(decode_responses=True)
        monkeypatch.setattr(redis.Redis, "from_url", lambda *a, **k: fake_client)

        first = cache_mod.get_tool_result_cache()
        second = cache_mod.get_tool_result_cache()
        assert first is second  # same singleton
        assert isinstance(first, ToolResultCache)

        cache_mod.reset_tool_result_cache()
