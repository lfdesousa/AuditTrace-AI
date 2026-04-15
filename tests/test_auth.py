"""Tests for OAuth2 JWT authentication middleware — ADR-022, ADR-023.

Uses python-jose with a test RSA key pair to create JWTs.
No real Keycloak required — all validation is tested in isolation.
"""

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from sovereign_memory.auth import _fetch_jwks_keys, _jwks_cache, require_scope

# ── Test RSA key pair ──────────────────────────────────────────────────────────

_test_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_test_public_key = _test_private_key.public_key()

TEST_PRIVATE_PEM = _test_private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()

TEST_PUBLIC_PEM = _test_public_key.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()

TEST_ISSUER = "http://keycloak:8080/realms/sovereign-ai"
TEST_AUDIENCE = "sovereign-memory-server"


def _make_token(
    scope: str = "sovereign-ai:query",
    audience: str = TEST_AUDIENCE,
    issuer: str = TEST_ISSUER,
    exp_offset: int = 3600,
    extra_claims: dict | None = None,
) -> str:
    """Create a signed JWT for testing."""
    now = int(time.time())
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": "opencode-agent",
        "scope": scope,
        "iat": now,
        "exp": now + exp_offset,
    }
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, TEST_PRIVATE_PEM, algorithm="RS256")


def _mock_settings(**overrides):
    """Create a mock Settings object with auth fields."""
    defaults = {
        "auth_enabled": True,
        "keycloak_issuer": TEST_ISSUER,
        "keycloak_issuer_extras": [],
        "keycloak_jwks_url": "http://keycloak:8080/realms/sovereign-ai/protocol/openid-connect/certs",
        "jwt_audience": TEST_AUDIENCE,
    }
    defaults.update(overrides)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


def _create_test_app(scope: str) -> FastAPI:
    """Create a minimal FastAPI app with a protected endpoint."""
    app = FastAPI()

    @app.get("/protected")
    async def protected(payload: dict = Depends(require_scope(scope))):
        return {"status": "ok", "payload": payload}

    return app


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_jwks_cache():
    """Clear JWKS cache before each test."""
    _jwks_cache.clear()
    yield
    _jwks_cache.clear()


@pytest.fixture
def mock_jwks():
    """Patch JWKS fetching to return test public key."""
    with patch("sovereign_memory.auth._fetch_jwks_keys") as mock:
        mock.return_value = [TEST_PUBLIC_PEM]
        yield mock


# ── JWKS fetch retry tests ────────────────────────────────────────────────────


class TestJWKSFetchRetry:
    """_fetch_jwks_keys retries with exponential backoff on HTTP errors."""

    def test_succeeds_on_second_attempt(self):
        """First call fails, second succeeds — must return keys."""
        fake_keys = [{"kty": "RSA", "n": "abc"}]
        ok_response = MagicMock()
        ok_response.json.return_value = {"keys": fake_keys}
        ok_response.raise_for_status = MagicMock()

        with (
            patch("sovereign_memory.auth.httpx.get") as mock_get,
            patch("sovereign_memory.auth.time.sleep") as mock_sleep,
        ):
            mock_get.side_effect = [
                httpx.ConnectError("refused"),
                ok_response,
            ]
            result = _fetch_jwks_keys("http://keycloak:8080/jwks")

        assert result == fake_keys
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once()

    def test_exhausts_retries_and_reraises(self):
        """All attempts fail — must raise the last exception."""
        with (
            patch("sovereign_memory.auth.httpx.get") as mock_get,
            patch("sovereign_memory.auth.time.sleep"),
        ):
            mock_get.side_effect = httpx.ConnectError("down")
            with pytest.raises(httpx.ConnectError, match="down"):
                _fetch_jwks_keys("http://keycloak:8080/jwks")

        # 1 initial + 3 retries = 4 calls
        assert mock_get.call_count == 4

    def test_backoff_delays_are_exponential(self):
        """Sleep delays must follow 2^(attempt+1): 2, 4, 8."""
        with (
            patch("sovereign_memory.auth.httpx.get") as mock_get,
            patch("sovereign_memory.auth.time.sleep") as mock_sleep,
        ):
            mock_get.side_effect = httpx.ConnectError("down")
            with pytest.raises(httpx.ConnectError):
                _fetch_jwks_keys("http://keycloak:8080/jwks")

        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays == [2.0, 4.0, 8.0]

    def test_succeeds_immediately_without_sleep(self):
        """Happy path — no retries, no sleep."""
        ok_response = MagicMock()
        ok_response.json.return_value = {"keys": [{"kty": "RSA"}]}
        ok_response.raise_for_status = MagicMock()

        with (
            patch("sovereign_memory.auth.httpx.get", return_value=ok_response),
            patch("sovereign_memory.auth.time.sleep") as mock_sleep,
        ):
            result = _fetch_jwks_keys("http://keycloak:8080/jwks")

        assert result == [{"kty": "RSA"}]
        mock_sleep.assert_not_called()


