"""Tests for bff/auth.py — inbound-token validation (fail-closed).

No real Keycloak: JWKS fetches are stubbed via ``httpx.MockTransport``.
"""

from __future__ import annotations

import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from bff.auth import InboundTokenError, get_jwks_keys, validate_inbound_token
from bff.config import Settings
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
        jwks_cache_ttl_seconds=300,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _client_returning_keys(
    keys: list[str], call_counter: list[int] | None = None
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if call_counter is not None:
            call_counter.append(1)
        return httpx.Response(200, json={"keys": keys})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestValidateInboundToken:
    async def test_valid_token_returns_claims(self) -> None:
        token = make_token(sub="alice")
        client = _client_returning_keys([TEST_PUBLIC_PEM])
        claims = await validate_inbound_token(token, _settings(), client)
        assert claims["sub"] == "alice"

    async def test_missing_token_rejected(self) -> None:
        client = _client_returning_keys([TEST_PUBLIC_PEM])
        with pytest.raises(InboundTokenError, match="missing bearer token"):
            await validate_inbound_token(None, _settings(), client)

    async def test_blank_token_rejected(self) -> None:
        client = _client_returning_keys([TEST_PUBLIC_PEM])
        with pytest.raises(InboundTokenError, match="missing bearer token"):
            await validate_inbound_token("   ", _settings(), client)

    async def test_expired_token_rejected(self) -> None:
        token = make_token(sub="alice", exp_offset=-3600)
        client = _client_returning_keys([TEST_PUBLIC_PEM])
        with pytest.raises(InboundTokenError, match="invalid or expired"):
            await validate_inbound_token(token, _settings(), client)

    async def test_wrong_issuer_rejected(self) -> None:
        token = make_token(sub="alice", issuer="http://evil/realms/other")
        client = _client_returning_keys([TEST_PUBLIC_PEM])
        with pytest.raises(InboundTokenError, match="invalid or expired"):
            await validate_inbound_token(token, _settings(), client)

    async def test_extra_issuer_accepted(self) -> None:
        """ADR-032-style multi-issuer: a token from an EXTRA allowed
        issuer validates the same as the primary."""
        token = make_token(sub="alice", issuer="http://gateway/realms/audittrace")
        client = _client_returning_keys([TEST_PUBLIC_PEM])
        settings = _settings(
            keycloak_issuer_extras=["http://gateway/realms/audittrace"]
        )
        claims = await validate_inbound_token(token, settings, client)
        assert claims["sub"] == "alice"

    async def test_token_without_sub_rejected(self) -> None:
        now = int(time.time())
        # Deliberately no 'sub' claim.
        bad_token = jwt.encode(
            {"iss": TEST_ISSUER, "iat": now, "exp": now + 3600},
            TEST_PRIVATE_PEM,
            algorithm="RS256",
        )
        client = _client_returning_keys([TEST_PUBLIC_PEM])
        with pytest.raises(InboundTokenError, match="no usable 'sub'"):
            await validate_inbound_token(bad_token, _settings(), client)

    async def test_wrong_signing_key_rejected(self) -> None:
        """A token signed by a DIFFERENT key than what JWKS serves must
        fail — proves signature verification is real, not a no-op."""
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_pem = other_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        now = int(time.time())
        forged = jwt.encode(
            {"iss": TEST_ISSUER, "sub": "alice", "iat": now, "exp": now + 3600},
            other_pem,
            algorithm="RS256",
        )
        client = _client_returning_keys([TEST_PUBLIC_PEM])
        with pytest.raises(InboundTokenError, match="invalid or expired"):
            await validate_inbound_token(forged, _settings(), client)

    async def test_jwks_fetch_failure_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        token = make_token(sub="alice")
        with pytest.raises(InboundTokenError, match="JWKS fetch failed"):
            await validate_inbound_token(token, _settings(), client)

    async def test_jwks_response_missing_keys_array_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"not_keys": []})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        token = make_token(sub="alice")
        with pytest.raises(InboundTokenError, match="missing a 'keys' array"):
            await validate_inbound_token(token, _settings(), client)


class TestJwksCache:
    async def test_cache_hit_skips_refetch(self) -> None:
        calls: list[int] = []
        client = _client_returning_keys([TEST_PUBLIC_PEM], call_counter=calls)
        settings = _settings(jwks_cache_ttl_seconds=300)

        await get_jwks_keys(client, settings)
        await get_jwks_keys(client, settings)

        assert len(calls) == 1, "second call within TTL must reuse the cache"

    async def test_cache_expiry_triggers_refetch(self) -> None:
        calls: list[int] = []
        client = _client_returning_keys([TEST_PUBLIC_PEM], call_counter=calls)
        settings = _settings(jwks_cache_ttl_seconds=0)

        await get_jwks_keys(client, settings)
        await get_jwks_keys(client, settings)

        assert len(calls) == 2, "a TTL of 0 must refetch every call"
