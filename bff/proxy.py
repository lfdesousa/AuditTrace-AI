"""The transparent, byte-identical proxy to ``/v1/chat/completions``.

The OpenAI schema on ``/v1`` is byte-inviolate
(``feedback_openai_schema_inviolate``). This module never parses,
re-serializes, or otherwise touches the request or response BODY — it
forwards the exact bytes LibreChat sent and streams back the exact bytes
the orchestrator returns, so a JSON key-order change, a whitespace
difference, or an SSE-framing change on either side can never be
introduced here even by accident. The only things this module adds are
transport-level: the minted ``Authorization`` header (replacing whatever
LibreChat sent) and an ``X-Source`` attribution header.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx
from starlette.responses import StreamingResponse

from bff.config import Settings

logger = logging.getLogger(__name__)

# Hop-by-hop / transport headers that must never be forwarded verbatim
# from the upstream response — Starlette's own ASGI server manages
# framing (Content-Length/Transfer-Encoding) and connection lifecycle.
_STRIPPED_RESPONSE_HEADERS = frozenset(
    {"content-length", "transfer-encoding", "connection", "keep-alive"}
)


class ProxyError(Exception):
    """Raised when the orchestrator cannot be reached at all (transport
    failure before any response was received). Callers MUST map this to
    HTTP 502 — the caller's own request was fine; the BFF's downstream
    dependency is not answering."""


async def proxy_chat_completions(
    raw_body: bytes,
    content_type: str,
    minted_token: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> StreamingResponse:
    """Forward ``raw_body`` unchanged to the orchestrator's
    ``/v1/chat/completions`` and stream the response back byte-identical.

    Uses ``httpx``'s explicit build-request/send(stream=True) pair (not
    ``client.stream(...)`` as an ``async with``) so the upstream response
    object survives past this function's return — Starlette iterates a
    ``StreamingResponse``'s body AFTER the route handler returns, so a
    context manager closed here would truncate every SSE stream mid-frame.
    The upstream response is explicitly closed once the body iterator is
    exhausted (or the client disconnects), via the ``finally`` in
    :func:`_relay_body`.
    """
    url = settings.orchestrator_base_url.rstrip("/") + settings.orchestrator_chat_path
    headers = {
        "Authorization": f"Bearer {minted_token}",
        "Content-Type": content_type or "application/json",
        "X-Source": settings.proxy_source_label,
    }
    request = http_client.build_request(
        "POST",
        url,
        content=raw_body,
        headers=headers,
        timeout=settings.orchestrator_timeout_seconds,
    )
    try:
        upstream = await http_client.send(request, stream=True)
    except httpx.HTTPError as exc:
        raise ProxyError(f"orchestrator unreachable: {exc}") from exc

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