# ── Auth disabled tests ────────────────────────────────────────────────────────


class TestAuthDisabled:
    def test_auth_disabled_allows_all(self, mock_jwks):
        """When auth_enabled=False, all requests pass without token."""
        with patch(
            "sovereign_memory.auth.get_settings",
            return_value=_mock_settings(auth_enabled=False),
        ):
            app = _create_test_app("sovereign-ai:query")
            client = TestClient(app)
            resp = client.get("/protected")
            assert resp.status_code == 200

    def test_auth_disabled_returns_empty_payload(self, mock_jwks):
        with patch(
            "sovereign_memory.auth.get_settings",
            return_value=_mock_settings(auth_enabled=False),
        ):
            app = _create_test_app("sovereign-ai:query")
            client = TestClient(app)
            resp = client.get("/protected")
            assert resp.json()["payload"] == {}


# ── Auth enabled — error cases ─────────────────────────────────────────────────


class TestAuthErrors:
    def test_missing_token_returns_401(self, mock_jwks):
        with patch("sovereign_memory.auth.get_settings", return_value=_mock_settings()):
            app = _create_test_app("sovereign-ai:query")
            client = TestClient(app)
            resp = client.get("/protected")
            assert resp.status_code == 401

    def test_invalid_token_returns_401(self, mock_jwks):
        with patch("sovereign_memory.auth.get_settings", return_value=_mock_settings()):
            app = _create_test_app("sovereign-ai:query")
            client = TestClient(app)
            resp = client.get("/protected", headers={"Authorization": "Bearer garbage"})
            assert resp.status_code == 401

    def test_expired_token_returns_401(self, mock_jwks):
        token = _make_token(exp_offset=-3600)  # Expired 1 hour ago
        with patch("sovereign_memory.auth.get_settings", return_value=_mock_settings()):
            app = _create_test_app("sovereign-ai:query")
            client = TestClient(app)
            resp = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 401

    def test_wrong_audience_returns_401(self, mock_jwks):
        token = _make_token(audience="wrong-audience")
        with patch("sovereign_memory.auth.get_settings", return_value=_mock_settings()):
            app = _create_test_app("sovereign-ai:query")
            client = TestClient(app)
            resp = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 401

    def test_wrong_issuer_returns_401(self, mock_jwks):
        token = _make_token(issuer="http://evil-server/realms/fake")
        with patch("sovereign_memory.auth.get_settings", return_value=_mock_settings()):
            app = _create_test_app("sovereign-ai:query")
            client = TestClient(app)
            resp = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 401

    def test_missing_scope_returns_403(self, mock_jwks):
        token = _make_token(scope="sovereign-ai:audit")  # Wrong scope
        with patch("sovereign_memory.auth.get_settings", return_value=_mock_settings()):
            app = _create_test_app("sovereign-ai:query")  # Requires :query
            client = TestClient(app)
            resp = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 403

    def test_empty_scope_returns_403(self, mock_jwks):
        token = _make_token(scope="")
        with patch("sovereign_memory.auth.get_settings", return_value=_mock_settings()):
            app = _create_test_app("sovereign-ai:query")
            client = TestClient(app)
            resp = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 403


# ── Auth enabled — success cases ───────────────────────────────────────────────


