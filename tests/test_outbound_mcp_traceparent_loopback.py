"""WP-11b AC-11b.1 (ADDENDUM-3 S1/S2): outbound brokered MCP calls over a real socket.

Instrument, as ratified: the entry point is ``POST /mcp`` (``tools/call`` of a
``broker:`` tool, and ``tools/list``) on ``create_app()``; the downstream is a
real HTTP server on 127.0.0.1 (ephemeral port, real ``httpx.AsyncHTTPTransport``)
reached through the REAL broker singleton client. ``httpx.MockTransport`` is not
used anywhere in this file. The loopback records the RAW header lists so a
duplicated ``traceparent`` would be visible.

The server span is the one the ASGI instrumentor starts for the request: it
extracts the forged inbound headers with the process-global textmap and attaches
that context, so the inbound span context is in the CURRENT context while the
broker runs. That is what makes the declined-instrumentor variant sensitive to a
helper that forwards the inbound context (neuter RN1).

Variants: the HTTPX client instrumentor attached, and declined
(``instrumentation_strict=false`` path: only the explicit
``telemetry.outbound_trace_headers()`` decides the header). Each cell asserts
exactly one ``traceparent`` value, equal in trace id to the server span and not
the forged one, and no ``tracestate`` / ``baggage`` downstream, including when
baggage is present in the current context (NB-2).

Neuters (each alone, restored, tree hash checked) that must be RED here:
  RN1  helper forwards the inbound context   -> declined cells (tools/call, tools/list)
  RN2  install_trace_context_policy no-op    -> all cells
  RN3  helper returns {}                     -> declined cells
  RN4b helper injects baggage + returns every key -> baggage cells (RN4a, tracestate
       filter only, is caught by the unit file: no server span here carries tracestate)
  RN5  zero traceparent without a valid span -> unit file's no-span cell
  RN6/7 a broker egress site drops headers   -> declined cell of that method
  RN9  helper returns a random id            -> declined cells
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry import baggage, propagate, trace
from opentelemetry import context as otel_context
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)

from audittrace import config as config_mod
from audittrace import dependencies, telemetry
from audittrace.server import create_app
from audittrace.services import mcp_broker

FORGED_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
FORGED_HEADERS = {
    "traceparent": f"00-{FORGED_TRACE_ID}-00f067aa0ba902b7-01",
    "tracestate": "forged=1",
    "baggage": "langfuse_user_id=victim",
}
_TOOLS_CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "broker:weather:forecast", "arguments": {"city": "Zurich"}},
}
_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
_BODIES = {"tools/call": _TOOLS_CALL, "tools/list": _TOOLS_LIST}


class _Loopback:
    """Real HTTP server on 127.0.0.1 (ephemeral port) recording raw header lists."""

    def __init__(self) -> None:
        self.seen: list[dict[str, list[str]]] = []
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (stdlib hook name)
                outer.seen.append(
                    {
                        k.lower(): self.headers.get_all(k) or []
                        for k in set(self.headers)
                    }
                )
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                if body.get("method") == "tools/list":
                    result: dict[str, Any] = {
                        "tools": [
                            {"name": "forecast", "inputSchema": {"type": "object"}}
                        ]
                    }
                else:
                    result = {
                        "content": [{"type": "text", "text": "ok"}],
                        "isError": False,
                    }
                out = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args: Any) -> None:
                return

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/mcp"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _Loopback:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@contextlib.contextmanager
def _tracer_provider() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield provider, exporter
    finally:
        provider.shutdown()


@contextlib.contextmanager
def _global_textmap_at_sdk_default() -> Iterator[None]:
    """Start from the vulnerable SDK default so ``create_app()`` must install the policy."""
    saved = propagate.get_global_textmap()
    propagate.set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )
    try:
        yield
    finally:
        propagate.set_global_textmap(saved)


@contextlib.contextmanager
def _httpx_instrumentor(provider: TracerProvider, *, attached: bool) -> Iterator[None]:
    instrumentor = HTTPXClientInstrumentor()
    was_attached = instrumentor.is_instrumented_by_opentelemetry
    if was_attached:
        instrumentor.uninstrument()
    if attached:
        instrumentor.instrument(tracer_provider=provider)
    try:
        yield
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        if was_attached:
            instrumentor.instrument()


def _with_baggage_hook(span: Any, scope: Any) -> None:
    """The production inbound hook plus baggage set in the CURRENT context (NB-2)."""
    telemetry.inbound_trace_context_hook(span, scope)
    otel_context.attach(baggage.set_baggage("langfuse_user_id", "in-process"))


def _post_mcp(
    monkeypatch: pytest.MonkeyPatch,
    container: Any,
    *,
    attached: bool,
    method: str,
    baggage_in_context: bool = False,
) -> dict[str, Any]:
    with (
        _tracer_provider() as (provider, exporter),
        _global_textmap_at_sdk_default(),
        _httpx_instrumentor(provider, attached=attached),
        _Loopback() as loopback,
    ):
        monkeypatch.setenv("AUDITTRACE_TRACING_ENABLED", "false")
        monkeypatch.setenv(
            "AUDITTRACE_MCP_BROKER_SERVERS",
            json.dumps(
                {
                    "weather": {
                        "url": loopback.url,
                        "required_scope": "audittrace:broker:weather",
                    }
                }
            ),
        )
        config_mod.get_settings.cache_clear()
        monkeypatch.setattr(mcp_broker, "_downstream_client", None)
        monkeypatch.setattr(dependencies, "container", container)
        try:
            app = create_app()
            FastAPIInstrumentor.uninstrument_app(app)
            app.middleware_stack = None
            FastAPIInstrumentor.instrument_app(
                app,
                tracer_provider=provider,
                server_request_hook=(
                    _with_baggage_hook
                    if baggage_in_context
                    else telemetry.inbound_trace_context_hook
                ),
            )
            textmap = type(propagate.get_global_textmap()).__name__
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/mcp", json=_BODIES[method], headers=FORGED_HEADERS
                )
            downstream = mcp_broker._downstream_client
            if downstream is not None:
                asyncio.run(downstream.aclose())
        finally:
            config_mod.get_settings.cache_clear()
        spans = exporter.get_finished_spans()
    servers = [s for s in spans if s.kind == trace.SpanKind.SERVER]
    clients = [s for s in spans if s.kind == trace.SpanKind.CLIENT]
    return {
        "status": response.status_code,
        "body": response.json(),
        "seen": loopback.seen,
        "textmap": textmap,
        "server_tid": format(servers[0].context.trace_id, "032x") if servers else None,
        "client_sids": [format(s.context.span_id, "016x") for s in clients],
    }


def _assert_server_minted_only(res: dict[str, Any], *, attached: bool) -> None:
    assert res["status"] == 200, res["body"]
    assert res["textmap"] == "ServerMintedTraceContext"
    assert res["server_tid"] is not None
    assert len(res["seen"]) == 1
    headers = res["seen"][0]
    assert len(headers["traceparent"]) == 1, headers["traceparent"]
    traceparent = headers["traceparent"][0]
    _version, trace_id, parent_id, _flags = traceparent.split("-")
    assert trace_id == res["server_tid"]
    assert trace_id != FORGED_TRACE_ID
    assert FORGED_TRACE_ID not in traceparent
    assert "tracestate" not in headers
    assert "baggage" not in headers
    if attached:
        # The instrumentor overwrites the explicit header with its CLIENT span.
        assert parent_id in res["client_sids"]


@pytest.mark.parametrize(
    "attached", [True, False], ids=["httpx-instrumented", "instrumentor-declined"]
)
@pytest.mark.parametrize("method", ["tools/call", "tools/list"])
def test_post_mcp_downstream_carries_the_server_minted_traceparent_only(
    monkeypatch: pytest.MonkeyPatch, test_container: Any, method: str, attached: bool
) -> None:
    """AC-11b.1: POST /mcp -> real loopback; one traceparent == server span, not R."""
    res = _post_mcp(monkeypatch, test_container, attached=attached, method=method)
    _assert_server_minted_only(res, attached=attached)


@pytest.mark.parametrize(
    "attached", [True, False], ids=["httpx-instrumented", "instrumentor-declined"]
)
@pytest.mark.parametrize("method", ["tools/call", "tools/list"])
def test_post_mcp_baggage_in_the_current_context_never_goes_downstream(
    monkeypatch: pytest.MonkeyPatch, test_container: Any, method: str, attached: bool
) -> None:
    """NB-2: baggage set by server code in the current context is not forwarded."""
    res = _post_mcp(
        monkeypatch,
        test_container,
        attached=attached,
        method=method,
        baggage_in_context=True,
    )
    _assert_server_minted_only(res, attached=attached)


def test_helper_ignores_baggage_in_the_current_context() -> None:
    """NB-2 at the helper: the only key is traceparent even with baggage attached."""
    with _tracer_provider() as (provider, _exporter):
        tracer = provider.get_tracer("test")
        token = otel_context.attach(baggage.set_baggage("langfuse_user_id", "x"))
        try:
            with tracer.start_as_current_span("s"):
                assert list(telemetry.outbound_trace_headers()) == ["traceparent"]
        finally:
            otel_context.detach(token)
