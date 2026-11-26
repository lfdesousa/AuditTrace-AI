"""The evidence routes' caller-scope pre-check (CONSOLE-AUDIT-EVIDENCE WU-C).

Sibling of ``bff.chat_caller_scope`` (the chat copy is not touched): a caller
whose own, verified token lacks ``audittrace:audit`` is refused BEFORE any token
exchange, with an explicit, actionable error (RFC 6750 section 3.1
``insufficient_scope``, HTTP 403) in the non-chat route shape (the error object
plus the additive ``detail``). Scope names and ``sub`` only: no token, no other
claim value is logged or returned. The 502 of ``bff/app.py`` keeps one meaning:
Keycloak answered 200 with a scope outside the ceiling.
"""

from __future__ import annotations

import logging

from fastapi.responses import JSONResponse

from bff.console_evidence_scopes import EVIDENCE_SCOPES

logger = logging.getLogger(__name__)

_REQUIRED_SCOPES: tuple[str, ...] = tuple(sorted(EVIDENCE_SCOPES))
_CHALLENGE = (
    'Bearer error="insufficient_scope", scope="' + " ".join(_REQUIRED_SCOPES) + '"'
)


def missing_evidence_scopes(inbound_scopes: frozenset[str]) -> list[str]:
    """The required scopes the caller's token does not carry, sorted."""
    return sorted(set(_REQUIRED_SCOPES) - inbound_scopes)


def evidence_insufficient_scope_response(
    inbound_sub: str, missing: list[str]
) -> JSONResponse:
    """Log the refusal as its own event and build the explicit HTTP 403."""
    logger.warning(
        "evidence.caller_scope_insufficient sub=%s missing=%s",
        inbound_sub,
        ",".join(missing),
    )
    named = ", ".join(f"'{scope}'" for scope in missing)
    message = (
        "This sign-in cannot read audit evidence: its access token lacks the "
        f"required scope(s) {named}. Sign out and sign in again. If this message "
        "returns, ask an administrator to grant the scope(s) to this client."
    )
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": "insufficient_scope",
                "status": 403,
                "missing_scopes": missing,
            },
            "detail": message,
        },
        headers={"WWW-Authenticate": _CHALLENGE},
    )
