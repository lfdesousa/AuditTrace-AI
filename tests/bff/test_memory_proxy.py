"""Tests for bff/memory_proxy.py — the generic, byte-faithful ``/memory``
reverse proxy.

The load-bearing tests are in ``TestFailClosedStatusForwarding``: a
401/403/404 the orchestrator returns for the exchanged token must be
relayed AS-IS — never translated into a different status, never retried
with a different credential. Replace the status-code passthrough with a
hardcoded 200 (or any status normalisation) and those tests go RED.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.responses import StreamingResponse

from bff.config import Settings
from bff.memory_proxy import MemoryProxyError, proxy_memory_request


def _settings(**overrides) -> Settings:
    defaults = dict(
        exchange_client_secret="s3cr3t",
        orchestrator_base_url="http://orchestrator:8765",
        orchestrator_memory_path_prefix="/memory",
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


class TestMethodAndPathForwarding:
    async def test_method_and_url_built_from_path_suffix(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"items": []})

        client = _client(handler)
        response = await proxy_memory_request(
            "GET",
            "episodic",
            "",
            b"",
            None,
            "minted-token",
            _settings(),
            client,
        )
        await _collect(response)
        assert captured["method"] == "GET"
        assert captured["url"] == "http://orchestrator:8765/memory/episodic"

    async def test_nested_path_suffix_preserved(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"ok": True})

        client = _client(handler)
        response = await proxy_memory_request(
            "GET",
            "semantic/decisions/some-doc-id",
            "",
            b"",
            None,
            "t",
            _settings(),
            client,
        )
        await _collect(response)
        assert (
            captured["url"]
            == "http://orchestrator:8765/memory/semantic/decisions/some-doc-id"
        )

    async def test_query_string_appended(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"ok": True})

        client = _client(handler)
        response = await proxy_memory_request(
            "GET",
            "episodic",
            "limit=500&sort=recency",
            b"",
            None,
            "t",
            _settings(),
            client,
        )
        await _collect(response)
        assert captured["url"] == (
            "http://orchestrator:8765/memory/episodic?limit=500&sort=recency"
        )

    async def test_empty_query_string_no_trailing_question_mark(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"ok": True})

        client = _client(handler)
        response = await proxy_memory_request(
            "GET", "episodic", "", b"", None, "t", _settings(), client
        )
        await _collect(response)
        assert "?" not in captured["url"]


class TestBodyParity:
    async def test_request_body_forwarded_byte_identical(self) -> None:
        raw_body = b'{"content":  "caf\xc3\xa9 \xf0\x9f\x94\x92","zeta":1,"alpha":2}'
        captured: dict[str, bytes] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.read()
            return httpx.Response(201, json={"ok": True})

        client = _client(handler)
        response = await proxy_memory_request(
            "POST",
            "episodic",
            "",
            raw_body,
            "application/json",
            "minted-token",
            _settings(),
            client,
        )
        await _collect(response)
        assert captured["body"] == raw_body

    async def test_response_body_forwarded_byte_identical(self) -> None:
        upstream_body = b'{"id":"x","layer":"episodic","zeta":true}'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=upstream_body, headers={"content-type": "application/json"}
            )

        client = _client(handler)
        response = await proxy_memory_request(
            "GET", "episodic/foo", "", b"", None, "t", _settings(), client
        )
        collected = await _collect(response)
        assert collected == upstream_body
        assert response.media_type == "application/json"

    async def test_empty_body_sent_as_none_not_empty_bytes(self) -> None:
        """GET/DELETE typically carry no body — httpx's ``content=None``
        avoids stamping a spurious ``Content-Length: 0``/body frame that
        an empty-bytes ``content=b""`` could otherwise produce."""
        captured: dict[str, bytes] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.read()
            return httpx.Response(200, json={"ok": True})

        client = _client(handler)
        response = await proxy_memory_request(
            "DELETE", "episodic/foo", "", b"", None, "t", _settings(), client
        )
        await _collect(response)
        assert captured["body"] == b""


class TestHeaders:
    async def test_minted_token_stamped_as_authorization(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"ok": True})

        client = _client(handler)
        response = await proxy_memory_request(
            "GET", "episodic", "", b"", None, "minted-token-xyz", _settings(), client
        )
        await _collect(response)
        assert captured["authorization"] == "Bearer minted-token-xyz"

    async def test_x_source_stamped(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["x-source"] = request.headers.get("x-source", "")
            return httpx.Response(200, json={"ok": True})

        client = _client(handler)
        response = await proxy_memory_request(
            "GET",
            "episodic",
            "",
            b"",
            None,
            "t",
            _settings(proxy_source_label="librechat"),
            client,
        )
        await _collect(response)
        assert captured["x-source"] == "librechat"

    async def test_no_content_type_header_when_none_given(self) -> None:
        captured: dict[str, bool] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["has_content_type"] = "content-type" in {
                k.lower() for k in request.headers.keys()
            }
            return httpx.Response(200, json={"ok": True})

        client = _client(handler)
        response = await proxy_memory_request(
            "GET", "episodic", "", b"", None, "t", _settings(), client
        )
        await _collect(response)
        assert captured["has_content_type"] is False

    async def test_hop_by_hop_response_headers_stripped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"ok": True},
                headers={"Connection": "keep-alive", "X-Custom": "keep-me"},
            )

        client = _client(handler)
        response = await proxy_memory_request(
            "GET", "episodic", "", b"", None, "t", _settings(), client
        )
        await _collect(response)
        assert "connection" not in {k.lower() for k in response.headers.keys()}
        assert response.headers.get("x-custom") == "keep-me"


class TestFailClosedStatusForwarding:
    """The non-negotiable spec guard: a 401/403/404 from the orchestrator
    is relayed byte-for-byte — the BFF never manufactures access, never
    retries with a shared credential, never translates the status code."""

    @pytest.mark.parametrize("upstream_status", [401, 403, 404])
    async def test_error_status_forwarded_verbatim(self, upstream_status: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                upstream_status,
                json={"detail": "denied"},
                headers={"content-type": "application/json"},
            )

        client = _client(handler)
        response = await proxy_memory_request(
            "GET", "episodic", "", b"", None, "t", _settings(), client
        )
        assert response.status_code == upstream_status
        collected = await _collect(response)
        assert collected == b'{"detail":"denied"}'

    async def test_forbidden_body_forwarded_not_swallowed(self) -> None:
        """The 403 BODY (not just the status) must reach the caller —
        proves the error path doesn't drop it in favour of a generic
        BFF-authored message."""
        upstream_body = b'{"detail":"insufficient scope: memory:episodic:write"}'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403, content=upstream_body, headers={"content-type": "application/json"}
            )

        client = _client(handler)
        response = await proxy_memory_request(
            "PUT",
            "episodic/foo",
            "",
            b"{}",
            "application/json",
            "t",
            _settings(),
            client,
        )
        collected = await _collect(response)
        assert response.status_code == 403
        assert collected == upstream_body

    async def test_success_status_also_forwarded_verbatim(self) -> None:
        """Sibling of the error tests — proves the passthrough isn't
        always-403, just genuinely status-code-agnostic."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"id": "new-doc"})

        client = _client(handler)
        response = await proxy_memory_request(
            "POST", "episodic", "", b"{}", "application/json", "t", _settings(), client
        )
        assert response.status_code == 201


class TestUnreachableOrchestrator:
    async def test_transport_failure_raises_memory_proxy_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = _client(handler)
        with pytest.raises(MemoryProxyError, match="unreachable"):
            await proxy_memory_request(
                "GET", "episodic", "", b"", None, "t", _settings(), client
            )
