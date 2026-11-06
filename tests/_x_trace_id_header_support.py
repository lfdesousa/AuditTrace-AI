"""Shared support for the X-TRACE-ID-HEADER tests (``test_x_trace_id_header.py``
unit, ``test_x_trace_id_header_route.py`` integration). Not a test module:
nothing here is collected.

Mechanism (gate 5438c34e C3): the OTel global ``TracerProvider`` is set-once, so
every in-process case builds a FRESH ``create_app()`` and re-instruments it with
a LOCAL provider (``instrument_app(..., tracer_provider=)``): an SDK provider +
``InMemorySpanExporter`` ("sdk" mode) or a ``NoOpTracerProvider`` ("off" mode =
tracing disabled). The global provider is not touched and the coverage of
``server.py`` / ``trace_id_header.py`` is counted.

Synthetic routes are added to the REAL app after construction, so the whole real
middleware stack (CORS, ``TraceIdHeaderMiddleware``, the instrumentor, the real
auth dependencies for the real routes, the 500 handler) is what answers.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

import pytest
from fastapi import BackgroundTasks, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import NoOpTracerProvider

from audittrace import config as config_mod
from audittrace import logging_config, telemetry
from audittrace.server import create_app

Mode = Literal["sdk", "off"]

PRESET_VALUE = "f" * 32
INBOUND_TRACE = "0af7651916cd43dd8448eb211c80319c"
CORS_ORIGIN = "http://localhost:3000"  # in the default AUDITTRACE_CORS_ORIGINS
SENTINEL_BODY = b'{"ok":true}'


@dataclass(frozen=True)
class Case:
    """One request class. ``seen`` names the key under which the synthetic route
    records ``current_trace_id_hex()`` (None for real routes)."""

    name: str
    method: str
    path: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    seen: str | None = None
    body_embeds_id: bool = False  # body carries the per-request id by construction


# The 14 classes of the spec's section 3 plus the 12 extended classes of the
# gate verdict (HEAD, OPTIONS x2, 307, docs, redoc, mid-stream raise,
# BackgroundTasks, HTTPException(500), malformed / zero traceparent, preset).
CASES: tuple[Case, ...] = (
    Case("route_200_json", "GET", "/__x/json", 200, seen="json"),
    Case("health_outside_v1", "GET", "/health", 200),
    Case("metrics_outside_v1", "GET", "/metrics", 401),
    Case("v1_models_401_no_token", "GET", "/v1/models", 401),
    Case("v1_chat_401_no_token", "POST", "/v1/chat/completions", 401),
    Case("route_403_scope", "GET", "/__x/403", 403, seen="403"),
    Case("unknown_404", "GET", "/__x/no-such-route", 404),
    Case("method_405", "POST", "/health", 405),
    Case("validation_422", "GET", "/__x/validate?n=notint", 422),
    Case(
        "unhandled_500_envelope",
        "GET",
        "/__x/boom",
        500,
        seen="boom",
        body_embeds_id=True,
    ),
    Case("sse_stream_200", "GET", "/__x/sse", 200, seen="sse", body_embeds_id=True),
    Case(
        "cors_preflight_options",
        "OPTIONS",
        "/v1/chat/completions",
        200,
        headers={
            "Origin": CORS_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    ),
    Case("openapi_json", "GET", "/openapi.json", 200),
    Case(
        "inbound_traceparent",
        "GET",
        "/__x/json",
        200,
        headers={"traceparent": f"00-{INBOUND_TRACE}-00f067aa0ba902b7-01"},
        seen="json",
    ),
    Case("head_405", "HEAD", "/health", 405),
    Case("options_no_origin_405", "OPTIONS", "/health", 405),
    Case(
        "options_bad_origin_400",
        "OPTIONS",
        "/health",
        400,
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    ),
    Case("redirect_307", "GET", "/__x/redir", 307),
    Case("docs_200", "GET", "/docs", 200),
    Case("redoc_200", "GET", "/redoc", 200),
    Case("midstream_raise_200", "GET", "/__x/midstream", 200, seen="midstream"),
    Case("background_tasks_200", "GET", "/__x/bg", 200, seen="bg"),
    Case("http_exception_500", "GET", "/__x/http500", 500, seen="http500"),
    Case(
        "malformed_traceparent",
        "GET",
        "/__x/json",
        200,
        headers={"traceparent": "00-not-a-trace-id-00"},
        seen="json",
    ),
    Case(
        "zero_traceparent",
        "GET",
        "/__x/json",
        200,
        headers={"traceparent": f"00-{'0' * 32}-{'0' * 16}-01"},
        seen="json",
    ),
    Case("preset_header_replaced", "GET", "/__x/preset", 200, seen="preset"),
)
CASES_BY_NAME = {c.name: c for c in CASES}


@contextlib.contextmanager
def isolated_state() -> Iterator[None]:
    """Auth ON (so the real routes answer 401 without a token), settings cache
    cleared on both edges, and the root logging / telemetry state restored:
    ``create_app()`` reconfigures logging (see ``_log_trace_id_support``)."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_stdout = logging_config._STDOUT_HANDLER
    mp = pytest.MonkeyPatch()
    mp.setenv("AUDITTRACE_AUTH_ENABLED", "true")
    mp.setenv("AUDITTRACE_AUTH_REQUIRED", "true")
    config_mod.get_settings.cache_clear()
    try:
        yield
    finally:
        mp.undo()
        config_mod.get_settings.cache_clear()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging_config._STDOUT_HANDLER = saved_stdout
        telemetry._reset_for_tests()
        telemetry.init_telemetry(
            service_name="audittrace-server-tests",
            otlp_endpoint="",
            tracing_enabled=True,
            metrics_enabled=True,
        )


