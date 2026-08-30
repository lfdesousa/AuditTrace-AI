"""The transparent, byte-faithful proxy to ``/memory/*`` (M3-WU-D2-1).

Mirrors ``bff/proxy.py``'s shape and discipline (see that module's
docstring for the byte-fidelity rationale) but is generic across HTTP
method and path suffix, since the Souvenirs panel needs the full
per-layer CRUD surface (``GET/POST/PUT/DELETE /memory/{episodic,
procedural,semantic,conversational}/...`` — see
``src/audittrace/routes/memory.py``), not one fixed endpoint.

**Fail-closed on status code, always.** A 401/403/404 the orchestrator's
``/memory`` API returns for the exchanged token is relayed byte-for-byte
— never translated, retried with a different credential, or swallowed.
There is no code path in this module that can manufacture access the
exchanged token doesn't already carry: no shared key, no cross-user
query, no ``$or`` / global escape added at this layer. Isolation (RLS,
per-user manifest scoping, ADR-062) is entirely the memory API's job;
this module only forwards bytes with a different bearer token.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx
from starlette.responses import StreamingResponse

from bff.config import Settings

logger = logging.getLogger(__name__)

# Same hop-by-hop header set bff/proxy.py strips — Starlette's own ASGI
# server manages framing and connection lifecycle for the response it
# builds around our streamed body.
_STRIPPED_RESPONSE_HEADERS = frozenset(
    {"content-length", "transfer-encoding", "connection", "keep-alive"}
)


class MemoryProxyError(Exception):
    """Raised when the orchestrator's ``/memory`` API cannot be reached at
    all (transport failure before any response was received). Callers
    MUST map this to HTTP 502 — same discipline as ``bff.proxy.ProxyError``."""


async def proxy_memory_request(
    method: str,
    path_suffix: str,
    query_string: str,
    raw_body: bytes,
    content_type: str | None,
    minted_token: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> StreamingResponse:
    """Forward a ``/memory/<path_suffix>`` request to the orchestrator,
    byte-identical, and stream the response back byte-identical
    (including its status code — a 401/403/404 is relayed as-is, never
    reinterpreted).

    ``path_suffix`` and ``query_string`` are forwarded verbatim (no
    parsing, no re-encoding, no added filter) — the caller (``bff/app.py``)
    passes exactly what LibreChat sent after ``/memory/``. This module
    adds only the minted ``Authorization`` header and the ``X-Source``
    attribution header, same as ``bff/proxy.py``.
    """
    url = (
        settings.orchestrator_base_url.rstrip("/")
        + settings.orchestrator_memory_path_prefix
        + "/"
        + path_suffix.lstrip("/")
    )
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
        raise MemoryProxyError(f"orchestrator /memory unreachable: {exc}") from exc

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
