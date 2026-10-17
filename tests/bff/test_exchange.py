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


class TestRequestedScopeParameter:
    """M3-WU-D2-1 — the ``requested_scope`` kwarg is what lets the memory
    path ask for a different scope set than the chat path, from the same
    ``exchange_token`` call. Neuter the ``if requested_scope:`` branch (or
    always send an empty ``scope=``) and one of these two tests goes RED."""

    async def test_omitted_scope_sends_no_scope_form_field(self) -> None:
        """The chat path's default (``requested_scope=None``) — the exact
        WU-1/WU-2 request shape, byte for byte, no regression."""
        minted = _minted_token(sub="alice")
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/certs"):
                return httpx.Response(200, json={"keys": [TEST_PUBLIC_PEM]})
            captured["body"] = request.read().decode()
            return httpx.Response(
                200, json={"access_token": minted, "token_type": "Bearer"}
            )

        client = _client(handler)
        await exchange_token(make_token(sub="alice"), "alice", _settings(), client)
        assert "scope=" not in captured["body"]

    async def test_provided_scope_sent_verbatim_in_form_body(self) -> None:
        """The memory path passes an explicit, space-separated scope
        string — it must reach Keycloak in the ``scope`` form field,
        url-encoded (spaces as ``+`` or ``%20``, both valid
        ``application/x-www-form-urlencoded`` — assert on the decoded
        parse, not the raw encoding, so this test doesn't pin one or the
        other)."""
        from urllib.parse import parse_qs

        minted = _minted_token(sub="alice")
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/certs"):
                return httpx.Response(200, json={"keys": [TEST_PUBLIC_PEM]})
            captured["body"] = request.read().decode()
            return httpx.Response(
                200, json={"access_token": minted, "token_type": "Bearer"}
            )

        client = _client(handler)
        requested = "memory:episodic:read memory:episodic:write"
        await exchange_token(
            make_token(sub="alice"),
            "alice",
            _settings(),
            client,
            requested_scope=requested,
        )
        parsed = parse_qs(captured["body"])
        assert parsed["scope"] == [requested]

    async def test_empty_string_scope_treated_as_omitted(self) -> None:
        """An empty string is falsy — same as ``None``, no ``scope=``
        field at all, never a nonsensical empty grant request."""
        minted = _minted_token(sub="alice")
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/certs"):
                return httpx.Response(200, json={"keys": [TEST_PUBLIC_PEM]})
            captured["body"] = request.read().decode()
            return httpx.Response(
                200, json={"access_token": minted, "token_type": "Bearer"}
            )

        client = _client(handler)
        await exchange_token(
            make_token(sub="alice"),
            "alice",
            _settings(),
            client,
            requested_scope="",
        )
        assert "scope=" not in captured["body"]


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
