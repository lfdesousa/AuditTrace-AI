"""WP-11b OUTBOUND-MCP-TRACEPARENT (operator decision A3 = (a)).

Outbound brokered MCP calls carry the SERVER-MINTED ``traceparent`` (the
current server span's trace id), never an inbound, client-supplied one.
TRACEPARENT-TRUST stays the only inbound path.

UNIT cells only. These are NOT the AC-11b.1 cells: the ratified instrument
(ADDENDUM-3 S1/S2: POST /mcp, real loopback server, no MockTransport) lives in
``tests/test_outbound_mcp_traceparent_loopback.py``. Here a declared
``httpx.MockTransport`` sits behind ``mcp_broker.broker_tool_call`` /
``list_downstream_tools``. The server span is started on a context extracted
from the forged headers but that context is NOT attached, so the inbound span
context is never in the current context here; a helper that forwards the
inbound context is therefore invisible to these cells (it is caught by the
loopback file).

Neuters (each run alone, restored, tree hash checked):
  N-11b.1  forward the inbound header verbatim (broker posts ``headers`` taken
           from the inbound carrier)            -> not-R / equals-server cells RED
  N-11b.2  drop ``install_trace_context_policy()`` (default composite global)
           -> ``test_without_the_policy_the_outbound_id_is_the_inbound_one`` stays
           GREEN (it asserts the vulnerable state) and the policy cells RED
  N-11b.3  ``outbound_trace_headers`` returns ``{}`` (no explicit injection)
           -> every header-present cell RED
  N-11b.4  ``outbound_trace_headers`` also forwards tracestate/baggage
           -> the hygiene cells RED
  N-11b.5  emit a zero traceparent when no valid span -> the no-span cell RED
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import httpx
import pytest
from opentelemetry import propagate, trace
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanContext, TraceFlags, TraceState
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from audittrace import telemetry
from audittrace.identity import sentinel_user_context
from audittrace.services.mcp_broker import (
    DownstreamServer,
    broker_tool_call,
    list_downstream_tools,
)

# A syntactically valid, attacker-chosen inbound trace context.
FORGED_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
FORGED_TRACEPARENT = f"00-{FORGED_TRACE_ID}-00f067aa0ba902b7-01"

_SERVER = DownstreamServer(
    name="weather",
    url="http://weather-mcp.test/mcp",
    required_scope="audittrace:broker:weather",
)
_CALLER = replace(
    sentinel_user_context(), scopes=("audittrace:broker:weather",), is_admin=False
)


@pytest.fixture
def minted_policy() -> Iterator[None]:
    """The production state: the server-minted textmap is the global one."""
    saved = propagate.get_global_textmap()
    propagate.set_global_textmap(CompositePropagator([TraceContextTextMapPropagator()]))
    telemetry.install_trace_context_policy()
    try:
        yield
    finally:
        propagate.set_global_textmap(saved)


@pytest.fixture
def default_policy() -> Iterator[None]:
    """The vulnerable state N-11b.2 recreates: the SDK default parent-adopting textmap."""
    from opentelemetry.baggage.propagation import W3CBaggagePropagator

    saved = propagate.get_global_textmap()
    propagate.set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )
    try:
        yield
    finally:
        propagate.set_global_textmap(saved)


@pytest.fixture
def tracer() -> Iterator[trace.Tracer]:
    """A per-test provider, shut down on teardown."""
    provider = TracerProvider()
    try:
        yield provider.get_tracer("test")
    finally:
        provider.shutdown()


def _capture() -> tuple[list[httpx.Request], httpx.AsyncClient]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if body["method"] == "tools/list":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "ok"}]},
            },
        )

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _inbound_context() -> Any:
    """What the ASGI middleware does with a caller's forged headers."""
    return propagate.extract(
        {
            "traceparent": FORGED_TRACEPARENT,
            "tracestate": "forged=1",
            "baggage": "langfuse_user_id=victim",
        }
    )


def _tid(traceparent: str) -> str:
    return traceparent.split("-")[1]


