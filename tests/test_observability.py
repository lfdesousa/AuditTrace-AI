"""Tests for logging aspect + OpenTelemetry telemetry wiring (ADR-014.4)."""

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from sovereign_memory import telemetry
from sovereign_memory.logging_config import log_call, setup_logging


def test_set_current_span_attributes_routes_to_langfuse_sdk(monkeypatch):
    """ADR-024: when the Langfuse SDK is active, attributes must reach the SDK
    observation via update_current_span — not only the OTel tracer. The
    pre-fix code wrote OTel-only and Langfuse rendered the field as 'undefined'.
    """
    fake_lf = MagicMock()
    monkeypatch.setattr(telemetry, "_langfuse_client", fake_lf)
    try:
        telemetry.set_current_span_attributes(
            {
                "gen_ai.system": "llama.cpp",
                "input.value": "hello",
                "output.value": "world",
                "skip_me": None,
            }
        )
    finally:
        monkeypatch.setattr(telemetry, "_langfuse_client", None)

    # Metadata call: only non-None attributes
    metadata_call = fake_lf.update_current_span.call_args_list[0]
    assert metadata_call.kwargs["metadata"] == {
        "gen_ai.system": "llama.cpp",
        "input.value": "hello",
        "output.value": "world",
    }
    # Second call: input/output surfaced as first-class fields
    io_call = fake_lf.update_current_span.call_args_list[1]
    assert io_call.kwargs["input"] == "hello"
    assert io_call.kwargs["output"] == "world"


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

    try:
        telemetry._init_langfuse_client()
        assert telemetry._langfuse_client is fake_instance
        fake_lf_class.assert_called_once_with(
            public_key="pk-test",
            secret_key="sk-test",
            host="http://lf.test",
            tracing_enabled=True,
        )
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
        span_exporter_mock.assert_called_once_with(endpoint="http://otel.test")
        metric_exporter_mock.assert_called_once_with(endpoint="http://otel.test")
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
    """ADR-024 follow-up: the @log_call aspect must emit input.value even
    when a method is called with no positional args after self. The previous
    gate ``if payload:`` skipped these calls entirely, leaving Langfuse's
    Input panel rendering 'undefined' for spans like FileEpisodicService.load(self).
    """
    fake_lf = MagicMock()
    monkeypatch.setattr(telemetry, "_langfuse_client", fake_lf)

    class _Thing:
        @log_call(logger=logging.getLogger("sovereign_memory.tests.self_only"))
        def no_args_method(self):
            return ["item-a", "item-b"]

    try:
        _Thing().no_args_method()
    finally:
        monkeypatch.setattr(telemetry, "_langfuse_client", None)

    # Find the input write — must be present even though args[1:] is empty
    input_calls = [
        c for c in fake_lf.update_current_span.call_args_list if "input" in c.kwargs
    ]
    assert input_calls, (
        "input.value was not emitted for a self-only method — "
        "the len(args) > 1 gate is back"
    )
    # Empty payload renders as "{}", not undefined
    assert input_calls[0].kwargs["input"] == "{}"


def test_log_call_emits_output_for_none_returning_function(monkeypatch):
    """Functions that legitimately return None must still write output.value
    so the Langfuse Output panel renders 'null' instead of 'undefined'."""
    fake_lf = MagicMock()
    monkeypatch.setattr(telemetry, "_langfuse_client", fake_lf)

    @log_call(logger=logging.getLogger("sovereign_memory.tests.none_return"))
    def void_function(x):
        return None

    try:
        void_function("anything")
    finally:
        monkeypatch.setattr(telemetry, "_langfuse_client", None)

    output_calls = [
        c for c in fake_lf.update_current_span.call_args_list if "output" in c.kwargs
    ]
    assert output_calls, (
        "output.value was not emitted for a None-returning function — "
        "the early-return on `result is None` is back"
    )


def test_set_current_span_attributes_does_not_clobber_input_with_none(monkeypatch):
    """ADR-024 second-fix regression: the @log_call aspect calls
    set_current_span_attributes TWICE per span — once with input.value before
    the function runs, then with output.value after. The first version of the
    fix passed both kwargs unconditionally, so the second call's
    ``input=None`` cleared the input that the first call had set, leaving
    Langfuse's Input panel rendering 'undefined' for every memory layer span.

    This test asserts that two separate calls (input-only, then output-only)
    never pass a None kwarg to update_current_span — i.e. the second call
    must not have an ``input`` key at all.
    """
    fake_lf = MagicMock()
    monkeypatch.setattr(telemetry, "_langfuse_client", fake_lf)
    try:
        telemetry.set_current_span_attributes({"input.value": "the input"})
        telemetry.set_current_span_attributes({"output.value": "the output"})
    finally:
        monkeypatch.setattr(telemetry, "_langfuse_client", None)

    # Find the io kwargs calls (the second of each pair — first is metadata)
    io_calls = [
        call
        for call in fake_lf.update_current_span.call_args_list
        if "input" in call.kwargs or "output" in call.kwargs
    ]
    assert len(io_calls) == 2

    # First io call: input set, NO output key whatsoever
    assert io_calls[0].kwargs == {"input": "the input"}
    assert "output" not in io_calls[0].kwargs

    # Second io call: output set, NO input key whatsoever — this is the fix
    assert io_calls[1].kwargs == {"output": "the output"}
    assert "input" not in io_calls[1].kwargs


def test_start_span_routes_through_langfuse_sdk_when_initialised(monkeypatch):
    """start_span context manager must yield the Langfuse SDK observation
    when the SDK is set, so @log_call wrappers nest correctly."""
    fake_lf = MagicMock()
    sdk_span = MagicMock()
    fake_lf.start_as_current_observation.return_value.__enter__.return_value = sdk_span
    fake_lf.start_as_current_observation.return_value.__exit__.return_value = False
    monkeypatch.setattr(telemetry, "_langfuse_client", fake_lf)
    try:
        with telemetry.start_span("test-op", metadata={"k": "v"}) as span:
            assert span is sdk_span
    finally:
        monkeypatch.setattr(telemetry, "_langfuse_client", None)

    fake_lf.start_as_current_observation.assert_called_once_with(
        name="test-op", as_type="span", metadata={"k": "v"}
    )


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


def test_telemetry_start_span_returns_none_when_uninitialised():
    telemetry._reset_for_tests()
    with telemetry.start_span("whatever") as span:
        assert span is None
    # Re-init so downstream tests keep working
    telemetry.init_telemetry(
        service_name="sovereign-memory-server-tests", otlp_endpoint=""
    )
