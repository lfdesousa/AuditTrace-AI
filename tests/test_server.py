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

from sovereign_memory.server import urllib3_set_server_address


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
