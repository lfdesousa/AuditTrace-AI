"""OpenTelemetry bootstrap for sovereign-memory-server (ADR-014.4).

Provides a single aspect-friendly API (`start_span`, `record_operation`)
that the `@log_call` decorator uses to emit spans and histogram metrics
for every decorated call. FastAPI request spans are produced separately
via FastAPIInstrumentor in server.py.

When `SOVEREIGN_OTLP_ENDPOINT` is empty, OTel runs in no-op mode: spans
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

    The Langfuse SDK uses OTel under the hood but writes its own metadata
    fields (langgraph_node, langgraph_step, etc.) at the top level of the
    observation metadata Map in ClickHouse, which is what triggers the
    graph view in the trace UI. The OTLP exporter alone does not — it
    nests OTel attributes inside metadata['attributes'] as a JSON string.
    """
    global _langfuse_client
    if _langfuse_client is not None:
        return
    if os.environ.get("SOVEREIGN_LANGFUSE_ENABLED", "").lower() != "true":
        return
    public_key = os.environ.get("SOVEREIGN_LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("SOVEREIGN_LANGFUSE_SECRET_KEY", "")
    host = os.environ.get("SOVEREIGN_LANGFUSE_HOST", "")
    if not (public_key and secret_key and host):
        logger.info("Langfuse SDK disabled — missing keys or host")
        return
    try:
        from langfuse import Langfuse

        _langfuse_client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
            tracing_enabled=True,
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

    if tracing_enabled:
        tracer_provider = TracerProvider(resource=resource)
        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                tracer_provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
                )
                logger.info("OTel tracing exporter -> %s", otlp_endpoint)
            except Exception as e:  # pragma: no cover
                logger.warning("Failed to configure OTLP span exporter: %s", e)
        trace.set_tracer_provider(tracer_provider)
        _tracer = trace.get_tracer(service_name)

    if metrics_enabled:
        readers = []
        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter,
                )

                readers.append(
                    PeriodicExportingMetricReader(
                        OTLPMetricExporter(endpoint=otlp_endpoint)
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

    _initialized = True
    logger.info(
        "OpenTelemetry initialised (service=%s, tracing=%s, metrics=%s, export=%s)",
        service_name,
        tracing_enabled,
        metrics_enabled,
        otlp_endpoint or "disabled",
    )

    # Initialise the Langfuse SDK so spans can carry top-level metadata
    # (langgraph_node, langgraph_step, etc.) that the trace graph view needs.
    _init_langfuse_client()


@contextmanager
def start_span(
    name: str,
    metadata: dict[str, Any] | None = None,
) -> Iterator[object | None]:
    """Start a span and yield it.

    When the Langfuse SDK is initialised, route the span through it so that
    `metadata` lands at the top level of the observation in ClickHouse —
    that is what triggers the trace graph view in Langfuse's UI. Otherwise
    fall back to plain OpenTelemetry.
    """
    if _langfuse_client is not None:
        # SDK manages its own context — wraps OTel under the hood, so a
        # nested @log_call inside this block still nests correctly.
        with _langfuse_client.start_as_current_observation(
            name=name,
            as_type="span",
            metadata=metadata or {},
        ) as span:
            yield span
        return

    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as span:
        yield span


def set_current_span_attributes(attributes: dict[str, Any]) -> None:
    """Set attributes on the currently active span. No-op if tracing disabled.

    Used by route handlers to enrich chat completion spans with gen_ai.*
    semantic conventions so Langfuse displays prompts and completions.

    When the Langfuse SDK is active, attributes must be routed through
    ``_langfuse_client.update_current_span`` — the SDK does not push its
    observation onto the OTel current-span context, so writing only via
    OTel leaves the Langfuse observation empty (renders as ``undefined``
    in the trace UI).
    """
    if _langfuse_client is not None:
        try:
            metadata = {k: v for k, v in attributes.items() if v is not None}
            _langfuse_client.update_current_span(metadata=metadata)
            # Surface input/output to Langfuse's first-class fields too so the
            # trace UI populates the Input/Output panels (not just metadata).
            #
            # CRITICAL: Build kwargs conditionally — passing ``input=None`` to
            # Langfuse v4 ``update_current_span`` does NOT no-op, it CLEARS
            # the existing input field. The @log_call aspect calls this helper
            # twice per span (once with input.value before the call, once with
            # output.value after), so passing the missing key as None on the
            # second call would wipe the first call's write. Only pass keys
            # that are actually present.
            io_kwargs: dict[str, Any] = {}
            if "input.value" in attributes and attributes["input.value"] is not None:
                io_kwargs["input"] = attributes["input.value"]
            if "output.value" in attributes and attributes["output.value"] is not None:
                io_kwargs["output"] = attributes["output.value"]
            if io_kwargs:
                _langfuse_client.update_current_span(**io_kwargs)
        except Exception:  # pragma: no cover
            pass

    if _tracer is None:
        return
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span is None or not span.is_recording():
            return
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
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
