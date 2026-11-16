"""Refuse an oversized memory write request before its body is spooled or parsed.

FastAPI reads the whole body (multipart parts are spooled to disk without a
limit, JSON is parsed in memory) before a route handler runs, so a handler's own
size check comes too late to bound what the server RECEIVES. This pure ASGI
middleware bounds it: by ``Content-Length`` when the client declares one, and
by counting the stream when it does not.
"""

from __future__ import annotations

import json
from typing import Any

from audittrace.memory_request_too_large_error import MemoryRequestTooLargeError
from audittrace.services.memory_write_limit import request_body_ceiling

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH"})
_PREFIXES = ("/memory/", "/mcp")


class MemoryBodyLimitMiddleware:
    """413 for a memory write request whose body is over its path's ceiling."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or scope.get("method") not in _WRITE_METHODS
            or not path.startswith(_PREFIXES)
        ):
            await self.app(scope, receive, send)
            return
        ceiling = request_body_ceiling(path)
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
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
                    raise MemoryRequestTooLargeError
            return message

        async def guarded_send(message: dict[str, Any]) -> None:
            # The framework turns an error raised while it reads the body into
            # its own 400. Once the ceiling is passed, the only answer is 413.
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
        except MemoryRequestTooLargeError:
            if answered:
                raise
            await self._refuse(send, ceiling)

    @staticmethod
    async def _refuse(send: Any, ceiling: int) -> None:
        body = json.dumps(
            {"detail": f"memory request body exceeds the limit of {ceiling} bytes"}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
