"""G1 / G1b - the product's own engine path emits statement-level DB spans.

SQLALCHEMY-SPANS (operator directive 2026-10-19). Drives
``URLPostgresFactory`` - the construction path server.py and dependencies.py
use - into an in-memory span exporter on a REAL PostgreSQL, so the attribute
set the dashboards and Tempo queries depend on (``db.system="postgresql"``,
``db.statement``, ``db.operation``, ``db.name``, ``net.peer.name``) is asserted
rather than assumed. SQLite cannot stand in for it (``db.system="sqlite"``).

The Postgres precondition FAILS (never skips): CI provides the ``postgres:16``
service via ``AUDITTRACE_TEST_POSTGRES_URL``; locally the throwaway container
of ``tests/_pg_ephemeral.py`` is used. The URL is normalised to asyncpg with
``config._as_async_url`` because the CI URL is psycopg2 and
``create_async_engine`` rejects a sync driver (C2).

Order independence (C2): the fixture binds a LOCAL provider, resets the
chokepoint registry and ``uninstrument()``s ``SQLAlchemyInstrumentor`` before
and after, so the test passes alone and inside the full suite.

Neuters (each applied alone, recorded in the build record):
N1 delete the chokepoint attach in ``db/postgres.py``         -> G1, G1b RED
N2 ``instrumentation_dependencies`` -> ``("sqlalchemy < 1.0",)`` -> G1 RED
   (proved in-process by ``test_n2_*``)
N3 revert the C4 second-engine fix (bare per-engine call)    -> G1 second-factory RED
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import pytest_asyncio
from opentelemetry import trace
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import String, text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from audittrace import instrumentation
from audittrace.config import _as_async_url
from audittrace.db.postgres import URLPostgresFactory
from tests._pg_ephemeral import start_ephemeral_postgres

REQUIRED_KEYS = {
    "db.system",
    "db.statement",
    "db.operation",
    "db.name",
    "net.peer.name",
}


@pytest.fixture(scope="module")
def pg_async_url() -> str:
    url = os.environ.get("AUDITTRACE_TEST_POSTGRES_URL") or start_ephemeral_postgres(
        "audittrace-otel-spans-pg-",
        "otel_spans_ephemeral_pw",  # noqa: S106 - throwaway container credential
        "audittrace_otel_spans",
    )
    if not url:
        pytest.fail(
            "G1 needs a real PostgreSQL and none is available: set "
            "AUDITTRACE_TEST_POSTGRES_URL or make Docker available so "
            "tests/_pg_ephemeral.py can start postgres:16. This is a FAILURE, "
            "not a skip: a telemetry guard that skips guards nothing."
        )
    return _as_async_url(url)


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """A local provider bound as the API's global, with the instrumentor and
    the chokepoint registry reset before AND after (order independence)."""
    inst = SQLAlchemyInstrumentor()
    owned = set(instrumentation._OWNED)

    def _clean() -> None:
        if inst.is_instrumented_by_opentelemetry:
            inst.uninstrument()
        instrumentation._reset_for_tests()
        # Other instrumentors attached earlier in the process stay owned.
        instrumentation._OWNED.update(owned)

    _clean()
    provider = TracerProvider()
    mem = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(mem))
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider)
    yield mem
    _clean()


class _Base(DeclarativeBase):
    pass


class _Probe(_Base):
    __tablename__ = f"otel_probe_{uuid.uuid4().hex[:10]}"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(32))


def _statement_spans(mem: InMemorySpanExporter, system: str) -> list[ReadableSpan]:
    return [
        s
        for s in mem.get_finished_spans()
        if s.attributes
        and s.attributes.get("db.system") == system
        and s.attributes.get("db.statement")
    ]


def _assert_postgres_statement_span(
    spans: list[ReadableSpan], *, statement: str, operation: str
) -> None:
    hits = [s for s in spans if statement in str(s.attributes["db.statement"])]
    assert hits, (
        f"no postgresql statement span for {statement!r}; got "
        f"{[dict(s.attributes or {}) for s in spans]}"
    )
    # A1: exactly ONE span per statement per engine (a duplicate-listener
    # regression would double-count every query).
    assert len(hits) == 1, f"{len(hits)} spans for {statement!r}, expected exactly 1"
    attrs = dict(hits[0].attributes or {})
    assert REQUIRED_KEYS <= set(attrs), sorted(REQUIRED_KEYS - set(attrs))
    assert attrs["db.system"] == "postgresql"
    assert attrs["db.operation"] == operation


async def _dispose(engine: AsyncEngine) -> None:
    await engine.dispose()


@pytest_asyncio.fixture
async def probe_table(pg_async_url: str) -> Any:
    """Create/drop the ORM probe table with an UNINSTRUMENTED plain engine."""
    from sqlalchemy.ext.asyncio import create_async_engine

    admin = create_async_engine(pg_async_url)
    async with admin.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    yield
    async with admin.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
    await admin.dispose()


@pytest.mark.asyncio
async def test_product_engine_emits_postgres_statement_spans(
    pg_async_url: str, exporter: InMemorySpanExporter, probe_table: None
) -> None:
    first = URLPostgresFactory(pg_async_url)
    async with first.get_session_factory()() as session:
        await session.execute(text("SELECT 1 AS first_engine"))
        session.add(_Probe(label="one"))
        await session.commit()

    spans = _statement_spans(exporter, "postgresql")
    _assert_postgres_statement_span(spans, statement="first_engine", operation="SELECT")
    _assert_postgres_statement_span(
        spans, statement=_Probe.__tablename__, operation="INSERT"
    )

    # The SAME holds for a second factory built exactly as server.py:480 builds
    # the summariser factory (pool_size=2), after the first has served a
    # statement (C4: the singleton refuses a second plain instrument()).
    exporter.clear()
    second = URLPostgresFactory(pg_async_url, pool_size=2)
    async with second.get_session_factory()() as session:
        await session.execute(text("SELECT 2 AS second_engine"))
    _assert_postgres_statement_span(
        _statement_spans(exporter, "postgresql"),
        statement="second_engine",
        operation="SELECT",
    )

    # ...and creating the second engine must not have dropped the first.
    exporter.clear()
    async with first.get_session_factory()() as session:
        await session.execute(text("SELECT 3 AS first_again"))
    _assert_postgres_statement_span(
        _statement_spans(exporter, "postgresql"),
        statement="first_again",
        operation="SELECT",
    )
    await _dispose(first.get_engine())
    await _dispose(second.get_engine())


@pytest.mark.asyncio
async def test_sqlite_engine_is_instrumented_too(
    tmp_path: Any, exporter: InMemorySpanExporter
) -> None:
    """G1b control (cheap, no container; NOT a substitute for G1)."""
    factory = URLPostgresFactory(f"sqlite+aiosqlite:///{tmp_path}/probe.sqlite")
    async with factory.get_session_factory()() as session:
        await session.execute(text("SELECT 1 AS sqlite_probe"))
    hits = [
        s
        for s in _statement_spans(exporter, "sqlite")
        if "sqlite_probe" in str(s.attributes["db.statement"])
    ]
    assert len(hits) == 1 and hits[0].attributes["db.system"] == "sqlite"
    await _dispose(factory.get_engine())


@pytest.mark.asyncio
async def test_n2_out_of_range_instrumentor_fails_the_product_path_loudly(
    tmp_path: Any, exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2 in-process: an out-of-range instrumentor makes the product path
    raise under strict (default) instead of silently emitting nothing, and
    the engine is NOT cached uninstrumented for the next caller."""
    monkeypatch.setattr(
        SQLAlchemyInstrumentor,
        "instrumentation_dependencies",
        lambda self: ("sqlalchemy < 1.0",),
    )
    factory = URLPostgresFactory(f"sqlite+aiosqlite:///{tmp_path}/n2.sqlite")
    with pytest.raises(instrumentation.InstrumentationError):
        factory.get_engine()
    with pytest.raises(instrumentation.InstrumentationError):
        factory.get_engine()  # not cached after the failure
    assert _statement_spans(exporter, "sqlite") == []
