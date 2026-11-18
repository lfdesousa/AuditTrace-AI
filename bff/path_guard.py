"""Refuse a request path that does not mean what it spells (ADDENDUM-3, S5)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import unquote

from bff.route_refusals import NOT_FOUND, respond

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_ENCODED_SEPARATORS = (b"%2f", b"%5c")
# Still encoded AFTER the server's one decode: the mesh proxy decodes once more.
_RESIDUAL_SEPARATORS = ("%2f", "%5c")
_RESIDUAL_DOT = "%2e"


class PathGuard:
    """ASGI middleware in front of every BFF route.

    The proxy routes relay the path they were given under the BFF's own mesh
    identity, and an HTTP client resolves ``.`` and ``..`` before it sends. A
    path with a dot segment, or with an encoded slash or backslash (which the
    server decodes into a new segment boundary), would therefore reach a
    DIFFERENT memory-server route than the one the BFF route proxies. Such a
    path is answered 404 in the one shape, before any route, token check or
    upstream call. One place, so a new proxy route cannot forget it.

    The guard reads the path the way the mesh proxy will: per segment, with a
    ``;parameter`` stripped and one more percent-decode applied, and it refuses
    a path that is still percent-encoded for a dot, slash or backslash after
    the server's own decode.
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
        path = str(scope.get("path", ""))
        segments = path.replace("\\", "/").split("/")
        if any(_mesh_segment(segment) in (".", "..") for segment in segments):
            return "dot_segment"
        lowered = path.lower()
        if any(marker in lowered for marker in _RESIDUAL_SEPARATORS):
            return "encoded_separator"
        if _RESIDUAL_DOT in lowered:
            return "encoded_dot"
        return None


def _mesh_segment(segment: str) -> str:
    """The segment as the mesh proxy will normalise it."""
    return unquote(segment).split(";", 1)[0]
