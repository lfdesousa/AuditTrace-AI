"""LOG-TRACE-ID WU-1 (v1.31.0 WP-3), INTEGRATION layer: the real app through
``create_app()`` + ``TestClient`` against an in-memory exporter (AC6, AC3b) and a
subprocess for the process-wide handler set (AC16). The unit layer is
``test_log_trace_id.py``; the two layers are not mixed. Shared bootstrap:
``tests/_log_trace_id_support.py``.

Mocks declared: ``InMemorySpanExporter`` (OTel SDK test double); the probe route
is added to the real app, nothing in the app is patched.

Neuters (each alone, RED recorded in the build record): N1 / N2 / N3 -> AC6 RED;
N4 / N4b (re-add the contrib instrumentor block) -> AC16 RED; binding
``session_id`` in the request path -> the AC3b neuter test.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import sys
import textwrap
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from audittrace import telemetry
from audittrace.routes.chat import _current_trace_id_hex
from audittrace.server import create_app
from audittrace.services.console_store._context import bind_session_id
from tests._log_trace_id_support import (
    PROBE_LOG,
    SESSION,
    bootstrap,
    isolated_logging_state,
    log_line,
)

TRACE_SHAPE = re.compile(r"^[0-9a-f]{32}$")
SPAN_SHAPE = re.compile(r"^[0-9a-f]{16}$")


class _RealRoute:
    """``create_app()`` re-instrumented with a local in-memory provider and one
    probe route that logs INFO from inside the request."""

    def __init__(self, bind_session: bool = False) -> None:
        self.provider = TracerProvider()
        self.exporter = InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.route_trace_id: str | None = None
        app = create_app()
        FastAPIInstrumentor.uninstrument_app(app)
        app.middleware_stack = None  # rebuilt around the re-instrumented wrapper
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=self.provider,
            server_request_hook=telemetry.inbound_trace_context_hook,
        )
        outer = self

        @app.get("/__log/ok")
        async def _ok() -> dict[str, bool]:
            if bind_session:
                bind_session_id(SESSION)
            outer.route_trace_id = _current_trace_id_hex()
            PROBE_LOG.info("route.line")
            return {"ok": True}

        self.app = app

    def get(self, headers: dict[str, str] | None = None) -> int:
        with contextlib.closing(
            TestClient(self.app, raise_server_exceptions=False)
        ) as client:
            return client.get("/__log/ok", headers=headers or {}).status_code

    def server_span_trace_id(self) -> str:
        servers = [
            s
            for s in self.exporter.get_finished_spans()
            if str(s.kind).endswith("SERVER")
        ]
        assert len(servers) == 1, servers
        return format(servers[0].context.trace_id, "032x")


@pytest.fixture
def real_route() -> Iterator[tuple[_RealRoute, io.StringIO]]:
    with isolated_logging_state():
        route = _RealRoute()  # create_app() reconfigures logging itself ...
        yield route, bootstrap()  # ... so the capture bootstrap comes AFTER it


def test_ac6_the_request_line_carries_the_route_span_and_exported_server_trace_id(
    real_route: tuple[_RealRoute, io.StringIO],
) -> None:
    """AC6. Through ``create_app()`` + ``TestClient``: the line's ``trace_id`` ==
    the route's own ``_current_trace_id_hex()`` == the exported SERVER span's id.

    Neuters: N1, N2, N3 -> RED.
    """
    route, buf = real_route
    assert route.get() == 200
    line = log_line(buf, "route.line")
    assert TRACE_SHAPE.match(line["trace_id"])
    assert line["trace_id"] == route.route_trace_id == route.server_span_trace_id()
    assert SPAN_SHAPE.match(line["span_id"])


def test_ac3b_a_production_request_line_carries_no_session_id_today(
    real_route: tuple[_RealRoute, io.StringIO],
) -> None:
    """AC3b (ADDENDUM-1 C2). No production route binds ``session_id`` yet
    (SESSION-ID-BIND), so a real request line has NO ``session_id`` key.

    Neuter: bind in the request path (second test) -> the key appears, proving
    this one reads production truth.
    """
    route, buf = real_route
    assert route.get() == 200
    assert "session_id" not in log_line(buf, "route.line")


def test_ac3b_neuter_binding_in_the_request_path_makes_the_key_appear() -> None:
    with isolated_logging_state():
        route = _RealRoute(bind_session=True)
        buf = bootstrap()
        try:
            assert route.get() == 200
        finally:
            bind_session_id(None)
        assert log_line(buf, "route.line")["session_id"] == SESSION


_SUBPROCESS_PROBE = textwrap.dedent(
    """
    import contextlib, io, json, logging
    from audittrace import logging_config, telemetry

    with contextlib.redirect_stdout(io.StringIO()):
        logging_config.setup_logging(level="INFO", structured=True)
    telemetry.init_telemetry(
        service_name="log-trace-id-subprocess",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=True,
    )
    print(json.dumps({
        "handlers": [
            f"{type(h).__module__}.{type(h).__qualname__}"
            for h in logging.getLogger().handlers
        ],
        "factory_is_stdlib": logging.getLogRecordFactory() is logging.LogRecord,
    }))
    """
)


def test_ac16_the_real_bootstrap_leaves_exactly_the_setup_logging_handler() -> None:
    """AC16 (C4). In a SUBPROCESS (so pytest's own capture handlers are not in
    the list) the root handler set after the real bootstrap is
    ``[logging.StreamHandler]`` and the record factory is the stdlib one.

    Neuters: N4 (the historical instrumentor block) and N4b (with injection)
    add a ``LoggingHandler`` and override the factory -> RED.
    """
    proc = subprocess.run(  # noqa: S603 - fixed argv, the test interpreter
        [sys.executable, "-c", _SUBPROCESS_PROBE],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {
        "handlers": ["logging.StreamHandler"],
        "factory_is_stdlib": True,
    }
