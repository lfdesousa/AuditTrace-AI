"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-3 (gate A4): the memory server's own token
check selects the realm key like the BFF does, requires ``exp``, and refreshes
the key set once, bounded, on an unknown ``kid``.

Before this, ``audittrace.auth`` handed python-jose the whole JWKS: a key
published for encryption verified a signature, a key python-jose cannot build
listed first broke every request, the token's ``kid`` was ignored, a token
without ``exp`` was accepted, and a rotated key was unknown for the 300 s the
cache lives, so a chat turn the BFF had just accepted was refused here.

Neuters (each alone): V1 pass every key to the decode -> ``ineligible`` cells,
``kid_names_another_key``, ``unusable_key_listed_first``; V2 drop ``require_exp``
-> ``test_a_token_without_exp_is_refused``; V3 never refresh ->
``test_a_rotated_key_is_found_by_one_refresh``; V4 remove the bound ->
``test_unknown_kids_cannot_cause_more_than_one_fetch``; V5 let a failed refresh
raise -> ``test_a_failed_refresh_keeps_the_cached_keys``; V6 drop the issuer
type check -> ``test_an_issuer_that_is_not_a_string_is_refused``; V7 drop the arm for what
python-jose does not wrap -> ``test_what_jose_does_not_wrap_is_a_refusal``.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from jose import JWTError, jwk, jwt

import audittrace.auth as auth

ISSUER = "http://keycloak:8080/realms/audittrace"
AUDIENCE = "audittrace-server"
URL = "http://keycloak.test/jwks"


def _pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    return private, public


FIRST_PRIVATE, FIRST_PUBLIC = _pair()
SECOND_PRIVATE, SECOND_PUBLIC = _pair()


def _jwk(public: str, kid: str, **fields: str) -> dict[str, str]:
    return {
        **jwk.construct(public, "RS256").to_dict(),
        "kid": kid,
        "use": "sig",
        **fields,
    }


