"""Tests for bff/exchange.py — RFC 8693 token exchange + the identity guard.

The load-bearing test in this file is
``TestIdentityGuard.test_sub_mismatch_rejected``: it proves the
non-negotiable invariant ("the minted token carries the caller's sub")
is REAL — a Keycloak response that mints a token for a DIFFERENT
subject than the caller's own is rejected, never silently proxied.
Flip the ``!=`` to ``==`` (or delete the check) in
``exchange.exchange_token`` and this test goes RED.
"""

from __future__ import annotations

import time

import httpx
import pytest
from jose import jwt

from bff.config import Settings
from bff.exchange import TokenExchangeError, exchange_token
from tests.bff.conftest import (
    TEST_ISSUER,
    TEST_PRIVATE_PEM,
    TEST_PUBLIC_PEM,
    make_token,
)


def _settings(**overrides) -> Settings:
    defaults = dict(
        exchange_client_secret="s3cr3t",
        keycloak_issuer=TEST_ISSUER,
        keycloak_issuer_extras=[],
        keycloak_jwks_url="http://keycloak:8080/realms/audittrace/protocol/openid-connect/certs",
        keycloak_token_url="http://keycloak:8080/realms/audittrace/protocol/openid-connect/token",
        exchange_client_id="audittrace-librechat-bff",
        exchange_audience="audittrace-librechat",
        jwks_cache_ttl_seconds=300,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _minted_token(sub: str, aud: str = "audittrace-server") -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": TEST_ISSUER,
            "sub": sub,
            "aud": aud,
            "scope": "audittrace:query",
            "iat": now,
            "exp": now + 300,
        },
        TEST_PRIVATE_PEM,
        algorithm="RS256",
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestExchangeTokenSuccess:
    async def test_returns_minted_token_for_same_sub(self) -> None:
        minted = _minted_token(sub="alice")

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/certs"):
                return httpx.Response(200, json={"keys": [TEST_PUBLIC_PEM]})
            # /token endpoint
            body = request.read().decode()
            assert (
                "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Atoken-exchange"
                in body
            )
            assert "client_id=audittrace-librechat-bff" in body
            assert "client_secret=s3cr3t" in body
            assert "audience=audittrace-librechat" in body
            return httpx.Response(
                200, json={"access_token": minted, "token_type": "Bearer"}
            )

        client = _client(handler)
        result = await exchange_token(
            make_token(sub="alice"), "alice", _settings(), client
        )
        assert result == minted


class TestExchangeTokenFailureModes:
    async def test_keycloak_non_200_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant"})

        client = _client(handler)
        with pytest.raises(TokenExchangeError, match="rejected by Keycloak"):
            await exchange_token(make_token(sub="alice"), "alice", _settings(), client)

    async def test_transport_failure_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = _client(handler)
        with pytest.raises(TokenExchangeError, match="request failed"):
            await exchange_token(make_token(sub="alice"), "alice", _settings(), client)

    async def test_non_json_response_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        client = _client(handler)
        with pytest.raises(TokenExchangeError, match="not valid JSON"):
            await exchange_token(make_token(sub="alice"), "alice", _settings(), client)

    async def test_missing_access_token_field_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"token_type": "Bearer"})

        client = _client(handler)
        with pytest.raises(TokenExchangeError, match="no access_token"):
            await exchange_token(make_token(sub="alice"), "alice", _settings(), client)

    async def test_minted_token_bad_signature_raises(self) -> None:
        """The BFF re-verifies the minted token's signature rather than
        trusting the transport — a forged/corrupted access_token in the
        Keycloak response must fail, not be forwarded blindly."""

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/certs"):
                return httpx.Response(200, json={"keys": [TEST_PUBLIC_PEM]})
            return httpx.Response(
                200, json={"access_token": "not.a.validjwt", "token_type": "Bearer"}
            )

        client = _client(handler)
        with pytest.raises(TokenExchangeError, match="failed re-validation"):
            await exchange_token(make_token(sub="alice"), "alice", _settings(), client)


class TestIdentityGuard:
    """The falsifiable non-negotiable-invariant test."""

    async def test_sub_mismatch_rejected(self) -> None:
        # Keycloak (or a code regression) mints a token for "mallory"
        # even though the CALLER authenticated as "alice".
        minted_for_wrong_sub = _minted_token(sub="mallory")

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/certs"):
                return httpx.Response(200, json={"keys": [TEST_PUBLIC_PEM]})
            return httpx.Response(
                200,
                json={"access_token": minted_for_wrong_sub, "token_type": "Bearer"},
            )

        client = _client(handler)
        with pytest.raises(TokenExchangeError, match="identity assertion failed"):
            await exchange_token(make_token(sub="alice"), "alice", _settings(), client)

    async def test_matching_sub_accepted(self) -> None:
        """Sibling of the mismatch test: the SAME sub on both sides is
        the happy path — proves the guard isn't just always-raising."""
        minted = _minted_token(sub="alice")

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/certs"):
                return httpx.Response(200, json={"keys": [TEST_PUBLIC_PEM]})
            return httpx.Response(
                200, json={"access_token": minted, "token_type": "Bearer"}
            )

        client = _client(handler)
        result = await exchange_token(
            make_token(sub="alice"), "alice", _settings(), client
        )
        assert result == minted

    async def test_two_distinct_callers_mint_two_distinct_subs(self) -> None:
        """Two different callers exchanging concurrently each get back a
        token carrying THEIR OWN sub — never a shared/static identity."""

        def handler_for(sub: str):
            minted = _minted_token(sub=sub)

            def _h(request: httpx.Request) -> httpx.Response:
                if str(request.url).endswith("/certs"):
                    return httpx.Response(200, json={"keys": [TEST_PUBLIC_PEM]})
                return httpx.Response(
                    200, json={"access_token": minted, "token_type": "Bearer"}
                )

            return _h

        alice_client = _client(handler_for("alice"))
        bob_client = _client(handler_for("bob"))

        alice_minted = await exchange_token(
            make_token(sub="alice"), "alice", _settings(), alice_client
        )
        bob_minted = await exchange_token(
            make_token(sub="bob"), "bob", _settings(), bob_client
        )

        alice_claims = jwt.get_unverified_claims(alice_minted)
        bob_claims = jwt.get_unverified_claims(bob_minted)
        assert alice_claims["sub"] == "alice"
        assert bob_claims["sub"] == "bob"
        assert alice_claims["sub"] != bob_claims["sub"]
        assert alice_minted != bob_minted
