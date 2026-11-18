"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-2, items 4, 5 and 6: which realm key may
verify a token, ``exp`` is required, and an unknown ``kid`` refreshes the key
set once, bounded.

Lands as ``tests/bff/test_realm_keys.py``. The HTTP cells go through the real
chat route (the fixture of ``test_chat_route_errors``); the single-flight cell
drives ``bff.auth.get_signing_keys`` with an upstream that answers only when
every caller is waiting, so it proves the property without a clock.

Neuters (each alone): K1 select by position again (pass every key) -> the four
``ineligible`` cells and ``kid_names_another_key``; K2 drop ``use`` from the
eligibility rule -> ``published_for_encryption_without_alg``; K3 drop ``alg`` -> ``published
_for_rs384``; K4 drop ``require_exp`` -> both ``without_exp`` tests; K5 never
refresh on an unknown kid -> ``test_a_rotated_key_is_found_by_one_refresh``; K6
remove the bound -> ``test_unknown_kids_cannot_cause_more_than_one_fetch``; K7
remove the lock -> ``test_concurrent_unknown_kids_share_one_refresh``; K8 answer
an unknown kid 401 after a FAILED refresh -> ``test_a_failed_refresh_is_not_the
_callers_fault``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwk, jwt

import bff.auth
from bff.chat_scopes import CHAT_SCOPE_STRING
from bff.config import get_settings
from tests.bff.conftest import (
    TEST_ISSUER,
    TEST_JWK,
    TEST_KID,
    TEST_PRIVATE_PEM,
    make_token,
)
from tests.bff.test_app import _settings
from tests.bff.test_chat_route_errors import (  # noqa: F401  (chat is a fixture)
    AUTH_SERVICE_UNAVAILABLE,
    INVALID_TOKEN,
    OKP_JWK,
    TOKEN_EXCHANGE_INVALID,
    _expected_body,
    _post,
    _Upstreams,
    chat,
)

_second = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SECOND_PEM = _second.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
SECOND_PUBLIC_PEM = (
    _second.public_key()
    .public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    .decode()
)
SECOND_KID = "second-key"


def _second_jwk(**fields: str) -> dict[str, str]:
    """The second key as a JWKS entry; ``fields`` override kid/use/alg."""
    entry = {**jwk.construct(SECOND_PUBLIC_PEM, "RS256").to_dict(), "kid": SECOND_KID}
    return {**entry, "use": "sig", **fields}


def _signed_by_second(kid: str | None = SECOND_KID) -> str:
    return make_token(
        sub="alice",
        extra_claims={"scope": CHAT_SCOPE_STRING},
        private_pem=SECOND_PEM,
        kid=kid,
    )


def _caller() -> str:
    return make_token(sub="alice", extra_claims={"scope": CHAT_SCOPE_STRING})


def _serve(upstreams: _Upstreams, *keys: Any) -> None:
    upstreams.jwks = lambda _request: httpx.Response(200, json={"keys": list(keys)})


def _assert(response: httpx.Response, refusal: Any) -> None:
    assert response.status_code == refusal[0]
    assert response.headers.get("www-authenticate") == refusal[4]
    assert response.json() == _expected_body(refusal)


# ── item 4: selection by kid and by what the entry says it is ───────────────


def test_a_key_of_another_type_listed_first_does_not_matter(
    chat: tuple[TestClient, _Upstreams],  # noqa: F811
) -> None:
    """The order of a realm's keys is not stable across boots (measured)."""
    client, upstreams = chat
    _serve(upstreams, OKP_JWK, TEST_JWK)
    response = _post(client, "Bearer " + _caller())

    assert response.status_code == 200


INELIGIBLE: dict[str, dict[str, str]] = {
    "published_for_encryption": {"use": "enc", "alg": "RSA-OAEP"},
    "published_for_encryption_without_alg": {"use": "enc"},
    "published_for_rs384": {"alg": "RS384"},
    "published_as_another_key_type": {"kty": "EC"},
}


