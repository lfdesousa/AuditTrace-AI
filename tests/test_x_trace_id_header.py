"""X-TRACE-ID-HEADER (v1.31.0 WP-4), UNIT layer: the pure-ASGI middleware driven
directly with a stub inner app, the single-derivation identity pins, and
source-level pins. The integration layer (the real ``create_app()`` stack through
``TestClient``) is ``test_x_trace_id_header_route.py``; the two layers are not
mixed. Shared bootstrap: ``tests/_x_trace_id_header_support.py``.

Spec: ``audittrace-private/specs/2026-10-17-SPEC-x-trace-id-header.md``; gate
``decisions-in/2026-10-17-SPEC-GATE-x-trace-id-header.md`` (conditions C1-C7);
the cell names (AC-R2, AC5, ...) are the spec's.

Mocks declared: the inner ASGI app is a hand-written stub that emits a chosen
``http.response.start`` / ``http.response.body`` sequence; the OTel span comes
from a local ``TracerProvider`` (SDK, nothing exported); a lone test patches
``audittrace.trace_id_header.current_trace_id_hex`` to return ``None`` (the
"no valid span" branch). No clock, no network, no wall-clock assertion.

Neuters (each run ALONE, RED recorded in the build record):
  N3  the middleware emits the all-zero id when the context is invalid
  N4  the middleware stamps a hardcoded value
  N5  ``chat.py`` re-introduces a local copy of the derivation
  N6  the middleware keeps a pre-existing ``x-trace-id`` (first writer wins)
  N7  the middleware rewrites the other response headers / a body frame
  N9  ``memory_audit`` / ``console_store`` re-introduce a local copy
"""

from __future__ import annotations

import ast
import re
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from audittrace import telemetry
from audittrace.routes import chat
from audittrace.services import console_store, memory_audit
from audittrace.services.console_store import _context as console_context
from audittrace.trace_id_header import TraceIdHeaderMiddleware

TRACE_SHAPE = re.compile(r"^[0-9a-f]{32}$")
SRC = Path(telemetry.__file__).resolve().parent

Message = dict[str, Any]


def _stub_app(
    messages: list[Message],
) -> Callable[[dict[str, Any], Any, Any], Awaitable[None]]:
    """An inner ASGI app that sends ``messages`` verbatim."""

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        for message in messages:
            await send(message)

    return app


