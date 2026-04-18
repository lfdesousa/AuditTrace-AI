"""OpenTelemetry bootstrap for sovereign-memory-server (ADR-014.4).

Provides a single aspect-friendly API (`start_span`, `record_operation`)
that the `@log_call` decorator uses to emit spans and histogram metrics
for every decorated call. FastAPI request spans are produced separately
via FastAPIInstrumentor in server.py.

When `AUDITTRACE_OTLP_ENDPOINT` is empty, OTel runs in no-op mode: spans
and metrics are still created locally (so @log_call works identically
in tests) but nothing is exported off-process. Phase 1 will supply a
collector and set the endpoint.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_initialized = False
_tracer = None
_duration_histogram = None
_error_counter = None
_langfuse_client: Any | None = None


def _init_langfuse_client() -> None:
    """Best-effort init of the Langfuse SDK client.

    The Langfuse 4.x constructor installs its own ``TracerProvider`` as
    the OTel global. That provider already carries a
    ``LangfuseSpanProcessor`` for shipping spans to the Langfuse OTLP
    endpoint, so the caller just needs to attach its own OTLP exporter
    as an additional processor on the same provider to fan spans out
    to Tempo as well.
    """
    global _langfuse_client
    if _langfuse_client is not None:
        return
    if os.environ.get("AUDITTRACE_LANGFUSE_ENABLED", "").lower() != "true":
        return
    public_key = os.environ.get("AUDITTRACE_LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("AUDITTRACE_LANGFUSE_SECRET_KEY", "")
    host = os.environ.get("AUDITTRACE_LANGFUSE_HOST", "")
    if not (public_key and secret_key and host):
        logger.info("Langfuse SDK disabled — missing keys or host")
        return
    try:
        from langfuse import Langfuse
        from langfuse.span_filter import is_default_export_span

        def _should_export_span(span: Any) -> bool:
            """Langfuse export filter — ship only spans with signal.

            Pre-filter baseline saw 14,636 noise traces accumulated in
            Langfuse (name empty, user None, input/output False) because
            the default filter accepted every span from the known
            LLM-instrumentor scopes, including FastAPI inbound + HTTPX
            outbound auto-spans that carry none of our semantics.

            Accept only spans that actually belong on a user's audit
            timeline:
              - langfuse-sdk-sourced spans (default path)
              - spans with ``gen_ai.*`` semconv attributes (LLM calls)
              - our app-emitted spans with ``user.id`` set (memory
                services, tools, context builder — all Phase-2 tagged)
              - the FastAPI root span for ``/v1/chat/completions``
                so Langfuse's latency field matches wall time.

            Every other OTel auto-span stays in Tempo (full traces
            there are unchanged) but is dropped before Langfuse ingest.
            """
            attrs = span.attributes or {}

            # Keep the root chat-completions span so latency is accurate.
            if attrs.get("http.route") == "/v1/chat/completions":
                return True

            # Keep anything carrying gen_ai semconv or our user.id tag —
            # these are the spans that renders as useful observations in
            # the Langfuse trace tree.
            if attrs.get("user.id") or attrs.get("langfuse.user.id"):
                return True
            for key in attrs.keys():
                if key.startswith("gen_ai.") or key.startswith("langfuse."):
                    return True

            # Otherwise, apply the default filter (langfuse-sdk scope) —
            # do NOT accept the generic LLM-instrumentor scopes any more,
            # so FastAPI + HTTPX auto-spans without our tagging get
            # dropped before reaching Langfuse.
            scope_name = ""
            ils = getattr(span, "instrumentation_scope", None)
            if ils is not None:
                scope_name = getattr(ils, "name", "") or ""
            if scope_name == "langfuse-sdk":
                return bool(is_default_export_span(span))
            return False

        _langfuse_client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
            tracing_enabled=True,
            should_export_span=_should_export_span,
        )
        logger.info("Langfuse SDK initialised — host=%s", host)
    except Exception as exc:  # pragma: no cover - optional dep path
        logger.warning("Langfuse SDK init failed: %s", exc)


def init_telemetry(
    service_name: str,
    otlp_endpoint: str = "",
    tracing_enabled: bool = True,
    metrics_enabled: bool = True,
) -> None:
    """Initialise OTel tracer + meter providers (idempotent).

    No-op exporter when `otlp_endpoint` is empty — providers are still
    installed so spans/metrics objects exist for the @log_call aspect.
    """
    global _initialized, _tracer, _duration_histogram, _error_counter

    if _initialized:
        return

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:  # pragma: no cover - deps in requirements.txt
        logger.warning("OpenTelemetry SDK not installed; telemetry disabled")
        _initialized = True
        return

    resource = Resource.create({SERVICE_NAME: service_name})

    # When Langfuse is enabled we defer TracerProvider creation so the
    # Langfuse SDK's own provider becomes global (it would otherwise
    # refuse to override ours — OTel's ``set_tracer_provider`` enforces
    # set-once). We then attach OUR OTLP exporter as an extra span
    # processor on Langfuse's provider, so every span fans out to both
    # Tempo and Langfuse under a single trace tree.
    tracer_provider: Any = None
    otlp_processor: Any = None

    if tracing_enabled and otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            traces_endpoint = (
                otlp_endpoint.rstrip("/") + "/v1/traces"
                if not otlp_endpoint.endswith("/v1/traces")
                else otlp_endpoint
            )
            otlp_processor = BatchSpanProcessor(
                OTLPSpanExporter(endpoint=traces_endpoint)
            )
            logger.info("OTel tracing exporter -> %s", otlp_endpoint)
        except Exception as e:  # pragma: no cover
            logger.warning("Failed to configure OTLP span exporter: %s", e)

    if metrics_enabled:
        readers = []
        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter,
                )

                metrics_endpoint = (
                    otlp_endpoint.rstrip("/") + "/v1/metrics"
                    if not otlp_endpoint.endswith("/v1/metrics")
                    else otlp_endpoint
                )
                readers.append(
                    PeriodicExportingMetricReader(
                        OTLPMetricExporter(endpoint=metrics_endpoint)
                    )
                )
                logger.info("OTel metrics exporter -> %s", otlp_endpoint)
            except Exception as e:  # pragma: no cover
                logger.warning("Failed to configure OTLP metric exporter: %s", e)
        meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        metrics.set_meter_provider(meter_provider)
        meter = metrics.get_meter(service_name)
        _duration_histogram = meter.create_histogram(
            name="sovereign.operation.duration",
            unit="s",
            description="Duration of @log_call-decorated operations",
        )
        _error_counter = meter.create_counter(
            name="sovereign.operation.errors",
            description="Count of errors raised by @log_call-decorated operations",
        )

    # Correlate log records with active spans
    try:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        LoggingInstrumentor().instrument(set_logging_format=False)
    except Exception as e:  # pragma: no cover
        logger.debug("LoggingInstrumentor not enabled: %s", e)

    # Initialise Langfuse FIRST — its constructor sets the global
    # TracerProvider. We then attach our OTLP exporter as an additional
    # SpanProcessor on that same provider so every span reaches both
    # Tempo and Langfuse.
    _init_langfuse_client()
    if tracing_enabled:
        from opentelemetry import trace

        global_provider = trace.get_tracer_provider()
        if otlp_processor is not None and hasattr(
            global_provider, "add_span_processor"
        ):
            try:
                global_provider.add_span_processor(otlp_processor)
                logger.info(
                    "OTLP span processor attached to global TracerProvider (%s)",
                    type(global_provider).__name__,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to attach OTLP span processor: %s", exc)
        elif otlp_processor is not None:
            # No Langfuse, or Langfuse didn't install an SDK TracerProvider.
            # Install our own so spans still reach Tempo.
            tracer_provider = TracerProvider(resource=resource)
            tracer_provider.add_span_processor(otlp_processor)
            trace.set_tracer_provider(tracer_provider)
            logger.info("App TracerProvider installed (Tempo-only path)")

        # When Langfuse is enabled, tag our tracer with the scope name
        # "langfuse-sdk" and a public_key attribute so LangfuseSpanProcessor's
        # default filter (``is_default_export_span`` → ``is_langfuse_span``)
        # accepts every span we emit. Without this, Langfuse would drop
        # @log_call spans silently (they carry no ``gen_ai.*`` attrs and
        # wouldn't match any known-LLM-instrumentor prefix). Tempo is
        # unaffected — scope name doesn't change its trace grouping.
        if _langfuse_client is not None:
            public_key = os.environ.get("AUDITTRACE_LANGFUSE_PUBLIC_KEY", "")
            _tracer = trace.get_tracer_provider().get_tracer(
                "langfuse-sdk",
                attributes={"public_key": public_key} if public_key else None,
            )
        else:
            _tracer = trace.get_tracer(service_name)

    _initialized = True
    logger.info(
        "OpenTelemetry initialised (service=%s, tracing=%s, metrics=%s, export=%s)",
        service_name,
        tracing_enabled,
        metrics_enabled,
        otlp_endpoint or "disabled",
    )


@contextmanager
def start_span(
    name: str,
    metadata: dict[str, Any] | None = None,
) -> Iterator[object | None]:
    """Start an OTel span and yield it.

    Routes through the app's global TracerProvider, which has both the
    OTLP exporter (→ Tempo) and the Langfuse span processor attached.
    Every emitted span therefore reaches both backends under a single
    trace tree. Metadata keys are stamped with the ``langfuse.observation.metadata.``
    prefix so Langfuse's server-side mapping surfaces them as first-class
    observation metadata.
    """
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as span:
        if metadata:
            for k, v in metadata.items():
                if v is None:
                    continue
                try:
                    span.set_attribute(f"langfuse.observation.metadata.{k}", v)
                except Exception:  # pragma: no cover
                    pass
        yield span


# Attribute keys Langfuse maps to first-class observation fields. Anything
# we write on a span with these keys shows up in Langfuse's Input/Output
# panels; writes to the plain OTel keys (``input.value`` / ``output.value``)
# stay visible in Tempo for unified search.
_LANGFUSE_INPUT_KEY = "langfuse.observation.input"
_LANGFUSE_OUTPUT_KEY = "langfuse.observation.output"


def set_current_span_attributes(attributes: dict[str, Any]) -> None:
    """Set attributes on the currently active OTel span.

    Mirrors ``input.value`` / ``output.value`` to the Langfuse-specific
    attribute keys so the Langfuse UI populates the Input/Output panels,
    while leaving the OTel keys in place for Tempo. No-op if tracing is
    disabled or there is no active span.
    """
    if _tracer is None:
        return
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span is None or not span.is_recording():
            return
        for key, value in attributes.items():
            if value is None:
                continue
            span.set_attribute(key, value)
            if key == "input.value":
                span.set_attribute(_LANGFUSE_INPUT_KEY, value)
            elif key == "output.value":
                span.set_attribute(_LANGFUSE_OUTPUT_KEY, value)
    except Exception:  # pragma: no cover
        pass


def record_operation(operation: str, duration: float, error: str | None = None) -> None:
    """Record a histogram sample for an operation and bump the error counter."""
    attrs = {"operation": operation}
    if _duration_histogram is not None:
        _duration_histogram.record(duration, attributes=attrs)
    if error and _error_counter is not None:
        _error_counter.add(1, attributes={**attrs, "error_type": error})


def shutdown() -> None:
    """Flush providers on shutdown (best-effort)."""
    try:
        from opentelemetry import metrics, trace

        tp = trace.get_tracer_provider()
        if hasattr(tp, "shutdown"):
            tp.shutdown()
        mp = metrics.get_meter_provider()
        if hasattr(mp, "shutdown"):
            mp.shutdown()
    except Exception:  # pragma: no cover
        pass


def _reset_for_tests() -> None:
    """Reset module state. Used by tests only."""
    global _initialized, _tracer, _duration_histogram, _error_counter
    _initialized = False
    _tracer = None
    _duration_histogram = None
    _error_counter = None
