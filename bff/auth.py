"""Inbound-token validation for the LibreChat BFF.

The BFF is the confidential-client edge of the M3 console: it must
never proxy an unauthenticated or invalid request, and it must never
fall back to a shared/static credential when the forwarded token is
missing or bad (fail-closed, per the spec's non-negotiable invariant).

Validation here is deliberately narrower than
``audittrace.auth._decode_jwt_with_allowed_issuers``: it checks
signature + issuer + expiry, but NOT audience. That is the whole point
of the BFF — LibreChat's forwarded token might be an access token or an
id_token, with an audience the orchestrator would reject outright; the
BFF accepts anything signed by the AuditTrace realm and lets RFC 8693
token-exchange (``bff/exchange.py``) mint the correctly-audienced token
downstream.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from jose import JWTError, jwt
from jose.exceptions import JWKError

from bff.config import Settings
from bff.inbound_token_failure import InboundTokenFailure

logger = logging.getLogger(__name__)


class InboundTokenError(Exception):
    """Raised when the token LibreChat forwarded fails validation.

    ``failure`` names the class: the caller sent no credentials (``MISSING``),
    sent a token that is not valid for this realm (``INVALID``), or the token
    could not be judged because the realm's keys could not be fetched or used
    (``UNVERIFIABLE``). The chat route answers 401 for the first two and 502
    for the last (``bff/chat_refusals.py``); the other routes keep their own
    401. Never retry with a fallback credential.
    """

    def __init__(
        self,
        message: str,
        failure: InboundTokenFailure = InboundTokenFailure.INVALID,
    ) -> None:
        super().__init__(message)
        self.failure = failure


# Process-wide JWKS cache: ``{"keys": [...], "fetched_at": <epoch>}``.
# Keyed by nothing else — one BFF process talks to exactly one Keycloak
# realm (``Settings.keycloak_jwks_url``), so a single cache slot is
# correct (mirrors ``audittrace.auth._jwks_cache`` at smaller scale).
#
# The HTTP client itself is NOT owned here: ``bff.app`` creates ONE
# pooled ``httpx.AsyncClient`` for the process lifespan (PYTHON-
# ENGINEERING §2) and threads it into every call in this module — JWKS
# fetches, token-exchange, and the orchestrator proxy all share the same
# connection pool rather than each module opening its own.
_jwks_cache: dict[str, Any] = {}


def reset_jwks_state_for_tests() -> None:
    """Test-only reset of the module-level JWKS cache."""
    _jwks_cache.clear()


def _jwks_cache_fresh(now: float, ttl_seconds: int) -> bool:
    return (
        "keys" in _jwks_cache and now - _jwks_cache.get("fetched_at", 0) < ttl_seconds
    )


async def _fetch_jwks_keys(client: httpx.AsyncClient, jwks_url: str) -> list[Any]:
    """Fetch the raw ``keys`` array from the Keycloak JWKS endpoint.

    Raises :class:`InboundTokenError` (class ``UNVERIFIABLE``) on any
    transport/HTTP failure or a body that is not a JWKS object — a JWKS
    fetch failure means the BFF cannot validate ANY token right now, which
    is fail-closed, not a silent pass-through.
    """
    try:
        response = await client.get(jwks_url)
        response.raise_for_status()
        jwks = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise InboundTokenError(
            f"JWKS fetch failed: {exc}", InboundTokenFailure.UNVERIFIABLE
        ) from exc
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if not isinstance(keys, list):
        raise InboundTokenError(
            "JWKS response missing a 'keys' array", InboundTokenFailure.UNVERIFIABLE
        )
    return keys


async def get_jwks_keys(client: httpx.AsyncClient, settings: Settings) -> list[Any]:
    """Get JWKS keys with a process-wide TTL cache (single-flight is not
    needed at BFF scale — a cache-miss stampede here is bounded by one
    Keycloak realm and a handful of concurrent LibreChat sessions)."""
    now = time.time()
    if _jwks_cache_fresh(now, settings.jwks_cache_ttl_seconds):
        return list(_jwks_cache["keys"])
    keys = await _fetch_jwks_keys(client, settings.keycloak_jwks_url)
    _jwks_cache["keys"] = keys
    _jwks_cache["fetched_at"] = now
    return list(keys)


def _decode_with_allowed_issuers(
    token: str,
    keys: Any,
    primary_issuer: str,
    extra_issuers: list[str],
) -> dict[str, Any]:
    """Decode + validate signature/expiry, then cross-check ``iss``
    against the union of the primary + extra allowed issuers.

    Audience is deliberately NOT verified here — see the module
    docstring. python-jose therefore needs ``verify_aud=False`` and no
    ``audience=`` kwarg, or it raises regardless of the token's shape.
    """
    payload = jwt.decode(
        token,
        keys,
        algorithms=["RS256"],
        options={"verify_aud": False},
    )
    token_iss = payload.get("iss")
    allowed = {primary_issuer, *(extra_issuers or [])}
    if not isinstance(token_iss, str) or token_iss not in allowed:
        raise JWTError(
            f"Invalid issuer {token_iss!r}; expected one of {sorted(allowed)!r}"
        )
    assert isinstance(payload, dict), "jwt.decode returned a non-dict payload"
    return payload


async def validate_inbound_token(
    token: str | None,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Validate the token LibreChat forwarded; return its decoded claims.

    Fail-closed on every path: a missing token, a malformed
    ``Authorization`` value, an unparseable/unsigned/expired/wrong-issuer
    JWT, or an unreachable or unusable JWKS endpoint all raise
    :class:`InboundTokenError`, classified where it is raised (no catch-all
    here: the arms name the types python-jose is measured to raise). There is no fallback identity and no
    static credential this function can return instead.
    """
    if not token or not token.strip():
        raise InboundTokenError("missing bearer token", InboundTokenFailure.MISSING)
    try:
        keys = await get_jwks_keys(http_client, settings)
        claims = _decode_with_allowed_issuers(
            token,
            keys,
            primary_issuer=settings.keycloak_issuer,
            extra_issuers=settings.keycloak_issuer_extras,
        )
    except JWTError as exc:
        raise InboundTokenError(f"invalid or expired token: {exc}") from exc
    except (JWKError, ValueError, TypeError) as exc:
        # python-jose does not wrap these (measured): a realm key it cannot
        # build, or a claim of a type it cannot compare. Nobody but the realm
        # can cause either, so the token was not judged: unverifiable.
        raise InboundTokenError(
            f"realm keys or claims unusable: {type(exc).__name__}: {exc}",
            InboundTokenFailure.UNVERIFIABLE,
        ) from exc
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise InboundTokenError("token carries no usable 'sub' claim")
    return claims
