"""Tests for bff/proxy.py — the byte-identical, streaming reverse proxy.

The load-bearing test in this file is
``TestParity.test_body_forwarded_byte_identical``: it uses a
deliberately non-canonical JSON payload (odd whitespace, unusual key
order, a unicode value) so that ANY re-serialization step (a
``json.loads``/``json.dumps`` round-trip, key sorting, whitespace
normalisation) introduced into ``proxy_chat_completions`` would change
the bytes and fail the exact-equality assertion. Replace the raw-bytes
forward with a parse-and-re-encode and this test goes RED.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.responses import StreamingResponse

from bff.config import Settings
from bff.proxy import ProxyError, proxy_chat_completions


def _settings(**overrides) -> Settings:
    defaults = dict(
        exchange_client_secret="s3cr3t",
        orchestrator_base_url="http://orchestrator:8765",
        orchestrator_chat_path="/v1/chat/completions",
        proxy_source_label="librechat",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _collect(response: StreamingResponse) -> bytes:
    chunks = [chunk async for chunk in response.body_iterator]
    out = b""
    for chunk in chunks:
        out += chunk if isinstance(chunk, bytes) else chunk.encode()
    return out


class TestParity:
    async def test_body_forwarded_byte_identical(self) -> None:
        # Deliberately non-canonical: odd spacing, unusual key order,
        # a unicode value — anything that re-serializes would visibly
        # differ from this exact byte string.
        raw_body = (
            b'{"messages":  [{"role":"user",   "content":"caf\xc3\xa9 \xf0\x9f\x94\x92"}],'
            b'"model":"audittrace-chat","stream":false,"zeta":1,"alpha":2}'
        )
        captured: dict[str, bytes] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.read()
            return httpx.Response(200, json={"ok": True})

        client = _client(handler)
        response = await proxy_chat_completions(
            raw_body, "application/json", "minted-token-abc", _settings(), client
        )
        await _collect(response)
        assert captured["body"] == raw_body

    async def test_response_body_forwarded_byte_identical(self) -> None:
        upstream_body = b'{"id":"x","choices":[{"delta":{"content":"hi"}}],"zeta":true}'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=upstream_body, headers={"content-type": "application/json"}
            )

        client = _client(handler)
        response = await proxy_chat_completions(
            b"{}", "application/json", "minted-token-abc", _settings(), client
        )
        collected = await _collect(response)
        assert collected == upstream_body
        assert response.status_code == 200
        assert response.media_type == "application/json"

    async def test_sse_stream_forwarded_byte_identical(self) -> None:
        sse_body = b'data: {"delta":"hel"}\n\ndata: {"delta":"lo"}\n\ndata: [DONE]\n\n'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=sse_body, headers={"content-type": "text/event-stream"}
            )

        client = _client(handler)
        response = await proxy_chat_completions(
            b'{"stream": true}',
            "application/json",
            "minted-token-abc",
            _settings(),
            client,
        )
        collected = await _collect(response)
        assert collected == sse_body
        assert response.media_type == "text/event-stream"


class TestHeaders:
    async def test_minted_token_replaces_any_original_authorization(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"ok": True})

        client = _client(handler)
        response = await proxy_chat_completions(
            b"{}", "application/json", "minted-token-xyz", _settings(), client
        )
        await _collect(response)
        assert captured["authorization"] == "Bearer minted-token-xyz"

    async def test_x_source_stamped(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["x-source"] = request.headers.get("x-source", "")
            return httpx.Response(200, json={"ok": True})

        client = _client(handler)
        response = await proxy_chat_completions(
            b"{}",
            "application/json",
            "t",
            _settings(proxy_source_label="librechat"),
            client,
        )
        await _collect(response)
        assert captured["x-source"] == "librechat"

    async def test_hop_by_hop_response_headers_stripped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"ok": True},
                headers={"Connection": "keep-alive", "X-Custom": "keep-me"},
            )

        client = _client(handler)
        response = await proxy_chat_completions(
            b"{}", "application/json", "t", _settings(), client
        )
        await _collect(response)
        assert "connection" not in {k.lower() for k in response.headers.keys()}
        assert response.headers.get("x-custom") == "keep-me"

    async def test_orchestrator_url_built_from_settings(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"ok": True})

        client = _client(handler)
        settings = _settings(
            orchestrator_base_url="http://audittrace-server:8765/",
            orchestrator_chat_path="/v1/chat/completions",
        )
        response = await proxy_chat_completions(
            b"{}", "application/json", "t", settings, client
        )
        await _collect(response)
        assert captured["url"] == "http://audittrace-server:8765/v1/chat/completions"


class TestUnreachableOrchestrator:
    async def test_transport_failure_raises_proxy_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = _client(handler)
        with pytest.raises(ProxyError, match="unreachable"):
            await proxy_chat_completions(
                b"{}", "application/json", "t", _settings(), client
            )
