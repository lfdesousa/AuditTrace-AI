"""OpenTelemetry bootstrap for audittrace-server (ADR-014.4).

Provides a single aspect-friendly API (`start_span`, `record_operation`)
that the `@log_call` decorator uses to emit spans and histogram metrics
for every decorated call. FastAPI request spans are produced separately
via FastAPIInstrumentor in server.py.

When `AUDITTRACE_OTLP_ENDPOINT` is empty, OTel runs in no-op mode: spans
and metrics are still created locally (so @log_call works identically
in tests) but nothing is exported off-process. Phase 1 will supply a
collector and set the endpoint.

SEC-JWT-IN-LANGFUSE-SPAN (2026-10-08, ADDENDUM C D7): ``init_telemetry``
now ALWAYS owns a single SDK ``TracerProvider`` (previously it deferred
to Langfuse's own provider, or installed one only on the Tempo-only
path) and registers a ``ScrubbingSpanProcessor`` FIRST on it, via the
public ``add_span_processor`` API, before Langfuse's own processor or
our OTLP processor are added. Every span — the seam's own, the FastAPI
auto-instrumented SERVER span, any non-seam module span — passes through
the scrubber's ``on_end`` before any exporter (Tempo, Langfuse, a test's
in-memory exporter) ever sees it. See ``ScrubbingSpanProcessor`` for the
mechanism and ``_run_startup_scrub_self_test`` for the fail-closed
startup proof (S-C1).
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
    from opentelemetry.sdk.trace.export import SpanExportResult

logger = logging.getLogger(__name__)

_initialized = False
_tracer = None
_duration_histogram = None
_error_counter = None
_langfuse_client: Any | None = None

# Probe routes we never want to ship to the Langfuse audit tier.
# Kubelet/Docker liveness+readiness hit /health at ~20 req/min per replica
# and /metrics is admin-only telemetry; neither carries user identity or
# reconstructibility signal. They remain fully instrumented in Tempo
# (ADR-028) and in the operation-duration histogram — we just drop them
# at the Langfuse export boundary. Re-opened 2026-04-18 (85872db) when
# the langfuse-sdk scope started accepting every non-empty-name span;
# closed again 2026-04-24. See ADR-045 + project_langfuse_ux_regression.
#
# Two signals match the same spans depending on who emitted them:
#   - ``http.route``: set by the FastAPI auto-instrumentor on the outer
#     inbound span. Belt-and-suspenders — that scope is not langfuse-sdk
#     so the later scope check already rejects it, but keep this check
#     for defence-in-depth if a future change widens the scope accept.
#   - ``sovereign.operation``: set by the ``@log_call`` aspect on the
#     inner span. This IS the span Langfuse sees today (scope=langfuse-
#     sdk, name=health-check) — the production path that actually
#     leaked. Matching on the operation prefix catches both
#     ``audittrace.routes.health.health_check`` and
#     ``audittrace.routes.health.metrics`` from the same file.
_LANGFUSE_PROBE_DENYLIST = frozenset({"/health", "/metrics"})
_LANGFUSE_PROBE_OPERATION_PREFIX = "audittrace.routes.health."


# ═══════════════════════ SEC-JWT-IN-LANGFUSE-SPAN: G9 / S-C5 ══════════════
#
# A future ``OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*`` env var would
# put raw header VALUES (including ``Authorization``) onto the server
# span as attributes — a second, independent leak path the scrubber
# cannot close after the fact for the SAME reason D3 cannot: the value
# never gets registered (it enters via a header the ASGI instrumentor
# reads directly, not via ``oauth2_scheme``), so only the D4 shape net
# would catch it, and only if it still looks JWT-shaped. Refusing to
# start is cheaper and more honest than hoping the net catches it.
_SENSITIVE_HEADER_NAMES: tuple[str, ...] = (
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
)
_CAPTURE_HEADER_ENV_VARS: tuple[str, ...] = (
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST",
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE",
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_REQUEST",
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_RESPONSE",
)
_SANITIZE_FIELDS_ENV_VAR = "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS"


def _capture_entry_matches_sensitive(entry: str) -> bool:
    """True if one comma-separated capture-header entry can match a
    sensitive header name.

    OTel's own ASGI/HTTP instrumentation evaluates each entry as
    ``re.fullmatch(entry, header_name, re.IGNORECASE)`` (ADDENDUM C
    S-C5, ``opentelemetry/util/http.py``) — so ``auth.*`` matches
    ``authorization``, and the comparison must be case-insensitive
    (``AUTHORIZATION`` must not slip past a lowercase-only check). An
    entry that cannot even compile as a regex is treated as matching —
    "uncompilable means refuse", never "uncompilable means ignore".
    """
    entry = entry.strip()
    if not entry:
        return False
    try:
        compiled = re.compile(entry, re.IGNORECASE)
    except re.error:
        return True
    return any(compiled.fullmatch(name) for name in _SENSITIVE_HEADER_NAMES)


def _refuse_if_header_capture_configured() -> None:
    """Refuse to start if any capture-headers env var can reach a
    sensitive header; otherwise widen the sanitize-fields env var.

    G9 (spec) / S-B2 / S-C5 (ADDENDUM C). Checked BEFORE any provider is
    built — a misconfigured capture list is a reason not to run, not a
    reason to run with a weaker scrub. The ``SANITIZE_FIELDS`` append is
    additive (merges with whatever the operator already set) so a
    benign future capture entry still gets its value masked by the ASGI
    instrumentation layer, belt-and-suspenders with the refusal above.

    Residual (stated, not hidden): ``OpenTelemetryMiddleware`` reads
    ``OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS`` once,
    at ``FastAPIInstrumentor.instrument_app(app)`` time inside
    ``create_app()`` — which runs BEFORE ``init_telemetry`` (called from
    ``lifespan``) in this process's startup order. The REFUSAL above
    still fires in time to prevent the app from serving; the
    SANITIZE_FIELDS widening is defence-in-depth for a subsequent
    process start, not a guarantee for the one already constructing its
    middleware this call.
    """
    for var in _CAPTURE_HEADER_ENV_VARS:
        raw = os.environ.get(var, "")
        for entry in raw.split(","):
            if _capture_entry_matches_sensitive(entry):
                raise RuntimeError(
                    f"{var} entry {entry!r} can match a sensitive header "
                    "(authorization/cookie/set-cookie/proxy-authorization) "
                    "— refusing to start (SEC-JWT-IN-LANGFUSE-SPAN G9)"
                )
    existing = {
        s.strip()
        for s in os.environ.get(_SANITIZE_FIELDS_ENV_VAR, "").split(",")
        if s.strip()
    }
    merged = sorted(existing | set(_SENSITIVE_HEADER_NAMES))
    os.environ[_SANITIZE_FIELDS_ENV_VAR] = ",".join(merged)


# ═══════════════════ SEC-JWT-IN-LANGFUSE-SPAN: D7 export-time scrub ═══════


def _scrub_bounded_attributes(attrs: Any) -> None:
    """Rewrite every ``str`` (and sequence-of-``str``) value of a span's
    attribute mapping IN PLACE.

    OTel's ``BoundedAttributes`` refuses mutation through its own public
    ``__setitem__`` once a span has ended (``_raise_if_immutable``); this
    writes straight to its backing ``_dict`` instead — the same kind of
    private-field rebind ADDENDUM C measured on ``ReadableSpan`` itself
    (``_attributes``/``_events``/``_status`` have no ``__slots__``, so
    assignment always succeeds regardless of the public immutability
    guard). Falls back to a plain mapping/dict when ``_dict`` is absent
    (e.g. a test double) so this never depends on one SDK's internals
    more than it has to.

    S-C4 — sequence-valued attributes (OTel allows ``Sequence[str]``
    attribute values) are scrubbed element-wise; a secret inside one
    would otherwise escape because only direct ``str`` values were
    checked.
    """
    if attrs is None:
        return
    target = getattr(attrs, "_dict", attrs)
    if not hasattr(target, "items"):
        return
    from audittrace.logging_config import scrub_text

    for key, value in list(target.items()):
        if isinstance(value, str):
            target[key] = scrub_text(value)
        elif isinstance(value, (list, tuple)):
            target[key] = type(value)(
                scrub_text(v) if isinstance(v, str) else v for v in value
            )


class ScrubbingSpanProcessor:
    """D7 — the single export-time redaction chokepoint.

    ``on_end`` rewrites, in place on the ``ReadableSpan`` BEFORE any
    exporter runs: every ``str``/``Sequence[str]`` span attribute, every
    event's attributes (``exception.message``, ``exception.stacktrace``,
    any other), the status description, and (defensively) any link
    attributes. Nothing else is touched — name, kind, parent, timestamps
    and status CODE are untouched (ADDENDUM C's measured "nothing lost"
    property, G8).

    Registered FIRST via the public ``TracerProvider.add_span_processor``
    (S-C2) — ``SynchronousMultiSpanProcessor.on_end`` runs every
    registered processor IN ORDER on the same object, so whichever
    processor runs first fully determines what every later processor
    (Langfuse's own, our OTLP ``BatchSpanProcessor``, a test's in-memory
    exporter) observes. A batch exporter enqueues the span OBJECT, not a
    copy, so it exports whatever this processor already mutated,
    regardless of export timing (measured in ADDENDUM C: batch leak
    False across 21 batched spans).

    D6 (a ``build_middleware_stack`` shim that rewrote ``exc.args`` on
    the FastAPI SERVER span specifically) is RETIRED — this processor
    reaches that span too, since it is created through the same global
    provider, with no server.py changes needed.
    """

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        pass

    def _on_ending(self, span: Any) -> None:
        # Optional hook some SDK minors call before on_end; no-op here —
        # the actual scrub happens in on_end, once the span is final.
        pass

    def on_end(self, span: ReadableSpan) -> None:
        from audittrace.logging_config import scrub_text

        _scrub_bounded_attributes(getattr(span, "_attributes", None))
        for event in getattr(span, "_events", None) or ():
            _scrub_bounded_attributes(getattr(event, "_attributes", None))
        for link in getattr(span, "_links", None) or ():
            _scrub_bounded_attributes(
                getattr(link, "_attributes", getattr(link, "attributes", None))
            )
        status = getattr(span, "_status", None)
        description = getattr(status, "_description", None)
        if status is not None and description:
            try:
                status._description = scrub_text(description)
            except Exception:  # pragma: no cover - defensive
                pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _snapshot_span_for_self_test(span: ReadableSpan) -> str:
    """Serialise a span's attributes/events/status to a plain string.

    Used ONLY by the startup self-test's probe exporter. S-C3 — an
    exporter that merely stores a REFERENCE to the ``ReadableSpan`` can
    look "already scrubbed" later purely because the SAME mutable object
    was rewritten afterwards by a later-running processor; it proves
    nothing about ordering. Converting to a plain string INSIDE
    ``export()`` captures what is true at the moment this processor's
    turn in the chain actually runs — the only way to tell whether the
    scrubber really ran before it.
    """
    attrs = dict(getattr(span, "attributes", None) or {})
    events = [
        {"name": e.name, "attributes": dict(e.attributes or {})}
        for e in (getattr(span, "events", None) or ())
    ]
    status_desc = getattr(getattr(span, "status", None), "description", None)
    return json.dumps(
        {"attributes": attrs, "events": events, "status": status_desc},
        default=str,
    )


class _SerialisingProbeExporter:
    """Startup self-test exporter (S-C1b). Snapshots spans to plain
    strings at export time, then (after the one self-test call) goes
    inert so it costs nothing and holds no references for the rest of
    the process's life — it stays attached to the real provider
    forever (OTel has no public "remove a span processor" API), but an
    inert exporter touching nothing per span is negligible overhead.
    """

    def __init__(self) -> None:
        self.snapshots: list[str] = []
        self._active = True

    def export(self, spans: Any) -> SpanExportResult:
        from opentelemetry.sdk.trace.export import SpanExportResult

        if self._active:
            for span in spans:
                self.snapshots.append(_snapshot_span_for_self_test(span))
        return SpanExportResult.SUCCESS

    def disable(self) -> None:
        self._active = False
        self.snapshots.clear()

    def shutdown(self) -> None:  # pragma: no cover - SpanExporter protocol
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # pragma: no cover
        return True


def _run_startup_scrub_self_test(tracer_provider: TracerProvider) -> None:
    """Fail closed if the assembled provider does not actually redact.

    S-C1(b) — bounding the opentelemetry-api/-sdk pins (pyproject.toml /
    requirements.txt) to the minor this was tested against protects CI;
    it does nothing for an already-built IMAGE that resolves a newer
    minor at build time (the FINDING this whole spec traces back to:
    the deployed image picked up langfuse 4.17 a day after release,
    unbounded by CI's pin). A private-field rename in a later SDK
    (``_attributes``/``_events``/``_status``) would make
    ``ScrubbingSpanProcessor`` silently do nothing — ``BoundedAttributes``
    has no ``__slots__``, so a stale attribute name just creates a new,
    never-read one; nothing raises. This probe plants a registered
    secret through the REAL, fully-assembled provider (scrubber +
    Langfuse's processor if configured + the OTLP processor if
    configured) into a processor registered LAST, and refuses to start
    unless the secret comes out redacted. A regression in the mechanism
    — from this code OR from an SDK upgrade — reddens every subsequent
    request, not just this probe, so catching it at startup is the only
    place it is cheap.
    """
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from audittrace.identity import register_request_secret, reset_request_secrets

    probe_exporter = _SerialisingProbeExporter()
    # _SerialisingProbeExporter duck-types SpanExporter's protocol
    # (export/shutdown/force_flush) without importing the ABC at module
    # level — same defensive-import posture as the rest of this module.
    tracer_provider.add_span_processor(
        SimpleSpanProcessor(probe_exporter)  # type: ignore[arg-type]
    )

    marker = "audittrace-startup-self-test-" + uuid.uuid4().hex
    register_request_secret(marker)
    try:
        from opentelemetry.trace import Status, StatusCode

        tracer = tracer_provider.get_tracer("audittrace-startup-self-test")
        with tracer.start_as_current_span("startup-self-test") as span:
            span.set_attribute("input.value", f"planted {marker} planted")
            span.add_event("probe-event", {"detail": f"carries {marker}"})
            # Gate REJECT round 1 BLOCKER 2 — the probe span used to plant
            # the marker ONLY in an attribute and an event, so a later SDK
            # renaming the private field the scrubber reads for the
            # STATUS description (``_status``/``_description``) failed
            # OPEN: nothing in this self-test ever exercised that code
            # path. Plant it in the status too.
            span.set_status(Status(StatusCode.ERROR, f"status carries {marker}"))
    finally:
        reset_request_secrets()

    try:
        if not probe_exporter.snapshots:
            raise RuntimeError(
                "SEC-JWT-IN-LANGFUSE-SPAN startup self-test produced no "
                "probe span — refusing to start (the scrubbing mechanism "
                "could not be verified)"
            )
        _check_self_test_result(marker, probe_exporter.snapshots)
    finally:
        probe_exporter.disable()


def _check_self_test_result(marker: str, snapshots: list[str]) -> None:
    """The startup self-test's two pass/fail checks, extracted so each is
    independently unit-testable (gate REJECT round 1 BLOCKER 2 — the
    reviewer's neuter table showed BOTH checks vacuous when they lived
    only inline inside ``_run_startup_scrub_self_test``: a neuter of
    ``if marker in snapshot:`` alone stayed GREEN because, with the
    scrubber still functioning normally, the marker was never present
    either way — the check's OWN removal was unobservable from the full
    flow. Calling this function directly, with a hand-built snapshot,
    isolates each check from whether the scrubber happens to work.
    """
    snapshot = snapshots[-1]
    if marker in snapshot:
        raise RuntimeError(
            "SEC-JWT-IN-LANGFUSE-SPAN startup self-test FAILED: a "
            "planted secret reached a probe span processor "
            "unscrubbed — refusing to start. This guards against an "
            "SDK upgrade silently renaming the private fields the "
            "scrubber rewrites (S-C1)."
        )
    if "[REDACTED:bearer]" not in snapshot:
        raise RuntimeError(
            "SEC-JWT-IN-LANGFUSE-SPAN startup self-test FAILED: the "
            "redaction marker never appeared in the probe span — "
            "refusing to start."
        )


def _assert_scrubber_is_first(tracer_provider: TracerProvider) -> None:
    """S-C1(a) read-only pin: the scrubber must be the first span
    processor on the provider's chain. No private-list WRITE — this only
    asserts what the public-API construction order (S-C2) already
    guarantees.

    Extracted (gate REJECT round 1 BLOCKER 2) so a neuter of this check's
    own ``if`` condition reddens a DEDICATED unit test, independent of
    whether ``init_telemetry``'s broader flow happens to also exercise it.
    """
    processors = getattr(
        getattr(tracer_provider, "_active_span_processor", None),
        "_span_processors",
        (),
    )
    if not processors or not isinstance(processors[0], ScrubbingSpanProcessor):
        raise RuntimeError(
            "SEC-JWT-IN-LANGFUSE-SPAN: the scrubbing span processor is "
            "not first in the provider's processor chain — refusing "
            "to start (S-C1a)"
        )


def _init_langfuse_client(tracer_provider: TracerProvider | None = None) -> None:
    """Best-effort init of the Langfuse SDK client.

    SEC-JWT-IN-LANGFUSE-SPAN S-C2 — ``tracer_provider`` is OUR already-
    constructed provider (scrubber already attached at index 0 via the
    public API). Passing it to ``Langfuse(tracer_provider=...)`` makes
    its resource manager skip building its own provider and instead call
    ``tracer_provider.add_span_processor(langfuse_processor)`` — also the
    PUBLIC API (``langfuse/_client/resource_manager.py``, read in the
    venv) — so the Langfuse processor always lands AFTER the scrubber,
    with no private-list write needed on our side.
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

            # Probe routes (liveness/readiness/admin-metrics) never belong
            # in Langfuse. They carry a span name but no user identity,
            # so Langfuse's is_default_export_span lets them through on
            # the langfuse-sdk scope branch further down. Drop them up
            # front via either of two signals. Tempo still records them
            # in full. See the module-level _LANGFUSE_PROBE_DENYLIST
            # comment for background.
            if attrs.get("http.route") in _LANGFUSE_PROBE_DENYLIST:
                return False
            op = attrs.get("sovereign.operation", "")
            if isinstance(op, str) and op.startswith(_LANGFUSE_PROBE_OPERATION_PREFIX):
                return False

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
            tracer_provider=tracer_provider,
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

    SEC-JWT-IN-LANGFUSE-SPAN: ALWAYS builds and owns a single SDK
    ``TracerProvider`` — not conditionally, as before — because the
    scrubber must be attached FIRST via the public
    ``add_span_processor`` (S-C2), and because the startup self-test
    (S-C1) needs a real, fully-assembled provider to probe regardless
    of whether Tempo/Langfuse exporters are configured.
    """
    global _initialized, _tracer, _duration_histogram, _error_counter

    if _initialized:
        return

    _refuse_if_header_capture_configured()

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

    tracer_provider: Any = None
    if tracing_enabled:
        tracer_provider = TracerProvider(resource=resource)
        # D7/S-C2 — the scrubber is added FIRST, via the public API,
        # before anything else touches this provider. Every later
        # add_span_processor call (ours for OTLP, Langfuse's own) lands
        # strictly after it.
        tracer_provider.add_span_processor(ScrubbingSpanProcessor())
        trace.set_tracer_provider(tracer_provider)
        logger.info("App TracerProvider installed with scrubber at index 0")

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

    if tracing_enabled and tracer_provider is not None:
        # Langfuse SECOND — our scrubber is already index 0 (S-C2).
        _init_langfuse_client(tracer_provider)

        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                traces_endpoint = (
                    otlp_endpoint.rstrip("/") + "/v1/traces"
                    if not otlp_endpoint.endswith("/v1/traces")
                    else otlp_endpoint
                )
                tracer_provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=traces_endpoint))
                )
                logger.info("OTel tracing exporter -> %s", otlp_endpoint)
            except Exception as e:  # pragma: no cover
                logger.warning("Failed to configure OTLP span exporter: %s", e)

        # When Langfuse is enabled, tag our tracer with the scope name
        # "langfuse-sdk" and a public_key attribute so LangfuseSpanProcessor's
        # default filter (``is_default_export_span`` → ``is_langfuse_span``)
        # accepts every span we emit. Without this, Langfuse would drop
        # @log_call spans silently (they carry no ``gen_ai.*`` attrs and
        # wouldn't match any known-LLM-instrumentor prefix). Tempo is
        # unaffected — scope name doesn't change its trace grouping.
        if _langfuse_client is not None:
            public_key = os.environ.get("AUDITTRACE_LANGFUSE_PUBLIC_KEY", "")
            _tracer = tracer_provider.get_tracer(
                "langfuse-sdk",
                attributes={"public_key": public_key} if public_key else None,
            )
        else:
            _tracer = tracer_provider.get_tracer(service_name)

        # S-C1(b) — fail closed if the assembled chain does not actually
        # redact a planted, registered secret end to end.
        _run_startup_scrub_self_test(tracer_provider)
        # S-C1(a) — read-only pin: the scrubber must still be index 0
        # after everything else has registered. No private-list WRITE
        # happens here (S-C2) — this only asserts what the public-API
        # construction order above already guarantees.
        _assert_scrubber_is_first(tracer_provider)

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

    ``record_exception=False, set_status_on_exception=False`` — OTel's
    own automatic exception recording is disabled here because
    ``logging_config._record_span_error`` becomes the SINGLE exception
    recorder (ADDENDUM A §3 B2): letting both run recorded the exception
    TWICE per failing call (spec fact A2), the second time via the raw,
    unscrubbed ``__exit__`` path.
    """
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as span:
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
    """Reset module state. Used by tests only.

    Also resets OTel's own private, process-global "set tracer provider
    exactly once" guard (``opentelemetry.trace._TRACER_PROVIDER`` /
    ``_TRACER_PROVIDER_SET_ONCE``). Real OTel enforces set-once for good
    reason in production (one process, one provider) — but this test
    suite calls ``init_telemetry`` many times across many tests to probe
    different configurations (SEC-JWT-IN-LANGFUSE-SPAN made every call
    build + install its OWN fresh provider with the scrubber at index 0,
    per S-C1/S-C2). Without this reset, every call after the first would
    silently keep the FIRST test's provider, and ``telemetry._tracer``
    would end up bound to an orphan provider nobody else delegates to.
    Test-only; production calls ``init_telemetry`` exactly once per
    process and never calls this.
    """
    global _initialized, _tracer, _duration_histogram, _error_counter
    _initialized = False
    _tracer = None
    _duration_histogram = None
    _error_counter = None
    try:
        import opentelemetry.trace as _otel_trace_mod
        from opentelemetry.util._once import Once

        _otel_trace_mod._TRACER_PROVIDER = None
        _otel_trace_mod._TRACER_PROVIDER_SET_ONCE = Once()
    except Exception:  # pragma: no cover - defensive, test-only reset
        pass
