"""The refusals of every BFF route that is not ``/v1`` (ADDENDUM-2 item 1).

Constants and functions only. Same closed set as ``bff.chat_refusals``: same
codes, statuses and challenges BY CONSTRUCTION (each is derived from its chat
twin); only the wording names "AuditTrace" instead of "Chat". The body is the
same error object plus one additive top-level key, ``detail``, holding the
message: the LibreChat fork's clients read ``data.detail`` first and would
print ``[object Object]`` for an ``error`` object alone (measured).
"""

from __future__ import annotations

import dataclasses
import functools
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response

from bff import chat_refusals
from bff.chat_last_resort import frame_chain
from bff.chat_refusal import ChatRefusal

logger = logging.getLogger(__name__)

_MESSAGES: dict[str, str] = {
    "missing_token": chat_refusals.MISSING_TOKEN.message,
    "invalid_token": chat_refusals.INVALID_TOKEN.message,
    "auth_service_unavailable": (
        "AuditTrace is temporarily unavailable: the sign-in service cannot be "
        "reached or its answer cannot be used. Try again in a moment. If this "
        "message returns, contact an administrator."
    ),
    "token_exchange_refused": (
        "AuditTrace is unavailable: the sign-in service refused to issue the "
        "access token for this request. Contact an administrator: the cause is "
        "the server configuration, not your request."
    ),
    "token_exchange_invalid": (
        "AuditTrace is unavailable: the sign-in service issued an access token "
        "that the gateway refused. Contact an administrator: the cause is the "
        "server configuration, not your request."
    ),
    "upstream_unavailable": (
        "AuditTrace is temporarily unavailable: the memory server cannot be "
        "reached. Try again in a moment. If this message returns, contact an "
        "administrator."
    ),
    "internal_error": (
        "AuditTrace is unavailable: the gateway failed unexpectedly. Try again "
        "in a moment. If this message returns, contact an administrator."
    ),
}
_CHAT_SET = (
    chat_refusals.MISSING_TOKEN,
    chat_refusals.INVALID_TOKEN,
    chat_refusals.AUTH_SERVICE_UNAVAILABLE,
    chat_refusals.TOKEN_EXCHANGE_REFUSED,
    chat_refusals.TOKEN_EXCHANGE_INVALID,
    chat_refusals.UPSTREAM_UNAVAILABLE,
    chat_refusals.INTERNAL_ERROR,
)
ROUTE_REFUSALS: dict[str, ChatRefusal] = {
    refusal.code: dataclasses.replace(refusal, message=_MESSAGES[refusal.code])
    for refusal in _CHAT_SET
}
NOT_FOUND = ChatRefusal(
    404, "not_found", "invalid_request_error", "The BFF has no such route."
)
METHOD_NOT_ALLOWED = ChatRefusal(
    405,
    "method_not_allowed",
    "invalid_request_error",
    "The BFF does not serve this method on this route.",
)
_FOR_STATUS = {404: NOT_FOUND, 405: METHOD_NOT_ALLOWED}

Handler = Callable[..., Awaitable[Response]]


def respond(refusal: ChatRefusal) -> JSONResponse:
    """The error object plus the additive ``detail`` (the same message)."""
    headers = {"WWW-Authenticate": refusal.challenge} if refusal.challenge else None
    return JSONResponse(
        status_code=refusal.status,
        content={**refusal.body(), "detail": refusal.message},
        headers=headers,
    )


def refuse_route(
    route: str,
    chat_refusal: ChatRefusal,
    headers: Mapping[str, str],
    *,
    sub: str | None,
    cause: object,
) -> JSONResponse:
    """Log the one ``route.refused`` event and build the answer.

    ``chat_refusal`` is the member of the chat set the failure classifies to;
    its route twin (same code) is what the caller gets. Field rules are those
    of ``bff.chat_refusals.refuse``.
    """
    refusal = ROUTE_REFUSALS[chat_refusal.code]
    logger.log(
        logging.WARNING if refusal.status < 500 else logging.ERROR,
        "route.refused route=%s code=%s status=%d sub=%s session_id=%s "
        "inbound_trace_id=%s cause=%s",
        route,
        refusal.code,
        refusal.status,
        sub or "-",
        chat_refusals._session_id(headers),
        chat_refusals._inbound_trace_id(headers),
        " ".join(str(cause).split()),
    )
    return respond(refusal)


def route_last_resort(route: str) -> Callable[[Handler], Handler]:
    """The last resort of ``bff.chat_last_resort``, for a non-chat route."""

    def decorate(handler: Handler) -> Handler:
        @functools.wraps(handler)
        async def guarded(*args: Any, **kwargs: Any) -> Response:
            request: Request = kwargs["request"]
            try:
                return await handler(*args, **kwargs)
            except ClientDisconnect:
                raise
            except Exception as exc:  # noqa: BLE001 - the last resort, by design
                return refuse_route(
                    route,
                    chat_refusals.INTERNAL_ERROR,
                    request.headers,
                    sub=getattr(request.state, "verified_sub", None),
                    cause=frame_chain(exc),
                )

        return guarded

    return decorate


async def framework_error(_request: Request, exc: Exception) -> Response:
    """The framework's own 404 and 405, in the one shape (no log event)."""
    assert isinstance(exc, StarletteHTTPException)
    refusal = _FOR_STATUS.get(exc.status_code)
    if refusal is None:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=dict(exc.headers or {}),
        )
    return respond(refusal)