@pytest.mark.parametrize("why", sorted(INELIGIBLE))
def test_a_key_not_published_for_rs256_signing_verifies_nothing(
    chat: tuple[TestClient, _Upstreams],  # noqa: F811
    why: str,
) -> None:
    """A token signed by a key the realm publishes for something else is
    refused, even though the key material would verify it."""
    client, upstreams = chat
    _serve(upstreams, _second_jwk(**INELIGIBLE[why]), TEST_JWK)
    response = _post(client, "Bearer " + _signed_by_second())

    _assert(response, INVALID_TOKEN)
    assert (upstreams.exchange_calls, upstreams.memory_server_calls) == (0, 0)


def test_kid_names_another_key(
    chat: tuple[TestClient, _Upstreams],  # noqa: F811
) -> None:
    """The token says which key signed it. A token signed by the second key
    but naming the first is verified against the first only."""
    client, upstreams = chat
    _serve(upstreams, _second_jwk(), TEST_JWK)
    response = _post(client, "Bearer " + _signed_by_second(kid=TEST_KID))

    _assert(response, INVALID_TOKEN)


def test_a_token_naming_no_kid_is_tried_against_every_signing_key(
    chat: tuple[TestClient, _Upstreams],  # noqa: F811
) -> None:
    """RFC 7515 makes ``kid`` optional. Control: green before and after."""
    client, upstreams = chat
    _serve(upstreams, TEST_JWK, _second_jwk())
    response = _post(client, "Bearer " + _signed_by_second(kid=None))

    assert response.status_code == 200


def test_a_realm_without_any_rs256_signing_key_is_unusable(
    chat: tuple[TestClient, _Upstreams],  # noqa: F811
) -> None:
    client, upstreams = chat
    _serve(upstreams, _second_jwk(use="enc"), OKP_JWK)
    response = _post(client, "Bearer " + _caller())

    _assert(response, AUTH_SERVICE_UNAVAILABLE)


# ── item 5: exp is required ────────────────────────────────────────────────


def _without_exp(**claims: Any) -> str:
    payload = {"iss": TEST_ISSUER, "sub": "alice", "iat": int(time.time()), **claims}
    return jwt.encode(
        payload, TEST_PRIVATE_PEM, algorithm="RS256", headers={"kid": TEST_KID}
    )


def test_an_inbound_token_without_exp_is_refused(
    chat: tuple[TestClient, _Upstreams],  # noqa: F811
) -> None:
    client, upstreams = chat
    response = _post(client, "Bearer " + _without_exp(scope=CHAT_SCOPE_STRING))

    _assert(response, INVALID_TOKEN)
    assert (upstreams.exchange_calls, upstreams.memory_server_calls) == (0, 0)


def test_a_minted_token_without_exp_is_refused(
    chat: tuple[TestClient, _Upstreams],  # noqa: F811
) -> None:
    client, upstreams = chat
    upstreams.exchange = lambda _request: httpx.Response(
        200, json={"access_token": _without_exp(scope=CHAT_SCOPE_STRING)}
    )
    response = _post(client, "Bearer " + _caller())

    _assert(response, TOKEN_EXCHANGE_INVALID)
    assert upstreams.memory_server_calls == 0


# ── item 6: one bounded refresh on an unknown kid ───────────────────────────


def _rotating(upstreams: _Upstreams) -> None:
    """The realm publishes the second key from the SECOND fetch on."""

    def respond(_request: httpx.Request) -> httpx.Response:
        keys = [TEST_JWK] if upstreams.jwks_calls < 2 else [TEST_JWK, _second_jwk()]
        return httpx.Response(200, json={"keys": keys})

    upstreams.jwks = respond


