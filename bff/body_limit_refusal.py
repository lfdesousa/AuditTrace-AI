"""The BFF's 413 answer, in the BFF error shape.

Every answer a BFF route produces itself carries the error object of the chat
route's contract (``message``, ``type``, ``param``, ``code``, ``status``: the
shape of ``audittrace.routes.chat._openai_error_body``, which an OpenAI client
parses) plus the same text under ``detail``, which the console fork reads. The
body-limit middleware answers before any route runs, so it builds the shape
itself instead of borrowing a route's.

MERGE ORDER (gate r3 S15). The chat-route errors item
(``fix/bff-chat-route-errors``) owns the shape's single source
(``bff.chat_refusal.ChatRefusal``). This module is a local twin with identical
output, pinned by ``tests/bff/test_body_limit.py``; whichever of the two
changes merges second replaces :func:`request_too_large_body` with that helper
and keeps the test as it is.
"""

from __future__ import annotations

import json
from typing import Any

REQUEST_TOO_LARGE_STATUS = 413
REQUEST_TOO_LARGE_CODE = "request_too_large"
REQUEST_TOO_LARGE_TYPE = "invalid_request_error"


def request_too_large_body(ceiling: int) -> dict[str, Any]:
    """The JSON body of a 413 for a request over *ceiling* bytes."""
    message = f"request body exceeds the limit of {ceiling} bytes"
    return {
        "error": {
            "message": message,
            "type": REQUEST_TOO_LARGE_TYPE,
            "param": None,
            "code": REQUEST_TOO_LARGE_CODE,
            "status": REQUEST_TOO_LARGE_STATUS,
        },
        "detail": message,
    }


def request_too_large_response(ceiling: int) -> tuple[list[tuple[bytes, bytes]], bytes]:
    """``(headers, body)`` of the 413, as ASGI bytes."""
    body = json.dumps(request_too_large_body(ceiling)).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    return headers, body