async def _call_inside_server_span(tracer: trace.Tracer) -> tuple[str, httpx.Request]:
    """Run one brokered call inside a server span minted from a forged inbound."""
    seen, client = _capture()
    async with client:
        with tracer.start_as_current_span(
            "POST /mcp", context=_inbound_context(), kind=trace.SpanKind.SERVER
        ):
            server_tid = telemetry.current_trace_id_hex()
            assert server_tid is not None
            await broker_tool_call(
                user_context=_CALLER,
                tool_name="broker:weather:forecast",
                arguments={"city": "Zurich"},
                registry={"weather": _SERVER},
                client=client,
            )
    assert len(seen) == 1
    return server_tid, seen[0]


@pytest.mark.asyncio
async def test_tools_call_carries_the_server_minted_traceparent_not_the_forged_one(
    minted_policy: None, tracer: trace.Tracer
) -> None:
    """Unit cell (tools/call). Outbound id == server span id, and is not R."""
    server_tid, request = await _call_inside_server_span(tracer)
    tp = request.headers["traceparent"]
    assert _tid(tp) == server_tid
    assert _tid(tp) != FORGED_TRACE_ID
    assert FORGED_TRACE_ID not in tp
    assert "tracestate" not in request.headers
    assert "baggage" not in request.headers


@pytest.mark.asyncio
async def test_tools_list_carries_the_server_minted_traceparent_not_the_forged_one(
    minted_policy: None, tracer: trace.Tracer
) -> None:
    """Unit cell (tools/list, the other egress site of the broker)."""
    seen, client = _capture()
    async with client:
        with tracer.start_as_current_span(
            "POST /mcp", context=_inbound_context(), kind=trace.SpanKind.SERVER
        ):
            server_tid = telemetry.current_trace_id_hex()
            await list_downstream_tools(
                _CALLER, registry={"weather": _SERVER}, client=client
            )
    assert len(seen) == 1
    tp = seen[0].headers["traceparent"]
    assert _tid(tp) == server_tid
    assert _tid(tp) != FORGED_TRACE_ID
    assert "tracestate" not in seen[0].headers
    assert "baggage" not in seen[0].headers


@pytest.mark.asyncio
async def test_without_the_policy_the_outbound_id_is_the_inbound_one(
    default_policy: None, tracer: trace.Tracer
) -> None:
    """N-11b.2 recreated as a cell: with the parent-adopting default textmap the
    forged id DOES leave the process, so the assertions above are sensitive to
    ``install_trace_context_policy()`` being removed."""
    server_tid, request = await _call_inside_server_span(tracer)
    assert server_tid == FORGED_TRACE_ID
    assert _tid(request.headers["traceparent"]) == FORGED_TRACE_ID


@pytest.mark.asyncio
async def test_no_valid_span_means_no_traceparent_header(minted_policy: None) -> None:
    """No server span -> no header at all (never a zero id, never the forged one)."""
    assert telemetry.current_trace_id_hex() is None
    seen, client = _capture()
    async with client:
        await broker_tool_call(
            user_context=_CALLER,
            tool_name="broker:weather:forecast",
            arguments={},
            registry={"weather": _SERVER},
            client=client,
        )
    assert "traceparent" not in seen[0].headers
    assert "tracestate" not in seen[0].headers


def test_helper_returns_only_traceparent_and_matches_the_single_derivation(
    minted_policy: None, tracer: trace.Tracer
) -> None:
    with tracer.start_as_current_span("s"):
        headers = telemetry.outbound_trace_headers()
        assert list(headers) == ["traceparent"]
        assert _tid(headers["traceparent"]) == telemetry.current_trace_id_hex()


def test_helper_never_forwards_tracestate_of_the_current_span(
    minted_policy: None,
) -> None:
    ctx = SpanContext(
        trace_id=int(FORGED_TRACE_ID, 16),
        span_id=0x00F067AA0BA902B7,
        is_remote=False,
        trace_flags=TraceFlags(1),
        trace_state=TraceState([("vendor", "x")]),
    )
    with trace.use_span(trace.NonRecordingSpan(ctx)):
        assert list(telemetry.outbound_trace_headers()) == ["traceparent"]


def test_helper_is_empty_for_an_invalid_span_context(minted_policy: None) -> None:
    with trace.use_span(trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)):
        assert telemetry.outbound_trace_headers() == {}