async def _drive(
    messages: list[Message], scope_type: str = "http"
) -> tuple[list[Message], list[dict[str, Any]]]:
    """Run ``TraceIdHeaderMiddleware(stub)`` and return (sent messages, scopes
    the stub saw)."""
    sent: list[Message] = []
    seen_scopes: list[dict[str, Any]] = []

    async def send(message: Message) -> None:
        sent.append(message)

    async def receive() -> Message:  # pragma: no cover - never awaited by the stub
        return {"type": "http.request"}

    inner = _stub_app(messages)

    async def recording(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen_scopes.append(scope)
        await inner(scope, receive, send)

    await TraceIdHeaderMiddleware(recording)(  # type: ignore[arg-type]
        {"type": scope_type}, receive, send
    )
    return sent, seen_scopes


@pytest.fixture
def local_tracer() -> Iterator[trace.Tracer]:
    """A tracer from a LOCAL SDK provider (the global one is set-once)."""
    provider = TracerProvider()
    yield provider.get_tracer(__name__)
    provider.shutdown()


def _headers(message: Message) -> list[tuple[bytes, bytes]]:
    return list(message["headers"])


START = {"type": "http.response.start", "status": 200, "headers": [(b"a", b"1")]}
BODY = {"type": "http.response.body", "body": b"payload", "more_body": False}


# ---------------------------------------------------------------------------
# AC1 (middleware half) / D2 / D4
# ---------------------------------------------------------------------------
async def test_ac1_start_message_gains_exactly_one_header_equal_to_the_span(
    local_tracer: trace.Tracer,
) -> None:
    """With a valid server span the ``http.response.start`` message gains exactly
    one ``x-trace-id`` (32 lowercase hex) equal to the span's trace id and keeps
    its other headers in order; the body message passes through unchanged.

    Neuters: N4 (hardcoded value) -> the equality RED; N7 (other headers
    rewritten) -> the preservation RED.
    """
    with local_tracer.start_as_current_span("server") as span:
        expected = format(span.get_span_context().trace_id, "032x")
        sent, _ = await _drive([dict(START), dict(BODY)])
    assert sent[1] == BODY
    values = [v for k, v in _headers(sent[0]) if k == b"x-trace-id"]
    assert values == [expected.encode()]
    assert TRACE_SHAPE.match(values[0].decode())
    assert _headers(sent[0])[0] == (b"a", b"1")
    assert len(_headers(sent[0])) == 2


async def test_ac2_no_header_when_the_context_is_invalid_never_all_zero() -> None:
    """D4. No active span (invalid context): the message is forwarded
    byte-identical - the header is ABSENT, never ``0 * 32``.

    Neuter: N3 (emit the all-zero id when invalid) -> RED.
    """
    sent, _ = await _drive([dict(START), dict(BODY)])
    assert sent[0] == START
    assert all(k != b"x-trace-id" for k, _ in _headers(sent[0]))
    assert b"0" * 32 not in repr(sent).encode()


async def test_ac2_the_none_branch_of_the_helper_leaves_the_message_untouched(
    monkeypatch: pytest.MonkeyPatch, local_tracer: trace.Tracer
) -> None:
    """Even INSIDE a valid span, when the helper reports ``None`` the middleware
    adds nothing (the decision lives in the one helper)."""
    monkeypatch.setattr("audittrace.trace_id_header.current_trace_id_hex", lambda: None)
    with local_tracer.start_as_current_span("server"):
        sent, _ = await _drive([dict(START)])
    assert sent[0] == START


async def test_c2_a_pre_existing_header_is_replaced_case_insensitively(
    local_tracer: trace.Tracer,
) -> None:
    """Gate C2: replace, not keep. A route-set ``X-Trace-Id`` (any case) never
    survives next to - or instead of - the server span's id; exactly one remains.

    Neuter: N6 (keep the existing value) -> RED.
    """
    start = {
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"X-Trace-Id", b"f" * 32), (b"x-trace-id", b"e" * 32)],
    }
    with local_tracer.start_as_current_span("server") as span:
        expected = format(span.get_span_context().trace_id, "032x")
        sent, _ = await _drive([start])
    assert [v for k, v in _headers(sent[0]) if k.lower() == b"x-trace-id"] == [
        expected.encode()
    ]


async def test_c2_a_start_message_without_a_headers_key_is_handled(
    local_tracer: trace.Tracer,
) -> None:
    """``headers`` is optional in the ASGI spec; the stamp must create it."""
    with local_tracer.start_as_current_span("server") as span:
        expected = format(span.get_span_context().trace_id, "032x")
        sent, _ = await _drive([{"type": "http.response.start", "status": 204}])
    assert _headers(sent[0]) == [(b"x-trace-id", expected.encode())]


async def test_only_the_start_message_is_touched_body_frames_are_byte_identical(
    local_tracer: trace.Tracer,
) -> None:
    """Streaming safety (D2): every ``http.response.body`` frame - including the
    ``more_body`` ones - and any other message type pass through unchanged and
    in order.

    Neuter: N7 (rewrite a body frame) -> RED.
    """
    frames = [
        dict(START),
        {"type": "http.response.body", "body": b"data: 1\n\n", "more_body": True},
        {"type": "http.response.body", "body": b"data: 2\n\n", "more_body": True},
        {"type": "http.response.body", "body": b"", "more_body": False},
    ]
    with local_tracer.start_as_current_span("server"):
        sent, _ = await _drive([dict(f) for f in frames])
    assert sent[1:] == frames[1:]
    assert [m["type"] for m in sent] == [m["type"] for m in frames]


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
async def test_non_http_scopes_pass_through_untouched(
    scope_type: str, local_tracer: trace.Tracer
) -> None:
    """A lifespan / websocket scope reaches the inner app as-is and its messages
    are not rewritten, even inside a valid span (only ``http.response.start`` is
    ever touched; there is deliberately no scope guard to neuter).
    """
    with local_tracer.start_as_current_span("server"):
        sent, scopes = await _drive([{"type": "lifespan.startup.complete"}], scope_type)
    assert scopes == [{"type": scope_type}]
    assert sent == [{"type": "lifespan.startup.complete"}]


