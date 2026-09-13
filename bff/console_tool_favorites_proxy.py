"""The transparent, byte-faithful proxy to
``/console/tool-favorites/*`` (Tool-Favorites domain, MongoDB-
elimination EPIC).

Mirrors ``bff/console_conversation_tags_proxy.py``'s shape and
discipline exactly (see that module's docstring for the byte-fidelity
rationale) but targets the orchestrator's console-tool-favorites mount
(``settings.orchestrator_console_tool_favorites_path_prefix``) instead
of ``/console/conversation-tags`` — a genuinely different downstream
mount, so this is its own small module rather than a hardcoded prefix
threaded through an existing proxy function.

**Fail-closed on status code, always.** A 401/403/404/409 the
orchestrator's ``/console/tool-favorites`` API returns for the
exchanged token is relayed byte-for-byte — never translated, retried
with a different credential, or swallowed. There is no code path in
this module that can manufacture access the exchanged token doesn't
already carry. Isolation (RLS, per-user ``user_sub`` filtering) and cap
enforcement (``MAX_TOOL_FAVORITES``) are entirely the orchestrator
API's job; this module only forwards bytes with a different bearer
token.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx
from starlette.responses import StreamingResponse

from bff.config import Settings

logger = logging.getLogger(__name__)

# Same hop-by-hop header set bff/memory_proxy.py strips.
_STRIPPED_RESPONSE_HEADERS = frozenset(
    {"content-length", "transfer-encoding", "connection", "keep-alive"}
)


class ConsoleToolFavoritesProxyError(Exception):
    """Raised when the orchestrator's ``/console/tool-favorites`` API
    cannot be reached at all (transport failure before any response was
    received). Callers MUST map this to HTTP 502 — same discipline as
    ``bff.memory_proxy.MemoryProxyError``."""


async def proxy_console_tool_favorites_request(
    method: str,
    path_suffix: str,
    query_string: str,
    raw_body: bytes,
    content_type: str | None,
    minted_token: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> StreamingResponse:
    """Forward a ``/console/tool-favorites[/<path_suffix>]`` request to
    the orchestrator, byte-identical, and stream the response back
    byte-identical (including its status code).

    ``path_suffix`` may be empty (the base ``/console/tool-favorites``
    path — list/add) — in that case no trailing slash is appended, so
    the orchestrator sees exactly ``/console/tool-favorites``, not
    ``/console/tool-favorites/``.
    """
    base = (
        settings.orchestrator_base_url.rstrip("/")
        + settings.orchestrator_console_tool_favorites_path_prefix
    )
    url = f"{base}/{path_suffix.lstrip('/')}" if path_suffix else base
    if query_string:
        url = f"{url}?{query_string}"
    headers = {
        "Authorization": f"Bearer {minted_token}",
        "X-Source": settings.proxy_source_label,
    }
    if content_type:
        headers["Content-Type"] = content_type
    request = http_client.build_request(
        method,
        url,
        content=raw_body or None,
        headers=headers,
        timeout=settings.orchestrator_timeout_seconds,
    )
    try:
        upstream = await http_client.send(request, stream=True)
    except httpx.HTTPError as exc:
        raise ConsoleToolFavoritesProxyError(
            f"orchestrator /console/tool-favorites unreachable: {exc}"
        ) from exc

    response_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _STRIPPED_RESPONSE_HEADERS
    }
    return StreamingResponse(
        _relay_body(upstream),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers=response_headers,
    )


async def _relay_body(upstream: httpx.Response) -> AsyncIterator[bytes]:
    """Yield the upstream response bytes unchanged, closing it on exit."""
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk
    finally:
        await upstream.aclose()
