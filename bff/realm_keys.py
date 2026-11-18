"""Which of the realm's published keys may verify a token (ADDENDUM-2 items 4, 5).

Functions only. A JWKS lists every key of the realm: signing keys, encryption
keys, keys of other algorithms, in an order that is not stable across boots.
The BFF verifies RS256 signatures only, so it selects by what the entry SAYS it
is (``kty``, ``use``, ``alg``) and by the ``kid`` the token names, never by
position.
"""

from __future__ import annotations

from typing import Any

from jose import JWTError, jwt

ALGORITHM = "RS256"


def eligible(entry: object) -> bool:
    """An RSA key published for signing with RS256 (absent ``use``/``alg`` allowed)."""
    return (
        isinstance(entry, dict)
        and entry.get("kty") == "RSA"
        and entry.get("use", "sig") == "sig"
        and entry.get("alg", ALGORITHM) == ALGORITHM
    )


def select(keys: list[Any], kid: str | None) -> list[dict[str, Any]]:
    """The eligible keys; when the token names a ``kid``, only that one."""
    candidates = [entry for entry in keys if eligible(entry)]
    if kid is None:
        return candidates
    return [entry for entry in candidates if entry.get("kid") == kid]


def token_kid(token: str) -> str | None:
    """The ``kid`` of the token's JOSE header, or ``None`` when it names none.

    Raises :class:`jose.JWTError` for a token whose header cannot be read or
    whose ``kid`` is not a string: that is the caller's token, not the realm.
    """
    kid = jwt.get_unverified_header(token).get("kid")
    if kid is not None and not isinstance(kid, str):
        raise JWTError("the token's kid is not a string")
    return kid


def decode(token: str, keys: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify signature and expiry against the selected keys.

    ``exp`` is REQUIRED: a token without an expiry is refused. Audience is
    not checked here (see ``bff/auth.py``). No key selected means no key of
    the realm can verify this token.
    """
    if not keys:
        raise JWTError("no realm signing key matches the token's kid")
    claims = jwt.decode(
        token,
        keys,
        algorithms=[ALGORITHM],
        options={"verify_aud": False, "require_exp": True},
    )
    assert isinstance(claims, dict), "jwt.decode returned a non-dict payload"
    return claims
