"""Tests for logging aspect + OpenTelemetry telemetry wiring (ADR-014.4)."""

import asyncio
import logging
import sys
from unittest.mock import MagicMock

import pytest

from audittrace import telemetry
from audittrace.logging_config import log_call, setup_logging


def test_set_current_span_attributes_mirrors_langfuse_keys(monkeypatch):
    """Post-refactor (ADR-014.4 §Amendment 2026-04-14): the Langfuse SDK
    branching was removed. Attributes are written directly to the active
    OTel span; ``input.value`` / ``output.value`` are mirrored to
    ``langfuse.observation.input`` / ``.output`` so Langfuse's server-side
    attribute mapping still populates its Input/Output panels. None values
    are filtered so a second write never clobbers the first.
    """
    fake_span = MagicMock()
    fake_span.is_recording.return_value = True

    import opentelemetry.trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_current_span", lambda: fake_span)
    # _tracer must be truthy so set_current_span_attributes doesn't short-circuit
    monkeypatch.setattr(telemetry, "_tracer", MagicMock())

    telemetry.set_current_span_attributes(
        {
            "gen_ai.system": "llama.cpp",
            "input.value": "hello",
            "output.value": "world",
            "skip_me": None,
        }
    )

    attrs_set = {
        call.args[0]: call.args[1] for call in fake_span.set_attribute.call_args_list
    }
    assert attrs_set["gen_ai.system"] == "llama.cpp"
    assert attrs_set["input.value"] == "hello"
    assert attrs_set["output.value"] == "world"
    # Langfuse-specific mirrors for first-class panel rendering
    assert attrs_set["langfuse.observation.input"] == "hello"
    assert attrs_set["langfuse.observation.output"] == "world"
    # None values must not be set
    assert "skip_me" not in attrs_set


