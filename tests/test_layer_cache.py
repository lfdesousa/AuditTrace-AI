"""Tests for the shared cross-replica layer cache (backlog #15, Defect B).

The per-process cache was replaced by a shared :class:`LayerCacheStore` so a
write's invalidation is coherent across all replicas. Every Redis call must
fail OPEN — the cache is only an accelerator, S3 is the source of truth
(``feedback_storage_always_s3``).

All tests use fakeredis so nothing touches a live Redis instance.
"""

from __future__ import annotations

import json

import fakeredis
import pytest

from audittrace.services.layer_cache import (
    InMemoryLayerCacheStore,
    RedisLayerCacheStore,
    get_layer_cache,
    layer_list_cache_key,
    reset_layer_cache,
    set_layer_cache,
)

_ROWS = [{"file": "ADR-1.md", "content": "# a", "last_modified_ms": 1}]


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def store(redis_client):
    return RedisLayerCacheStore(redis_client)


class TestKeyScheme:
    def test_key_is_namespaced_per_layer(self) -> None:
        assert (
            layer_list_cache_key("episodic") == "audittrace:layer-cache:episodic:list"
        )
        assert (
            layer_list_cache_key("procedural")
            == "audittrace:layer-cache:procedural:list"
        )
        # Disjoint from token / tool-result namespaces.
        assert not layer_list_cache_key("episodic").startswith("sovereign:token:")


class TestRedisStore:
    def test_miss_returns_none(self, store) -> None:
        assert store.get(layer_list_cache_key("episodic")) is None

    def test_set_then_get_round_trips(self, store) -> None:
        key = layer_list_cache_key("episodic")
        store.set(key, _ROWS, ttl_seconds=900)
        assert store.get(key) == _ROWS

    def test_set_stores_json(self, store, redis_client) -> None:
        key = layer_list_cache_key("episodic")
        store.set(key, _ROWS, ttl_seconds=900)
        assert json.loads(redis_client.get(key)) == _ROWS

    def test_ttl_zero_is_noop(self, store, redis_client) -> None:
        key = layer_list_cache_key("episodic")
        store.set(key, _ROWS, ttl_seconds=0)
        assert redis_client.get(key) is None

    def test_invalidate_drops_the_key(self, store) -> None:
        key = layer_list_cache_key("episodic")
        store.set(key, _ROWS, ttl_seconds=900)
        store.invalidate(key)
        assert store.get(key) is None

    def test_invalidate_is_fleet_wide_for_two_stores_sharing_redis(
        self, redis_client
    ) -> None:
        """Two stores over ONE Redis == two replicas. An invalidation on one
        clears the entry the other reads — the property per-process caches
        lacked (Defect B)."""
        pod_a = RedisLayerCacheStore(redis_client)
        pod_b = RedisLayerCacheStore(redis_client)
        key = layer_list_cache_key("episodic")
        pod_a.set(key, _ROWS, ttl_seconds=900)
        assert pod_b.get(key) == _ROWS  # pod B sees pod A's write
        pod_a.invalidate(key)
        assert pod_b.get(key) is None  # pod B sees pod A's invalidation

    def test_get_malformed_payload_is_a_miss(self, store, redis_client) -> None:
        key = layer_list_cache_key("episodic")
        redis_client.set(key, "not valid json {{")
        assert store.get(key) is None

    def test_get_non_list_payload_is_a_miss(self, store, redis_client) -> None:
        key = layer_list_cache_key("episodic")
        redis_client.set(key, json.dumps({"not": "a list"}))
        assert store.get(key) is None


class TestFailOpen:
    def test_get_returns_none_on_redis_error(self) -> None:
        class BrokenRedis:
            def get(self, key):
                raise RuntimeError("redis down")

        store = RedisLayerCacheStore(BrokenRedis())
        assert store.get("k") is None  # fail open → caller reads fresh from S3

    def test_set_swallows_redis_error(self) -> None:
        class BrokenRedis:
            def setex(self, key, ttl, value):
                raise RuntimeError("redis down")

        RedisLayerCacheStore(BrokenRedis()).set("k", _ROWS, ttl_seconds=900)  # no raise

    def test_invalidate_swallows_redis_error(self) -> None:
        class BrokenRedis:
            def delete(self, key):
                raise RuntimeError("redis down")

        RedisLayerCacheStore(BrokenRedis()).invalidate("k")  # no raise


class TestInMemoryStore:
    def test_round_trip_and_isolation(self) -> None:
        store = InMemoryLayerCacheStore()
        key = layer_list_cache_key("procedural")
        assert store.get(key) is None
        store.set(key, _ROWS, ttl_seconds=900)
        got = store.get(key)
        assert got == _ROWS
        # Returned rows are copies — mutating them must not corrupt the cache.
        got[0]["file"] = "MUTATED"
        assert store.get(key)[0]["file"] == "ADR-1.md"

    def test_ttl_zero_is_noop(self) -> None:
        store = InMemoryLayerCacheStore()
        key = layer_list_cache_key("episodic")
        store.set(key, _ROWS, ttl_seconds=0)
        assert store.get(key) is None

    def test_invalidate(self) -> None:
        store = InMemoryLayerCacheStore()
        key = layer_list_cache_key("episodic")
        store.set(key, _ROWS, ttl_seconds=900)
        store.invalidate(key)
        assert store.get(key) is None
        store.invalidate(key)  # idempotent — no KeyError


class TestSingleton:
    def test_lazy_builds_once_and_reset(self, monkeypatch) -> None:
        reset_layer_cache()
        import redis

        fake = fakeredis.FakeRedis(decode_responses=True)
        monkeypatch.setattr(redis.Redis, "from_url", lambda *a, **k: fake)
        first = get_layer_cache()
        second = get_layer_cache()
        assert first is second
        assert isinstance(first, RedisLayerCacheStore)
        reset_layer_cache()

    def test_set_layer_cache_installs_instance(self) -> None:
        injected = InMemoryLayerCacheStore()
        set_layer_cache(injected)
        assert get_layer_cache() is injected
        reset_layer_cache()