# ---------------------------------------------------------------------------
# AC5 / AC-R2 / C5: one derivation, identity-pinned
# ---------------------------------------------------------------------------
def test_ac5_every_name_is_the_one_helper_object() -> None:
    """D1 / AC-R2. The five public names ARE ``telemetry.current_trace_id_hex``
    (object identity, not equal behaviour).

    Neuters: N5 (local copy in ``chat.py``) and N9 (local copy in
    ``memory_audit`` / ``console_store``) -> RED.
    """
    one = telemetry.current_trace_id_hex
    assert chat._current_trace_id_hex is one
    assert memory_audit._current_trace_id_hex is one
    assert console_context.current_trace_id_hex is one
    assert console_store.current_trace_id_hex is one


def _defs(path: str) -> set[str]:
    tree = ast.parse((SRC / path).read_text(encoding="utf-8"))
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def _format_trace_sites(path: str) -> list[int]:
    """Lines where ``format(<x>.trace_id, "032x")`` appears in ``path``."""
    # index-gap: BS-003 (an expression pattern inside function bodies is not an
    # index node; stdlib ast is the instrument, and a different one from the index)
    tree = ast.parse((SRC / path).read_text(encoding="utf-8"))
    out: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "format"
            and len(node.args) == 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "032x"
        ):
            out.append(node.lineno)
    return out


@pytest.mark.parametrize(
    "path",
    [
        "routes/chat.py",
        "services/memory_audit.py",
        "services/console_store/_context.py",
        "server.py",
        "trace_id_header.py",
    ],
)
def test_ac5_the_four_former_copies_and_the_middleware_hold_no_derivation(
    path: str,
) -> None:
    """Source-level half of AC5 (a different instrument from the identity pin):
    none of the files that used to carry a copy formats ``trace_id`` itself.

    Neuters: N5 / N9 and re-inlining the 500-handler copy -> RED.
    """
    assert _format_trace_sites(path) == []


def test_ac5_the_derivation_lives_only_where_the_gate_scoped_it() -> None:
    """Gate C4. After D1 the ``032x`` derivations in ``src/`` are: the helper
    itself, and the NAMED out-of-scope ones (``memory_upload/handler.py``
    ``_current_traceparent`` which also forwards ``traceparent``;
    ``memory_pdf/signature.py`` ``_current_trace_id``;
    ``session_summarizer.py`` ``_span_trace_id_hex``, span-scoped by design;
    ``telemetry.py``'s inbound-link formatter and startup self-test). A new copy
    anywhere else turns this RED.
    """
    allowed = {
        "telemetry.py",
        "routes/memory_upload/handler.py",
        "routes/memory_pdf/signature.py",
        "services/session_summarizer.py",
    }
    found = {
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if "migrations" not in p.parts and _format_trace_sites(str(p.relative_to(SRC)))
    }
    assert found == allowed


def test_the_chat_module_no_longer_defines_its_own_helper() -> None:
    """AC-R1/R2: the NAME survives (the tests and ``audit.py`` / ``mcp.py`` import
    it) but only as an alias, never as a ``def``."""
    assert "_current_trace_id_hex" not in _defs("routes/chat.py")
    assert "_current_trace_id_hex" not in _defs("services/memory_audit.py")
    assert "current_trace_id_hex" not in _defs("services/console_store/_context.py")
