"""X-TRACE-ID-HEADER: every memory-server HTTP response carries ``X-Trace-Id``.

The header value is the 32-lowercase-hex W3C trace id of the request's own
server span, read from the ONE derivation ``telemetry.current_trace_id_hex()``
(X-TRACE-ID D1), so the header, the 500 envelope's ``trace_id``, the
``interactions.trace_id`` column (sync persist branch), the memory-audit rows
and the log ``trace_id`` field all come from one function.

Design (spec ``2026-10-17-SPEC-x-trace-id-header.md`` D2-D4, gate 5438c34e):

* A pure ASGI middleware, NOT ``BaseHTTPMiddleware`` (which re-wraps the body
  iterator and would sit between the streaming sites and uvicorn). It holds no
  resource; it wraps ``send`` and touches only ``http.response.start`` messages
  (a lifespan / websocket scope never emits one, so no scope guard is needed),
  headers only, never a body byte (``/v1`` OpenAI schema is inviolate: additive
  header only).
* Registered by ``create_app()`` after the CORS block, so it is OUTER to CORS
  and preflight responses are stamped too. It sits INSIDE the FastAPI
  instrumentor's server span (the instrumentor wraps the whole stack at build
  time), so the current span is the request's server span.
* Pre-existing ``x-trace-id`` header: REPLACED (gate C2). The invariant "the
  header equals this request's server span id" wins over "first writer wins".
* Absent when the context is invalid (tracing disabled): never an all-zero id,
  and a route-set value is STRIPPED (D4: the header only ever carries a value
  the server minted; v1.31.0 WP-6 closes WP-4 NB-4).
* The inbound request's ``X-Trace-Id`` is never read (WP-4 NB-3, pinned by
  ``test_nb3_a_forged_inbound_x_trace_id_is_never_echoed``).
* The one response this middleware cannot see is the unhandled-exception 500:
  Starlette sends it from ``ServerErrorMiddleware``, outside user middleware.
  ``server.unhandled_exception_handler`` stamps its own response from the same
  helper.

Falsifiability: ``tests/test_x_trace_id_header.py`` (unit) and
``tests/test_x_trace_id_header_route.py`` (integration, the real app).
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from audittrace.telemetry import current_trace_id_hex

TRACE_ID_HEADER = "X-Trace-Id"
_TRACE_ID_HEADER_LOWER = TRACE_ID_HEADER.lower().encode("ascii")


class TraceIdHeaderMiddleware:
    """Stamp ``X-Trace-Id`` on ``http.response.start`` (pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_trace_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                trace_id = current_trace_id_hex()
                headers = list(message.get("headers", ()))
                kept = [
                    (name, value)
                    for name, value in headers
                    if name.lower() != _TRACE_ID_HEADER_LOWER
                ]
                if trace_id is not None:
                    message = {
                        **message,
                        "headers": [
                            *kept,
                            (_TRACE_ID_HEADER_LOWER, trace_id.encode("ascii")),
                        ],
                    }
                elif len(kept) != len(headers):
                    # D4: with no valid span the header is ABSENT. A value a
                    # route set itself is a value the server did not mint, so
                    # it is stripped, never forwarded (WP-4 NB-4).
                    message = {**message, "headers": kept}
            await send(message)

        await self.app(scope, receive, send_with_trace_id)
