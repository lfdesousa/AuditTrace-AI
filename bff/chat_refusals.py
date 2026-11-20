"""The closed set of refusals of the chat route, and their one log event.

Constants and functions only. Every message is fixed text that names a cause
class; no token, client secret, Keycloak response or request header value is
ever part of an answer. The operator's detail goes to the log event
``chat.refused`` (user, session, the caller's trace id, cause).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

from fastapi.responses import JSONResponse

from bff.chat_refusal import ChatRefusal
from bff.exchange_failure import ExchangeFailure
from bff.inbound_token_failure import InboundTokenFailure

logger = logging.getLogger(__name__)

MISSING_TOKEN = ChatRefusal(
    401,
    "missing_token",
    "invalid_request_error",
    "Sign-in required: this request carries no bearer access token. Sign in and "
    "try again.",
    "Bearer",
)
INVALID_TOKEN = ChatRefusal(
    401,
    "invalid_token",
    "invalid_request_error",
    "Your sign-in is no longer valid: the access token is invalid or expired. "
    "Sign out and sign in again.",
    'Bearer error="invalid_token", '
    'error_description="The access token is invalid or expired"',
)
AUTH_SERVICE_UNAVAILABLE = ChatRefusal(
    502,
    "auth_service_unavailable",
    "api_error",
    "Chat is temporarily unavailable: the sign-in service cannot be reached or "
    "its answer cannot be used. Try again in a moment. If this message returns, "
    "contact an administrator.",
)
TOKEN_EXCHANGE_REFUSED = ChatRefusal(
    502,
    "token_exchange_refused",
    "api_error",
    "Chat is unavailable: the sign-in service refused to issue the access token "
    "for this chat turn. Contact an administrator: the cause is the server "
    "configuration, not your message.",
)
TOKEN_EXCHANGE_INVALID = ChatRefusal(
    502,
    "token_exchange_invalid",
    "api_error",
    "Chat is unavailable: the sign-in service issued an access token that the "
    "gateway refused. Contact an administrator: the cause is the server "
    "configuration, not your message.",
)
UPSTREAM_UNAVAILABLE = ChatRefusal(
    502,
    "upstream_unavailable",
    "api_error",
    "Chat is temporarily unavailable: the AuditTrace memory server cannot be "
    "reached. Try again in a moment. If this message returns, contact an "
    "administrator.",
)
# The last resort (``bff.chat_last_resort``): 500, so a defect stays visible
# and distinct from the 502 classes; the fork retries a 5xx unless the chart
# bounds ``maxRetries``.
INTERNAL_ERROR = ChatRefusal(
    500,
    "internal_error",
    "api_error",
    "Chat is unavailable: the gateway failed unexpectedly. Try again in a moment. "
    "If this message returns, contact an administrator.",
)

_FOR_INBOUND = {
    InboundTokenFailure.MISSING: MISSING_TOKEN,
    InboundTokenFailure.INVALID: INVALID_TOKEN,
    InboundTokenFailure.UNVERIFIABLE: AUTH_SERVICE_UNAVAILABLE,
}
_FOR_EXCHANGE = {
    ExchangeFailure.SUBJECT_REFUSED: INVALID_TOKEN,
    ExchangeFailure.REFUSED: TOKEN_EXCHANGE_REFUSED,
    ExchangeFailure.UNAVAILABLE: AUTH_SERVICE_UNAVAILABLE,
    ExchangeFailure.INVALID: TOKEN_EXCHANGE_INVALID,
}

_SESSION_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_TRACEPARENT = re.compile(r"[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}")


def for_inbound_failure(failure: InboundTokenFailure) -> ChatRefusal:
    return _FOR_INBOUND[failure]


def for_exchange_failure(failure: ExchangeFailure) -> ChatRefusal:
    return _FOR_EXCHANGE[failure]


def _session_id(headers: Mapping[str, str]) -> str:
    value = (headers.get("x-session-id") or "").strip()
    return value if _SESSION_ID.fullmatch(value) else "-"


def _inbound_trace_id(headers: Mapping[str, str]) -> str:
    match = _TRACEPARENT.fullmatch((headers.get("traceparent") or "").strip())
    return match.group(1) if match else "-"


def refuse(
    refusal: ChatRefusal,
    headers: Mapping[str, str],
    *,
    sub: str | None,
    cause: object,
) -> JSONResponse:
    """Log the one ``chat.refused`` event and build the answer.

    ``sub`` is given only when it comes from a verified token. The session id
    and trace id are the caller's own correlation values: logged only in their
    accepted form, otherwise ``-``, so a header cannot write fields.
    ``cause`` is flattened to one physical line and is the LAST field, so text
    inside it cannot stand for a field. The level follows the status class.
    """
    logger.log(
        logging.WARNING if refusal.status < 500 else logging.ERROR,
        "chat.refused code=%s status=%d sub=%s session_id=%s inbound_trace_id=%s "
        "cause=%s",
        refusal.code,
        refusal.status,
        sub or "-",
        _session_id(headers),
        _inbound_trace_id(headers),
        " ".join(str(cause).split()),
    )
    return refusal.response()
