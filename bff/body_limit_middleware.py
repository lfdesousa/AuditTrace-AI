"""Refuse an oversized request before the BFF buffers it.

Every proxy route reads its body whole (``await request.body()``) before it
forwards it, so without a ceiling the sidecar holds whatever a client sends.
This pure ASGI middleware bounds it: by ``Content-Length`` when declared, and
by counting the stream when not.
"""

from __future__ import annotations

from typing import Any

from bff.body_limit_refusal import (
    REQUEST_TOO_LARGE_STATUS,
    request_too_large_response,
)
from bff.config import get_settings
from bff.request_too_large_error import RequestTooLargeError


class BodyLimitMiddleware:
    """413 for a request whose body is over its path's ceiling."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        # Every method: the proxies read ``await request.body()`` whatever the
        # verb, so a GET or DELETE carrying a body is buffered just the same.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        files = scope.get("path", "").rstrip("/") == "/console/files"
        ceiling = (
            settings.console_files_max_body_bytes
            if files
            else settings.max_request_body_bytes
        )
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > ceiling:
            await self._refuse(send, ceiling)
            return

        received = 0
        overflowed = False
        answered = False

        async def counted_receive() -> dict[str, Any]:
            nonlocal received, overflowed
            message: dict[str, Any] = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > ceiling:
                    overflowed = True
                    raise RequestTooLargeError
            return message

        async def guarded_send(message: dict[str, Any]) -> None:
            nonlocal answered
            if overflowed:
                if not answered:
                    answered = True
                    await self._refuse(send, ceiling)
                return
            answered = answered or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except RequestTooLargeError:
            if answered:
                raise
            await self._refuse(send, ceiling)

    @staticmethod
    async def _refuse(send: Any, ceiling: int) -> None:
        headers, body = request_too_large_response(ceiling)
        await send(
            {
                "type": "http.response.start",
                "status": REQUEST_TOO_LARGE_STATUS,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})