@pytest.fixture
def cached(
    chat: tuple[TestClient, _Upstreams],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, _Upstreams, list[float]]:
    """The chat fixture with a real key cache (TTL 300 s), one good turn
    already served (so the cache holds the first key), and a clock a test can
    step: ``clock[0]`` seconds."""
    client, upstreams = chat
    clock = [1000.0]
    monkeypatch.setattr(bff.auth, "_monotonic", lambda: clock[0], raising=False)
    client.app.dependency_overrides[get_settings] = _settings  # type: ignore[attr-defined]
    _rotating(upstreams)
    assert _post(client, "Bearer " + _caller()).status_code == 200
    assert upstreams.jwks_calls == 1
    return client, upstreams, clock


def test_a_rotated_key_is_found_by_one_refresh(
    cached: tuple[TestClient, _Upstreams, list[float]],
) -> None:
    client, upstreams, clock = cached
    clock[0] += 31  # the cache is older than the refresh bound (30 s)
    response = _post(client, "Bearer " + _signed_by_second())

    assert response.status_code == 200
    assert upstreams.jwks_calls == 2


def test_unknown_kids_cannot_cause_more_than_one_fetch(
    cached: tuple[TestClient, _Upstreams, list[float]],
) -> None:
    """However many tokens name unknown kids, the realm is asked at most once
    per bound; inside it the answer is 401 without a fetch."""
    client, upstreams, clock = cached
    for attempt in range(5):  # fresh cache: no refresh at all
        response = _post(client, "Bearer " + _signed_by_second(kid=f"nope-{attempt}"))
        _assert(response, INVALID_TOKEN)
    assert upstreams.jwks_calls == 1
    clock[0] += 31
    for attempt in range(5):  # bound elapsed: exactly one refresh for all five
        response = _post(client, "Bearer " + _signed_by_second(kid=f"nope-{attempt}"))
        _assert(response, INVALID_TOKEN)
    assert upstreams.jwks_calls == 2
    assert _post(client, "Bearer " + _caller()).status_code == 200  # known kid: served


def test_a_failed_refresh_is_not_the_callers_fault(
    cached: tuple[TestClient, _Upstreams, list[float]],
) -> None:
    """The one allowed refresh fails: the realm was not asked, so the answer is
    'sign-in service unavailable', now and until the bound allows a retry.
    Tokens of a cached kid are still served."""
    client, upstreams, clock = cached
    clock[0] += 31

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    upstreams.jwks = down
    for _ in range(3):
        _assert(
            _post(client, "Bearer " + _signed_by_second()), AUTH_SERVICE_UNAVAILABLE
        )
    assert upstreams.jwks_calls == 2
    assert _post(client, "Bearer " + _caller()).status_code == 200


def test_concurrent_unknown_kids_share_one_refresh() -> None:
    """Ten callers name a kid the cache does not hold. The upstream answers
    only once all ten are waiting; exactly one refresh happens and all ten get
    the key."""
    fetches: list[int] = []

    async def scenario() -> list[list[dict[str, Any]]]:
        release = asyncio.Event()

        async def respond(_request: httpx.Request) -> httpx.Response:
            fetches.append(1)
            if len(fetches) == 1:
                return httpx.Response(200, json={"keys": [TEST_JWK]})
            await release.wait()
            return httpx.Response(200, json={"keys": [TEST_JWK, _second_jwk()]})

        settings = _settings().model_copy(
            update={"jwks_refresh_min_interval_seconds": 0}
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await bff.auth.get_signing_keys(client, settings, TEST_KID)
            tasks = [
                asyncio.create_task(
                    bff.auth.get_signing_keys(client, settings, SECOND_KID)
                )
                for _ in range(10)
            ]
            for _ in range(20):  # let every task reach the lock or the fetch
                await asyncio.sleep(0)
            release.set()
            return list(await asyncio.gather(*tasks))

    results = asyncio.run(scenario())
    assert len(fetches) == 2
    assert [[key["kid"] for key in keys] for keys in results] == [[SECOND_KID]] * 10
