"""G3 / G3b - a declined instrumentor is an ERROR, a metric and (strict) a stop.

SQLALCHEMY-SPANS (operator directive 2026-10-19, element 3: fail loud at
runtime). ``BaseInstrumentor.instrument()`` is fail-open by design; the single
chokepoint ``telemetry.attach_instrumentor`` (``audittrace/instrumentation.py``)
turns "attached nothing" into one stable log key ``instrumentation.attach``
with ``instrumentor`` / ``result`` / ``conflict`` fields, the counter
``audittrace.instrumentation.attached_total{instrumentor,result}`` and, under
``instrumentation_strict`` (default true), an ``InstrumentationError``.

G3b is the chokepoint-coverage guard (C5): every ``.instrument`` /
``.instrument_app`` call in ``src/`` sits inside ``attach_instrumentor``'s own
body (stdlib ``ast`` parity with the CODE-INDEX ``calls_with_caller`` query).

Neuters (each applied alone, recorded in the build record):
N7  drop ``raise_exception_on_conflict=True`` (conflict read) -> declined/strict RED
N8  drop the counter increment                               -> metric test RED
N9  drop the ERROR log                                       -> log test RED
N10 route ``db/postgres.py``'s attach around the chokepoint  -> G3b RED
N11 drop the ``disabled`` branch                             -> disabled test RED
N12 drop the INFO success log                                -> attached-log test RED
"""

from __future__ import annotations

import ast
import logging
import pathlib
from collections.abc import Collection, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import create_engine

from audittrace import config, instrumentation, telemetry
from audittrace.config import Settings
from audittrace.instrumentation import (
    AttachResult,
    InstrumentationError,
    attach_instrumentor,
)

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
OUT_OF_RANGE = ("sqlalchemy < 1.0",)


def _fake_instrumentor(
    deps: Collection[str] = (), boom: bool = False
) -> BaseInstrumentor:
    """A throwaway singleton instrumentor class (its own ``_instance``)."""

    class _Fake(BaseInstrumentor):
        _instance = None
        _is_instrumented_by_opentelemetry = False
        calls = 0

        def instrumentation_dependencies(self) -> Collection[str]:
            return deps

        def _instrument(self, **kwargs: Any) -> None:
            type(self).calls += 1
            if boom:
                raise RuntimeError("kaboom")

        def _uninstrument(self, **kwargs: Any) -> None:
            return None

    return _Fake()


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    """Isolate the chokepoint registries but leave the process as found: the
    global instrumentors other tests attached stay owned (restored on exit)."""
    owned = set(instrumentation._OWNED)
    instrumentation._reset_for_tests()
    yield
    instrumentation._reset_for_tests()
    instrumentation._OWNED.update(owned)


@pytest.fixture
def metric_reader(monkeypatch: pytest.MonkeyPatch) -> InMemoryMetricReader:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    counter = provider.get_meter("test").create_counter(
        "audittrace.instrumentation.attached_total"
    )
    monkeypatch.setattr(instrumentation, "_attach_total", counter)
    return reader


def _points(reader: InMemoryMetricReader) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for pt in metric.data.data_points:
                    key = (pt.attributes["instrumentor"], pt.attributes["result"])
                    out[key] = out.get(key, 0) + int(pt.value)
    return out


def _attach_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "instrumentation.attach" in r.getMessage()]


