"""Unit tests for sovereign_memory.server module-level helpers.

Integration coverage of the FastAPI app assembly + lifespan lives in the
route tests (``test_routes.py``, ``test_chat_proxy.py``). These tests
cover the small pieces of ``server.py`` that don't exercise naturally
through request traffic — primarily the urllib3 OTel request hook that
back-fills ``server.address`` per ADR-029.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from sovereign_memory.server import (
    _build_httpx_peer_service_map,
    make_httpx_async_peer_service_hook,
    make_httpx_peer_service_hook,
    urllib3_set_server_address,
)


def _mk_span(is_recording: bool = True) -> MagicMock:
    """Build a minimal fake of opentelemetry.trace.Span for assertion."""
    span = MagicMock()
    span.is_recording.return_value = is_recording
    return span


def _mk_request(url: str) -> SimpleNamespace:
    """Build a minimal fake of the urllib3 instrumentor's RequestInfo."""
    return SimpleNamespace(url=url)


class TestUrllib3SetServerAddress:
    """ADR-029: hook back-fills server.address / server.port that the
    upstream urllib3 instrumentor fails to emit."""

    def test_sets_server_address_from_hostname(self):
        span = _mk_span()
        urllib3_set_server_address(span, None, _mk_request("http://minio:9000/bucket"))
        span.set_attribute.assert_any_call("server.address", "minio")

    def test_sets_server_port_when_present(self):
        span = _mk_span()
        urllib3_set_server_address(span, None, _mk_request("http://minio:9000/bucket"))
        span.set_attribute.assert_any_call("server.port", 9000)

    def test_omits_port_when_url_has_no_port(self):
        """URLs without an explicit port (http://host/path) should only
        set server.address — no bogus default port attribute."""
        span = _mk_span()
        urllib3_set_server_address(span, None, _mk_request("http://example/path"))
        # server.address is set
        span.set_attribute.assert_any_call("server.address", "example")
        # server.port is NOT in any of the calls
        keys = [c.args[0] for c in span.set_attribute.call_args_list]
        assert "server.port" not in keys

    def test_no_op_when_span_is_none(self):
        """Passing None as span must not raise — the instrumentor's
        contract allows a None span (e.g. when the sampler drops)."""
        urllib3_set_server_address(None, None, _mk_request("http://minio:9000/x"))

    def test_no_op_when_span_not_recording(self):
        """Non-recording spans are a hot-path optimisation: skip work."""
        span = _mk_span(is_recording=False)
        urllib3_set_server_address(span, None, _mk_request("http://minio:9000/x"))
        span.set_attribute.assert_not_called()

    def test_no_attributes_when_url_has_no_hostname(self):
        """A malformed URL with no hostname still must not raise, just
        no-op. urlparse('///path') parses cleanly but hostname is None."""
        span = _mk_span()
        urllib3_set_server_address(span, None, _mk_request("///nohost"))
        span.set_attribute.assert_not_called()

    def test_https_url_with_port(self):
        """HTTPS URLs round-trip cleanly too (symmetric with http://)."""
        span = _mk_span()
        urllib3_set_server_address(
            span, None, _mk_request("https://api.example.com:8443/v1/x")
        )
        span.set_attribute.assert_any_call("server.address", "api.example.com")
        span.set_attribute.assert_any_call("server.port", 8443)