class TestAuthSuccess:
    def test_valid_token_with_correct_scope(self, mock_jwks):
        token = _make_token(scope="sovereign-ai:query")
        with patch("sovereign_memory.auth.get_settings", return_value=_mock_settings()):
            app = _create_test_app("sovereign-ai:query")
            client = TestClient(app)
            resp = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 200

    def test_multiple_scopes_includes_required(self, mock_jwks):
        token = _make_token(
            scope="sovereign-ai:query sovereign-ai:context sovereign-ai:admin"
        )
        with patch("sovereign_memory.auth.get_settings", return_value=_mock_settings()):
            app = _create_test_app("sovereign-ai:context")
            client = TestClient(app)
            resp = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 200

    def test_payload_contains_claims(self, mock_jwks):
        token = _make_token(scope="sovereign-ai:query")
        with patch("sovereign_memory.auth.get_settings", return_value=_mock_settings()):
            app = _create_test_app("sovereign-ai:query")
            client = TestClient(app)
            resp = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
            payload = resp.json()["payload"]
            assert payload["sub"] == "opencode-agent"
            assert payload["iss"] == TEST_ISSUER
            assert "sovereign-ai:query" in payload["scope"]


# ── ADR-032 multi-issuer acceptance ────────────────────────────────────────────


class TestMultiIssuer:
    """ADR-032: service-account tokens carry the internal docker URL as
    ``iss``; human Device-Flow tokens arrive via Traefik and carry a
    different ``iss``. Both must validate as long as either is in the
    configured allow-set."""

    EXTRA_ISSUER = "http://localhost/realms/sovereign-ai"

    def test_primary_issuer_accepted(self, mock_jwks):
        token = _make_token(scope="sovereign-ai:query", issuer=TEST_ISSUER)
        settings = _mock_settings(keycloak_issuer_extras=[self.EXTRA_ISSUER])
        with patch("sovereign_memory.auth.get_settings", return_value=settings):
            app = _create_test_app("sovereign-ai:query")
            resp = TestClient(app).get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 200

    def test_extra_issuer_accepted(self, mock_jwks):
        """Token minted with the extra issuer claim must pass even
        though it does not match the primary keycloak_issuer."""
        token = _make_token(scope="sovereign-ai:query", issuer=self.EXTRA_ISSUER)
        settings = _mock_settings(keycloak_issuer_extras=[self.EXTRA_ISSUER])
        with patch("sovereign_memory.auth.get_settings", return_value=settings):
            app = _create_test_app("sovereign-ai:query")
            resp = TestClient(app).get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 200

    def test_unknown_issuer_rejected(self, mock_jwks):
        token = _make_token(
            scope="sovereign-ai:query", issuer="http://evil.example/realms/sovereign-ai"
        )
        settings = _mock_settings(keycloak_issuer_extras=[self.EXTRA_ISSUER])
        with patch("sovereign_memory.auth.get_settings", return_value=settings):
            app = _create_test_app("sovereign-ai:query")
            resp = TestClient(app).get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 401

    def test_empty_extras_falls_back_to_primary_only(self, mock_jwks):
        """Default config (empty list) must preserve single-issuer behaviour."""
        token = _make_token(
            scope="sovereign-ai:query", issuer="http://other/realms/sovereign-ai"
        )
        with patch("sovereign_memory.auth.get_settings", return_value=_mock_settings()):
            app = _create_test_app("sovereign-ai:query")
            resp = TestClient(app).get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 401


# ── JWKS caching tests ────────────────────────────────────────────────────────


class TestJWKSCaching:
    def test_jwks_fetched_once_for_multiple_requests(self, mock_jwks):
        token = _make_token(scope="sovereign-ai:query")
        with patch("sovereign_memory.auth.get_settings", return_value=_mock_settings()):
            app = _create_test_app("sovereign-ai:query")
            client = TestClient(app)
            client.get("/protected", headers={"Authorization": f"Bearer {token}"})
            client.get("/protected", headers={"Authorization": f"Bearer {token}"})
            # JWKS should be fetched only once (cached)
            assert mock_jwks.call_count == 1

    def test_cache_cleared_forces_refetch(self, mock_jwks):
        token = _make_token(scope="sovereign-ai:query")
        with patch("sovereign_memory.auth.get_settings", return_value=_mock_settings()):
            app = _create_test_app("sovereign-ai:query")
            client = TestClient(app)
            client.get("/protected", headers={"Authorization": f"Bearer {token}"})
            _jwks_cache.clear()
            client.get("/protected", headers={"Authorization": f"Bearer {token}"})
            assert mock_jwks.call_count == 2