class Harness:
    """A fresh ``create_app()`` re-instrumented with a local provider, plus the
    synthetic routes of :data:`CASES`.

    ``without_middleware=True`` removes ``TraceIdHeaderMiddleware`` from the user
    middleware list before the stack is built: the body-identity baseline.
    """

    def __init__(self, mode: Mode, *, without_middleware: bool = False) -> None:
        self.mode = mode
        self.exporter: InMemorySpanExporter | None = None
        provider: Any
        if mode == "sdk":
            provider = TracerProvider()
            self.exporter = InMemorySpanExporter()
            provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        else:
            provider = NoOpTracerProvider()
        self.seen: dict[str, str | None] = {}
        app = create_app()
        if without_middleware:
            from audittrace.trace_id_header import TraceIdHeaderMiddleware

            app.user_middleware = [
                m for m in app.user_middleware if m.cls is not TraceIdHeaderMiddleware
            ]
        FastAPIInstrumentor.uninstrument_app(app)
        app.middleware_stack = None  # rebuilt around the re-instrumented wrapper
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=provider,
            server_request_hook=telemetry.inbound_trace_context_hook,
        )
        self.app = app
        self._routes()

    def _routes(self) -> None:
        app, seen = self.app, self.seen

        @app.get("/__x/json")
        async def _json() -> dict[str, bool]:
            seen["json"] = telemetry.current_trace_id_hex()
            return {"ok": True}

        @app.get("/__x/403")
        async def _forbidden() -> None:
            seen["403"] = telemetry.current_trace_id_hex()
            raise HTTPException(status_code=403, detail="scope")

        @app.get("/__x/validate")
        async def _validate(n: int = Query(...)) -> dict[str, int]:
            return {"n": n}

        @app.get("/__x/boom")
        async def _boom() -> None:
            seen["boom"] = telemetry.current_trace_id_hex()
            raise RuntimeError("synthetic unhandled")

        @app.get("/__x/http500")
        async def _http500() -> None:
            seen["http500"] = telemetry.current_trace_id_hex()
            raise HTTPException(status_code=500, detail="explicit")

        @app.get("/__x/sse")
        async def _sse() -> StreamingResponse:
            async def frames() -> AsyncIterator[bytes]:
                seen["sse"] = telemetry.current_trace_id_hex()
                yield f"data: {seen['sse']}\n\n".encode()
                yield b"data: [DONE]\n\n"

            return StreamingResponse(
                frames(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        @app.get("/__x/midstream")
        async def _midstream() -> StreamingResponse:
            async def frames() -> AsyncIterator[bytes]:
                seen["midstream"] = telemetry.current_trace_id_hex()
                yield b"data: first\n\n"
                raise RuntimeError("raised after the first frame")

            return StreamingResponse(frames(), media_type="text/event-stream")

        @app.get("/__x/bg")
        async def _bg(tasks: BackgroundTasks) -> dict[str, bool]:
            def task() -> None:
                seen["bg"] = telemetry.current_trace_id_hex()

            tasks.add_task(task)
            return {"ok": True}

        @app.get("/__x/preset")
        async def _preset() -> JSONResponse:
            seen["preset"] = telemetry.current_trace_id_hex()
            return JSONResponse({"ok": True}, headers={"X-Trace-Id": PRESET_VALUE})

        # Starlette's own trailing-slash redirect: GET /__x/redir -> 307 /__x/redir/
        @app.get("/__x/redir/")
        async def _redir_slash() -> JSONResponse:
            return JSONResponse({"ok": True})

    def request(self, case: Case) -> Any:
        """Send ``case`` through the real stack (no redirect following)."""
        with contextlib.closing(
            TestClient(self.app, raise_server_exceptions=False)
        ) as client:
            return client.request(
                case.method,
                case.path,
                headers=case.headers,
                follow_redirects=False,
            )

    def server_span_trace_ids(self) -> list[str]:
        assert self.exporter is not None
        return [
            format(s.context.trace_id, "032x")
            for s in self.exporter.get_finished_spans()
            if str(s.kind).endswith("SERVER")
        ]
