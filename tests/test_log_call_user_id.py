"""Tests for Phase-2 user_id propagation to @log_call child spans.

Without this, the Langfuse UI cannot filter child observations by user
and the EU AI Act Art. 12 audit trail stops at the root span. The
``@log_call`` decorator now sets ``langfuse.user.id`` + ``user.id`` on
the span whenever a ``UserContext`` is in scope (first arg, any kwarg,
or the request-scoped ContextVar).
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from audittrace import telemetry
from audittrace.db import rls
from audittrace.identity import UserContext
from audittrace.logging_config import _extract_user_id, log_call


@pytest.fixture
def span_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Plug a fresh in-memory exporter into the telemetry module.

    ``@log_call`` drives spans through ``telemetry.start_span`` which uses
    its module-global ``_tracer``. The app only sets that at FastAPI
    startup, so tests need to stub it explicitly. Using monkeypatch keeps
    the stub scoped to one test — no global-state leaks across the suite.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("test"))
    return exporter


def _make_ctx(sub: str = "user-alpha") -> UserContext:
    return UserContext(
        user_id=sub,
        username="alpha",
        agent_type="curl",
        scopes=(),
    )


def test_user_id_from_positional_usercontext(
    span_exporter: InMemorySpanExporter,
) -> None:
    @log_call()
    def svc(ctx: UserContext, query: str) -> str:
        return f"ok {query}"

    svc(_make_ctx("sub-123"), "hello")
    spans = span_exporter.get_finished_spans()
    assert spans, "decorator must emit a span"
    attrs = spans[0].attributes or {}
    assert attrs.get("langfuse.user.id") == "sub-123"
    assert attrs.get("user.id") == "sub-123"


def test_user_id_from_kwarg_usercontext(span_exporter: InMemorySpanExporter) -> None:
    @log_call()
    def svc(*, ctx: UserContext, query: str) -> str:
        return query

    svc(ctx=_make_ctx("sub-kw"), query="hi")
    attrs = span_exporter.get_finished_spans()[0].attributes or {}
    assert attrs.get("langfuse.user.id") == "sub-kw"


def test_user_id_from_contextvar_fallback(span_exporter: InMemorySpanExporter) -> None:
    """When no UserContext arg, the RLS ContextVar is consulted."""
    rls.set_current_user_id("sub-ctxvar")
    try:

        @log_call()
        def svc(query: str) -> str:
            return query

        svc("no-ctx-arg")
        attrs = span_exporter.get_finished_spans()[0].attributes or {}
        assert attrs.get("langfuse.user.id") == "sub-ctxvar"
    finally:
        rls.set_current_user_id(None)


def test_user_id_absent_when_neither_source_yields(
    span_exporter: InMemorySpanExporter,
) -> None:
    """Absence, not an empty string — avoids spurious 'user=' in Langfuse."""
    rls.set_current_user_id(None)

    @log_call()
    def svc(query: str) -> str:
        return query

    svc("no-user-anywhere")
    attrs = span_exporter.get_finished_spans()[0].attributes or {}
    assert "langfuse.user.id" not in attrs
    assert "user.id" not in attrs


# ----- helper unit tests (no span plumbing) -----


def test_extract_user_id_prefers_positional_over_contextvar() -> None:
    rls.set_current_user_id("from-contextvar")
    try:
        result = _extract_user_id((_make_ctx("from-arg"),), {})
        assert result == "from-arg"
    finally:
        rls.set_current_user_id(None)


def test_extract_user_id_none_when_empty() -> None:
    rls.set_current_user_id(None)
    assert _extract_user_id((), {}) is None