# ───────────────── require_user (Keycloak JWT + Redis cache) ─────────────────
# ADR-026 §15 — identity is delegated to Keycloak.
# require_user validates a JWT against the JWKS endpoint, builds a typed
# UserContext, and caches it under sha256(token) in a Redis-backed cache
# (fakeredis in tests).

import fakeredis  # noqa: E402

from sovereign_memory import identity as _identity_mod  # noqa: E402
from sovereign_memory.auth import require_user  # noqa: E402
from sovereign_memory.identity import (  # noqa: E402
    SENTINEL_SUBJECT,
    SENTINEL_USERNAME,
    TokenCache,
    UserContext,
    hash_token,
)


@pytest.fixture
def fake_token_cache(monkeypatch):
    """Replace the production singleton with a fakeredis-backed TokenCache.

    Keeps tests hermetic — no real Redis container needed, no shared
    state across tests. The same TokenCache class is exercised, only
    the underlying client is the in-process fake.
    """
    fake = fakeredis.FakeRedis(decode_responses=True)
    cache = TokenCache(fake, default_ttl_seconds=300)
    monkeypatch.setattr(_identity_mod, "_token_cache", cache)
    monkeypatch.setattr(_identity_mod, "_redis_client", fake)
    yield cache
    monkeypatch.setattr(_identity_mod, "_token_cache", None)
    monkeypatch.setattr(_identity_mod, "_redis_client", None)


@pytest.fixture
def auth_required_app(fake_token_cache):
    """Tiny FastAPI app exposing /whoami protected by require_user."""
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: UserContext = Depends(require_user)):
        return {
            "user_id": user.user_id,
            "username": user.username,
            "agent_type": user.agent_type,
            "scopes": list(user.scopes),
            "is_admin": user.is_admin,
            "token_id": user.token_id,
            "extra": user.extra,
        }

    return app


@pytest.fixture
def auth_client(auth_required_app):
    return TestClient(auth_required_app)


def _enable_auth(monkeypatch):
    """Flip SOVEREIGN_AUTH_REQUIRED on and rebind the settings cache."""
    from sovereign_memory import config as config_mod

    config_mod.get_settings.cache_clear()
    monkeypatch.setenv("SOVEREIGN_AUTH_REQUIRED", "true")
    monkeypatch.setenv("SOVEREIGN_KEYCLOAK_ISSUER", TEST_ISSUER)
    monkeypatch.setenv("SOVEREIGN_KEYCLOAK_JWKS_URL", "http://test/jwks")
    monkeypatch.setenv("SOVEREIGN_JWT_AUDIENCE", TEST_AUDIENCE)


@pytest.fixture
def auth_required_env(monkeypatch):
    """Enable required mode + clean up afterwards."""
    _enable_auth(monkeypatch)
    yield
    from sovereign_memory import config as config_mod

    config_mod.get_settings.cache_clear()


def _make_user_token(
    *,
    sub: str = "kc-luis-001",
    preferred_username: str = "luis",
    scope: str = "memory:read memory:admin session:read-own",
    email: str = "luis@test",
    name: str = "Luis Filipe",
    jti: str = "jti-test-1",
    exp_offset: int = 3600,
) -> str:
    """Build a Keycloak-shaped JWT for require_user testing."""
    return _make_token(
        scope=scope,
        exp_offset=exp_offset,
        extra_claims={
            "sub": sub,
            "preferred_username": preferred_username,
            "email": email,
            "name": name,
            "jti": jti,
        },
    )


class TestRequireUserBypass:
    """SOVEREIGN_AUTH_REQUIRED=false → sentinel UserContext, no validation."""

    def test_no_token_returns_sentinel(self, auth_client):
        response = auth_client.get("/whoami", headers={"User-Agent": "opencode/1.0"})
        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == SENTINEL_SUBJECT
        assert body["username"] == SENTINEL_USERNAME
        assert body["is_admin"] is True

    def test_token_ignored_in_bypass_mode(self, auth_client):
        response = auth_client.get(
            "/whoami",
            headers={
                "Authorization": "Bearer not-a-real-jwt",
                "User-Agent": "opencode/1.0",
            },
        )
        assert response.status_code == 200
        assert response.json()["username"] == SENTINEL_USERNAME

    def test_user_agent_detected_in_bypass(self, auth_client):
        response = auth_client.get("/whoami", headers={"User-Agent": "continue/0.5"})
        assert response.json()["agent_type"] == "continue"

    def test_unknown_user_agent_falls_back_to_opencode(self, auth_client):
        response = auth_client.get("/whoami", headers={"User-Agent": "wget/1.0"})
        assert response.json()["agent_type"] == "opencode"