def test_init_langfuse_client_initialises_when_env_set(monkeypatch):
    """_init_langfuse_client should construct a Langfuse SDK instance when
    AUDITTRACE_LANGFUSE_ENABLED + keys + host are all present."""
    monkeypatch.setattr(telemetry, "_langfuse_client", None)
    monkeypatch.setenv("AUDITTRACE_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("AUDITTRACE_LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("AUDITTRACE_LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("AUDITTRACE_LANGFUSE_HOST", "http://lf.test")

    fake_lf_class = MagicMock()
    fake_instance = MagicMock()
    fake_lf_class.return_value = fake_instance

    import sys

    monkeypatch.setitem(sys.modules, "langfuse", MagicMock(Langfuse=fake_lf_class))
    # is_default_export_span mocked to ACCEPT (return True) so we actually
    # prove the denylist bites BEFORE the is_default_export_span accept
    # branch. Production's real is_default_export_span accepts any span
    # with a non-empty name, which is why /health (span name "health-check")
    # was leaking. Matching that behaviour in the test is the whole point.
    monkeypatch.setitem(
        sys.modules,
        "langfuse.span_filter",
        MagicMock(is_default_export_span=lambda span: True),
    )

    try:
        telemetry._init_langfuse_client()
        assert telemetry._langfuse_client is fake_instance
        assert fake_lf_class.call_count == 1
        call_kwargs = fake_lf_class.call_args.kwargs
        assert call_kwargs["public_key"] == "pk-test"
        assert call_kwargs["secret_key"] == "sk-test"
        assert call_kwargs["host"] == "http://lf.test"
        assert call_kwargs["tracing_enabled"] is True
        # should_export_span is our custom filter. Exercise every branch
        # with a realistic instrumentation_scope — a previous version of
        # this test left scope unset, which hid the "/health under
        # langfuse-sdk scope leaks" regression (see
        # project_langfuse_ux_regression).
        filter_fn = call_kwargs["should_export_span"]

        langfuse_scope = MagicMock()
        langfuse_scope.name = "langfuse-sdk"

        other_scope = MagicMock()
        other_scope.name = "httpx"

        chat_root = MagicMock(
            attributes={"http.route": "/v1/chat/completions"},
            instrumentation_scope=langfuse_scope,
        )
        # Two flavours of probe span, corresponding to the two emitters:
        # FastAPI auto-instrumentor (http.route) and the @log_call aspect
        # (sovereign.operation). Production's actual leak was the second
        # — the first is belt-and-suspenders.
        health_fastapi = MagicMock(
            attributes={"http.route": "/health"},
            instrumentation_scope=langfuse_scope,
        )
        health_log_call = MagicMock(
            attributes={
                "sovereign.operation": "audittrace.routes.health.health_check",
            },
            instrumentation_scope=langfuse_scope,
        )
        metrics_span = MagicMock(
            attributes={
                "sovereign.operation": "audittrace.routes.health.metrics",
            },
            instrumentation_scope=langfuse_scope,
        )
        user_tagged = MagicMock(
            attributes={"user.id": "alice-42", "http.route": "/somepath"},
            instrumentation_scope=langfuse_scope,
        )
        gen_ai_tagged = MagicMock(
            attributes={"gen_ai.system": "llama.cpp", "http.route": "/other"},
            instrumentation_scope=langfuse_scope,
        )
        # Bare langfuse-sdk-scoped span (no user tag, no gen_ai attrs) —
        # should reach the is_default_export_span accept branch and pass
        # because the mock returns True there.
        bare_langfuse = MagicMock(
            attributes={"http.route": "/audit/something"},
            instrumentation_scope=langfuse_scope,
        )
        # Span under a non-langfuse-sdk scope (e.g. httpx auto-span)
        # with no tagging — must be rejected. Guards ADR-045 PM's
        # intent that generic auto-spans stay Tempo-only.
        other_scope_span = MagicMock(
            attributes={"http.route": "/audit/something"},
            instrumentation_scope=other_scope,
        )

        assert filter_fn(chat_root) is True
        assert filter_fn(health_fastapi) is False, (
            "liveness/readiness FastAPI auto-spans carrying "
            "http.route='/health' must not reach Langfuse"
        )
        assert filter_fn(health_log_call) is False, (
            "@log_call inner spans carrying "
            "sovereign.operation='audittrace.routes.health.health_check' "
            "must not reach Langfuse — this is the production path that "
            "actually leaked, FastAPI-scoped spans don't make it here"
        )
        assert filter_fn(metrics_span) is False
        assert filter_fn(user_tagged) is True
        assert filter_fn(gen_ai_tagged) is True
        assert filter_fn(bare_langfuse) is True
        assert filter_fn(other_scope_span) is False
        # Second call must early-return (idempotent guard at line 41-42)
        telemetry._init_langfuse_client()
        assert fake_lf_class.call_count == 1
    finally:
        monkeypatch.setattr(telemetry, "_langfuse_client", None)


def test_init_langfuse_client_skips_when_disabled(monkeypatch):
    """No SDK when AUDITTRACE_LANGFUSE_ENABLED is unset/false."""
    monkeypatch.setattr(telemetry, "_langfuse_client", None)
    monkeypatch.delenv("AUDITTRACE_LANGFUSE_ENABLED", raising=False)
    telemetry._init_langfuse_client()
    assert telemetry._langfuse_client is None


def test_init_langfuse_client_skips_when_keys_missing(monkeypatch):
    """SDK is skipped (with INFO log) when env flag is true but keys absent."""
    monkeypatch.setattr(telemetry, "_langfuse_client", None)
    monkeypatch.setenv("AUDITTRACE_LANGFUSE_ENABLED", "true")
    monkeypatch.delenv("AUDITTRACE_LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("AUDITTRACE_LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("AUDITTRACE_LANGFUSE_HOST", raising=False)
    telemetry._init_langfuse_client()
    assert telemetry._langfuse_client is None


def test_init_telemetry_wires_otlp_exporters_when_endpoint_set(monkeypatch):
    """When otlp_endpoint is non-empty, init_telemetry must construct both
    OTLP span and metric exporters and attach them to the providers."""
    telemetry._reset_for_tests()
    monkeypatch.setattr(telemetry, "_langfuse_client", None)
    monkeypatch.delenv("AUDITTRACE_LANGFUSE_ENABLED", raising=False)

    span_exporter_mock = MagicMock()
    metric_exporter_mock = MagicMock()

    import sys

    fake_trace_mod = MagicMock()
    fake_trace_mod.OTLPSpanExporter = span_exporter_mock
    fake_metric_mod = MagicMock()
    fake_metric_mod.OTLPMetricExporter = metric_exporter_mock
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        fake_trace_mod,
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.http.metric_exporter",
        fake_metric_mod,
    )

    try:
        telemetry.init_telemetry(
            service_name="test-svc",
            otlp_endpoint="http://otel.test",
            tracing_enabled=True,
            metrics_enabled=True,
        )
        span_exporter_mock.assert_called_once_with(
            endpoint="http://otel.test/v1/traces"
        )
        metric_exporter_mock.assert_called_once_with(
            endpoint="http://otel.test/v1/metrics"
        )
    finally:
        telemetry._reset_for_tests()
        # Restore the no-op state used by the rest of the suite
        telemetry.init_telemetry(
            service_name="audittrace-server-tests",
            otlp_endpoint="",
            tracing_enabled=True,
            metrics_enabled=True,
        )


def _restore_suite_telemetry():
    """Put the module globals back into the no-op state the rest of the
    suite assumes (tracer installed, metrics installed, no exporters)."""
    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="audittrace-server-tests",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=True,
    )


def test_init_telemetry_skips_metrics_pipeline_when_metrics_disabled(monkeypatch):
    """``metrics_enabled=False`` must leave the histogram/counter uninstalled
    while tracing keeps working.

    Metrics are the optional half of the observability stack (operators turn
    them off on constrained nodes); tracing is not, because the EU AI Act
    Art. 12 trail rides on spans. If ``record_operation`` dereferenced a
    missing histogram, every ``@log_call`` in the process would raise and take
    the request down with it — a metrics opt-out would become an outage.
    """
    telemetry._reset_for_tests()
    monkeypatch.setattr(telemetry, "_langfuse_client", None)
    monkeypatch.delenv("AUDITTRACE_LANGFUSE_ENABLED", raising=False)
    try:
        telemetry.init_telemetry(
            service_name="metrics-off",
            otlp_endpoint="",
            tracing_enabled=True,
            metrics_enabled=False,
        )
        assert telemetry._duration_histogram is None
        assert telemetry._error_counter is None
        # Tracing must be unaffected — traceability does not depend on metrics.
        assert telemetry._tracer is not None
        # And the metric-recording call site must stay a silent no-op.
        telemetry.record_operation("metrics-off.op", 0.25, error="ValueError")
    finally:
        _restore_suite_telemetry()


def test_init_telemetry_leaves_tracer_unset_when_tracing_disabled(monkeypatch):
    """``tracing_enabled=False`` must leave ``_tracer`` unset so ``start_span``
    yields ``None`` and ``@log_call`` degrades to logging only.

    The degraded shape is what every span-consuming helper branches on. If
    ``_tracer`` were left populated by a half-initialised provider, the
    decorator would hand real span objects to code paths that were never
    exercised with tracing off, and a tracing opt-out would start raising
    inside the aspect wrapping every service call.
    """
    telemetry._reset_for_tests()
    monkeypatch.setattr(telemetry, "_langfuse_client", None)
    monkeypatch.delenv("AUDITTRACE_LANGFUSE_ENABLED", raising=False)
    try:
        telemetry.init_telemetry(
            service_name="tracing-off",
            otlp_endpoint="",
            tracing_enabled=False,
            metrics_enabled=False,
        )
        assert telemetry._tracer is None
        with telemetry.start_span("anything", metadata={"k": "v"}) as span:
            assert span is None

        # The aspect must still run the wrapped function and return its value.
        @log_call(logger=logging.getLogger("audittrace.tests.tracing_off"))
        def compute(x):
            return x * 2

        assert compute(21) == 42
    finally:
        _restore_suite_telemetry()


def test_init_telemetry_installs_own_provider_when_global_lacks_processor_api(
    monkeypatch,
):
    """Without Langfuse the global provider is OTel's ``ProxyTracerProvider``,
    which has no ``add_span_processor``. ``init_telemetry`` must then install
    its own SDK ``TracerProvider`` carrying the OTLP exporter.

    This is the Tempo-only deployment path. If the code only ever attached the
    processor to a pre-existing provider, disabling Langfuse would silently
    stop every span from reaching Tempo — the reconstructibility walkthrough
    (trace-ID hop) would have nothing to show, with no error anywhere.
    """
    telemetry._reset_for_tests()
    monkeypatch.setattr(telemetry, "_langfuse_client", None)
    monkeypatch.delenv("AUDITTRACE_LANGFUSE_ENABLED", raising=False)

    span_exporter_mock = MagicMock()
    fake_trace_mod = MagicMock()
    fake_trace_mod.OTLPSpanExporter = span_exporter_mock
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        fake_trace_mod,
    )

    import opentelemetry.trace as otel_trace

    class _ProviderWithoutProcessorApi:
        """Stand-in for ProxyTracerProvider: no add_span_processor."""

        def get_tracer(self, *a, **kw):  # pragma: no cover - not the path used
            return MagicMock()

    installed: list = []
    monkeypatch.setattr(
        otel_trace, "get_tracer_provider", lambda: _ProviderWithoutProcessorApi()
    )
    monkeypatch.setattr(otel_trace, "set_tracer_provider", installed.append)
    monkeypatch.setattr(otel_trace, "get_tracer", lambda name: MagicMock(name=name))

    try:
        telemetry.init_telemetry(
            service_name="tempo-only",
            otlp_endpoint="http://otel.test",
            tracing_enabled=True,
            metrics_enabled=False,
        )
        assert len(installed) == 1, "an app-owned TracerProvider must be installed"
        provider = installed[0]
        # The installed provider must actually carry our OTLP exporter —
        # installing an empty provider would look identical from the outside
        # but export nothing.
        exporters = [
            getattr(p, "span_exporter", None)
            for p in provider._active_span_processor._span_processors
        ]
        assert span_exporter_mock.return_value in exporters
        span_exporter_mock.assert_called_once_with(
            endpoint="http://otel.test/v1/traces"
        )
    finally:
        _restore_suite_telemetry()