FIRST = _jwk(FIRST_PUBLIC, "first")
SECOND = _jwk(SECOND_PUBLIC, "second")
OKP = {
    "kty": "OKP",
    "crv": "Ed25519",
    "use": "sig",
    "alg": "EdDSA",
    "kid": "okp",
    "x": base64.urlsafe_b64encode(
        ed25519.Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    .rstrip(b"=")
    .decode(),
}


def _token(
    private: str = FIRST_PRIVATE, kid: str | None = "first", **claims: Any
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "alice",
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims)
    payload = {name: value for name, value in payload.items() if value is not None}
    headers = {"kid": kid} if kid is not None else None
    return str(jwt.encode(payload, private, algorithm="RS256", headers=headers))


def _decode(token: str, keys: list[Any]) -> dict[str, Any]:
    claims: dict[str, Any] = auth._decode_jwt_with_allowed_issuers(
        token, keys, audience=AUDIENCE, primary_issuer=ISSUER, extra_issuers=[]
    )
    return claims


def test_an_unusable_key_listed_first_does_not_matter() -> None:
    assert _decode(_token(), [OKP, FIRST])["sub"] == "alice"


INELIGIBLE: dict[str, dict[str, str]] = {
    "published_for_encryption": {"use": "enc"},
    "published_for_rs384": {"alg": "RS384"},
    "published_as_another_key_type": {"kty": "EC"},
}


@pytest.mark.parametrize("why", sorted(INELIGIBLE))
def test_a_key_not_published_for_rs256_signing_verifies_nothing(why: str) -> None:
    second = _jwk(SECOND_PUBLIC, "second", **INELIGIBLE[why])
    with pytest.raises(JWTError):
        _decode(_token(SECOND_PRIVATE, "second"), [second, FIRST])


def test_kid_names_another_key() -> None:
    with pytest.raises(JWTError):
        _decode(_token(SECOND_PRIVATE, "first"), [SECOND, FIRST])


def test_a_token_naming_no_kid_is_tried_against_every_signing_key() -> None:
    assert _decode(_token(SECOND_PRIVATE, None), [FIRST, SECOND])["sub"] == "alice"


def test_a_token_without_exp_is_refused() -> None:
    with pytest.raises(JWTError):
        _decode(_token(exp=None), [FIRST])


UNWRAPPED: dict[str, tuple[list[Any], dict[str, Any]]] = {
    # an RSA entry that names the token's kid and carries no key material
    "realm_key_without_material": ([{"kty": "RSA", "kid": "first"}], {}),
    # a claim python-jose compares without checking its type
    "audience_claim_is_a_number": ([FIRST], {"aud": 7}),
}


@pytest.mark.parametrize("what", sorted(UNWRAPPED))
def test_what_jose_does_not_wrap_is_a_refusal(what: str) -> None:
    """Never an exception outside ``JWTError``: that would be a 500."""
    keys, claims = UNWRAPPED[what]
    with pytest.raises(JWTError):
        _decode(_token(**claims), keys)


def test_an_issuer_that_is_not_a_string_is_refused() -> None:
    with pytest.raises(JWTError):
        _decode(_token(iss=[ISSUER]), [FIRST])


# ── the bounded refresh on an unknown kid ───────────────────────────────────


@pytest.fixture
def realm(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A fake JWKS endpoint that publishes the second key from the second fetch
    on, a fresh cache holding the first fetch, and a clock a test can step."""
    state: dict[str, Any] = {"fetches": 0, "clock": 1000.0, "down": False}

    async def fetch(_url: str) -> list[Any]:
        state["fetches"] += 1
        if state["down"]:
            raise httpx.ConnectError("refused")
        return [FIRST] if state["fetches"] < 2 else [FIRST, SECOND]

    monkeypatch.setattr(auth, "_fetch_jwks_keys", fetch)
    monkeypatch.setattr(auth, "_jwks_cache", {})
    monkeypatch.setattr(auth, "_jwks_fetch_lock", None)
    monkeypatch.setattr(auth, "_monotonic", lambda: state["clock"], raising=False)
    assert asyncio.run(auth._get_jwks_keys(URL, kid="first")) == [FIRST]
    assert state["fetches"] == 1
    return state


def _keys(kid: str) -> list[Any]:
    monkey_lock_reset()
    return asyncio.run(auth._get_jwks_keys(URL, kid=kid))


def monkey_lock_reset() -> None:
    # Each asyncio.run is a new loop; the module's lock belongs to the old one.
    auth._jwks_fetch_lock = None


def test_a_rotated_key_is_found_by_one_refresh(realm: dict[str, Any]) -> None:
    realm["clock"] += 31
    assert _keys("second") == [FIRST, SECOND]
    assert realm["fetches"] == 2


def test_unknown_kids_cannot_cause_more_than_one_fetch(realm: dict[str, Any]) -> None:
    for attempt in range(5):  # fresh cache: no refresh at all
        assert _keys(f"nope-{attempt}") == [FIRST]
    assert realm["fetches"] == 1
    realm["clock"] += 31
    for attempt in range(5):  # bound elapsed: exactly one refresh for all five
        _keys(f"nope-{attempt}")
    assert realm["fetches"] == 2


def test_a_failed_refresh_keeps_the_cached_keys(realm: dict[str, Any]) -> None:
    realm["clock"] += 31
    realm["down"] = True
    for _ in range(3):
        assert _keys("second") == [FIRST]
    assert realm["fetches"] == 2
    assert _decode(_token(), _keys("first"))["sub"] == "alice"


def test_concurrent_unknown_kids_share_one_refresh(realm: dict[str, Any]) -> None:
    realm["clock"] += 31
    monkey_lock_reset()

    async def ten() -> list[list[Any]]:
        return list(
            await asyncio.gather(
                *(auth._get_jwks_keys(URL, kid="second") for _ in range(10))
            )
        )

    results = asyncio.run(ten())
    assert realm["fetches"] == 2
    assert results == [[FIRST, SECOND]] * 10
