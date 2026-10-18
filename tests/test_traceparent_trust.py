"""TRACEPARENT-TRUST: the memory server mints the trace identity of every request.

An inbound W3C ``traceparent`` is untrusted caller input. The policy under
test (``telemetry.ServerMintedTraceContext`` + ``inbound_trace_context_hook``,
installed by ``create_app()``) keeps it ONLY as a span link and two validated
attributes; it never becomes the parent, never decides sampling, never reaches
an audit row, a log line or an outbound call; ``tracestate`` and ``baggage``
are neither adopted nor forwarded.

Spec: ``audittrace-private/specs/2026-10-17-SPEC-traceparent-trust.md``;
gate: ``decisions-in/2026-10-18-SPEC-GATE-traceparent-trust.md`` (conditions
C1-C8). Cell names below (AC1 ... AC-BG ... AC-D9) are the spec's.

Mechanism (gate C3 of the X-TRACE-ID verdict): the OTel global
``TracerProvider`` is set-once per process, so every in-process case builds a
FRESH app with ``create_app()`` and re-instruments it with a LOCAL SDK
provider + ``InMemorySpanExporter`` (``instrument_app(..., tracer_provider=)``).
The global provider is not touched and coverage of ``telemetry.py`` /
``server.py`` is counted. The real, un-re-instrumented ``create_app()`` wiring
(the hook argument) is pinned separately by ``test_create_app_wires_*``.
The laptop ``init_telemetry`` mode is global and set-once: it is reduced to
"the policy survives ``init_telemetry``" (the Langfuse init path never swaps
the textmap), stated rather than silently skipped.

Neuters (each run INDIVIDUALLY, RED/GREEN recorded in the build record):
  N1  default composite propagator (adopt the parent), hook kept
  N1b policy + ``W3CBaggagePropagator`` composite
  N2  propagator kept, hook dropped
  N3  ``install_trace_context_policy()`` moved inside the swallowing ``try``
  N4  ``install_trace_context_policy()`` call removed from ``create_app()``
  N5  hook records the raw header bytes / links an invalid context
  N6  ``create_app()`` instruments without ``server_request_hook=``
The two consistency pins (AC3, AC6 traceparent==minted) hold under every
neuter above by construction; ``test_ac3_pin_can_fail`` and
``test_ac6_pin_can_fail`` prove each pin is load-bearing by feeding it a
divergent derivation / a rewritten inject (gate C5).

The log half of AC7 (gate C6): the product calls
``LoggingInstrumentor().instrument(set_logging_format=False)`` only, so on the
contrib line in use its records carry NO ``otelTraceID`` (measured in Loki:
500/500 request-scoped lines without ``trace_id``). AC7a therefore asserts the
product configuration writes none of the caller's values into a log line;
AC7b asserts, under ``inject_trace_context=True``, that the line carries the
MINTED root id (forward protection for the log-trace-id unit).
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import logging
import textwrap
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import pytest
import urllib3
from fastapi import HTTPException
from fastapi.testclient import TestClient
from opentelemetry import baggage, propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.context import Context
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import NoOpTracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from audittrace import config as config_mod
from audittrace import telemetry
from audittrace.identity import sentinel_user_context
from audittrace.logging_config import StructuredFormatter
from audittrace.routes.chat import _current_trace_id_hex as chat_tid
from audittrace.routes.memory_pdf.signature import _current_trace_id as sig_tid
from audittrace.routes.memory_upload.handler import _current_traceparent as upload_tp
from audittrace.server import create_app
from audittrace.services import mcp_broker
from audittrace.services.console_store._context import current_trace_id_hex as con_tid
from audittrace.services.memory_audit import _build_row
from audittrace.services.memory_audit import _current_trace_id_hex as audit_tid
from audittrace.services.scan_amqp_client import ScanAmqpClient
from audittrace.services.scan_request_publisher import ScanRequestEnvelope
from audittrace.services.session_summarizer import _span_trace_id_hex as summ_tid

T = "0af7651916cd43dd8448eb211c80319c"
P1 = "b7ad6b7169203331"
P2 = "00f067aa0ba902b7"
TS = "vendor=evil,injected=1"
BG = "k=v,injected=1"
V_USER = "victim-sub-0001"
W_SESSION = "victim-session-0001"
LF_BAGGAGE = f"langfuse_user_id={V_USER},langfuse_session_id={W_SESSION}"
INBOUND = {"tracestate": TS, "baggage": BG}


def _tp(flags: str = "01", parent: str = P1) -> str:
    return f"00-{T}-{parent}-{flags}"


# ---------------------------------------------------------------------------
# helpers: sandbox, loopback upstream, app builder
# ---------------------------------------------------------------------------
def _default_composite() -> CompositePropagator:
    """What the OTel SDK installs when nothing else does (the vulnerable state)."""
    return CompositePropagator(
        [TraceContextTextMapPropagator(), W3CBaggagePropagator()]
    )


@pytest.fixture(scope="module")
def sandbox() -> Iterator[None]:
    """Save/restore the process-global textmap (gate A5); start from the default.

    Starting from the default composite makes ``create_app()`` the thing that
    installs the policy, so removing that call (N4) is observable.
    """
    saved = propagate.get_global_textmap()
    propagate.set_global_textmap(_default_composite())
    mp = pytest.MonkeyPatch()
    mp.setenv("AUDITTRACE_AUTH_ENABLED", "true")
    mp.setenv("AUDITTRACE_AUTH_REQUIRED", "true")
    config_mod.get_settings.cache_clear()
    try:
        yield
    finally:
        mp.undo()
        config_mod.get_settings.cache_clear()
        propagate.set_global_textmap(saved)


class _Loopback:
    """Real loopback HTTP upstream that records the request headers it gets."""

    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def _record(self) -> None:
                outer.requests.append({k.lower(): v for k, v in self.headers.items()})
                length = int(self.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)
                body = json.dumps({"result": {"tools": []}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _record  # noqa: N815 - http.server naming
            do_POST = _record  # noqa: N815

            def log_message(self, *args: Any) -> None:  # silence stderr
                return

        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _Loopback:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)


class _StubExchange:
    """Captures the aio_pika message the real ScanAmqpClient builds."""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def publish(self, message: Any, routing_key: str) -> None:
        self.messages.append(message)


def _load_should_export() -> Any:
    """The Langfuse export filter is nested in ``_init_langfuse_client``; pull
    its source with ``ast`` (the spec's AC5 instrument) and exec it."""
    # index-gap: BS-003 (nested function; the index lists it, cannot call it)
    src = open(telemetry.__file__, encoding="utf-8").read()
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_should_export_span"
    )
    from langfuse.span_filter import is_default_export_span

    ns: dict[str, Any] = {
        "_LANGFUSE_PROBE_DENYLIST": telemetry._LANGFUSE_PROBE_DENYLIST,
        "_LANGFUSE_PROBE_OPERATION_PREFIX": telemetry._LANGFUSE_PROBE_OPERATION_PREFIX,
        "is_default_export_span": is_default_export_span,
        "Any": object,
    }
    exec(textwrap.dedent(ast.get_source_segment(src, fn) or ""), ns)  # noqa: S102
    return ns["_should_export_span"]