def test_init_telemetry_tags_tracer_with_langfuse_scope(monkeypatch):
    """With the Langfuse SDK active, the app tracer must be created under the
    ``langfuse-sdk`` instrumentation scope carrying the ``public_key``.

    ``LangfuseSpanProcessor``'s default filter rejects spans emitted under any
    other scope (see ``_should_export_span``). Getting this wrong empties the
    Langfuse trace tree without a single error log — the failure mode is a
    dashboard that looks fine but has no observations to filter by user.
    """
    telemetry._reset_for_tests()
    monkeypatch.setattr(telemetry, "_langfuse_client", MagicMock())
    monkeypatch.setenv("AUDITTRACE_LANGFUSE_PUBLIC_KEY", "pk-scope-test")

    import opentelemetry.trace as otel_trace

    fake_provider = MagicMock()
    monkeypatch.setattr(otel_trace, "get_tracer_provider", lambda: fake_provider)

    try:
        telemetry.init_telemetry(
            service_name="langfuse-scoped",
            otlp_endpoint="",
            tracing_enabled=True,
            metrics_enabled=False,
        )
        fake_provider.get_tracer.assert_called_once_with(
            "langfuse-sdk", attributes={"public_key": "pk-scope-test"}
        )
        # And that scoped tracer is what @log_call will emit through.
        assert telemetry._tracer is fake_provider.get_tracer.return_value
    finally:
        monkeypatch.setattr(telemetry, "_langfuse_client", None)
        _restore_suite_telemetry()


