"""RFC 8693 token exchange — the identity guarantee.

Exchanges the caller's forwarded token for a fresh access token minted
by the AuditTrace realm's confidential client
(``audittrace-librechat-bff``), requesting the ``audittrace-librechat``
client's audience so the result deterministically carries
``aud=audittrace-server`` (the ``aud-audittrace-server`` protocol mapper
WU-1 already put on that client — see ``bff/README.md``).

The exchange's ``scope`` is per-caller, not fixed: the chat path
(``bff/app.py::chat_completions``) calls :func:`exchange_token` with
``requested_scope=None``, which omits the ``scope`` form field entirely —
Keycloak then falls back to the target client's own DEFAULT scopes
(``audittrace:query`` among them), exactly WU-1/WU-2's original,
unregressed behaviour. The memory-proxy path (M3-WU-D2-1,
``bff/memory_proxy.py``) passes
``requested_scope=bff.memory_scopes.MEMORY_SCOPE_STRING`` to explicitly
request the memory read/write scopes, which are OPTIONAL (not default) on
``audittrace-librechat`` — see ``keycloak/realm-audittrace.json`` — so
only an exchange that asks for them by name ever gets them; the browser's
own login token never carries write access to the store.

The non-negotiable invariant this module exists to prove: **the minted
token carries the caller's ``sub``, never a static/shared identity.**
:func:`exchange_token` re-validates the exchanged token's signature and
asserts its ``sub`` matches the inbound token's ``sub`` before ever
returning it — a Keycloak misconfiguration (or a code regression that
stamps a static subject) fails closed here, not silently downstream. This
guarantee holds identically for both the chat and memory-scoped exchange
— ``requested_scope`` only changes what the minted token can DO, never
whose identity it carries.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from jose import JWTError, jwt

from bff.auth import get_jwks_keys
from bff.config import Settings

logger = logging.getLogger(__name__)


class TokenExchangeError(Exception):
    """Raised when RFC 8693 token exchange fails, or the exchanged token
    fails re-validation / the identity (``sub``) consistency check.

    Callers (the FastAPI route) MUST map this to HTTP 502 — a downstream
    dependency (Keycloak) failure, not a caller auth failure — EXCEPT the
    identity-mismatch case, which is a fail-closed internal-invariant
    violation and should never reach a caller as anything but a hard
    failure to proxy.
    """


async def exchange_token(
    inbound_token: str,
    inbound_sub: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
    requested_scope: str | None = None,
) -> str:
    """RFC 8693 token-exchange ``inbound_token`` for an
    ``aud=audittrace-server`` access token minted for the SAME subject.

    ``requested_scope`` is an explicit, space-separated RFC 8693 ``scope``
    request (see the module docstring). ``None`` (the chat path's default)
    omits the ``scope`` form field entirely, preserving the original
    WU-1/WU-2 exchange shape byte-for-byte — this is a deliberate
    non-regression choice, not an oversight: adding an always-present
    empty-string ``scope=`` field would itself be a behaviour change for
    Keycloak's exchange endpoint.

    Returns the raw minted access token string. Raises
    :class:`TokenExchangeError` on any Keycloak-side failure, a
    malformed/unsigned response, or a ``sub`` mismatch between the
    inbound and exchanged tokens.
    """
    data = {
        "grant_type": settings.exchange_grant_type,
        "client_id": settings.exchange_client_id,
        "client_secret": settings.exchange_client_secret,
        "subject_token": inbound_token,
        "subject_token_type": settings.exchange_subject_token_type,
        "requested_token_type": settings.exchange_requested_token_type,
        "audience": settings.exchange_audience,
    }
    if requested_scope:
        data["scope"] = requested_scope
    try:
        response = await http_client.post(
            settings.keycloak_token_url,
            data=data,
            timeout=settings.exchange_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise TokenExchangeError(f"token-exchange request failed: {exc}") from exc

    if response.status_code != 200:
        raise TokenExchangeError(
            f"token-exchange rejected by Keycloak: HTTP {response.status_code} "
            f"{_safe_body(response)}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise TokenExchangeError("token-exchange response is not valid JSON") from exc

    minted = body.get("access_token")
    if not isinstance(minted, str) or not minted.strip():
        raise TokenExchangeError("token-exchange response carries no access_token")

    minted_claims = await _revalidate_minted_token(minted, settings, http_client)
    minted_sub = minted_claims.get("sub")
    if minted_sub != inbound_sub:
        # Fail closed: this is the falsifiable identity guard. A code
        # regression that stamped a static/shared subject instead of
        # forwarding the caller's own would trip this exact check.
        raise TokenExchangeError(
            "identity assertion failed: minted token sub "
            f"{minted_sub!r} != inbound token sub {inbound_sub!r}"
        )
    return minted


async def _revalidate_minted_token(
    minted_token: str, settings: Settings, http_client: httpx.AsyncClient
) -> dict[str, Any]:
    """Decode + verify the signature of the token Keycloak just minted.

    The BFF received this token directly from Keycloak over TLS, but it
    re-verifies the signature anyway (defense in depth — the same
    discipline ``audittrace.auth`` applies to every inbound token) rather
    than trusting the transport alone. Audience is not checked here
    either: the exchange ``audience`` parameter already selected the
    client whose mappers stamp ``aud=audittrace-server``, and asserting
    the exact claim would duplicate that client's own drift-guarded
    protocol-mapper test (``tests/test_chart_drift_guards.py``).
    """
    try:
        keys = await get_jwks_keys(http_client, settings)
        claims = jwt.decode(
            minted_token,
            keys,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
    except JWTError as exc:
        raise TokenExchangeError(f"minted token failed re-validation: {exc}") from exc
    assert isinstance(claims, dict), "jwt.decode returned a non-dict payload"
    return claims


def _safe_body(response: httpx.Response) -> str:
    """Best-effort truncated response body for error messages.

    Never includes the request's ``client_secret`` (it was in the
    OUTBOUND form body, not this response); truncated so a large/odd
    error page never bloats logs.
    """
    try:
        return response.text[:500]
    except Exception:  # noqa: BLE001 - error-formatting must never raise
        return "<unreadable response body>"