# ---------------------------------------------------------------- G3 ----------
def test_declined_instrumentor_is_an_error_and_a_metric(
    metric_reader: InMemoryMetricReader,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inst = SQLAlchemyInstrumentor()
    assert not inst.is_instrumented_by_opentelemetry
    monkeypatch.setattr(
        SQLAlchemyInstrumentor,
        "instrumentation_dependencies",
        lambda self: OUT_OF_RANGE,
    )
    engine = create_engine("sqlite://")
    with caplog.at_level(logging.INFO, logger="audittrace.instrumentation"):
        result = attach_instrumentor(
            "SQLAlchemyInstrumentor", inst, engine=engine, strict=False, enabled=True
        )
    assert result is AttachResult.DECLINED
    assert not inst.is_instrumented_by_opentelemetry
    (rec,) = _attach_records(caplog)
    assert rec.levelno == logging.ERROR
    assert (rec.instrumentor, rec.result) == ("SQLAlchemyInstrumentor", "declined")  # type: ignore[attr-defined]
    assert "sqlalchemy < 1.0" in rec.conflict  # type: ignore[attr-defined]
    assert _points(metric_reader) == {("SQLAlchemyInstrumentor", "declined"): 1}


def test_declined_sqlalchemy_raises_under_strict_when_tracing_enabled(
    metric_reader: InMemoryMetricReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        SQLAlchemyInstrumentor,
        "instrumentation_dependencies",
        lambda self: OUT_OF_RANGE,
    )
    with pytest.raises(InstrumentationError, match="declined"):
        attach_instrumentor(
            "SQLAlchemyInstrumentor",
            SQLAlchemyInstrumentor(),
            engine=create_engine("sqlite://"),
            strict=True,
            enabled=True,
        )
    assert _points(metric_reader) == {("SQLAlchemyInstrumentor", "declined"): 1}


def test_strict_with_tracing_disabled_starts_cleanly(
    metric_reader: InMemoryMetricReader, caplog: pytest.LogCaptureFixture
) -> None:
    """C3: tracing off is NOT a failure; the instrumentor is never called."""
    fake = _fake_instrumentor()
    with caplog.at_level(logging.INFO, logger="audittrace.instrumentation"):
        result = attach_instrumentor("Fake", fake, strict=True, enabled=False)
    assert result is AttachResult.DISABLED
    assert type(fake).calls == 0  # type: ignore[attr-defined]
    assert not fake.is_instrumented_by_opentelemetry
    (rec,) = _attach_records(caplog)
    assert rec.levelno == logging.INFO and rec.result == "disabled"  # type: ignore[attr-defined]
    assert _points(metric_reader) == {("Fake", "disabled"): 1}


def test_attached_logs_info_and_counts(
    metric_reader: InMemoryMetricReader, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _fake_instrumentor()
    with caplog.at_level(logging.INFO, logger="audittrace.instrumentation"):
        result = attach_instrumentor("Fake", fake, strict=True, enabled=True)
    assert result is AttachResult.ATTACHED and fake.is_instrumented_by_opentelemetry
    (rec,) = _attach_records(caplog)
    assert rec.levelno == logging.INFO
    assert (rec.instrumentor, rec.result, rec.conflict) == ("Fake", "attached", "none")  # type: ignore[attr-defined]
    assert _points(metric_reader) == {("Fake", "attached"): 1}


def test_second_identical_attach_is_idempotent_not_refused(
    metric_reader: InMemoryMetricReader,
) -> None:
    fake = _fake_instrumentor()
    assert (
        attach_instrumentor("Fake", fake, strict=True, enabled=True)
        is AttachResult.ATTACHED
    )
    assert (
        attach_instrumentor("Fake", fake, strict=True, enabled=True)
        is AttachResult.ATTACHED
    )
    assert type(fake).calls == 1  # type: ignore[attr-defined]


def test_instrumented_by_someone_else_is_refused(
    metric_reader: InMemoryMetricReader, caplog: pytest.LogCaptureFixture
) -> None:
    """S2: the singleton returns None for an already-instrumented library; the
    chokepoint reports it instead of calling it ``attached``."""
    fake = _fake_instrumentor()
    fake.instrument()  # attached outside the chokepoint
    with caplog.at_level(logging.INFO, logger="audittrace.instrumentation"):
        assert (
            attach_instrumentor("Fake", fake, strict=False, enabled=True)
            is AttachResult.REFUSED_ALREADY_INSTRUMENTED
        )
        with pytest.raises(InstrumentationError):
            attach_instrumentor("Fake", fake, strict=True, enabled=True)
    assert {r.levelno for r in _attach_records(caplog)} == {logging.ERROR}
    assert _points(metric_reader) == {("Fake", "refused_already_instrumented"): 2}


def test_instrumentor_exception_is_error_result(
    metric_reader: InMemoryMetricReader, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _fake_instrumentor(boom=True)
    with caplog.at_level(logging.INFO, logger="audittrace.instrumentation"):
        assert (
            attach_instrumentor("Boom", fake, strict=False, enabled=True)
            is AttachResult.ERROR
        )
        with pytest.raises(InstrumentationError, match="kaboom"):
            attach_instrumentor("Boom", fake, strict=True, enabled=True)
    assert "RuntimeError: kaboom" in _attach_records(caplog)[0].conflict  # type: ignore[attr-defined]
    assert _points(metric_reader) == {("Boom", "error"): 2}


def test_non_strict_never_raises_on_a_declined_instrumentor(
    metric_reader: InMemoryMetricReader,
) -> None:
    fake = _fake_instrumentor(deps=("definitely-not-installed-xyz >= 1",))
    assert (
        attach_instrumentor("Fake", fake, strict=False, enabled=True)
        is AttachResult.DECLINED
    )


# ------------------------------------------------------------- FastAPI app ----
def test_fastapi_app_path_attached_refused_and_declined(
    metric_reader: InMemoryMetricReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FastAPI()
    inst = FastAPIInstrumentor()
    assert (
        attach_instrumentor(
            "FastAPIInstrumentor", inst, app=app, strict=True, enabled=True
        )
        is AttachResult.ATTACHED
    )
    assert getattr(app, "_is_instrumented_by_opentelemetry", False)
    assert (
        attach_instrumentor(
            "FastAPIInstrumentor", inst, app=app, strict=False, enabled=True
        )
        is AttachResult.REFUSED_ALREADY_INSTRUMENTED
    )
    monkeypatch.setattr(
        FastAPIInstrumentor, "instrumentation_dependencies", lambda self: OUT_OF_RANGE
    )
    assert (
        attach_instrumentor(
            "FastAPIInstrumentor", inst, app=FastAPI(), strict=False, enabled=True
        )
        is AttachResult.DECLINED
    )
    FastAPIInstrumentor.uninstrument_app(app)


# ----------------------------------------------------------- policy defaults --
def test_strict_defaults_to_true_and_follows_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings().instrumentation_strict is True
    monkeypatch.setenv("AUDITTRACE_INSTRUMENTATION_STRICT", "false")
    assert Settings().instrumentation_strict is False
    fake = _fake_instrumentor(deps=("definitely-not-installed-xyz >= 1",))
    monkeypatch.setattr(config, "get_settings", lambda: Settings(tracing_enabled=True))
    assert (
        attach_instrumentor("Fake", fake) is AttachResult.DECLINED
    )  # strict off: no raise
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: Settings(tracing_enabled=True, instrumentation_strict=True),
    )
    with pytest.raises(InstrumentationError):
        attach_instrumentor("Fake", fake)
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: Settings(tracing_enabled=False, instrumentation_strict=True),
    )
    assert attach_instrumentor("Fake", fake) is AttachResult.DISABLED


def test_telemetry_exposes_the_single_entry_point() -> None:
    assert telemetry.attach_instrumentor is attach_instrumentor


def test_reset_for_tests_forgets_registries() -> None:
    engine = create_engine("sqlite://")
    instrumentation._SA_ENGINES.add(engine)
    instrumentation._OWNED.add("X")
    instrumentation._reset_for_tests()
    assert not instrumentation._SA_ENGINES and not instrumentation._OWNED


# ---------------------------------------------------------------- G3b ----------
_ATTACH_ATTRS = {"instrument", "instrument_app"}


def attach_call_sites(root: pathlib.Path) -> list[tuple[str, str, int]]:
    """(relative path, enclosing function qualname, line) of every
    ``.instrument(...)`` / ``.instrument_app(...)`` call under ``root``."""
    sites: list[tuple[str, str, int]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())

        def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
            for child in ast.iter_child_nodes(node):
                inner = scope
                if isinstance(
                    child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
                ):
                    inner = (*scope, child.name)
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr in _ATTACH_ATTRS
                ):
                    sites.append(
                        (
                            str(path.relative_to(root)),
                            ".".join(scope) or "<module>",
                            child.lineno,
                        )
                    )
                visit(child, inner)

        visit(tree, ())
    return sites


def test_every_attach_call_in_src_is_inside_the_chokepoint() -> None:
    sites = attach_call_sites(SRC / "audittrace")
    outside = [
        s for s in sites if s[:2] != ("instrumentation.py", "attach_instrumentor")
    ]
    assert not outside, f"instrumentor attach outside the chokepoint: {outside}"
    assert sites, "the scanner found no attach call at all (vacuous)"


def test_the_six_product_families_all_attach_through_the_chokepoint() -> None:
    names: set[str] = set()
    for path in (SRC / "audittrace").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and (
                    getattr(node.func, "attr", None) == "attach_instrumentor"
                    or getattr(node.func, "id", None) == "attach_instrumentor"
                )
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                names.add(str(node.args[0].value))
    assert names == {
        "FastAPIInstrumentor",
        "HTTPXClientInstrumentor",
        "LoggingInstrumentor",
        "RedisInstrumentor",
        "SQLAlchemyInstrumentor",
        "URLLib3Instrumentor",
    }


def test_scanner_flags_a_bypass(tmp_path: pathlib.Path) -> None:
    """The scanner is not vacuous: a bare ``X().instrument()`` is reported."""
    (tmp_path / "bypass.py").write_text(
        "def boot():\n    SQLAlchemyInstrumentor().instrument(engine=e)\n"
    )
    assert attach_call_sites(tmp_path) == [("bypass.py", "boot", 2)]
