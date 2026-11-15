"""The chat route's last resort (BFF-CHAT-ROUTE-ERRORS, ADDENDUM-1 Q8).

Every failure the route foresees is classified where it is raised and answered
by ``bff.chat_refusals``. This decorator is for the one nobody foresaw: instead
of the ASGI server's ``text/plain`` 500, which an OpenAI client prints as
"500 status code (no body)", the caller gets the fixed ``internal_error``
answer, and the operator gets one ``chat.refused`` event naming the exception
CLASS and the chain of frames (file:line each) it travelled through. The
exception's own text is never logged or returned: nobody has vetted it.

It is a backstop in the route, not a substitute for classification, and it
sits in no auth or exchange module. A 500 is retried by the fork's client
unless the chart bounds ``maxRetries`` (``console.librechat.endpointMaxRetries``).
"""

from __future__ import annotations

import functools
import traceback
from collections.abc import Awaitable, Callable
from pathlib import PurePath
from typing import Any

from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response

from bff.chat_refusals import INTERNAL_ERROR, refuse

Handler = Callable[..., Awaitable[Response]]

# A runaway recursion has ~1000 frames; the log line keeps the innermost ones.
MAX_FRAMES = 24


def frame_chain(exc: BaseException) -> str:
    """``Class at file:line > file:line ...``, outermost frame first.

    File base names and line numbers only: no exception text, no argument, no
    local. The last frame is where the exception was raised.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    shown = [f"{PurePath(f.filename).name}:{f.lineno}" for f in frames[-MAX_FRAMES:]]
    if len(frames) > MAX_FRAMES:
        shown.insert(0, "...")
    return f"{type(exc).__name__} at " + " > ".join(shown)


def last_resort(handler: Handler) -> Handler:
    """Answer any unforeseen exception of ``handler`` with ``internal_error``.

    A caller that went away (``ClientDisconnect``) is not a refusal and is
    re-raised. ``request.state.verified_sub`` is the user, when the handler
    got as far as verifying the token.
    """

    @functools.wraps(handler)
    async def guarded(request: Request, *args: Any, **kwargs: Any) -> Response:
        try:
            return await handler(request, *args, **kwargs)
        except ClientDisconnect:
            raise
        except Exception as exc:  # noqa: BLE001 - the last resort, by design
            return refuse(
                INTERNAL_ERROR,
                request.headers,
                sub=getattr(request.state, "verified_sub", None),
                cause=frame_chain(exc),
            )

    return guarded
