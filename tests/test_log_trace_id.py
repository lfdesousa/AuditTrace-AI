"""LOG-TRACE-ID WU-1 (v1.31.0 WP-3): every structured log line emitted inside a
request carries the server-minted ``trace_id`` / ``span_id`` (plus ``user_id`` /
``session_id`` where the request's ContextVars hold them), stamped by the
project-owned ``TraceContextFilter`` on the one stdout handler.

This file is the UNIT layer (spec section 7): the Filter and ``setup_logging``
/ ``init_telemetry`` through the REAL production bootstrap, never a hand-built
``LogRecord`` attribute (the 90803b0 lesson:
``test_structured_formatter_renames_otel_trace_fields`` set ``otelTraceID`` by
hand, so it never exercised the instrumentor and a vacuous fix stayed green for
five months). The INTEGRATION layer (``create_app()`` + ``TestClient``, and the
subprocess for the process-wide handler set) is ``test_log_trace_id_route.py``;
the two layers are not mixed. Shared bootstrap: ``tests/_log_trace_id_support.py``.

Mocks declared: ``InMemorySpanExporter`` / ``InMemoryMetricReader`` (OTel SDK
test doubles, not behaviour mocks); the counter-reentrancy test (AC20)
monkeypatches ``_log_records_counter`` with a recorder that logs from inside
``add``. Nothing here asserts on elapsed time.

Neuters (each run ALONE, RED recorded in the build record):
N1 no attach | N2 no-op filter | N3 fixed id | N4 re-add the contrib
instrumentor block | N4b the same with ``inject_trace_context`` | N5 drop the
filter's ``is_valid`` guard | N5b drop the helper's | N6 drop user/session |
N7 drop a pin / re-add the ``-logging`` line | N8 stop incrementing the
counter | N11 swap the lifespan calls | N12 drop the re-entrancy guard.
"""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import io
import json
import logging
import re
import tomllib
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from audittrace import logging_config, telemetry
from audittrace.identity import register_request_secret
from audittrace.logging_config import (
    TRACE_CONTEXT_FIELDS,
    TraceContextFilter,
)
from audittrace.services.recall_telemetry import emit_recall_telemetry
from tests._log_trace_id_support import (
    PROBE_LOG,
    SERVICE,
    SESSION,
    SUB,
    bootstrap,
    bound,
    in_span,
    isolated_logging_state,
    log_line,
    root_handler,
)

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
TRACE_SHAPE = re.compile(r"^[0-9a-f]{32}$")
SPAN_SHAPE = re.compile(r"^[0-9a-f]{16}$")
PRODUCT_KEYS = {"timestamp", "level", "logger", "message"}


@pytest.fixture
def product_logging() -> Iterator[io.StringIO]:
    with isolated_logging_state():
        yield bootstrap()


# ---------------------------------------------------------------------------
# unit: the Filter through the real bootstrap (AC1-AC5, AC7-AC9, AC12, AC20)
# ---------------------------------------------------------------------------
def test_ac1_memory_read_line_inside_a_span_carries_the_span_ids(
    product_logging: io.StringIO,
) -> None:
    """AC1. The REAL ``emit_recall_telemetry()`` ``memory.read`` line, inside a
    span, carries ``trace_id`` / ``span_id`` equal to the span's, shape-pinned.

    Neuters: N1 (no attach), N2 (no-op filter), N3 (fixed id) -> RED.
    """
    with in_span() as span:
        ctx = span.get_span_context()
        emit_recall_telemetry("tool", "decisions", 3)
    line = log_line(product_logging, "memory.read")
    assert line["trace_id"] == format(ctx.trace_id, "032x")
    assert line["span_id"] == format(ctx.span_id, "016x")
    assert TRACE_SHAPE.match(line["trace_id"])
    assert SPAN_SHAPE.match(line["span_id"])


