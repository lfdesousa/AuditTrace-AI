"""F2 - the EMITTED ``audittrace.instrumentation.attached_total`` metric, in
PRODUCT boot order, on the product's OWN MeterProvider.

The G3 metric tests in ``test_instrumentation_attach.py`` replace the counter
with a test-made one, so they cannot see a renamed counter (neuter N21) or a
measurement lost because it was taken before ``init_telemetry`` installed the
real MeterProvider (review F1: ``create_app()`` runs at import and the FastAPI
outcome was dropped). This guard boots a FRESH interpreter in product order
(``import audittrace.server`` -> lifespan -> one engine), appends an in-memory
reader to the provider ``init_telemetry`` builds, and asserts exactly one point
per family, found BY METRIC NAME, with the right ``instrumentor`` / ``result``.

Neuters (each alone): (a) drop ``flush_pending_outcomes()`` in
``init_telemetry`` (the F1 bug) -> FastAPIInstrumentor missing -> RED;
(b) N21 rename the product counter -> no point by name -> RED;
(c) N8 drop the increment -> RED.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

METRIC = "audittrace.instrumentation.attached_total"
FAMILIES = {
    "FastAPIInstrumentor",
    "HTTPXClientInstrumentor",
    "LoggingInstrumentor",
    "RedisInstrumentor",
    "SQLAlchemyInstrumentor",
    "URLLib3Instrumentor",
}

_PROBE = textwrap.dedent(
    """
    import json, sys
    from unittest import mock
    import opentelemetry.sdk.metrics as sdkm
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    import logging

    errors = []

    class _H(logging.Handler):
        def emit(self, r):
            if "instrumentation.attach" in r.getMessage() and r.levelno >= logging.ERROR:
                errors.append(r.getMessage())

    logging.getLogger("audittrace.instrumentation").addHandler(_H())
    %(pre)s
    reader = InMemoryMetricReader()
    _orig = sdkm.MeterProvider.__init__

    def _init(self, *a, metric_readers=(), **kw):
        _orig(self, *a, metric_readers=[*metric_readers, reader], **kw)

    sdkm.MeterProvider.__init__ = _init
    import audittrace.db.factory as F

    F.HTTPChromaDBFactory.get_client = lambda self: mock.MagicMock()
    from fastapi.testclient import TestClient
    import audittrace.server as server  # create_app() runs HERE (product order)

    with TestClient(server.app) as client:
        assert client.get("/health").status_code == 200
        from audittrace.dependencies import get_postgres_factory

        get_postgres_factory().get_engine()
    out = []
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "%(metric)s":
                    for p in m.data.data_points:
                        out.append([p.attributes["instrumentor"], p.attributes["result"], p.value])
    print("POINTS=" + json.dumps(sorted(out)))
    print("ERRORS=" + json.dumps(errors))
    """
)


_DECLINE_FASTAPI = (
    "from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor\n"
    "FastAPIInstrumentor.instrumentation_dependencies = lambda self: ('fastapi < 0.1',)"
)


def _boot(tmp: Path, *, pre: str = "", strict: bool = True) -> tuple[list, list]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("AUDITTRACE_") and k != "PYTEST_CURRENT_TEST"
    }
    env.update(
        AUDITTRACE_POSTGRES_URL=f"sqlite+aiosqlite:///{tmp}/order.sqlite",
        AUDITTRACE_AUTH_ENABLED="false",
        AUDITTRACE_MINIO_SECRET_KEY="probe-not-a-secret",  # noqa: S106 - inert probe value
        AUDITTRACE_INSTRUMENTATION_STRICT="true" if strict else "false",
        PYTHONDONTWRITEBYTECODE="1",
    )
    proc = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [sys.executable, "-I", "-c", _PROBE % {"metric": METRIC, "pre": pre}],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-4000:]
    lines = {
        ln.split("=", 1)[0]: json.loads(ln.split("=", 1)[1])
        for ln in proc.stdout.splitlines()
        if ln.startswith(("POINTS=", "ERRORS="))
    }
    assert "POINTS" in lines, f"probe printed no POINTS line:\n{proc.stdout[-2000:]}"
    return lines["POINTS"], lines["ERRORS"]


@pytest.fixture(scope="module")
def points(tmp_path_factory: pytest.TempPathFactory) -> list[list[object]]:
    return _boot(tmp_path_factory.mktemp("order"))[0]


def test_every_family_reaches_the_product_meter_provider_exactly_once(
    points: list[list[object]],
) -> None:
    assert points, f"no {METRIC} point reached the product MeterProvider"
    by_family = {p[0]: [q for q in points if q[0] == p[0]] for p in points}
    assert set(by_family) == FAMILIES, sorted(FAMILIES ^ set(by_family))
    for family, rows in by_family.items():
        assert rows == [[family, "attached", 1]], (family, rows)


def test_declined_fastapi_under_non_strict_is_an_error_log_and_a_metric_point(
    tmp_path: Path,
) -> None:
    pts, errors = _boot(tmp_path, pre=_DECLINE_FASTAPI, strict=False)
    assert ["FastAPIInstrumentor", "declined", 1] in pts, pts
    assert any("instrumentor=FastAPIInstrumentor result=declined" in e for e in errors)
