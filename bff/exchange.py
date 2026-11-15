"""RFC 8693 token exchange — the identity guarantee.

Exchanges the caller's forwarded token for a fresh access token minted
by the AuditTrace realm's confidential client
(``audittrace-librechat-bff``), requesting the ``audittrace-librechat``
client's audience so the result deterministically carries
``aud=audittrace-server`` (the ``aud-audittrace-server`` protocol mapper
WU-1 already put on that client — see ``bff/README.md``).

The exchange's ``scope`` is per-caller, not fixed. What Keycloak 24's legacy
exchange grants (measured, LC10A-CHAT-EXCHANGE-SCOPE): the target client's
DEFAULT scopes, plus the requested scopes that are optional on the target and
held by the subject token, plus ``openid``; when the ``scope`` field is
OMITTED the requested set is the subject token's own scope set (since the
2026-11-08 ruling the login token carries every BFF write scope, so an omitted
field mints all of them). The chat path (``bff/app.py::chat_completions``)
therefore requests ``bff.chat_scopes.CHAT_SCOPE_STRING`` from the dedicated
``audittrace-librechat-chat`` audience (``audience=``) and passes a
``scope_ceiling``: a minted token with a scope outside it is refused before
it is returned. The memory-proxy path (M3-WU-D2-1,
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
from jose.exceptions import JWKError

from bff.auth import InboundTokenError, get_jwks_keys
from bff.config import Settings
from bff.exchange_failure import ExchangeFailure

logger = logging.getLogger(__name__)


class TokenExchangeError(Exception):
    """Raised when RFC 8693 token exchange fails, or the exchanged token
    fails re-validation / the identity (``sub``) consistency check.

    The class is ``failure``. The chat route answers 502 for every class but
    ``SUBJECT_REFUSED``: a downstream dependency (Keycloak) failure is not a
    caller auth failure. ``SUBJECT_REFUSED`` is Keycloak refusing the CALLER's
    token (see :func:`_refusal_class`), answered 401. The identity-mismatch
    case stays a fail-closed internal-invariant violation (``INVALID``) and
    never reaches a caller as anything but a hard failure to proxy.
    """

    def __init__(
        self, message: str, failure: ExchangeFailure = ExchangeFailure.INVALID
    ) -> None:
        super().__init__(message)
        self.failure = failure


async def exchange_token(
    inbound_token: str,
    inbound_sub: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
    requested_scope: str | None = None,
    *,
    audience: str | None = None,
    scope_ceiling: frozenset[str] | None = None,
) -> str:
    """RFC 8693 token-exchange ``inbound_token`` for an
    ``aud=audittrace-server`` access token minted for the SAME subject.

    ``requested_scope`` is an explicit, space-separated RFC 8693 ``scope``
    request (see the module docstring). ``None`` omits the ``scope`` form
    field entirely, which makes Keycloak grant the subject token's own
    scopes: only a caller that wants that passes ``None``.

    ``audience`` (keyword-only) overrides ``settings.exchange_audience``; it
    is chosen with ``is not None``, never ``or``, so an empty string is sent
    as given and fails at Keycloak, never silently falling back to the login
    audience. ``scope_ceiling`` (keyword-only) is an allow-list: when given
    and the minted token's scope set is not a subset of it, a
    :class:`TokenExchangeError` naming the unexpected scopes (names only,
    never the token) is raised BEFORE the ``granted=`` line is logged and
    before the token is returned.

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
        "audience": audience if audience is not None else settings.exchange_audience,
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
        raise TokenExchangeError(
            f"token-exchange request failed: {exc}", ExchangeFailure.UNAVAILABLE
        ) from exc

    if response.status_code != 200:
        raise TokenExchangeError(
            f"token-exchange rejected by Keycloak: HTTP {response.status_code} "
            f"{_safe_body(response)}",
            _refusal_class(response),
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise TokenExchangeError("token-exchange response is not valid JSON") from exc

    if not isinstance(body, dict):
        raise TokenExchangeError("token-exchange response is not a JSON object")

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
    if scope_ceiling is not None:
        unexpected = sorted(
            set(str(minted_claims.get("scope", "")).split()) - scope_ceiling
        )
        if unexpected:
            raise TokenExchangeError(
                "scope ceiling exceeded: unexpected=" + ",".join(unexpected)
            )
    _log_scope_outcome(requested_scope, minted_claims)
    return minted


def _log_scope_outcome(requested_scope: str | None, claims: dict[str, Any]) -> None:
    """Name the scopes the minted token carries, and any REQUESTED scope it
    does not (names only, never the token).

    Keycloak 24's legacy token exchange silently drops a requested optional
    scope the subject token lacks and still answers 200, so the downstream
    403 is otherwise the first sign. A warning here makes the cause visible.
    """
    granted = sorted(set(str(claims.get("scope", "")).split()))
    requested = sorted(set((requested_scope or "").split()))
    missing = [name for name in requested if name not in granted]
    if missing:
        logger.warning(
            "token_exchange.scopes_missing missing=%s granted=%s",
            ",".join(missing),
            ",".join(granted),
        )
    else:
        logger.info("token_exchange.scopes granted=%s", ",".join(granted))


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
    except InboundTokenError as exc:
        # The second JWKS fetch failed (the key cache had expired between the
        # inbound check and here): the sign-in service, not the caller's token.
        raise TokenExchangeError(
            f"minted token could not be re-validated: {exc}",
            ExchangeFailure.UNAVAILABLE,
        ) from exc
    except JWTError as exc:
        raise TokenExchangeError(f"minted token failed re-validation: {exc}") from exc
    except (JWKError, ValueError, TypeError) as exc:
        # Not wrapped by python-jose (measured): the realm's key or a claim it
        # cannot use. Same class as the inbound decode: unavailable.
        raise TokenExchangeError(
            f"realm keys or claims unusable: {type(exc).__name__}: {exc}",
            ExchangeFailure.UNAVAILABLE,
        ) from exc
    assert isinstance(claims, dict), "jwt.decode returned a non-dict payload"
    return claims


def _refusal_class(response: httpx.Response) -> ExchangeFailure:
    """Class of a non-200 answer of the token endpoint.

    Keycloak answers ``400`` with ``"error": "invalid_token"`` in the RFC 6749
    section 5.2 ``error`` field when it refuses the SUBJECT token (expired,
    user disabled, session gone; measured on Keycloak 24.0.5). ``invalid_token``
    is Keycloak's own value there, not an RFC 6749 section 5.2 code: RFC 8693
    section 2.2.2 says ``invalid_request`` for an invalid ``subject_token``. A
    standards-following exchange (Keycloak 26, exchange V2) will therefore fall
    to ``REFUSED`` (502), the safe direction, and the 401 would silently stop;
    re-measure on a Keycloak upgrade. Only that exact status and value pair is
    the caller's token; every other 4xx is the BFF's configuration. The field is
    read as data: any other shape falls to the safe class.
    """
    if response.status_code >= 500:
        return ExchangeFailure.UNAVAILABLE
    if response.status_code == 400:
        try:
            answer = response.json()
        except ValueError:
            return ExchangeFailure.REFUSED
        if isinstance(answer, dict) and answer.get("error") == "invalid_token":
            return ExchangeFailure.SUBJECT_REFUSED
    return ExchangeFailure.REFUSED


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
