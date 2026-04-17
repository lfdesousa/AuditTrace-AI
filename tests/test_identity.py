"""Tests for src/audittrace/identity.py — ADR-026 §15.

Covers:
  - UserContext frozen dataclass (immutability, construction)
  - hash_token / is_admin_scope / sentinel_user_context helpers
  - TokenCache (Redis-backed via fakeredis): get/put/invalidate/clear/size,
    JSON round-trip preserves all UserContext fields, TTL eviction,
    namespace isolation, malformed payload handling

The Redis-protocol-compatible ``fakeredis`` library is used in place of
a real Redis instance — same TokenCache class is constructed with a
fake client, the same code paths are exercised. Production wires
``redis.Redis`` from settings via ``get_token_cache``.
"""

from __future__ import annotations

import json
import time

import fakeredis
import pytest

from audittrace.identity import (
    SENTINEL_SUBJECT,
    SENTINEL_USERNAME,
    TokenCache,
    UserContext,
    hash_token,
    is_admin_scope,
    sentinel_user_context,
)


@pytest.fixture
def fake_redis():
    """Fresh in-process Redis-protocol-compatible client per test."""
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def cache(fake_redis):
    """TokenCache wrapping the fake Redis with a 5-minute default TTL."""
    return TokenCache(fake_redis, default_ttl_seconds=300)


def _user_ctx(**overrides) -> UserContext:
    base = {
        "user_id": "kc-sub-luis",
        "username": "luis",
        "agent_type": "opencode",
        "scopes": ("memory:read", "memory:admin"),
        "token_id": "jti-1",
        "is_admin": True,
        "extra": {"email": "luis@test"},
    }
    base.update(overrides)
    return UserContext(**base)


# ────────────────────────────── UserContext ──────────────────────────────