class TestBuildHttpxPeerServiceMap:
    """The per-port label map is built from Settings at lifespan start,
    so changing an LLM endpoint URL propagates to the service-graph
    label automatically."""

    def test_maps_all_three_llm_endpoints(self):
        settings = SimpleNamespace(
            llama_url="http://host.docker.internal:11435/v1",
            embed_url="http://host.docker.internal:11436/v1",
            summarizer_url="http://host.docker.internal:11437/v1",
        )
        got = _build_httpx_peer_service_map(settings)
        assert got == {
            11435: "qwen-chat-llm",
            11436: "nomic-embed-server",
            11437: "mistral-summariser-llm",
        }

    def test_omits_entry_when_port_is_missing(self):
        """A URL without a port (http://host/path) contributes nothing
        to the map; the edge falls back to server.address."""
        settings = SimpleNamespace(
            llama_url="http://llama/v1",
            embed_url="http://host:11436/v1",
            summarizer_url=None,
        )
        got = _build_httpx_peer_service_map(settings)
        assert got == {11436: "nomic-embed-server"}

    def test_empty_settings_produces_empty_map(self):
        settings = SimpleNamespace(llama_url=None, embed_url=None, summarizer_url=None)
        assert _build_httpx_peer_service_map(settings) == {}


class TestHttpxPeerServiceHook:
    """The hook sets peer.service from the URL port via a closed-over map.
    Tempo's service-graph processor reads peer.service ahead of
    server.address, so this produces one semantic edge per endpoint."""

    def _hook_with_defaults(self):
        mapping = {
            11435: "qwen-chat-llm",
            11437: "mistral-summariser-llm",
        }
        return make_httpx_peer_service_hook(mapping)

    def test_sets_peer_service_for_known_port(self):
        span = _mk_span()
        hook = self._hook_with_defaults()
        hook(span, _mk_request("http://host.docker.internal:11437/v1/chat/completions"))
        span.set_attribute.assert_any_call("peer.service", "mistral-summariser-llm")

    def test_does_not_set_for_unknown_port(self):
        """Ports not in the map fall back to server.address — the hook
        must not emit a misleading peer.service for chromadb etc."""
        span = _mk_span()
        hook = self._hook_with_defaults()
        hook(span, _mk_request("http://chromadb:8000/api/v2/foo"))
        keys = [c.args[0] for c in span.set_attribute.call_args_list]
        assert "peer.service" not in keys

    def test_no_op_on_none_span(self):
        hook = self._hook_with_defaults()
        # Must not raise.
        hook(None, _mk_request("http://host.docker.internal:11435/v1/x"))

    def test_no_op_on_non_recording_span(self):
        span = _mk_span(is_recording=False)
        hook = self._hook_with_defaults()
        hook(span, _mk_request("http://host.docker.internal:11435/v1/x"))
        span.set_attribute.assert_not_called()

    def test_no_op_when_url_has_no_port(self):
        span = _mk_span()
        hook = self._hook_with_defaults()
        hook(span, _mk_request("http://host/path"))
        keys = [c.args[0] for c in span.set_attribute.call_args_list]
        assert "peer.service" not in keys


class TestHttpxAsyncPeerServiceHook:
    """AsyncClient outbound calls need an ``async_request_hook`` that
    passes ``iscoroutinefunction()`` in the httpx instrumentor. A sync
    hook passed there is silently dropped — the failure mode that caused
    the Mistral summariser's spans to land without ``peer.service`` on
    the first wiring pass (2026-04-15)."""

    import pytest as _pytest

    @_pytest.mark.asyncio
    async def test_async_hook_is_coroutine_function(self):
        """The instrumentor gates async hooks on
        ``iscoroutinefunction`` — regression guard for the silent-drop."""
        import inspect

        hook = make_httpx_async_peer_service_hook({11437: "mistral"})
        assert inspect.iscoroutinefunction(hook)

    @_pytest.mark.asyncio
    async def test_async_hook_sets_peer_service(self):
        hook = make_httpx_async_peer_service_hook({11437: "mistral-summariser-llm"})
        span = _mk_span()
        await hook(
            span,
            _mk_request("http://host.docker.internal:11437/v1/chat/completions"),
        )
        span.set_attribute.assert_any_call("peer.service", "mistral-summariser-llm")

    @_pytest.mark.asyncio
    async def test_async_hook_no_op_on_none_span(self):
        hook = make_httpx_async_peer_service_hook({11437: "mistral"})
        await hook(None, _mk_request("http://host.docker.internal:11437/v1/x"))
