"""The durable record of a refused chat turn (ADDENDUM-2 item 2).

Functions only. When the BFF refuses a chat turn AFTER it verified who the
caller is, it tells the memory server, which writes one audit row. The BFF
sends the caller's own token as the evidence of identity; the server verifies
it and derives the user from it. The BFF asserts no user. The record is best
effort and runs after the answer is sent: it can never change what the caller
gets. No token is logged.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Mapping

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response

from bff import chat_refusals
from bff.config import Settings

logger = logging.getLogger(__name__)


async def send(
    http_client: httpx.AsyncClient,
    settings: Settings,
    subject_token: str,
    headers: Mapping[str, str],
    *,
    code: str,
    status: int,
) -> None:
    """POST the record. Every failure ends in one WARNING, never an exception."""
    session_id = chat_refusals._session_id(headers)
    trace_id = chat_refusals._inbound_trace_id(headers)
    body = {
        "route": "chat",
        "code": code,
        "http_status": status,
        "subject_token": subject_token,
        "session_id": None if session_id == "-" else session_id,
        "inbound_trace_id": None if trace_id == "-" else trace_id,
    }
    url = (
        settings.orchestrator_base_url.rstrip("/") + settings.orchestrator_refusal_path
    )
    try:
        response = await http_client.post(
            url,
            json=body,
            headers={"X-Source": settings.proxy_source_label},
            timeout=settings.refusal_record_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        outcome = type(exc).__name__
    else:
        if response.status_code == 201:
            return
        outcome = f"HTTP {response.status_code}"
    logger.warning("chat.refusal_record_failed code=%s outcome=%s", code, outcome)


def arm(
    request: Request,
    http_client: httpx.AsyncClient,
    settings: Settings,
    subject_token: str,
) -> None:
    """Called once the caller's token is verified: from here on a refusal of
    this request can be recorded."""
    if settings.refusal_record_enabled:
        request.state.refusal_recorder = functools.partial(
            send, http_client, settings, subject_token, request.headers
        )


def attach[R: Response](response: R, request: Request) -> R:
    """Record ``response`` after it is sent, if it is a refusal of an armed
    request. The code and status are read from the answer itself."""
    recorder = getattr(request.state, "refusal_recorder", None)
    code = getattr(response, "refusal_code", None)
    if recorder is not None and code is not None:
        response.background = BackgroundTask(
            recorder, code=code, status=response.status_code
        )
    return response