def test_ac2_outside_a_span_and_unbound_no_field_is_present(
    product_logging: io.StringIO,
) -> None:
    """AC2. No valid span, nothing bound: none of the four keys (no ``"0"``
    sentinel; absent is absent).

    Neuters: N3 (fixed id), N4b-style injection, N5 (drop the filter's
    ``is_valid`` guard: ``span_id`` appears), N5b (drop the helper's: an
    all-zero ``trace_id`` appears) -> RED.
    """
    assert not trace.get_current_span().get_span_context().is_valid
    PROBE_LOG.info("outside.any.span")
    line = log_line(product_logging, "outside.any.span")
    assert set(line) == PRODUCT_KEYS
    assert not set(TRACE_CONTEXT_FIELDS) & set(line)


def test_ac3_bound_user_and_session_are_stamped_from_the_contextvars(
    product_logging: io.StringIO,
) -> None:
    """AC3. ``user_id`` / ``session_id`` come from the REAL ContextVars
    (``rls`` and the console-store binder), not a hand-set record attribute.

    Neuters: N2 (no-op filter), N6 (drop user/session) -> RED.
    """
    with bound(user=SUB, session=SESSION):
        PROBE_LOG.info("bound.line")
    line = log_line(product_logging, "bound.line")
    assert line["user_id"] == SUB
    assert line["session_id"] == SESSION


def test_ac4_the_record_factory_is_the_stdlib_one_afterbootstrap(
    product_logging: io.StringIO,
) -> None:
    """AC4. No library or instrumentor overrides the process record factory.

    Neuters: N4 / N4b (re-add the contrib ``LoggingInstrumentor``) -> RED. Run in
    the full ordered ``pytest tests/`` run so a leak from any earlier test also
    reddens it (ADDENDUM-1 C1).
    """
    assert logging.getLogRecordFactory() is logging.LogRecord


@pytest.mark.parametrize("structured", [True, False], ids=["json", "plain"])
def test_ac5_the_one_handler_carries_exactly_one_trace_context_filter(
    structured: bool,
) -> None:
    """AC5. In BOTH modes the single root handler's filters are exactly
    ``[TraceContextFilter]``, and ``setup_logging`` hands that same object to
    the startup self-test via ``_STDOUT_HANDLER``.

    Neuters: N1 (no attach) -> RED.
    """
    with isolated_logging_state():
        bootstrap(structured=structured)
        handler = root_handler()
        assert [type(f) for f in handler.filters] == [TraceContextFilter]
        assert logging_config._STDOUT_HANDLER is handler


def test_ac7_a_record_from_third_party_code_outside_any_span_is_never_stamped(
    product_logging: io.StringIO,
) -> None:
    """AC7. A non-request record (here: a library logger) carries none of the
    four keys. Neuters: N3, N5, N5b -> RED."""
    logging.getLogger("asyncio").warning("third.party.record")
    line = log_line(product_logging, "third.party.record")
    assert not set(TRACE_CONTEXT_FIELDS) & set(line)


def test_ac8_pii_scrub_still_applies_and_only_the_closed_field_set_is_added(
    product_logging: io.StringIO,
) -> None:
    """AC8. A registered request secret in the message is scrubbed, the line
    still carries ``trace_id``, and the emitted keys are the product keys plus
    ``TRACE_CONTEXT_FIELDS`` members only (no ``service`` / ``otelTraceSampled``,
    the contrib factory's extras).

    Neuters: N4b (re-add the instrumentor with injection) -> extra keys, RED.
    """
    secret = "audittrace-test-secret-" + uuid.uuid4().hex
    register_request_secret(secret)
    with in_span() as span, bound(user=SUB, session=SESSION):
        PROBE_LOG.info("carries %s here", secret)
        expected = format(span.get_span_context().trace_id, "032x")
    line = log_line(product_logging, "carries")
    assert secret not in json.dumps(line)
    assert "[REDACTED:" in line["message"]
    assert line["trace_id"] == expected
    assert set(line) <= PRODUCT_KEYS | set(TRACE_CONTEXT_FIELDS)


def test_ac9_explicit_extra_semantics(product_logging: io.StringIO) -> None:
    """AC9. The span's ids WIN over an explicit ``extra=`` of the same name; an
    explicit ``user_id`` is PRESERVED over the bound one.

    Neuters: N2 (no-op), N3 (fixed id) -> RED.
    """
    with in_span() as span, bound(user=SUB):
        PROBE_LOG.info(
            "extras.line", extra={"trace_id": "f" * 32, "user_id": "explicit-user"}
        )
        expected = format(span.get_span_context().trace_id, "032x")
    line = log_line(product_logging, "extras.line")
    assert line["trace_id"] == expected != "f" * 32
    assert line["user_id"] == "explicit-user"


