"""WP-13 (ADDENDUM-2 B1): the Langfuse public key never reaches Tempo.

Our ``langfuse-sdk`` tracer carries ``public_key`` as an instrumentation-scope
attribute (the Langfuse SDK's routing contract). ``ScopeScrubbingSpanExporter``
removes it on the OTLP/Tempo path only.

* AC-13.T (Tempo chain): the REAL ``init_telemetry`` (global, set-once, so it runs
  in a fresh interpreter) with Langfuse enabled; the OTLP exporter is replaced by
  an in-memory exporter AT THE SCRUBBER'S ``inner`` seam; >= 3 spans are driven;
  no span / event / resource / scope attribute value in the Tempo chain matches
  ``[ps]k-lf-<uuid>``. One span is the real ``@log_call`` span of
  ``auth.require_user`` (bypass mode, minimal Starlette ``Request``), asserted
  present in the exported set and scrubbed (ADDENDUM-2 B1).
* AC-13.L (Langfuse positive): a real ``LangfuseSpanProcessor`` attached to the
  same provider receives the same spans with ``scope.attributes["public_key"] ==
  PK``.

Neuters (each alone, recorded RED): N-13.1 remove the scrubber wrap -> AC-13.T;
N-13.2 drop the scope attribute from the tracer -> AC-13.L.

Mocks declared: the OTLP exporter class (replaced by ``InMemorySpanExporter``);
the Langfuse HTTP endpoint is a closed loopback port (nothing listens); the
``Request`` is a hand-built ASGI scope (no server, no JWT: bypass mode). Keys are
built at runtime (S1): nothing key-shaped is committed. The child prints counts
only, never a key.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from audittrace.telemetry_scope_scrub import ScopeScrubbingSpanExporter

KEY_SHAPE = re.compile(r"[ps]k-lf-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-")


def _runtime_key(prefix: str) -> str:
    """A key of the real shape, assembled at runtime (never a source literal)."""
    return prefix + "-lf-" + str(uuid.uuid4())


_CHILD = r"""
import json, os, re
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import opentelemetry.exporter.otlp.proto.http.trace_exporter as te
from audittrace import telemetry
from audittrace.logging_config import setup_logging
setup_logging(level="INFO", structured=True)

tempo_inner = InMemorySpanExporter()
OUR = "http://127.0.0.1:9/v1/traces"   # init_telemetry's own endpoint; Langfuse's SDK builds its own
te.OTLPSpanExporter = lambda endpoint=None, **kw: tempo_inner if endpoint == OUR else InMemorySpanExporter()  # mock: the scrubber's `inner`
telemetry.init_telemetry(service_name="wp13-child", otlp_endpoint="http://127.0.0.1:9",
                         tracing_enabled=True, metrics_enabled=False)
provider = trace.get_tracer_provider()

from langfuse._client.span_processor import LangfuseSpanProcessor
lf_inner = InMemorySpanExporter()
provider.add_span_processor(LangfuseSpanProcessor(
    public_key=os.environ["WP13_PK"], secret_key=os.environ["WP13_SK"],
    base_url="http://127.0.0.1:9", span_exporter=lf_inner))

# ADDENDUM-2 B1: drive the REAL @log_call span of auth.require_user (bypass mode,
# AUDITTRACE_AUTH_REQUIRED unset -> false) with a minimal real Starlette Request.
import asyncio
from starlette.requests import Request
from audittrace.auth import require_user
for n in ("wp13-a", "wp13-b"):
    with telemetry._tracer.start_as_current_span(n):
        pass
provider.force_flush()
before = {s.context.span_id for s in tempo_inner.get_finished_spans()}
request = Request({"type": "http", "method": "GET", "path": "/", "headers": [(b"user-agent", b"wp13-child")]})
ctx = asyncio.run(require_user(request, None))
provider.force_flush()

KEY = re.compile(r"[ps]k-lf-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-")
def values(span):
    out = [str(v) for v in (span.attributes or {}).values()]
    for ev in span.events or ():
        out += [str(v) for v in (ev.attributes or {}).values()]
    out += [str(v) for v in (span.resource.attributes or {}).values()]
    sc = span.instrumentation_scope
    if sc is not None:
        out += [str(v) for v in (sc.attributes or {}).values()]
    return out

