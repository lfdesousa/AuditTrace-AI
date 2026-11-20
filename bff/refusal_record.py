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
import time
from collections import deque
from collections.abc import Mapping

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response

from bff import chat_refusals
from bff.config import Settings

logger = logging.getLogger(__name__)

# The rate bound (per verified user, sliding minute). ``_monotonic`` is a
# module name so a test can step the clock; ``_MAX_USERS`` bounds the table.
_monotonic = time.monotonic
_WINDOW_SECONDS = 60.0
# The table is capped and evicts by insertion order: 4096 distinct users within
# a minute reset a bounded user's window (accepted at this scale, A1 of gate r2).
_MAX_USERS = 4096
_recent: dict[str, deque[float]] = {}


def reset_rate_state_for_tests() -> None:
    _recent.clear()


def _within_bound(sub: str, limit: int) -> bool:
    """Count this record against ``sub``'s minute; False when it is over."""
    now = _monotonic()
    stamps = _recent.get(sub)
    if stamps is None:
        if len(_recent) >= _MAX_USERS:
            _recent.pop(next(iter(_recent)))
        stamps = _recent[sub] = deque()
    while stamps and now - stamps[0] >= _WINDOW_SECONDS:
        stamps.popleft()
    if len(stamps) >= limit:
        return False
    stamps.append(now)
    return True


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
    sub: str,
) -> None:
    """Called once the caller's token is verified: from here on a refusal of
    this request can be recorded (``sub`` is the verified user, for the rate
    bound only; it is never sent)."""
    if settings.refusal_record_enabled:
        request.state.refusal_recorder = functools.partial(
            send, http_client, settings, subject_token, request.headers
        )
        request.state.refusal_rate = (sub, settings.refusal_record_max_per_minute)


def attach[R: Response](response: R, request: Request) -> R:
    """Record ``response`` after it is sent, if it is a refusal of an armed
    request. The code and status are read from the answer itself."""
    recorder = getattr(request.state, "refusal_recorder", None)
    code = getattr(response, "refusal_code", None)
    if recorder is not None and code is not None:
        sub, limit = request.state.refusal_rate
        if not _within_bound(sub, limit):
            logger.warning("chat.refusal_record_skipped code=%s reason=rate", code)
            return response
        response.background = BackgroundTask(
            recorder, code=code, status=response.status_code
        )
    return response
