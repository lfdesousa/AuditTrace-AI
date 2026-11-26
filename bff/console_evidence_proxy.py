"""The byte-faithful proxy to the memory server's ``/evidence/*`` routes
(CONSOLE-AUDIT-EVIDENCE WU-C).

Two GET targets only, built from an id the route already validated and
percent-encoded here (``quote(id, safe="")``), so the BFF can never be steered to
another memory-server route (ADDENDUM-A1 section 3). No query string, no request
body and no inbound header other than the minted ``Authorization`` is forwarded.

**Fail-closed on status code, always.** A 401/403/404 the memory server answers
for the exchanged token is relayed byte-for-byte, never translated or retried
with another credential. Existence (unknown vs not yours), RLS and the owner
filter are the memory server's job; this module only forwards bytes.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Literal
from urllib.parse import quote

import httpx
from starlette.responses import StreamingResponse

from bff.config import Settings

logger = logging.getLogger(__name__)

EvidenceKind = Literal["messages", "interactions"]

# Same hop-by-hop header set the sibling proxies strip.
_STRIPPED_RESPONSE_HEADERS = frozenset(
    {"content-length", "transfer-encoding", "connection", "keep-alive"}
)


class ConsoleEvidenceProxyError(Exception):
    """Raised when the memory server's ``/evidence`` API cannot be reached at all
    (transport failure before any response). Callers MUST map this to HTTP 502."""


async def proxy_console_evidence_request(
    kind: EvidenceKind,
    identifier: str,
    minted_token: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> StreamingResponse:
    """GET ``<prefix>/<kind>/<identifier>`` upstream; stream the answer back
    unchanged, status code included."""
    base = (
        settings.orchestrator_base_url.rstrip("/")
        + settings.orchestrator_evidence_path_prefix
    )
    url = f"{base}/{kind}/{quote(identifier, safe='')}"
    request = http_client.build_request(
        "GET",
        url,
        headers={
            "Authorization": f"Bearer {minted_token}",
            "X-Source": settings.proxy_source_label,
        },
        timeout=settings.orchestrator_timeout_seconds,
    )
    try:
        upstream = await http_client.send(request, stream=True)
    except httpx.HTTPError as exc:
        raise ConsoleEvidenceProxyError(
            f"orchestrator /evidence unreachable: {exc}"
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