def test_langfuse_filter_rejects_span_without_instrumentation_scope(monkeypatch):
    """A span whose ``instrumentation_scope`` is ``None`` has no scope name, so
    the filter must fall through to reject it.

    Spans reach that state when an exporter re-emits a span stripped of its
    scope. Treating "no scope" as acceptable would re-open the leak ADR-045
    closed: untagged third-party auto-spans (httpx, FastAPI probes) flooding
    the Langfuse trace tree and burying the @log_call observations operators
    actually reconstruct from.
    """
    monkeypatch.setattr(telemetry, "_langfuse_client", None)
    monkeypatch.setenv("AUDITTRACE_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("AUDITTRACE_LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("AUDITTRACE_LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("AUDITTRACE_LANGFUSE_HOST", "http://lf.test")

    fake_lf_class = MagicMock()
    monkeypatch.setitem(sys.modules, "langfuse", MagicMock(Langfuse=fake_lf_class))
    monkeypatch.setitem(
        sys.modules,
        "langfuse.span_filter",
        MagicMock(is_default_export_span=lambda span: True),
    )

    try:
        telemetry._init_langfuse_client()
        filter_fn = fake_lf_class.call_args.kwargs["should_export_span"]

        scopeless = MagicMock(attributes={}, instrumentation_scope=None)
        assert filter_fn(scopeless) is False

        # Control: the *only* difference is the scope, and it flips the
        # verdict — proving the rejection came from the scope check and not
        # from the empty attribute bag.
        langfuse_scope = MagicMock()
        langfuse_scope.name = "langfuse-sdk"
        scoped = MagicMock(attributes={}, instrumentation_scope=langfuse_scope)
        assert filter_fn(scoped) is True
    finally:
        monkeypatch.setattr(telemetry, "_langfuse_client", None)


def test_start_span_drops_none_metadata_values(monkeypatch):
    """``start_span`` must skip ``None`` metadata values instead of writing
    them onto the span.

    The OTel API rejects ``None`` attribute values. ``span_metadata`` is built
    by ``@log_call`` from values that can legitimately be absent (an unset
    ``langgraph_step`` ContextVar, an unclassified component). Passing one
    through makes the SDK complain and can abort the remaining metadata writes
    for that observation, so the Langfuse graph view loses the node.
    """
    fake_span = MagicMock()
    fake_tracer = MagicMock()
    fake_tracer.start_as_current_span.return_value.__enter__.return_value = fake_span
    monkeypatch.setattr(telemetry, "_tracer", fake_tracer)

    with telemetry.start_span("op", metadata={"kept": "yes", "dropped": None}) as span:
        assert span is fake_span

    written = {
        call.args[0]: call.args[1] for call in fake_span.set_attribute.call_args_list
    }
    assert written == {"langfuse.observation.metadata.kept": "yes"}, (
        "None-valued metadata must never reach span.set_attribute"
    )


def test_shutdown_flushes_traces_when_meter_provider_has_no_shutdown(monkeypatch):
    """``shutdown()`` must flush the TracerProvider even when the MeterProvider
    is a no-op object without ``shutdown()``.

    That is exactly the state after ``init_telemetry(metrics_enabled=False)``.
    An unguarded ``mp.shutdown()`` would raise ``AttributeError``, get
    swallowed by the outer ``except``, and the pending span batch would be lost
    on pod termination — dropping the trail for every in-flight request at
    exactly the moment (a rollout) an operator most needs it.
    """
    import opentelemetry.metrics as otel_metrics
    import opentelemetry.trace as otel_trace

    fake_tp = MagicMock()

    class _MeterProviderWithoutShutdown:
        pass

    monkeypatch.setattr(otel_trace, "get_tracer_provider", lambda: fake_tp)
    monkeypatch.setattr(
        otel_metrics, "get_meter_provider", lambda: _MeterProviderWithoutShutdown()
    )

    telemetry.shutdown()

    fake_tp.shutdown.assert_called_once_with()


def test_record_operation_emits_error_counter():
    """record_operation should bump the error counter when an error type is given."""
    # No-op in test mode (histogram + counter exist as MeterProvider primitives),
    # but the call must not raise.
    telemetry.record_operation("test.op", 0.123, error="ValueError")
    telemetry.record_operation("test.op", 0.456, error=None)


def test_shutdown_invokes_provider_shutdown():
    """shutdown() should call provider.shutdown() when available — best-effort."""
    # Just exercise the path; the providers are real but no-op exporters in tests.
    telemetry.shutdown()


def test_set_current_span_attributes_no_op_when_uninitialised(monkeypatch):
    """When neither Langfuse SDK nor OTel tracer are configured, the helper
    must early-return without raising."""
    monkeypatch.setattr(telemetry, "_langfuse_client", None)
    monkeypatch.setattr(telemetry, "_tracer", None)
    try:
        telemetry.set_current_span_attributes({"input.value": "x"})
    finally:
        # Restore test session tracer
        telemetry._reset_for_tests()
        telemetry.init_telemetry(
            service_name="audittrace-server-tests",
            otlp_endpoint="",
            tracing_enabled=True,
            metrics_enabled=True,
        )


def test_log_call_emits_input_for_self_only_methods(monkeypatch):
    """The @log_call aspect must emit ``input.value`` (as an OTel attribute)
    even when a method is called with no positional args after self. The
    value must be a *meaningful* placeholder (Langfuse renders ``{}`` as
    empty, so we write ``{"called_with": "no arguments"}`` instead).
    """
    fake_span = MagicMock()
    fake_span.is_recording.return_value = True
    import opentelemetry.trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_current_span", lambda: fake_span)
    monkeypatch.setattr(telemetry, "_tracer", MagicMock())

    class _Thing:
        @log_call(logger=logging.getLogger("audittrace.tests.self_only"))
        def no_args_method(self):
            return ["item-a", "item-b"]

    _Thing().no_args_method()

    attrs_set = {
        call.args[0]: call.args[1] for call in fake_span.set_attribute.call_args_list
    }
    input_val = attrs_set.get("input.value", "")
    assert "no arguments" in input_val, (
        "self-only methods must render a meaningful placeholder in Langfuse, "
        f"got {input_val!r}"
    )
    # Mirrored for Langfuse's Input panel
    assert attrs_set.get("langfuse.observation.input") == input_val


def test_log_call_emits_output_for_none_returning_function(monkeypatch):
    """Functions that legitimately return ``None`` must still write
    ``output.value`` (serialised as JSON ``null``) so downstream tooling
    shows the real outcome rather than ``undefined``.
    """
    fake_span = MagicMock()
    fake_span.is_recording.return_value = True
    import opentelemetry.trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_current_span", lambda: fake_span)
    monkeypatch.setattr(telemetry, "_tracer", MagicMock())

    @log_call(logger=logging.getLogger("audittrace.tests.none_return"))
    def void_function(x):
        return None

    void_function("anything")

    attrs_set = {
        call.args[0]: call.args[1] for call in fake_span.set_attribute.call_args_list
    }
    assert attrs_set.get("output.value") == "null", (
        "output.value was not emitted for a None-returning function — "
        "the early-return on `result is None` is back"
    )
    # Mirrored for Langfuse's Output panel
    assert attrs_set.get("langfuse.observation.output") == "null"


def test_set_current_span_attributes_skips_none_values(monkeypatch):
    """Post-refactor: ``None`` values are dropped at the OTel-API call site
    rather than cleared via a second-call clobber. Two separate calls
    (input-only, then output-only) must leave both values intact on the
    underlying span.
    """
    fake_span = MagicMock()
    fake_span.is_recording.return_value = True
    import opentelemetry.trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_current_span", lambda: fake_span)
    monkeypatch.setattr(telemetry, "_tracer", MagicMock())

    telemetry.set_current_span_attributes({"input.value": "the input"})
    telemetry.set_current_span_attributes({"output.value": "the output"})

    attrs_set: dict[str, object] = {}
    for call in fake_span.set_attribute.call_args_list:
        attrs_set[call.args[0]] = call.args[1]

    assert attrs_set["input.value"] == "the input"
    assert attrs_set["output.value"] == "the output"
    assert attrs_set["langfuse.observation.input"] == "the input"
    assert attrs_set["langfuse.observation.output"] == "the output"
    # No None writes — would have clobbered prior values
    for call in fake_span.set_attribute.call_args_list:
        assert call.args[1] is not None


def test_start_span_uses_otel_tracer(monkeypatch):
    """Post-refactor: ``start_span`` always routes through the global OTel
    tracer. Metadata keys are stamped with the ``langfuse.observation.metadata.``
    prefix so Langfuse's server-side mapping surfaces them.
    """
    fake_span = MagicMock()
    ctx_mgr = MagicMock()
    ctx_mgr.__enter__ = MagicMock(return_value=fake_span)
    ctx_mgr.__exit__ = MagicMock(return_value=False)

    fake_tracer = MagicMock()
    fake_tracer.start_as_current_span.return_value = ctx_mgr
    monkeypatch.setattr(telemetry, "_tracer", fake_tracer)

    with telemetry.start_span("test-op", metadata={"node": "foo", "step": 1}) as span:
        assert span is fake_span

    fake_tracer.start_as_current_span.assert_called_once_with("test-op")
    stamped = {
        call.args[0]: call.args[1] for call in fake_span.set_attribute.call_args_list
    }
    assert stamped["langfuse.observation.metadata.node"] == "foo"
    assert stamped["langfuse.observation.metadata.step"] == 1


def test_setup_logging_uses_stdout_only():
    """All handlers must be StreamHandlers to stdout — no file I/O."""
    import sys

    setup_logging(level="DEBUG")
    root = logging.getLogger()
    assert root.handlers, "root logger has no handlers"
    for h in root.handlers:
        assert isinstance(h, logging.StreamHandler)
        assert getattr(h, "stream", None) is sys.stdout


def test_log_call_emits_input_output_duration(caplog):
    """The aspect must produce INPUT/OUTPUT/DURATION records."""
    logger = logging.getLogger("audittrace.tests.sync")

    @log_call(logger=logger)
    def add(a, b):
        return a + b

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        result = add(2, 3)

    assert result == 5
    messages = [r.message for r in caplog.records if r.name == logger.name]
    assert any(m.startswith("INPUT ") for m in messages)
    assert any(m.startswith("OUTPUT ") for m in messages)
    assert any(m.startswith("DURATION ") for m in messages)


def test_log_call_works_on_async_functions(caplog):
    logger = logging.getLogger("audittrace.tests.async")

    @log_call(logger=logger)
    async def aop(x):
        await asyncio.sleep(0)
        return x * 2

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        result = asyncio.run(aop(21))

    assert result == 42
    msgs = [r.message for r in caplog.records if r.name == logger.name]
    assert any(m.startswith("DURATION ") for m in msgs)


def test_log_call_logs_and_reraises_errors(caplog):
    logger = logging.getLogger("audittrace.tests.err")

    @log_call(logger=logger)
    def boom():
        raise ValueError("kapow")

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        with pytest.raises(ValueError, match="kapow"):
            boom()

    assert any("ERROR" in r.message for r in caplog.records if r.name == logger.name)


def test_log_call_4xx_httpexception_warns_without_traceback(caplog):
    """Pentest F-L1: an expected 4xx HTTPException (bad/expired token, missing
    scope, not found, validation) must be logged concisely at WARNING with NO
    stack trace — not ERROR+traceback — so attacker-controllable 4xx cannot
    flood ERROR logs or disclose internal paths."""
    from fastapi import HTTPException

    logger = logging.getLogger("audittrace.tests.client_err")

    @log_call(logger=logger)
    async def deny():
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        with pytest.raises(HTTPException):
            asyncio.run(deny())

    recs = [r for r in caplog.records if r.name == logger.name]
    # No ERROR record from the wrapper, and no traceback attached.
    assert not any(r.levelno >= logging.ERROR for r in recs), (
        "4xx must not log at ERROR"
    )
    warns = [r for r in recs if r.levelno == logging.WARNING]
    assert warns, "expected a WARNING for the 4xx"
    assert all(r.exc_info is None for r in warns), "WARNING must carry no traceback"


def test_log_call_5xx_httpexception_still_errors_with_traceback(caplog):
    """A 5xx HTTPException is a real server fault and must still log at ERROR
    with the traceback (only 4xx are downgraded)."""
    from fastapi import HTTPException

    logger = logging.getLogger("audittrace.tests.server_err")

    @log_call(logger=logger)
    async def fault():
        raise HTTPException(status_code=502, detail="upstream boom")

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        with pytest.raises(HTTPException):
            asyncio.run(fault())

    recs = [r for r in caplog.records if r.name == logger.name]
    errs = [r for r in recs if r.levelno >= logging.ERROR]
    assert errs, "5xx must still log at ERROR"
    assert any(r.exc_info is not None for r in errs), "5xx ERROR must carry traceback"


def test_log_call_sync_4xx_httpexception_warns_without_traceback(caplog):
    """The same F-L1 downgrade must hold for SYNC decorated callables.

    ``log_call`` builds two independent wrappers (async and sync) with
    duplicated error handling. Most auth/scope guards that raise 4xx are plain
    sync functions, so a divergence here means the very call sites the
    downgrade was written for keep emitting ERROR+traceback on
    attacker-controllable input.
    """
    from fastapi import HTTPException

    logger = logging.getLogger("audittrace.tests.sync_client_err")

    @log_call(logger=logger)
    def deny_sync(token: str):
        raise HTTPException(status_code=403, detail="missing scope memory:write")

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        with pytest.raises(HTTPException):
            deny_sync("bad-token")

    recs = [r for r in caplog.records if r.name == logger.name]
    assert not any(r.levelno >= logging.ERROR for r in recs), (
        "sync 4xx must not log at ERROR"
    )
    warns = [r for r in recs if r.levelno == logging.WARNING]
    assert warns, "expected a WARNING for the sync 4xx"
    assert all(r.exc_info is None for r in warns), "WARNING must carry no traceback"
    # The operation is still identified, so the WARNING remains actionable.
    assert all(r.operation.endswith("deny_sync") for r in warns)


def test_log_call_payload_opt_out_keeps_span_but_drops_input_and_output(monkeypatch):
    """``include_input=False`` / ``include_output=False`` must suppress the
    payload attributes while still emitting a classified span.

    This is the opt-out used by call sites that handle secrets or PII (token
    exchange, credential lookup): they stay observable in Langfuse/Tempo
    without shipping their arguments and return values to the trace backend.
    If the flags only gated the DEBUG log lines, every such call would leak its
    payload into an external observability store.
    """
    fake_span = MagicMock()
    fake_span.is_recording.return_value = True
    fake_tracer = MagicMock()
    fake_tracer.start_as_current_span.return_value.__enter__.return_value = fake_span

    import opentelemetry.trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_current_span", lambda: fake_span)
    monkeypatch.setattr(telemetry, "_tracer", fake_tracer)

    @log_call(
        logger=logging.getLogger("audittrace.tests.optout"),
        include_input=False,
        include_output=False,
    )
    def exchange_token(client_secret):
        return "access-token-abc"

    assert exchange_token("s3cret-value") == "access-token-abc"

    attrs = {
        call.args[0]: call.args[1] for call in fake_span.set_attribute.call_args_list
    }
    assert "input.value" not in attrs
    assert "output.value" not in attrs
    # And neither Langfuse mirror key was written either.
    assert "langfuse.observation.input" not in attrs
    assert "langfuse.observation.output" not in attrs
    # The secret / token never appear under any attribute key.
    assert not any(
        isinstance(v, str) and ("s3cret-value" in v or "access-token-abc" in v)
        for v in attrs.values()
    )
    # The span is still emitted and classified — observability is not lost,
    # only the payload is.
    assert attrs["sovereign.operation"].endswith("exchange_token")


def test_structured_formatter_renames_otel_trace_fields(monkeypatch):
    """``otelTraceID`` / ``otelSpanID`` / ``otelServiceName`` must be renamed to
    ``trace_id`` / ``span_id`` / ``service`` in the JSON line.

    Those are the exact field names the reconstructibility walkthrough's Loki
    query filters on (``| json | trace_id="..."``). OTel's LoggingInstrumentor
    only ever sets the SDK-internal names, so without this rename an operator
    holding a trace ID from Tempo cannot pivot to the matching log lines and
    the log↔trace hop of the audit trail breaks.
    """
    import json as _json

    from audittrace.logging_config import StructuredFormatter

    fmt = StructuredFormatter()
    record = logging.LogRecord(
        name="audittrace.routes.chat",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="chat completed",
        args=(),
        exc_info=None,
    )
    record.otelTraceID = "4bf92f3577b34da6a3ce929d0e0e4736"
    record.otelSpanID = "00f067aa0ba902b7"
    record.otelServiceName = "audittrace-server"

    data = _json.loads(fmt.format(record))
    assert data["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert data["span_id"] == "00f067aa0ba902b7"
    assert data["service"] == "audittrace-server"
    # The SDK-internal names must not also be emitted — Loki would index two
    # labels for the same value.
    assert "otelTraceID" not in data
    assert "otelSpanID" not in data


def test_structured_formatter_preserves_stack_info():
    """``stack_info=True`` must survive into the JSON line under ``stack_info``.

    Callers pass ``stack_info=True`` when the interesting part is *where the
    call came from*, not an exception — the diagnostic for silent misbehaviour
    with no raise (e.g. an unexpected re-entry into a worker loop). Dropping it
    leaves such a log line with no way to identify the caller, which is the
    only reason it was emitted.
    """
    import json as _json

    from audittrace.logging_config import StructuredFormatter

    fmt = StructuredFormatter()
    record = logging.LogRecord(
        name="audittrace.workers.summariser",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="unexpected re-entry",
        args=(),
        exc_info=None,
    )
    record.stack_info = 'File "worker.py", line 12, in run\n    loop()'

    data = _json.loads(fmt.format(record))
    assert "stack_info" in data
    assert "worker.py" in data["stack_info"]
    # A stack-info line is not an exception line — the two fields are distinct.
    assert "exception" not in data


def test_telemetry_init_is_idempotent_and_noop_without_endpoint():
    telemetry._reset_for_tests()
    telemetry.init_telemetry(service_name="test-svc", otlp_endpoint="")
    telemetry.init_telemetry(service_name="test-svc", otlp_endpoint="")  # idempotent
    # start_span should yield a span object (no-op provider still gives one)
    with telemetry.start_span("unit-test-op") as span:
        assert span is not None
    # record_operation must not raise with or without an error argument
    telemetry.record_operation("unit-test-op", 0.01, None)
    telemetry.record_operation("unit-test-op", 0.02, "ValueError")


def test_log_call_bare_form_without_parentheses(caplog):
    logger = logging.getLogger("audittrace.tests.bare")

    @log_call
    def echo(x):
        return x

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        assert echo("hi") == "hi"


def test_structured_formatter_emits_json():
    import json as _json

    from audittrace.logging_config import StructuredFormatter

    fmt = StructuredFormatter()
    record = logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.operation = "op"
    line = fmt.format(record)
    data = _json.loads(line)
    assert data["message"] == "hello"
    assert data["operation"] == "op"
    # No exception → no exception field.
    assert "exception" not in data


def test_structured_formatter_preserves_exception_traceback():
    """logger.exception()/exc_info=True must survive into the JSON line.

    Regression: the formatter previously dropped record.exc_info, so a
    background worker's only failure signal carried just the message and the
    stack was lost — which masked the 2026-06-09 summariser poison-pill root
    cause. The traceback must now appear under an ``exception`` field.
    """
    import json as _json

    from audittrace.logging_config import StructuredFormatter

    fmt = StructuredFormatter()
    try:
        raise ValueError("boom-detail")
    except ValueError:
        record = logging.LogRecord(
            name="x",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="it failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    data = _json.loads(fmt.format(record))
    assert data["message"] == "it failed"
    assert "exception" in data
    assert "ValueError" in data["exception"]
    assert "boom-detail" in data["exception"]
    assert "Traceback" in data["exception"]


class TestRecordSpanError:
    """Tests for _record_span_error — dispatches to OTel or Langfuse APIs."""

    def test_otel_span_calls_record_exception(self):
        """OTel spans expose record_exception; it must be called."""
        from audittrace.logging_config import _record_span_error

        span = MagicMock(spec=["record_exception"])
        exc = ValueError("boom")
        _record_span_error(span, exc)
        span.record_exception.assert_called_once_with(exc)

    def test_langfuse_span_calls_update_with_error_level(self):
        """Langfuse spans have update() but not record_exception()."""
        from audittrace.logging_config import _record_span_error

        span = MagicMock(spec=["update"])
        exc = RuntimeError("crash")
        _record_span_error(span, exc)
        span.update.assert_called_once_with(
            level="ERROR", status_message="RuntimeError: crash"
        )

    def test_none_span_is_noop(self):
        """None span must not raise."""
        from audittrace.logging_config import _record_span_error

        _record_span_error(None, ValueError("ignored"))

    def test_span_with_neither_method_is_noop(self):
        """A span that has neither record_exception nor update must not raise."""
        from audittrace.logging_config import _record_span_error

        span = MagicMock(spec=[])
        _record_span_error(span, ValueError("ignored"))

    def test_failing_record_exception_is_swallowed(self):
        """If record_exception itself raises, the error must be swallowed."""
        from audittrace.logging_config import _record_span_error

        span = MagicMock(spec=["record_exception"])
        span.record_exception.side_effect = RuntimeError("telemetry broken")
        # Must not raise
        _record_span_error(span, ValueError("original"))


def test_telemetry_start_span_returns_none_when_uninitialised():
    telemetry._reset_for_tests()
    with telemetry.start_span("whatever") as span:
        assert span is None
    # Re-init so downstream tests keep working
    telemetry.init_telemetry(service_name="audittrace-server-tests", otlp_endpoint="")
