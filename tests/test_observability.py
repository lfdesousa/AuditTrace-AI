"""Tests for logging aspect + OpenTelemetry telemetry wiring (ADR-014.4)."""

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from sovereign_memory import telemetry
from sovereign_memory.logging_config import log_call, setup_logging


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
    SOVEREIGN_LANGFUSE_ENABLED + keys + host are all present."""
    monkeypatch.setattr(telemetry, "_langfuse_client", None)
    monkeypatch.setenv("SOVEREIGN_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("SOVEREIGN_LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("SOVEREIGN_LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("SOVEREIGN_LANGFUSE_HOST", "http://lf.test")

    fake_lf_class = MagicMock()
    fake_instance = MagicMock()
    fake_lf_class.return_value = fake_instance

    import sys

    monkeypatch.setitem(sys.modules, "langfuse", MagicMock(Langfuse=fake_lf_class))
    monkeypatch.setitem(
        sys.modules,
        "langfuse.span_filter",
        MagicMock(is_default_export_span=lambda span: False),
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
        # should_export_span is our custom filter — accepts the FastAPI
        # /v1/chat/completions root span on top of Langfuse's defaults.
        filter_fn = call_kwargs["should_export_span"]
        chat_root = MagicMock(attributes={"http.route": "/v1/chat/completions"})
        other_span = MagicMock(attributes={"http.route": "/health"})
        assert filter_fn(chat_root) is True
        assert filter_fn(other_span) is False
        # Second call must early-return (idempotent guard at line 41-42)
        telemetry._init_langfuse_client()
        assert fake_lf_class.call_count == 1
    finally:
        monkeypatch.setattr(telemetry, "_langfuse_client", None)


def test_init_langfuse_client_skips_when_disabled(monkeypatch):
    """No SDK when SOVEREIGN_LANGFUSE_ENABLED is unset/false."""
    monkeypatch.setattr(telemetry, "_langfuse_client", None)
    monkeypatch.delenv("SOVEREIGN_LANGFUSE_ENABLED", raising=False)
    telemetry._init_langfuse_client()
    assert telemetry._langfuse_client is None


def test_init_langfuse_client_skips_when_keys_missing(monkeypatch):
    """SDK is skipped (with INFO log) when env flag is true but keys absent."""
    monkeypatch.setattr(telemetry, "_langfuse_client", None)
    monkeypatch.setenv("SOVEREIGN_LANGFUSE_ENABLED", "true")
    monkeypatch.delenv("SOVEREIGN_LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("SOVEREIGN_LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SOVEREIGN_LANGFUSE_HOST", raising=False)
    telemetry._init_langfuse_client()
    assert telemetry._langfuse_client is None


def test_init_telemetry_wires_otlp_exporters_when_endpoint_set(monkeypatch):
    """When otlp_endpoint is non-empty, init_telemetry must construct both
    OTLP span and metric exporters and attach them to the providers."""
    telemetry._reset_for_tests()
    monkeypatch.setattr(telemetry, "_langfuse_client", None)
    monkeypatch.delenv("SOVEREIGN_LANGFUSE_ENABLED", raising=False)

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
            service_name="sovereign-memory-server-tests",
            otlp_endpoint="",
            tracing_enabled=True,
            metrics_enabled=True,
        )


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
            service_name="sovereign-memory-server-tests",
            otlp_endpoint="",
            tracing_enabled=True,
            metrics_enabled=True,
        )


def test_log_call_emits_input_for_self_only_methods(monkeypatch):
    """The @log_call aspect must emit ``input.value`` (as an OTel attribute)
    even when a method is called with no positional args after self —
    ``{}`` is a meaningful display value, ``undefined`` is misleading.
    """
    fake_span = MagicMock()
    fake_span.is_recording.return_value = True
    import opentelemetry.trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_current_span", lambda: fake_span)
    monkeypatch.setattr(telemetry, "_tracer", MagicMock())

    class _Thing:
        @log_call(logger=logging.getLogger("sovereign_memory.tests.self_only"))
        def no_args_method(self):
            return ["item-a", "item-b"]

    _Thing().no_args_method()

    attrs_set = {
        call.args[0]: call.args[1] for call in fake_span.set_attribute.call_args_list
    }
    assert attrs_set.get("input.value") == "{}", (
        "input.value was not emitted for a self-only method — "
        "the len(args) > 1 gate is back"
    )
    # Mirrored for Langfuse's Input panel
    assert attrs_set.get("langfuse.observation.input") == "{}"


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

    @log_call(logger=logging.getLogger("sovereign_memory.tests.none_return"))
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
    logger = logging.getLogger("sovereign_memory.tests.sync")

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
    logger = logging.getLogger("sovereign_memory.tests.async")

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
    logger = logging.getLogger("sovereign_memory.tests.err")

    @log_call(logger=logger)
    def boom():
        raise ValueError("kapow")

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        with pytest.raises(ValueError, match="kapow"):
            boom()

    assert any("ERROR" in r.message for r in caplog.records if r.name == logger.name)


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
    logger = logging.getLogger("sovereign_memory.tests.bare")

    @log_call
    def echo(x):
        return x

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        assert echo("hi") == "hi"


def test_structured_formatter_emits_json():
    import json as _json

    from sovereign_memory.logging_config import StructuredFormatter

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


class TestRecordSpanError:
    """Tests for _record_span_error — dispatches to OTel or Langfuse APIs."""

    def test_otel_span_calls_record_exception(self):
        """OTel spans expose record_exception; it must be called."""
        from sovereign_memory.logging_config import _record_span_error

        span = MagicMock(spec=["record_exception"])
        exc = ValueError("boom")
        _record_span_error(span, exc)
        span.record_exception.assert_called_once_with(exc)

    def test_langfuse_span_calls_update_with_error_level(self):
        """Langfuse spans have update() but not record_exception()."""
        from sovereign_memory.logging_config import _record_span_error

        span = MagicMock(spec=["update"])
        exc = RuntimeError("crash")
        _record_span_error(span, exc)
        span.update.assert_called_once_with(
            level="ERROR", status_message="RuntimeError: crash"
        )

    def test_none_span_is_noop(self):
        """None span must not raise."""
        from sovereign_memory.logging_config import _record_span_error

        _record_span_error(None, ValueError("ignored"))

    def test_span_with_neither_method_is_noop(self):
        """A span that has neither record_exception nor update must not raise."""
        from sovereign_memory.logging_config import _record_span_error

        span = MagicMock(spec=[])
        _record_span_error(span, ValueError("ignored"))

    def test_failing_record_exception_is_swallowed(self):
        """If record_exception itself raises, the error must be swallowed."""
        from sovereign_memory.logging_config import _record_span_error

        span = MagicMock(spec=["record_exception"])
        span.record_exception.side_effect = RuntimeError("telemetry broken")
        # Must not raise
        _record_span_error(span, ValueError("original"))


def test_telemetry_start_span_returns_none_when_uninitialised():
    telemetry._reset_for_tests()
    with telemetry.start_span("whatever") as span:
        assert span is None
    # Re-init so downstream tests keep working
    telemetry.init_telemetry(
        service_name="sovereign-memory-server-tests", otlp_endpoint=""
    )