def _series(reader: InMemoryMetricReader) -> dict[tuple[str, ...], int]:
    out: dict[tuple[str, ...], int] = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != "audittrace_log_records_total":
                    continue
                for point in metric.data.data_points:
                    key = tuple(sorted(point.attributes.items()))
                    out[tuple(f"{k}={v}" for k, v in key)] = int(point.value)
    return out


def test_ac12_the_metric_counts_user_bound_and_trace_present_per_record(
    product_logging: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC12. Exactly the two label keys; ``user_bound`` reads the RLS ContextVar
    (a different source from the span), so user_bound=true AND
    trace_present=false is "a request-scoped record lost its span".

    Neuters: N8 (stop incrementing), N2 (no-op filter), N6 (``user_bound``
    stuck false) -> RED.
    """
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("log-trace-id-test")
    monkeypatch.setattr(
        logging_config,
        "_log_records_counter",
        meter.create_counter("audittrace_log_records_total"),
    )
    with in_span(), bound(user=SUB):
        PROBE_LOG.info("series.true.true")
    with bound(user=SUB):
        PROBE_LOG.info("series.true.false")
    PROBE_LOG.info("series.false.false")
    assert _series(reader) == {
        ("trace_present=true", "user_bound=true"): 1,
        ("trace_present=false", "user_bound=true"): 1,
        ("trace_present=false", "user_bound=false"): 1,
    }


def test_ac20_a_record_logged_from_inside_the_counter_does_not_recurse(
    product_logging: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC20 (A4). The metrics SDK may log while counting; the ``_IN_FILTER``
    guard keeps that record from recursing, from being counted and from being
    stamped, and the OUTER record is still stamped.

    Neuter: N12 (remove the guard) -> RecursionError.
    """
    adds: list[dict[str, str]] = []

    class _ReentrantCounter:
        def add(self, amount: int, attributes: dict[str, str]) -> None:
            adds.append(attributes)
            PROBE_LOG.info("nested.record")

    monkeypatch.setattr(logging_config, "_log_records_counter", _ReentrantCounter())
    with in_span() as span:
        PROBE_LOG.info("outer.record")
        expected = format(span.get_span_context().trace_id, "032x")
    assert len(adds) == 1  # only the outer record was counted
    assert log_line(product_logging, "outer.record")["trace_id"] == expected
    assert "trace_id" not in log_line(product_logging, "nested.record")


def test_a_failing_counter_never_breaks_the_log_call_and_the_record_is_stamped(
    product_logging: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counting is best-effort: an exception from the metrics SDK (measured: a
    patched ``trace.get_current_span`` made the SDK's exemplar filter raise
    ``TypeError`` from inside ``Counter.add``) must not propagate out of
    ``logger.info`` and must not cost the record its stamp.

    Neuter N18: drop the ``contextlib.suppress`` around ``add`` -> RED.
    """

    class _BrokenCounter:
        def add(self, amount: int, attributes: dict[str, str]) -> None:
            raise TypeError("metrics SDK failure")

    monkeypatch.setattr(logging_config, "_log_records_counter", _BrokenCounter())
    with in_span() as span:
        PROBE_LOG.info("counter.fails")
        expected = format(span.get_span_context().trace_id, "032x")
    assert log_line(product_logging, "counter.fails")["trace_id"] == expected


def test_filter_obtains_the_trace_hex_from_the_one_helper(
    product_logging: io.StringIO,
) -> None:
    """Identity pin (ADDENDUM-1 C7/S9): the Filter calls
    ``telemetry.current_trace_id_hex`` and never formats ``ctx.trace_id`` itself,
    and the stamped value equals the helper's in the same record.

    Neuter: format ``ctx.trace_id`` inside the Filter -> RED.
    """
    tree = ast.parse((SRC / "audittrace" / "logging_config.py").read_text())
    cls = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "TraceContextFilter"
    )
    method = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "filter"
    )
    calls = [ast.unparse(n.func) for n in ast.walk(method) if isinstance(n, ast.Call)]
    assert "telemetry.current_trace_id_hex" in calls
    formatted = [
        ast.unparse(n)
        for n in ast.walk(method)
        if isinstance(n, ast.Call)
        and ast.unparse(n.func) == "format"
        and "trace_id" in ast.unparse(n)
    ]
    assert formatted == []
    with in_span():
        PROBE_LOG.info("helper.pin")
        helper_value = telemetry.current_trace_id_hex()
    assert log_line(product_logging, "helper.pin")["trace_id"] == helper_value


def test_constants_and_formatter_need_no_new_branch() -> None:
    """L1 chokepoint + AC-R6. The field set is the ratified tuple and none of
    its names is a reserved ``LogRecord`` attribute (the #457 merge loop renders
    them with no formatter change)."""
    assert TRACE_CONTEXT_FIELDS == ("trace_id", "span_id", "user_id", "session_id")
    assert not set(TRACE_CONTEXT_FIELDS) & logging_config._RESERVED_LOGRECORD_ATTRS


def test_ac_r7_the_plain_line_is_byte_identical_with_and_without_the_filter() -> None:
    """AC-R7. Plain (non-JSON) mode: the filter stamps attributes on the record
    but the format string is unchanged, so the line is byte-identical."""
    fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    def _render(with_filter: bool) -> str:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
        if with_filter:
            handler.addFilter(TraceContextFilter())
        record = logging.makeLogRecord(
            {"name": "plain", "msg": "plain.line", "levelno": 20, "created": 0.0}
        )
        record.levelname = "INFO"
        record.msecs = 0.0
        with in_span(), bound(user=SUB):
            handler.handle(record)
        return stream.getvalue()

    assert _render(True) == _render(False) != ""


# ---------------------------------------------------------------------------
# unit: the startup self-test (AC10, AC17, AC18)
# ---------------------------------------------------------------------------
class _NoStampFilter(TraceContextFilter):
    """A Filter that is present but stamps nothing (the N2 shape)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return True


def _init() -> None:
    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name=SERVICE,
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=True,
    )


def test_ac10_the_self_test_passes_with_the_filter_attached(
    product_logging: io.StringIO,
) -> None:
    """AC10 (positive). The bootstrap in the fixture already ran the self-test;
    running it again on a fresh init still passes."""
    _init()


@pytest.mark.parametrize(
    "neutered",
    [[], [_NoStampFilter()]],
    ids=["filter-missing", "filter-does-not-stamp"],
)
def test_ac10_init_telemetry_refuses_to_start_when_the_filter_is_neutered(
    product_logging: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
    neutered: list[logging.Filter],
) -> None:
    """AC10. ``init_telemetry`` raises ``RuntimeError`` (fail closed) when the
    handler ``setup_logging`` built has no Filter, or a Filter that does not
    stamp the live span's id.

    Neuters: N1 (no attach), N2 (no-op filter), N3 (fixed id) -> the production
    bootstrap raises, so every test that boots it is RED too.
    """
    handler = logging_config._STDOUT_HANDLER
    assert handler is not None
    monkeypatch.setattr(handler, "filters", neutered)
    with pytest.raises(RuntimeError, match="log-correlation self-test failed"):
        _init()


def test_ac17_init_telemetry_alone_logs_one_warning_and_adds_no_handler(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC17. When ``setup_logging`` never ran (``_STDOUT_HANDLER is None``) the
    self-test logs one WARNING and returns: no raise, no handler added.

    Neuter: make the self-test raise on ``None`` -> RED.
    """
    with isolated_logging_state():
        monkeypatch.setattr(logging_config, "_STDOUT_HANDLER", None)
        before = list(logging.getLogger().handlers)
        with caplog.at_level(logging.WARNING, logger="audittrace.telemetry"):
            _init()
        skipped = [r for r in caplog.records if "self-test skipped" in r.getMessage()]
        assert len(skipped) == 1
        assert logging.getLogger().handlers == before


def test_ac18_lifespan_calls_setup_logging_as_a_top_level_statement_before_init() -> (
    None
):
    """AC18 (+ gate AA1). ``setup_logging`` is a DIRECT statement of
    ``lifespan``'s body (not nested in an ``if`` / ``try``) and precedes
    ``telemetry.init_telemetry``, so the self-test's skip branch is unreachable
    in production.

    Neuter: swap the two calls -> RED.
    """
    tree = ast.parse((SRC / "audittrace" / "server.py").read_text())
    lifespan = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan"
    )

    def _top_level_call(name: str) -> int:
        hits = [
            i
            for i, stmt in enumerate(lifespan.body)
            if isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and ast.unparse(stmt.value.func) == name
        ]
        assert len(hits) == 1, (name, hits)
        return hits[0]

    assert _top_level_call("setup_logging") < _top_level_call(
        "telemetry.init_telemetry"
    )


# ---------------------------------------------------------------------------
# contract: pins and the removed dependency (AC11, AC19)
# ---------------------------------------------------------------------------
CONTRIB_PINNED = (
    "opentelemetry-instrumentation-fastapi",
    "opentelemetry-instrumentation-httpx",
    "opentelemetry-instrumentation-urllib3",
    "opentelemetry-instrumentation-sqlalchemy",
    "opentelemetry-instrumentation-redis",
)
CONTRIB_VERSION = "0.66b1"
API_PINNED = ("opentelemetry-api", "opentelemetry-sdk")
API_VERSION = "1.45.1"
REMOVED = "opentelemetry-instrumentation-logging"


def _pyproject_pins() -> dict[str, str]:
    deps = tomllib.loads((REPO / "pyproject.toml").read_text())["project"][
        "dependencies"
    ]
    pins: dict[str, str] = {}
    for dep in deps:
        match = re.fullmatch(r"([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==(\S+)", dep.strip())
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


def test_ac11_otel_pins_are_exact_in_pyproject_and_resolved_in_the_interpreter() -> (
    None
):
    """AC11 (ADDENDUM-DRIFT-93b659c section 1: exact ``==`` pins, not ranges).
    pyproject (read with ``tomllib``, a different source from
    ``importlib.metadata``) carries the exact pin for the five contrib packages
    and api/sdk, and the running interpreter resolves exactly it. The langfuse
    row is owned by its own test and only cited here (gate D2).

    Neuters: N7 (edit/drop one pin in pyproject) -> RED.
    """
    pins = _pyproject_pins()
    for name in CONTRIB_PINNED:
        assert pins.get(name) == CONTRIB_VERSION, name
        assert importlib.metadata.version(name) == CONTRIB_VERSION, name
    for name in API_PINNED:
        assert pins.get(name) == API_VERSION, name
        assert importlib.metadata.version(name) == API_VERSION, name


def test_ac11_the_logging_instrumentor_is_absent_from_every_lock_and_the_env() -> None:
    """AC11 absence clause + AC19. No ``opentelemetry-instrumentation-logging``
    requirement line in pyproject or the hashed locks (line-start match) and the
    module is not importable in the test interpreter.

    Neuter N7: re-add the line and relock -> RED.
    """
    for name in ("pyproject.toml", "requirements.txt", "requirements-dev.txt"):
        starts = [
            ln
            for ln in (REPO / name).read_text().splitlines()
            if re.match(rf'\s*"?{re.escape(REMOVED)}\b', ln)
        ]
        assert starts == [], (name, starts)
    assert importlib.util.find_spec("opentelemetry.instrumentation.logging") is None


def test_ac19_no_python_file_imports_the_logging_instrumentor() -> None:
    """AC19. 0 importers across src, tests, scripts and bff (``ast``, imports
    only - strings and comments do not count).

    Neuter: re-add the import to a source file -> RED.
    """
    importers: list[str] = []
    for root in ("src", "tests", "scripts", "bff"):
        for path in sorted((REPO / root).rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                mods = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
                if any(
                    m.startswith("opentelemetry.instrumentation.logging") for m in mods
                ):
                    importers.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert importers == []
    scanned = list((REPO / "src").rglob("*.py"))
    assert len(scanned) > 100  # positive control: the scanner saw the tree