tempo = tempo_inner.get_finished_spans()
lf = lf_inner.get_finished_spans()
ru = [s for s in tempo if s.context.span_id not in before]
print("RESULT " + json.dumps({
    "tempo_spans": len(tempo),
    "tempo_langfuse_sdk_spans": sum(1 for s in tempo if s.instrumentation_scope.name == "langfuse-sdk"),
    "tempo_key_hits": sum(1 for s in tempo for v in values(s) if KEY.search(v)),
    "require_user_returned_ctx": ctx is not None and type(ctx).__name__ == "UserContext",
    "require_user_spans": sum(1 for s in ru if "require_user" in s.name),
    "require_user_key_hits": sum(1 for s in ru for v in values(s) if KEY.search(v)),
    "lf_spans": len(lf),
    "lf_scope_pk_matches": sum(1 for s in lf if (s.instrumentation_scope.attributes or {}).get("public_key") == os.environ["WP13_PK"]),
    "tracer_scope_has_pk": any((s.instrumentation_scope.attributes or {}).get("public_key") == os.environ["WP13_PK"] for s in lf),
}), flush=True)
os._exit(0)
"""


def _run_child() -> dict[str, Any]:
    env = {
        **os.environ,
        "AUDITTRACE_ENV": "test",
        "AUDITTRACE_LANGFUSE_ENABLED": "true",
        "AUDITTRACE_LANGFUSE_HOST": "http://127.0.0.1:9",
        "WP13_PK": _runtime_key("pk"),
        "WP13_SK": _runtime_key("sk"),
    }
    env["AUDITTRACE_LANGFUSE_PUBLIC_KEY"] = env["WP13_PK"]
    env["AUDITTRACE_LANGFUSE_SECRET_KEY"] = env["WP13_SK"]
    proc = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert lines, proc.stdout[-500:] + proc.stderr[-1500:]
    assert env["WP13_PK"] not in proc.stdout + proc.stderr  # the child leaks no key
    result: dict[str, Any] = json.loads(lines[-1][len("RESULT ") :])
    return result


_RESULT: dict[str, Any] | None = None


def _result() -> dict[str, Any]:
    global _RESULT
    if _RESULT is None:
        _RESULT = _run_child()
    return _RESULT


def test_ac_13_t_no_key_shaped_value_in_the_tempo_chain() -> None:
    r = _result()
    # fail closed: the chain really carried spans, including langfuse-sdk ones
    assert r["tempo_spans"] >= 3
    assert r["tempo_langfuse_sdk_spans"] >= 3
    assert r["tempo_key_hits"] == 0
    # ADDENDUM-2 B1: the real request-path span (auth.require_user) is among the
    # exported spans, and it is scrubbed
    assert r["require_user_returned_ctx"] is True
    assert r["require_user_spans"] >= 1
    assert r["require_user_key_hits"] == 0


def test_ac_13_l_langfuse_processor_still_receives_the_scope_attribute() -> None:
    r = _result()
    assert r["lf_spans"] >= 3
    assert r["lf_scope_pk_matches"] == r["lf_spans"]
    assert r["tracer_scope_has_pk"] is True


# ── unit cells for the exporter itself ──────────────────────────────────────


class _Recorder(SpanExporter):
    def __init__(self) -> None:
        self.batches: list[list[ReadableSpan]] = []
        self.shut = False

    def export(self, spans: Any) -> SpanExportResult:
        self.batches.append(list(spans))
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.shut = True


def _span_with_scope(attrs: dict[str, str] | None, name: str = "langfuse-sdk") -> Any:
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider.add_span_processor(SimpleSpanProcessor(mem))
    tracer = provider.get_tracer(name, attributes=attrs)
    with tracer.start_as_current_span("s"):
        pass
    (span,) = mem.get_finished_spans()
    return span


def test_scrubber_forwards_copy_without_key_and_never_mutates_the_original() -> None:
    pk = _runtime_key("pk")
    span = _span_with_scope({"public_key": pk, "other": "kept"})
    inner = _Recorder()
    exporter = ScopeScrubbingSpanExporter(inner)
    assert exporter.export([span]) is SpanExportResult.SUCCESS
    (sent,) = inner.batches[0]
    assert "public_key" not in dict(sent.instrumentation_scope.attributes)
    assert dict(sent.instrumentation_scope.attributes) == {"other": "kept"}
    assert sent.name == span.name and sent.context == span.context
    # the original span and its shared scope keep the attribute (Langfuse routing)
    assert dict(span.instrumentation_scope.attributes)["public_key"] == pk
    assert sent is not span


def test_scrubber_drops_the_attribute_dict_when_nothing_else_remains() -> None:
    span = _span_with_scope({"public_key": _runtime_key("pk")})
    inner = _Recorder()
    ScopeScrubbingSpanExporter(inner).export([span])
    (sent,) = inner.batches[0]
    assert not sent.instrumentation_scope.attributes
    assert sent.instrumentation_scope.name == "langfuse-sdk"


def test_scrubber_passes_through_spans_without_the_attribute() -> None:
    plain = _span_with_scope(None, name="other-scope")
    inner = _Recorder()
    ScopeScrubbingSpanExporter(inner).export([plain])
    assert inner.batches[0][0] is plain  # same object: nothing to scrub, no copy


def test_scrubber_handles_a_span_without_scope() -> None:
    span = _span_with_scope(None)
    span._instrumentation_scope = None
    inner = _Recorder()
    ScopeScrubbingSpanExporter(inner).export([span])
    assert inner.batches[0][0] is span


def test_scrubber_delegates_shutdown_and_force_flush() -> None:
    inner = _Recorder()
    exporter = ScopeScrubbingSpanExporter(inner)
    assert exporter.force_flush() is True  # base returns None = ok
    exporter.shutdown()
    assert inner.shut is True

    class _NoFlush:  # a duck exporter without force_flush
        def export(self, spans: Any) -> SpanExportResult:
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            pass

    assert ScopeScrubbingSpanExporter(_NoFlush()).force_flush() is True  # type: ignore[arg-type]
    mem = InMemorySpanExporter()
    assert ScopeScrubbingSpanExporter(mem).force_flush() is bool(mem.force_flush())


def test_key_shape_predicate_positive_control() -> None:
    """The predicate used by AC-13.T flags a planted value of the real shape."""
    assert KEY_SHAPE.search("x " + _runtime_key("pk") + " y")
    assert KEY_SHAPE.search(_runtime_key("sk"))
    assert not KEY_SHAPE.search("pk-lf-placeholder")
