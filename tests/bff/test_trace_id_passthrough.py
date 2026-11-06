"""X-TRACE-ID-HEADER (v1.31.0 WP-4), AC-R10: the BFF forwards the memory
server's ``X-Trace-Id`` response header on every proxied surface.

Ten proxy modules each define ``_STRIPPED_RESPONSE_HEADERS`` and forward every
other upstream header into their ``StreamingResponse`` (spec F10). ``x-trace-id``
must never be added to any of those sets.

Mocks declared: ``httpx.MockTransport`` plays the memory server (no network).
BFF-local responses (the BFF's own 401/502) carry no header by design (spec D7).

Neuters (each alone, RED recorded in the build record): add ``"x-trace-id"`` to
the strip set of ``bff/proxy.py`` -> the behavioural cell for the chat proxy AND
the structural cell RED; add it to ``bff/memory_proxy.py`` -> the memory
behavioural cell AND its structural cell RED; add it to any of the other eight
-> that module's structural cell RED.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import httpx
import pytest
from starlette.responses import StreamingResponse

import bff
from bff.config import Settings
from bff.memory_proxy import proxy_memory_request
from bff.proxy import proxy_chat_completions

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"


def _proxy_modules() -> list[str]:
    """Every BFF module that defines ``_STRIPPED_RESPONSE_HEADERS`` (found by
    reading the package directory, so a NEW proxy module is covered at once)."""
    # index-gap: BS-001 (free-text membership of a constant name across files)
    root = Path(bff.__file__).resolve().parent
    return sorted(
        f"bff.{p.stem}"
        for p in root.glob("*.py")
        if "_STRIPPED_RESPONSE_HEADERS" in p.read_text(encoding="utf-8")
    )


PROXY_MODULES = _proxy_modules()


def test_all_ten_proxy_modules_are_found() -> None:
    """The structural cells below would be vacuous over an empty list."""
    assert len(PROXY_MODULES) == 10


@pytest.mark.parametrize("module", PROXY_MODULES)
def test_no_strip_set_contains_the_trace_header(module: str) -> None:
    """AC-R10 (structural): ``x-trace-id`` is in none of the ten strip sets."""
    stripped = importlib.import_module(module)._STRIPPED_RESPONSE_HEADERS
    assert "x-trace-id" not in {h.lower() for h in stripped}


def _settings() -> Settings:
    return Settings(
        exchange_client_secret="s3cr3t",
        orchestrator_base_url="http://orchestrator:8765",
        orchestrator_chat_path="/v1/chat/completions",
        orchestrator_memory_path_prefix="/memory",
        proxy_source_label="librechat",
    )


def _client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True},
            headers={"X-Trace-Id": TRACE_ID, "Connection": "keep-alive"},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _drain(response: StreamingResponse) -> None:
    async for _ in response.body_iterator:
        pass


async def test_chat_proxy_keeps_the_upstream_trace_header() -> None:
    """AC-R10 (behavioural, ``/v1/chat/completions``): the upstream's
    ``X-Trace-Id`` reaches the caller; a hop-by-hop header is still stripped."""
    async with _client() as client:
        response = await proxy_chat_completions(
            b"{}", "application/json", "tok", _settings(), client
        )
        await _drain(response)
    assert response.headers["x-trace-id"] == TRACE_ID
    assert "connection" not in response.headers


async def test_memory_proxy_keeps_the_upstream_trace_header() -> None:
    """AC-R10 (behavioural, ``/memory``): same, through the generic memory
    proxy."""
    async with _client() as client:
        response = await proxy_memory_request(
            method="GET",
            path_suffix="semantic",
            query_string="",
            raw_body=b"",
            content_type="application/json",
            minted_token="tok",
            settings=_settings(),
            http_client=client,
        )
        await _drain(response)
    assert response.headers["x-trace-id"] == TRACE_ID