def _log_capture() -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    log = logging.getLogger("audittrace.test.traceparent_trust")
    log.setLevel(logging.INFO)
    log.propagate = False
    for h in list(log.handlers):
        log.removeHandler(h)
    handler = logging.StreamHandler(buf)
    handler.setFormatter(StructuredFormatter())
    log.addHandler(handler)
    return log, buf


class _Harness:
    """A fresh ``create_app()`` re-instrumented with a local provider."""

    def __init__(
        self,
        provider: Any,
        exporter: InMemorySpanExporter | None,
        loopback: _Loopback | None = None,
    ) -> None:
        self.exporter = exporter
        self.loopback = loopback
        self.seen: dict[str, Any] = {}
        self.amqp = _StubExchange()
        self.log, self.log_buf = _log_capture()
        app = create_app()
        FastAPIInstrumentor.uninstrument_app(app)
        # uninstrument_app() caches a BUILT middleware stack; reset it so the
        # first request builds the stack around the re-instrumented wrapper.
        app.middleware_stack = None
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=provider,
            server_request_hook=telemetry.inbound_trace_context_hook,
        )
        self.app = app
        self._routes()
        self.client = TestClient(app, raise_server_exceptions=False)

    async def _probe(self) -> None:
        span = trace.get_current_span()
        row = _build_row(
            user=sentinel_user_context(),
            op="read",
            layer="episodic",
            collection=None,
            key="k",
            tier="private",
            outcome="success",
            trace_id=None,
            detail_extra=None,
        )
        self.log.info("measure.line")
        line = self.log_buf.getvalue().strip().splitlines()[-1]
        self.log_buf.seek(0)
        self.log_buf.truncate(0)
        self.seen.update(
            {
                "chat": chat_tid(),
                "memory_audit": audit_tid(),
                "console": con_tid(),
                "signature": sig_tid(),
                "summariser": summ_tid(span),
                "row": row.trace_id,
                "upload": list(upload_tp()),
                "log_line": line,
                "baggage": dict(baggage.get_all()),
            }
        )
        if self.loopback is not None:
            await self._outbound()

    async def _outbound(self) -> None:
        assert self.loopback is not None
        lb = self.loopback
        lb.requests.clear()
        async with httpx.AsyncClient() as hc:  # (i) global instrument() path
            await hc.get(f"{lb.url}/httpx")
        httpx_hdrs = lb.requests[-1]
        lb.requests.clear()
        with urllib3.PoolManager() as pm:  # (ii) urllib3 global instrument()
            pm.request("GET", f"{lb.url}/urllib3")
        urllib3_hdrs = lb.requests[-1]
        lb.requests.clear()
        # (iv) the REAL broker client singleton, created after instrument()
        with contextlib.ExitStack() as stack:
            mp = pytest.MonkeyPatch()
            stack.callback(mp.undo)
            mp.setattr(mcp_broker, "_downstream_client", None)
            await mcp_broker.list_downstream_tools(
                sentinel_user_context(),
                registry={
                    "x": mcp_broker.DownstreamServer("x", f"{lb.url}/mcp", "x:scope")
                },
            )
            client = mcp_broker._downstream_client
            if client is not None:
                await client.aclose()
        broker_hdrs = lb.requests[-1]
        # (iii) AMQP: the real publisher builds the message; the envelope is
        # built from the same helper the upload route uses.
        trace_id_hex, traceparent = upload_tp()
        envelope = ScanRequestEnvelope(
            scan_id="s1",
            user_id="u1",
            trace_id=trace_id_hex,
            object_uri="s3://b/k",
            object_sha256="0" * 64,
            size_bytes=1,
            claimed_content_type="application/pdf",
            traceparent=traceparent,
        )
        amqp = ScanAmqpClient(config_mod.get_settings())
        amqp._exchange = self.amqp
        payload = envelope.as_amqp_payload()
        await amqp.publish_scan_request(payload)
        self.seen["outbound"] = {
            "httpx": httpx_hdrs,
            "urllib3": urllib3_hdrs,
            "broker": broker_hdrs,
            "amqp_headers": dict(self.amqp.messages[-1].headers),
            "amqp_payload": payload,
        }

    def _routes(self) -> None:
        app = self.app

        @app.get("/__m/ok")
        async def _ok() -> dict[str, bool]:
            await self._probe()
            return {"ok": True}

        @app.get("/__m/403")
        async def _forbidden() -> None:
            await self._probe()
            raise HTTPException(status_code=403, detail="Required scope: x")

    def run(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.seen.clear()
        if self.exporter is not None:
            self.exporter.clear()
        body = b'{"model":"x","messages":[]}' if method == "POST" else None
        hdrs = dict(headers or {})
        if body:
            hdrs.setdefault("content-type", "application/json")
        resp = self.client.request(method, path, headers=hdrs, content=body)
        spans = list(self.exporter.get_finished_spans()) if self.exporter else []
        servers = [s for s in spans if str(s.kind).endswith("SERVER")]
        return {
            "status": resp.status_code,
            "seen": dict(self.seen),
            "spans": spans,
            "server": servers[0] if servers else None,
        }


def _sdk_provider(
    *extra_processors: Any,
) -> tuple[TracerProvider, InMemorySpanExporter]:
    provider = TracerProvider()
    for proc in extra_processors:
        provider.add_span_processor(proc)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _hex(n: int) -> str:
    return format(n, "032x")


# ---------------------------------------------------------------------------
# the matrix: every case is run once per module on the real create_app()
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def matrix(sandbox: None) -> Iterator[dict[str, dict[str, Any]]]:
    results: dict[str, dict[str, Any]] = {}
    provider, exporter = _sdk_provider()
    with _Loopback() as loopback, contextlib.ExitStack() as stack:
        for inst in (HTTPXClientInstrumentor(), URLLib3Instrumentor()):
            if inst.is_instrumented_by_opentelemetry:
                inst.uninstrument()
            inst.instrument(tracer_provider=provider)
            stack.callback(inst.uninstrument)
        h = _Harness(provider, exporter, loopback)
        results["_install"] = {
            "textmap": propagate.get_global_textmap(),
            "fields": sorted(propagate.get_global_textmap().fields),
        }
        cases: list[tuple[str, str, str, dict[str, str]]] = [
            ("control", "GET", "/__m/ok", {}),
            ("tp01", "GET", "/__m/ok", {"traceparent": _tp(), **INBOUND}),
            ("tp00", "GET", "/__m/ok", {"traceparent": _tp("00"), **INBOUND}),
            ("401_chat", "POST", "/v1/chat/completions", {"traceparent": _tp()}),
            ("401_models", "GET", "/v1/models", {"traceparent": _tp()}),
            ("403", "GET", "/__m/403", {"traceparent": _tp()}),
            ("malformed", "GET", "/__m/ok", {"traceparent": "garbage"}),
            ("zero_trace", "GET", "/__m/ok", {"traceparent": f"00-{'0' * 32}-{P1}-01"}),
            ("ts_bg_only", "GET", "/__m/ok", dict(INBOUND)),
            (
                "future_version",
                "GET",
                "/__m/ok",
                {"traceparent": f"01-{T}-{P1}-01-future-extension"},
            ),
            ("collide_a", "GET", "/__m/ok", {"traceparent": _tp()}),
            ("collide_b", "GET", "/__m/ok", {"traceparent": _tp(parent=P2)}),
        ]
        for name, method, path, headers in cases:
            results[name] = h.run(method, path, headers)
        results["_amqp_exchange"] = {"n": len(h.amqp.messages)}
        yield results


def _root(res: dict[str, Any]) -> Any:
    server = res["server"]
    assert server is not None, "no SERVER span was exported"
    return server


# ------------------------------------------------------------------- AC1 ----
def test_ac1_identity_pin_global_textmap_is_the_policy(
    matrix: dict[str, dict[str, Any]],
) -> None:
    """AC1a. After ``create_app()`` the global textmap IS the policy class.

    Neuters: N1/N1b/N4 -> RED (a different textmap is installed or none is).
    """
    assert isinstance(matrix["_install"]["textmap"], telemetry.ServerMintedTraceContext)
    # W3C fields only; no baggage propagator in the composite
    assert matrix["_install"]["fields"] == ["traceparent", "tracestate"]


def test_ac1_root_is_not_inbound_and_is_sampled(
    matrix: dict[str, dict[str, Any]],
) -> None:
    """AC1b. Server span is a local root, sampled, ``trace_id != T``.

    Neuters: N1 -> RED (parent adopted).
    """
    server = _root(matrix["tp01"])
    assert server.parent is None
    assert _hex(server.context.trace_id) != T
    assert server.context.trace_flags & 1
    assert not server.context.trace_state  # AC7: nothing adopted from tracestate


# ------------------------------------------------------------------- AC2 ----
def test_ac2_link_to_inbound_with_validated_attributes(
    matrix: dict[str, dict[str, Any]],
) -> None:
    """AC2. Exactly one link to T, ``audittrace.link`` attr, re-serialised form.

    Neuters: N2 (no hook) -> RED; N1 -> RED (parent adopted, nothing to link);
    N5 (raw header bytes recorded) -> the ``future_version`` equality RED.
    """
    server = _root(matrix["tp01"])
    assert len(server.links) == 1
    link = server.links[0]
    assert _hex(link.context.trace_id) == T
    assert format(link.context.span_id, "016x") == P1
    assert dict(link.attributes) == {"audittrace.link": "inbound_traceparent"}
    attrs = server.attributes
    assert attrs["audittrace.inbound.traceparent"] == _tp()
    assert len(attrs["audittrace.inbound.traceparent"]) == 55
    assert attrs["audittrace.inbound.trace_id"] == T
    assert "vendor=evil" not in json.dumps(dict(attrs), default=str)  # no tracestate


def test_ac2_attribute_is_the_parsed_form_not_the_raw_header(
    matrix: dict[str, dict[str, Any]],
) -> None:
    """A future-version header with a suffix is recorded as the parsed 00- form."""
    server = _root(matrix["future_version"])
    assert len(server.links) == 1
    assert server.attributes["audittrace.inbound.traceparent"] == _tp()
    assert "future-extension" not in json.dumps(dict(server.attributes), default=str)


# ------------------------------------------------------------------- AC3 ----
_DERIVATIONS = ("chat", "memory_audit", "console", "signature", "summariser", "row")


def _derivation_mismatches(res: dict[str, Any]) -> list[str]:
    root = _hex(_root(res).context.trace_id)
    seen = res["seen"]
    bad = [k for k in _DERIVATIONS if seen[k] != root]
    if seen["upload"][0] != root:
        bad.append("upload")
    return bad


def test_ac3_all_derivations_equal_the_minted_root(
    matrix: dict[str, dict[str, Any]],
) -> None:
    """AC3 (consistency pin). 7 derivations equal the server span's id.

    Holds under every neuter by construction; its identity half is AC1.
    ``test_ac3_pin_can_fail`` proves the pin is load-bearing (gate C5).
    Neuter N1 additionally makes them equal T (see AC1b).
    """
    assert _derivation_mismatches(matrix["tp01"]) == []
    assert matrix["tp01"]["seen"]["chat"] != T


def test_ac3_pin_can_fail(matrix: dict[str, dict[str, Any]]) -> None:
    """Gate C5 neuter for AC3: one divergent derivation turns the pin RED."""
    res = matrix["tp01"]
    assert _derivation_mismatches(res) == []
    diverged = {**res, "seen": {**res["seen"], "console": "f" * 32}}
    assert _derivation_mismatches(diverged) == ["console"]


# ------------------------------------------------------------------- AC4 ----
def test_ac4_inbound_unsampled_flag_cannot_switch_the_request_off(
    matrix: dict[str, dict[str, Any]],
) -> None:
    """AC4. Inbound flags ``00``: spans exported, root sampled and != T, row == root.

    Neuters: N1 -> RED (0 spans exported, root == T).
    """
    res = matrix["tp00"]
    server = _root(res)
    assert len(res["spans"]) > 0
    assert server.context.trace_flags & 1
    assert _hex(server.context.trace_id) != T
    assert res["seen"]["row"] == _hex(server.context.trace_id)


# ------------------------------------------------------------------- AC5 ----
@pytest.mark.parametrize("case", ["401_chat", "401_models", "403"])
def test_ac5_rejected_requests_are_not_joined_to_the_callers_trace(
    matrix: dict[str, dict[str, Any]], case: str
) -> None:
    """AC5. 401/403 get a minted root too; the 403 span still links to T.

    Neuters: N1 -> RED (joined); N2 -> the 403 link assertion RED.
    """
    res = matrix[case]
    assert res["status"] in (401, 403)
    server = _root(res)
    assert server.parent is None
    assert _hex(server.context.trace_id) != T
    assert {_hex(s.context.trace_id) for s in res["spans"]} == {
        _hex(server.context.trace_id)
    }
    if case == "403":
        assert [_hex(link.context.trace_id) for link in server.links] == [T]


def test_ac5_langfuse_filter_keeps_the_chat_root_under_a_server_id(
    matrix: dict[str, dict[str, Any]],
) -> None:
    """AC5. The export's own filter keeps the 401 chat root; its id is not T."""
    should_export = _load_should_export()
    res = matrix["401_chat"]
    kept = [s for s in res["spans"] if should_export(s)]
    assert [str(s.kind).endswith("SERVER") for s in kept] == [True]
    assert _hex(kept[0].context.trace_id) != T


# ------------------------------------------------------------------- AC6 ----
def _assert_outbound_hygiene(hdrs: dict[str, str], root: str) -> None:
    assert "tracestate" not in hdrs
    assert "baggage" not in hdrs
    assert hdrs["traceparent"].split("-")[1] == root
    assert hdrs["traceparent"].split("-")[1] != T


@pytest.mark.parametrize("family", ["httpx", "urllib3", "broker"])
def test_ac6_outbound_http_families_carry_minted_traceparent_only(
    matrix: dict[str, dict[str, Any]], family: str
) -> None:
    """AC6 (C3 i, ii, iv). Real loopback upstream, global instrumentors, inbound
    traceparent + tracestate + baggage: no tracestate, no baggage, minted id.

    Neuters: N1 -> tracestate/baggage forwarded, id == T (RED); N1b -> the
    baggage assertion RED.
    """
    res = matrix["tp01"]
    _assert_outbound_hygiene(
        res["seen"]["outbound"][family], _hex(_root(res).context.trace_id)
    )


def test_ac6_amqp_header_key_set_and_payload(
    matrix: dict[str, dict[str, Any]],
) -> None:
    """AC6 (C3 iii). Message ``headers`` keys == {traceparent}; payload has no
    tracestate/baggage key; the traceparent carries the minted root id."""
    res = matrix["tp01"]
    out = res["seen"]["outbound"]
    root = _hex(_root(res).context.trace_id)
    assert set(out["amqp_headers"]) == {"traceparent"}
    assert out["amqp_headers"]["traceparent"].split("-")[1] == root
    assert "tracestate" not in out["amqp_payload"]
    assert "baggage" not in out["amqp_payload"]
    assert out["amqp_payload"]["trace_id"] == root


def test_ac6_upload_snapshot_has_sampled_flags_even_for_inbound_00(
    matrix: dict[str, dict[str, Any]],
) -> None:
    """AC6. ``_current_traceparent()`` under inbound ``00`` -> minted root, flags
    with bit 0 set (SDK emits ``03``). Neuters: N1 -> id == T, flags 00 (RED)."""
    res = matrix["tp00"]
    trace_id_hex, traceparent = res["seen"]["upload"]
    assert trace_id_hex == _hex(_root(res).context.trace_id) != T
    assert int(traceparent.rsplit("-", 1)[1], 16) & 1


def test_ac6_pin_can_fail(matrix: dict[str, dict[str, Any]]) -> None:
    """Gate C5 neuter for AC6 (traceparent == minted): a rewritten inject, or an
    adopted id, turns the helper RED."""
    res = matrix["tp01"]
    root = _hex(_root(res).context.trace_id)
    good = res["seen"]["outbound"]["httpx"]
    _assert_outbound_hygiene(good, root)
    for bad in (
        {**good, "traceparent": f"00-{T}-{P1}-01"},
        {**good, "tracestate": TS},
        {**good, "baggage": BG},
    ):
        with pytest.raises(AssertionError):
            _assert_outbound_hygiene(bad, root)


# ------------------------------------------------------------------- AC8/9 --
def test_ac8_controls(matrix: dict[str, dict[str, Any]]) -> None:
    """AC8. No header -> sampled root, exported; malformed / all-zero /
    tracestate+baggage without traceparent -> root with 0 links.

    Neuter N5b (hook links an invalid context) -> RED.
    """
    for name in ("control", "malformed", "zero_trace", "ts_bg_only"):
        server = _root(matrix[name])
        assert server.parent is None, name
        assert server.context.trace_flags & 1, name
        assert len(server.links) == 0, name
        assert "audittrace.inbound.traceparent" not in server.attributes, name
        assert matrix[name]["seen"]["baggage"] == {}, name


def test_ac9_two_requests_with_the_same_inbound_id_get_distinct_server_ids(
    matrix: dict[str, dict[str, Any]],
) -> None:
    """AC9. Neuters: N1 -> equal (RED)."""
    a, b = (_root(matrix[n]) for n in ("collide_a", "collide_b"))
    assert a.context.trace_id != b.context.trace_id


# ------------------------------------------------------------------- AC7 ----
def _log_line(
    harness: _Harness, headers: dict[str, str], inject_trace_context: bool
) -> tuple[dict[str, Any], str]:
    inst = LoggingInstrumentor()
    was = inst.is_instrumented_by_opentelemetry
    if was:
        inst.uninstrument()
    inst.instrument(set_logging_format=False, inject_trace_context=inject_trace_context)
    try:
        res = harness.run("GET", "/__m/ok", headers)
    finally:
        inst.uninstrument()
        if was:
            inst.instrument(set_logging_format=False)  # product configuration
    return json.loads(res["seen"]["log_line"]), _hex(_root(res).context.trace_id)


def test_ac7a_product_configuration_logs_none_of_the_callers_values(
    sandbox: None,
) -> None:
    """AC7a (gate C6). Product config (``set_logging_format=False`` only): the
    structured line contains neither the inbound trace id nor any tracestate /
    baggage value. Today the line has no ``trace_id`` at all (Loki: 0/500).

    Neuters: N1 alone stays GREEN here (no trace id is stamped on the record);
    it reddens only together with the log-trace-id fix, which is why AC7b
    exists. Recorded honestly in the build record.
    """
    provider, exporter = _sdk_provider()
    h = _Harness(provider, exporter)
    line, _ = _log_line(
        h, {"traceparent": _tp(), **INBOUND}, inject_trace_context=False
    )
    raw = json.dumps(line)
    for forbidden in (T, "evil", "injected", "k=v"):
        assert forbidden not in raw


def test_ac7b_with_trace_injection_the_line_carries_the_minted_root(
    sandbox: None,
) -> None:
    """AC7b (forward protection for the log-trace-id unit). With
    ``inject_trace_context=True`` the line's ``trace_id`` is the minted root.

    Neuters: N1 -> the line carries T (RED).
    """
    provider, exporter = _sdk_provider()
    h = _Harness(provider, exporter)
    line, root = _log_line(
        h, {"traceparent": _tp(), **INBOUND}, inject_trace_context=True
    )
    assert line["trace_id"] == root != T
    raw = json.dumps(line)
    for forbidden in ("evil", "injected", "k=v"):
        assert forbidden not in raw


# ------------------------------------------------------------------- AC-BG --
def test_ac_bg_forged_langfuse_baggage_never_reaches_a_span(sandbox: None) -> None:
    """AC-BG (gate C1). With a REAL ``LangfuseSpanProcessor`` installed, inbound
    ``baggage: langfuse_user_id=V,langfuse_session_id=W`` on a 401 models
    request, a 401 chat request and a 200 route yields 0 spans carrying
    ``user.id==V`` or ``session.id==W``, and ``baggage.get_all()`` is ``{}``.

    Neuters: N1 -> RED; N1b (policy + W3CBaggagePropagator) -> RED.
    """
    from langfuse._client.span_processor import LangfuseSpanProcessor

    lf = LangfuseSpanProcessor(
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:9",
        span_exporter=InMemorySpanExporter(),  # never touches the network
    )
    provider, exporter = _sdk_provider(lf)
    try:
        h = _Harness(provider, exporter)
        hdrs = {"traceparent": _tp(), "baggage": LF_BAGGAGE}
        runs = [
            h.run("GET", "/v1/models", hdrs),
            h.run("POST", "/v1/chat/completions", hdrs),
            h.run("GET", "/__m/ok", hdrs),
        ]
    finally:
        provider.shutdown()
    assert [r["status"] for r in runs] == [401, 401, 200]
    total = 0
    for run in runs:
        for span in run["spans"]:
            total += 1
            values = {str(v) for v in dict(span.attributes).values()}
            assert V_USER not in values and W_SESSION not in values
            assert span.attributes.get("user.id") != V_USER
            assert span.attributes.get("session.id") != W_SESSION
    assert total >= 3  # the positive control: spans were actually inspected
    assert runs[2]["seen"]["baggage"] == {}


# ------------------------------------------------------------------- AC-D9 --
def test_ac_d9_tracing_disabled_inbound_header_yields_no_context(
    sandbox: None,
) -> None:
    """AC-D9. NoOp provider + inbound header: helper None, row None, upload
    snapshot ("", ""), status 200. Neuters: N1 -> the inbound id (RED)."""
    h = _Harness(NoOpTracerProvider(), None)
    res = h.run("GET", "/__m/ok", {"traceparent": _tp(), **INBOUND})
    assert res["status"] == 200
    assert res["seen"]["chat"] is None
    assert res["seen"]["row"] is None
    assert res["seen"]["upload"] == ["", ""]


# ------------------------------------------------------------ wiring / unit --
def test_create_app_wires_the_hook_and_the_policy(sandbox: None) -> None:
    """The REAL ``create_app()`` instrumentation (no re-instrumentation): a
    forged traceparent on the global provider is linked, not adopted.

    Neuters: N6 (``instrument_app`` without ``server_request_hook=``) -> RED;
    N4 (install call removed) -> RED.
    """
    propagate.set_global_textmap(_default_composite())
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    add = getattr(provider, "add_span_processor", None)
    assert add is not None, "conftest installs an SDK provider for the session"
    add(SimpleSpanProcessor(exporter))
    try:
        app = create_app()
        res = TestClient(app, raise_server_exceptions=False).get(
            "/v1/models", headers={"traceparent": _tp(), **INBOUND}
        )
    finally:
        exporter.shutdown()
    assert res.status_code == 401
    servers = [
        s for s in exporter.get_finished_spans() if str(s.kind).endswith("SERVER")
    ]
    assert len(servers) == 1
    assert _hex(servers[0].context.trace_id) != T
    assert [_hex(link.context.trace_id) for link in servers[0].links] == [T]


def test_install_failure_aborts_create_app(
    sandbox: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate C2/C5. ``install_trace_context_policy()`` runs OUTSIDE the swallowing
    ``try`` in ``create_app()``: an exception propagates (fails closed).

    Neuter N3 (call moved inside the ``try``) -> RED: the error is swallowed."""

    def _boom() -> None:
        raise RuntimeError("policy install failed")

    monkeypatch.setattr(telemetry, "install_trace_context_policy", _boom)
    with pytest.raises(RuntimeError, match="policy install failed"):
        create_app()


def test_install_is_unconditional_not_gated_on_tracing_enabled(
    sandbox: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate C2 / spec L7. With tracing disabled in settings the policy is still
    installed (the caller must not supply the only valid context)."""
    monkeypatch.setenv("AUDITTRACE_TRACING_ENABLED", "false")
    config_mod.get_settings.cache_clear()
    propagate.set_global_textmap(_default_composite())
    try:
        create_app()
    finally:
        config_mod.get_settings.cache_clear()
    assert isinstance(
        propagate.get_global_textmap(), telemetry.ServerMintedTraceContext
    )


def test_install_is_idempotent(sandbox: None) -> None:
    telemetry.install_trace_context_policy()
    first = propagate.get_global_textmap()
    telemetry.install_trace_context_policy()
    assert propagate.get_global_textmap() is first


def test_policy_survives_init_telemetry_laptop_mode(sandbox: None) -> None:
    """Laptop mode (``init_telemetry`` + Langfuse path) is process-global and
    set-once, so it is reduced to: the policy installed beforehand is still the
    global textmap afterwards (spec R10/F3: neither init path swaps it)."""
    telemetry.install_trace_context_policy()
    before = propagate.get_global_textmap()
    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="audittrace-server-tests",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=False,
    )
    assert propagate.get_global_textmap() is before


def test_propagator_unit_extract_inject_fields() -> None:
    """Unit pins on the propagator: extract never sets a span or baggage and
    stores only a VALID context; inject writes traceparent only."""
    p = telemetry.ServerMintedTraceContext()
    ctx = p.extract({"traceparent": _tp(), "baggage": BG, "tracestate": TS})
    assert not trace.get_current_span(ctx).get_span_context().is_valid
    assert baggage.get_all(ctx) == {}
    assert ctx.get(telemetry._INBOUND_SPAN_CONTEXT_KEY).trace_id == int(T, 16)
    base = Context()
    assert (
        p.extract({"traceparent": "garbage"}, base).get(
            telemetry._INBOUND_SPAN_CONTEXT_KEY
        )
        is None
    )
    carrier: dict[str, str] = {}
    p.inject(carrier)
    assert carrier == {}  # no current span -> nothing written, never baggage
    assert "baggage" not in p.fields
    assert "traceparent" in p.fields


def test_hook_is_inert_for_non_recording_spans_and_missing_context() -> None:
    """Unit pins on the hook (N5): nothing stored -> nothing recorded; a
    non-recording span is left untouched."""

    class _Span:
        def __init__(self, recording: bool) -> None:
            self.recording = recording
            self.links: list[Any] = []
            self.attrs: dict[str, Any] = {}

        def is_recording(self) -> bool:
            return self.recording

        def add_link(self, ctx: Any, attributes: Any = None) -> None:
            self.links.append((ctx, attributes))

        def set_attribute(self, key: str, value: Any) -> None:
            self.attrs[key] = value

    span = _Span(True)
    telemetry.inbound_trace_context_hook(span, {})  # no stashed context
    assert span.links == [] and span.attrs == {}

    ctx = telemetry.ServerMintedTraceContext().extract({"traceparent": _tp()})
    from opentelemetry import context as otel_context

    token = otel_context.attach(ctx)
    try:
        quiet = _Span(False)
        telemetry.inbound_trace_context_hook(quiet, {})
        assert quiet.links == [] and quiet.attrs == {}
        loud = _Span(True)
        telemetry.inbound_trace_context_hook(loud, {})
    finally:
        otel_context.detach(token)
    assert len(loud.links) == 1
    assert loud.attrs["audittrace.inbound.traceparent"] == _tp()