class TestRequireUserJWTSuccess:
    def test_valid_jwt_returns_user_context(
        self, auth_client, auth_required_env, mock_jwks
    ):
        token = _make_user_token(sub="kc-luis-001", preferred_username="luis")
        response = auth_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "opencode/1.2.3",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == "kc-luis-001"
        assert body["username"] == "luis"
        assert body["agent_type"] == "opencode"
        assert body["is_admin"] is True
        assert "memory:admin" in body["scopes"]
        assert body["token_id"] == "jti-test-1"
        assert body["extra"]["email"] == "luis@test"

    def test_member_scopes_yield_non_admin(
        self, auth_client, auth_required_env, mock_jwks
    ):
        token = _make_user_token(
            sub="kc-junior",
            preferred_username="junior",
            scope="memory:read session:read-own",
        )
        response = auth_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "opencode/1.0",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["is_admin"] is False
        assert "memory:read" in body["scopes"]
        assert "memory:admin" not in body["scopes"]

    def test_username_falls_back_to_sub_when_missing(
        self, auth_client, auth_required_env, mock_jwks
    ):
        token = _make_token(
            scope="memory:read",
            extra_claims={"sub": "kc-anon", "jti": "jti-anon"},
        )
        response = auth_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "opencode/1.0",
            },
        )
        assert response.status_code == 200
        assert response.json()["username"] == "kc-anon"


class TestRequireUserCacheBehavior:
    def test_cold_path_caches_result(
        self, auth_client, auth_required_env, mock_jwks, fake_token_cache
    ):
        token = _make_user_token(sub="kc-cache-test")

        # First call: cold path, validates JWT, caches
        auth_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "opencode/1.0",
            },
        )

        cached = fake_token_cache.get(hash_token(token))
        assert cached is not None
        assert cached.user_id == "kc-cache-test"

    def test_hot_path_skips_jwks_fetch(
        self, auth_client, auth_required_env, mock_jwks, fake_token_cache
    ):
        token = _make_user_token(sub="kc-hot")

        # Cold path
        auth_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "opencode/1.0",
            },
        )
        first_call_count = mock_jwks.call_count

        # Hot path: same token, cache hit, no new JWKS fetch
        auth_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "opencode/1.0",
            },
        )
        assert mock_jwks.call_count == first_call_count


class TestRequireUserJWTFailures:
    def test_missing_token_raises_401(self, auth_client, auth_required_env):
        response = auth_client.get("/whoami", headers={"User-Agent": "opencode/1.0"})
        assert response.status_code == 401

    def test_garbage_token_raises_401(self, auth_client, auth_required_env, mock_jwks):
        response = auth_client.get(
            "/whoami",
            headers={
                "Authorization": "Bearer not.a.jwt",
                "User-Agent": "opencode/1.0",
            },
        )
        assert response.status_code == 401

    def test_expired_token_raises_401(self, auth_client, auth_required_env, mock_jwks):
        token = _make_user_token(sub="kc-expired", exp_offset=-60)
        response = auth_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "opencode/1.0",
            },
        )
        assert response.status_code == 401

    def test_wrong_audience_raises_401(self, auth_client, auth_required_env, mock_jwks):
        token = _make_token(
            scope="memory:read",
            audience="wrong-audience",
            extra_claims={"sub": "kc-aud", "jti": "jti-aud"},
        )
        response = auth_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "opencode/1.0",
            },
        )
        assert response.status_code == 401

    def test_token_without_sub_raises_401(
        self, auth_client, auth_required_env, mock_jwks
    ):
        # _make_token sets sub="opencode-agent" by default; override to empty
        token = _make_token(
            scope="memory:read",
            extra_claims={"sub": "", "jti": "jti-no-sub"},
        )
        response = auth_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "opencode/1.0",
            },
        )
        assert response.status_code == 401
