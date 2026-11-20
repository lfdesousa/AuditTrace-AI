"""The BFF's 413 answer, in the BFF error shape.

Every answer a BFF route produces itself carries the error object of the chat
route's contract (``message``, ``type``, ``param``, ``code``, ``status``: the
shape of ``audittrace.routes.chat._openai_error_body``, which an OpenAI client
parses) plus the same text under ``detail``, which the console fork reads. The
body-limit middleware answers before any route runs, so it builds the answer
itself instead of borrowing a route's.

The shape comes from ``bff.chat_refusal.ChatRefusal``, the single source of
the BFF error object (gate r3 S15): this module only adds the ``detail`` key
and the ASGI bytes. ``tests/bff/test_body_limit.py`` pins the output.
"""

from __future__ import annotations

import json
from typing import Any

from bff.chat_refusal import ChatRefusal

REQUEST_TOO_LARGE_STATUS = 413
REQUEST_TOO_LARGE_CODE = "request_too_large"
REQUEST_TOO_LARGE_TYPE = "invalid_request_error"


def request_too_large_body(ceiling: int) -> dict[str, Any]:
    """The JSON body of a 413 for a request over *ceiling* bytes."""
    refusal = ChatRefusal(
        REQUEST_TOO_LARGE_STATUS,
        REQUEST_TOO_LARGE_CODE,
        REQUEST_TOO_LARGE_TYPE,
        f"request body exceeds the limit of {ceiling} bytes",
    )
    return {**refusal.body(), "detail": refusal.message}


def request_too_large_response(ceiling: int) -> tuple[list[tuple[bytes, bytes]], bytes]:
    """``(headers, body)`` of the 413, as ASGI bytes."""
    body = json.dumps(request_too_large_body(ceiling)).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    return headers, body
