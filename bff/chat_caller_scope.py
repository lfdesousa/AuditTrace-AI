"""The chat route's caller-scope pre-check (LC10A-CHAT-EXCHANGE-SCOPE).

A caller whose own, verified token lacks a scope the chat turn requires is
refused BEFORE any token exchange, with an explicit, actionable error
(RFC 6750 section 3.1 ``insufficient_scope``, HTTP 403), never a bare 502.
Scope names and ``sub`` only: no token, no other claim value is logged or
returned. Functions only, no I/O beyond the log record.

The 502 of ``bff/app.py`` keeps one meaning: Keycloak answered 200 with a
scope outside the ceiling (``bff.exchange``).
"""

from __future__ import annotations

import logging

from fastapi.responses import JSONResponse

from bff.chat_scopes import CHAT_SCOPES

logger = logging.getLogger(__name__)

_REQUIRED_SCOPES: tuple[str, ...] = tuple(sorted(CHAT_SCOPES))
_CHALLENGE = (
    'Bearer error="insufficient_scope", scope="' + " ".join(_REQUIRED_SCOPES) + '"'
)


def missing_chat_scopes(inbound_scopes: frozenset[str]) -> list[str]:
    """The required scopes the caller's token does not carry, sorted."""
    return sorted(set(_REQUIRED_SCOPES) - inbound_scopes)


def insufficient_scope_response(inbound_sub: str, missing: list[str]) -> JSONResponse:
    """Log the refusal as its own event and build the explicit HTTP 403."""
    logger.warning(
        "chat.caller_scope_insufficient sub=%s missing=%s",
        inbound_sub,
        ",".join(missing),
    )
    named = ", ".join(f"'{scope}'" for scope in missing)
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "message": (
                    "This sign-in cannot use chat: its access token lacks the "
                    f"required scope(s) {named}. Sign out and sign in again. If "
                    "this message returns, ask an administrator to grant the "
                    "scope(s) to this client."
                ),
                "type": "invalid_request_error",
                "param": None,
                "code": "insufficient_scope",
                "status": 403,
                "missing_scopes": missing,
            }
        },
        headers={"WWW-Authenticate": _CHALLENGE},
    )