class TestUserContext:
    def test_construction_minimal(self):
        ctx = UserContext(
            user_id="uuid-1",
            username="luis",
            agent_type="opencode",
            scopes=("memory:read",),
        )
        assert ctx.user_id == "uuid-1"
        assert ctx.username == "luis"
        assert ctx.agent_type == "opencode"
        assert ctx.scopes == ("memory:read",)
        assert ctx.token_id is None
        assert ctx.is_admin is False
        assert ctx.extra == {}

    def test_construction_with_admin(self):
        ctx = UserContext(
            user_id="uuid-1",
            username="admin",
            agent_type="opencode",
            scopes=("memory:read", "memory:admin"),
            is_admin=True,
            token_id="jti-1",
        )
        assert ctx.is_admin is True
        assert ctx.token_id == "jti-1"

    def test_immutable(self):
        """Frozen dataclass — assigning fields must raise."""
        ctx = UserContext(
            user_id="u", username="luis", agent_type="opencode", scopes=()
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            ctx.user_id = "other"  # type: ignore[misc]


# ─────────────────────────── hash_token / helpers ────────────────────────


class TestHashToken:
    def test_deterministic(self):
        assert hash_token("smk_test123") == hash_token("smk_test123")

    def test_differs_for_different_inputs(self):
        assert hash_token("smk_a") != hash_token("smk_b")

    def test_is_sha256_hex(self):
        h = hash_token("eyJhbGciOiJSUzI1NiJ9.payload.sig")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_is_not_the_raw_token(self):
        token = "smk_test"
        assert hash_token(token) != token


class TestIsAdminScope:
    def test_memory_admin_is_admin(self):
        assert is_admin_scope(["memory:admin"]) is True

    def test_admin_namespace_is_admin(self):
        assert is_admin_scope(["admin:tokens:write"]) is True
        assert is_admin_scope(["admin:audit:read"]) is True

    def test_memory_read_is_not_admin(self):
        assert is_admin_scope(["memory:read", "session:read-own"]) is False

    def test_empty_scopes_is_not_admin(self):
        assert is_admin_scope([]) is False


class TestSentinelUserContext:
    def test_default_is_admin_for_dev(self):
        ctx = sentinel_user_context()
        assert ctx.user_id == SENTINEL_SUBJECT
        assert ctx.username == SENTINEL_USERNAME
        assert ctx.is_admin is True
        assert "memory:admin" in ctx.scopes
        assert ctx.token_id is None

    def test_sentinel_id_is_uuid_format(self):
        parts = SENTINEL_SUBJECT.split("-")
        assert len(parts) == 5
        assert [len(p) for p in parts] == [8, 4, 4, 4, 12]

    def test_custom_agent_type(self):
        ctx = sentinel_user_context(agent_type="continue")
        assert ctx.agent_type == "continue"


# ─────────────────────────────── TokenCache ─────────────────────────────


class TestTokenCacheBasic:
    def test_get_returns_none_for_missing_key(self, cache):
        assert cache.get("does-not-exist") is None

    def test_put_then_get_round_trips(self, cache):
        ctx = _user_ctx()
        cache.put("hash-1", ctx)
        loaded = cache.get("hash-1")
        assert loaded is not None
        assert loaded.user_id == ctx.user_id
        assert loaded.username == ctx.username
        assert loaded.agent_type == ctx.agent_type
        assert loaded.scopes == ctx.scopes  # tuple after deserialization
        assert loaded.token_id == ctx.token_id
        assert loaded.is_admin == ctx.is_admin
        assert loaded.extra == ctx.extra

    def test_invalidate_removes_entry(self, cache):
        cache.put("hash-1", _user_ctx())
        assert cache.get("hash-1") is not None
        cache.invalidate("hash-1")
        assert cache.get("hash-1") is None

    def test_invalidate_unknown_is_noop(self, cache):
        cache.invalidate("never-existed")  # must not raise

    def test_clear_removes_only_sovereign_keys(self, cache, fake_redis):
        """clear() must NOT touch keys outside the audittrace:token: namespace."""
        cache.put("hash-1", _user_ctx())
        cache.put("hash-2", _user_ctx(user_id="other"))
        # Stash an unrelated key
        fake_redis.set("not-our-key", "should-survive")

        cache.clear()

        assert cache.get("hash-1") is None
        assert cache.get("hash-2") is None
        assert fake_redis.get("not-our-key") == "should-survive"

    def test_size_counts_only_sovereign_keys(self, cache, fake_redis):
        cache.put("hash-1", _user_ctx())
        cache.put("hash-2", _user_ctx(user_id="other"))
        fake_redis.set("not-our-key", "x")
        assert cache.size() == 2

    def test_size_empty_cache(self, cache):
        assert cache.size() == 0


class TestTokenCacheTTL:
    def test_explicit_ttl_overrides_default(self, cache, fake_redis):
        cache.put("hash-1", _user_ctx(), ttl_seconds=60)
        # Verify Redis TTL via the fake's TTL command
        ttl = fake_redis.ttl("audittrace:token:hash-1")
        assert 0 < ttl <= 60

    def test_default_ttl_used_when_unspecified(self, cache, fake_redis):
        cache.put("hash-1", _user_ctx())
        ttl = fake_redis.ttl("audittrace:token:hash-1")
        assert 250 < ttl <= 300  # default 300, allow drift

    def test_zero_or_negative_ttl_skips_write(self, cache):
        cache.put("hash-1", _user_ctx(), ttl_seconds=0)
        assert cache.get("hash-1") is None
        cache.put("hash-2", _user_ctx(), ttl_seconds=-5)
        assert cache.get("hash-2") is None

    def test_expired_entry_returns_none(self, cache, fake_redis):
        """An entry past its TTL is gone — fakeredis honours expirations."""
        cache.put("hash-1", _user_ctx(), ttl_seconds=1)
        # Force fakeredis time forward
        time.sleep(1.1)
        assert cache.get("hash-1") is None


class TestTokenCacheNamespace:
    def test_keys_are_prefixed(self, cache, fake_redis):
        cache.put("hash-1", _user_ctx())
        # Direct Redis key should be namespaced
        assert fake_redis.exists("audittrace:token:hash-1")
        assert not fake_redis.exists("hash-1")  # bare key not used

    def test_two_caches_share_redis_safely(self, fake_redis):
        """Multiple TokenCache instances on the same Redis must coexist
        as long as they use the same prefix — but writes from one
        must be visible to the other."""
        cache_a = TokenCache(fake_redis, default_ttl_seconds=60)
        cache_b = TokenCache(fake_redis, default_ttl_seconds=60)

        cache_a.put("hash-1", _user_ctx(user_id="from-a"))
        loaded = cache_b.get("hash-1")
        assert loaded is not None
        assert loaded.user_id == "from-a"


class TestTokenCacheMalformed:
    def test_get_returns_none_on_malformed_json(self, cache, fake_redis):
        """A corrupted cache entry must not crash the middleware."""
        fake_redis.setex("audittrace:token:bad-json", 60, "this is not json")
        assert cache.get("bad-json") is None

    def test_get_returns_none_on_missing_required_field(self, cache, fake_redis):
        """Payload missing user_id should be treated as cache miss."""
        fake_redis.setex(
            "audittrace:token:missing-fields",
            60,
            json.dumps({"username": "luis"}),
        )
        assert cache.get("missing-fields") is None
