"""Refuse a request path that does not mean what it spells (ADDENDUM-3, S5)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from bff.route_refusals import NOT_FOUND, respond

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_ENCODED_SEPARATORS = (b"%2f", b"%5c")


class PathGuard:
    """ASGI middleware in front of every BFF route.

    The proxy routes relay the path they were given under the BFF's own mesh
    identity, and an HTTP client resolves ``.`` and ``..`` before it sends. A
    path with a dot segment, or with an encoded slash or backslash (which the
    server decodes into a new segment boundary), would therefore reach a
    DIFFERENT memory-server route than the one the BFF route proxies. Such a
    path is answered 404 in the one shape, before any route, token check or
    upstream call. One place, so a new proxy route cannot forget it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and (reason := self._refusal(scope)) is not None:
            logger.warning("path_guard.refused reason=%s", reason)
            await respond(NOT_FOUND)(scope, receive, send)
            return
        await self._app(scope, receive, send)

    @staticmethod
    def _refusal(scope: Scope) -> str | None:
        raw = bytes(scope.get("raw_path") or b"").lower()
        if any(marker in raw for marker in _ENCODED_SEPARATORS):
            return "encoded_separator"
        segments = str(scope.get("path", "")).replace("\\", "/").split("/")
        if any(segment in (".", "..") for segment in segments):
            return "dot_segment"
        return None
