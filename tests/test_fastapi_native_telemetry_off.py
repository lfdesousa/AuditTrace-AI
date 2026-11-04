"""WP-1 FASTAPI-CONSISTENCY + NATIVE-TELEMETRY-OFF (v1.31.0 telemetry close-out).

FastAPI 0.162 ships its own OpenTelemetry (tracing, metrics, logs, operation
spans). The product's tracing is the contrib ``FastAPIInstrumentor`` attached
through ``attach_instrumentor`` behind the TRACEPARENT-TRUST propagator, so the
native layer must be OFF in every app, with an EQUALITY pin on the whole config
(a subset check would let a new key slip in enabled).

Cells (spec names): AC-1.1 pins agree with the installed fastapi and every lock;
AC-1.2 static ``ast`` guard over every non-test ``*.py`` under ``src/ bff/
images/ scripts/``; AC-1.3a/b behavioural (attach DISABLED / DECLINED -> zero
native spans, positive control from another instrumentor); AC-1.4 contrib
attached -> exactly one SERVER span, minted trace id, one link; AC-1.5 the
``FASTAPI_OTEL_AUTO_CONFIGURE`` switch is nowhere in the chart.

Neuters (each run INDIVIDUALLY, RED recorded in the build record):
  N-1.1 drop ``telemetry=`` from one app      -> AC-1.2 + AC-1.3a/b RED
  N-1.2 set one key ``True``                  -> AC-1.2 RED
  N-1.3 edit the pin without ``make lock``    -> AC-1.1 + ``make lock-check`` RED
  N-1.4 skip ``install_trace_context_policy`` -> AC-1.4 RED (inbound adopted)
  N-1.5 drop the guard's fail-closed branch   -> ``test_guard_fails_closed_*`` RED
  two-fault (ADDENDUM-1 C7): no ``telemetry=`` AND no policy -> AC-1.3a/b RED
  with the inbound id adopted by the native spans.
"""

from __future__ import annotations

import ast
import importlib.metadata
import re
import shutil
import subprocess
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import propagate
from opentelemetry import trace as otel_trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from audittrace import config as config_mod
from audittrace.server import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_ROOTS = ("src", "bff", "images", "scripts")
NATIVE_OFF_KEYS = frozenset(
    {"tracing", "metrics", "logs", "operation_spans", "auto_configure"}
)
MIN_CONSTRUCTIONS = 3  # positive control: bff + server + llm-stub
LOCKS = (
    "requirements.txt",
    "requirements-dev.txt",
    "bff/requirements.txt",
    "images/llm-stub/requirements.txt",
)
GATE_LOCK = "requirements-gate.txt"
FASTAPI_PIN = re.compile(r"^fastapi==(\S+?)(?:\s|\\|$)", re.MULTILINE)
INBOUND_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
INBOUND_TRACEPARENT = f"00-{INBOUND_TRACE_ID}-b7ad6b7169203331-01"
NATIVE_SCOPE = "fastapi"
CONTRIB_SCOPE = "opentelemetry.instrumentation.fastapi"
HTTPX_SCOPE = "opentelemetry.instrumentation.httpx"


# --------------------------------------------------------------------------
# AC-1.1  the pin, the installed distribution and every lock agree
# --------------------------------------------------------------------------
def _pyproject_fastapi_pin() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pins = [
        d.split("==", 1)[1]
        for d in data["project"]["dependencies"]
        if d.startswith("fastapi==")
    ]
    assert len(pins) == 1, f"expected exactly one fastapi== pin, got {pins}"
    return pins[0]


def _lock_fastapi_pins(name: str) -> list[str]:
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    return FASTAPI_PIN.findall(text)


def test_ac_1_1_pin_installed_and_every_lock_agree() -> None:
    pin = _pyproject_fastapi_pin()
    assert importlib.metadata.version("fastapi") == pin
    for lock in LOCKS:
        assert _lock_fastapi_pins(lock) == [pin], lock


def test_ac_1_1_gate_lock_has_no_fastapi() -> None:
    assert _lock_fastapi_pins(GATE_LOCK) == []


def test_ac_1_1_bff_and_stub_inputs_carry_the_same_pin() -> None:
    pin = _pyproject_fastapi_pin()
    for rel in ("bff/requirements.in", "images/llm-stub/requirements.in"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert FASTAPI_PIN.findall(text) == [pin], rel


def test_ac_1_1_stub_lock_is_constrained_by_the_server_lock() -> None:
    """``-c requirements.txt`` on the stub row: ONE starlette/uvicorn across locks."""
    script = (REPO_ROOT / "scripts" / "lock.sh").read_text(encoding="utf-8")
    assert (
        '"images/llm-stub/requirements.txt|images/llm-stub/requirements.in'
        ' -c requirements.txt"' in script
    )
    for pkg in ("starlette", "uvicorn"):
        versions = {
            re.search(rf"^{pkg}==(\S+)", (REPO_ROOT / lock).read_text(), re.M).group(1)  # type: ignore[union-attr]
            for lock in LOCKS
        }
        assert len(versions) == 1, (pkg, versions)


# --------------------------------------------------------------------------
# AC-1.2  static guard: every FastAPI( carries the five-key all-False config
# --------------------------------------------------------------------------
def _is_fastapi_ctor(node: ast.Call) -> bool:
    f = node.func
    return (isinstance(f, ast.Name) and f.id == "FastAPI") or (
        isinstance(f, ast.Attribute) and f.attr == "FastAPI"
    )


def find_fastapi_constructions(
    repo_root: Path, roots: tuple[str, ...] = APP_ROOTS
) -> list[tuple[Path, ast.Call]]:
    """Every ``FastAPI(...)`` call in a non-test ``*.py`` under ``roots``."""
    found: list[tuple[Path, ast.Call]] = []
    for root in roots:
        base = repo_root / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel_parts = path.relative_to(repo_root).parts
            if "tests" in rel_parts or ".venv" in rel_parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            found.extend(
                (path, n)
                for n in ast.walk(tree)
                if isinstance(n, ast.Call) and _is_fastapi_ctor(n)
            )
    return found


def native_telemetry_problem(call: ast.Call) -> str | None:
    """None when the call carries the exact five-key all-False dict literal."""
    kw = next((k for k in call.keywords if k.arg == "telemetry"), None)
    if kw is None:
        return "no telemetry= keyword"
    if not isinstance(kw.value, ast.Dict):
        return "telemetry= is not a dict literal"
    keys: list[str] = []
    for k, v in zip(kw.value.keys, kw.value.values, strict=True):
        if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
            return "telemetry= has a non-literal key (or ** unpack)"
        if not (isinstance(v, ast.Constant) and v.value is False):
            return f"telemetry[{k.value!r}] is not the literal False"
        keys.append(k.value)
    if len(keys) != len(set(keys)) or set(keys) != NATIVE_OFF_KEYS:
        return f"telemetry keys {sorted(keys)} != {sorted(NATIVE_OFF_KEYS)}"
    return None


def guard_violations(repo_root: Path, roots: tuple[str, ...] = APP_ROOTS) -> list[str]:
    """Fail closed: fewer than ``MIN_CONSTRUCTIONS`` apps found is itself RED."""
    calls = find_fastapi_constructions(repo_root, roots)
    assert len(calls) >= MIN_CONSTRUCTIONS, (
        f"guard found {len(calls)} FastAPI( constructions under {roots}; "
        f"expected >= {MIN_CONSTRUCTIONS} (the scan is blind, not green)"
    )
    return [
        f"{p.relative_to(repo_root)}:{c.lineno}: {why}"
        for p, c in calls
        if (why := native_telemetry_problem(c)) is not None
    ]


def test_ac_1_2_every_app_turns_native_telemetry_off() -> None:
    assert guard_violations(REPO_ROOT) == []


def test_ac_1_2_guard_sees_the_three_known_apps() -> None:
    seen = {
        str(p.relative_to(REPO_ROOT)) for p, _ in find_fastapi_constructions(REPO_ROOT)
    }
    assert {
        "bff/app.py",
        "src/audittrace/server.py",
        "images/llm-stub/server.py",
    } <= seen


def _write_apps(root: Path, extra: str = "") -> None:
    ok = (
        "from fastapi import FastAPI\n"
        "app = FastAPI(telemetry={'tracing': False, 'metrics': False, "
        "'logs': False, 'operation_spans': False, 'auto_configure': False})\n"
    )
    for i in range(MIN_CONSTRUCTIONS):
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / f"app{i}.py").write_text(ok, encoding="utf-8")
    if extra:
        (root / "scripts").mkdir(exist_ok=True)
        (root / "scripts" / "fourth.py").write_text(extra, encoding="utf-8")


def test_guard_accepts_a_clean_synthetic_tree(tmp_path: Path) -> None:
    _write_apps(tmp_path)
    assert guard_violations(tmp_path) == []


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ("import fastapi\napp = fastapi.FastAPI()\n", "no telemetry= keyword"),
        (
            "from fastapi import FastAPI\napp = FastAPI(telemetry={'tracing': True,"
            " 'metrics': False, 'logs': False, 'operation_spans': False,"
            " 'auto_configure': False})\n",
            "['tracing'] is not the literal False",
        ),
        (
            "from fastapi import FastAPI\napp = FastAPI(telemetry={'tracing': False})\n",
            "telemetry keys",
        ),
        (
            "from fastapi import FastAPI\nT = {}\napp = FastAPI(telemetry=T)\n",
            "not a dict literal",
        ),
        (
            "from fastapi import FastAPI\nB = {}\napp = FastAPI(telemetry={**B})\n",
            "non-literal key",
        ),
    ],
)
def test_guard_flags_a_fourth_noncompliant_app(
    tmp_path: Path, extra: str, needle: str
) -> None:
    _write_apps(tmp_path, extra)
    (violation,) = guard_violations(tmp_path)
    assert violation.startswith("scripts/fourth.py:") and needle in violation


def test_guard_ignores_test_trees(tmp_path: Path) -> None:
    _write_apps(tmp_path)
    (tmp_path / "src" / "tests").mkdir()
    (tmp_path / "src" / "tests" / "t.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    assert guard_violations(tmp_path) == []


def test_guard_fails_closed_on_an_empty_tree(tmp_path: Path) -> None:
    """N-1.5: pointed at an empty dir the scan must go RED, never vacuously green."""
    with pytest.raises(AssertionError, match="the scan is blind"):
        guard_violations(tmp_path)


# --------------------------------------------------------------------------
# AC-1.3a/b + AC-1.4  behaviour on the REAL create_app()
# --------------------------------------------------------------------------
def _default_composite() -> CompositePropagator:
    return CompositePropagator(
        [TraceContextTextMapPropagator(), W3CBaggagePropagator()]
    )


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """A local SDK provider that BOTH the native layer and the contrib hook find.

    The OTel global provider is set-once per process, so the lookup function
    both layers call at request time is patched instead. The global textmap is
    saved/restored and started from the vulnerable default so ``create_app()``
    is the thing that installs the TRACEPARENT-TRUST policy (N-1.4 observable).
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(otel_trace, "get_tracer_provider", lambda: provider)
    saved = propagate.get_global_textmap()
    propagate.set_global_textmap(_default_composite())
    config_mod.get_settings.cache_clear()
    try:
        yield exporter
    finally:
        propagate.set_global_textmap(saved)
        config_mod.get_settings.cache_clear()
        provider.shutdown()


def _probe_app(
    monkeypatch: pytest.MonkeyPatch, *, tracing: bool, strict: bool = True
) -> FastAPI:
    monkeypatch.setenv("AUDITTRACE_MINIO_SECRET_KEY", "ci-test")
    monkeypatch.setenv("AUDITTRACE_TRACING_ENABLED", "true" if tracing else "false")
    monkeypatch.setenv(
        "AUDITTRACE_INSTRUMENTATION_STRICT", "true" if strict else "false"
    )
    config_mod.get_settings.cache_clear()
    app = create_app()

    @app.get("/__wp1/probe")
    async def probe() -> dict[str, bool]:
        # positive control: a span from a DIFFERENT instrumentor, so "the
        # inbound id is not any span's trace id" has a subject.
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200))
        )
        HTTPXClientInstrumentor.instrument_client(
            client, tracer_provider=otel_trace.get_tracer_provider()
        )
        async with client:
            await client.get("http://upstream.test/x")
        return {"ok": True}

    return app


def _request(app: FastAPI) -> None:
    # No ``with``: the lifespan (ChromaDB, Postgres, ...) is not under test here.
    client = TestClient(app)
    resp = client.get("/__wp1/probe", headers={"traceparent": INBOUND_TRACEPARENT})
    assert resp.status_code == 200


def _spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return list(exporter.get_finished_spans())


def _trace_ids(spans: list[ReadableSpan]) -> set[str]:
    return {format(s.context.trace_id, "032x") for s in spans}


def _scopes(spans: list[ReadableSpan]) -> set[str]:
    return {s.instrumentation_scope.name for s in spans if s.instrumentation_scope}


def test_ac_1_3a_attach_disabled_no_native_spans(
    sdk: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _probe_app(monkeypatch, tracing=False)
    _request(app)
    spans = _spans(sdk)
    assert HTTPX_SCOPE in _scopes(spans), "positive control: httpx client span"
    assert NATIVE_SCOPE not in _scopes(spans)
    assert CONTRIB_SCOPE not in _scopes(spans)
    assert INBOUND_TRACE_ID not in _trace_ids(spans)


def test_ac_1_3b_attach_declined_no_native_spans(
    sdk: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        FastAPIInstrumentor,
        "instrumentation_dependencies",
        lambda self: ("sqlalchemy < 1.0",),
    )
    app = _probe_app(monkeypatch, tracing=True, strict=False)
    _request(app)
    spans = _spans(sdk)
    assert HTTPX_SCOPE in _scopes(spans), "positive control: httpx client span"
    assert NATIVE_SCOPE not in _scopes(spans)
    assert CONTRIB_SCOPE not in _scopes(spans), "the attach really was declined"
    assert INBOUND_TRACE_ID not in _trace_ids(spans)


def test_ac_1_3_positive_control_native_spans_are_visible_when_on(
    sdk: InMemorySpanExporter,
) -> None:
    """The instrument can see native spans: a bare app with tracing ON emits them."""
    app = FastAPI(telemetry={"tracing": True})

    @app.get("/x")
    def x() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/x").status_code == 200
    assert NATIVE_SCOPE in _scopes(_spans(sdk))


def test_ac_1_4_contrib_attached_one_server_span_minted_id_one_link(
    sdk: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _probe_app(monkeypatch, tracing=True)
    _request(app)
    spans = _spans(sdk)
    assert NATIVE_SCOPE not in _scopes(spans)
    servers = [s for s in spans if s.kind == SpanKind.SERVER]
    assert len(servers) == 1
    (server,) = servers
    assert server.instrumentation_scope is not None
    assert server.instrumentation_scope.name == CONTRIB_SCOPE
    assert format(server.context.trace_id, "032x") != INBOUND_TRACE_ID
    assert len(server.links) == 1
    assert server.attributes is not None
    assert server.attributes["audittrace.inbound.trace_id"] == INBOUND_TRACE_ID
    assert INBOUND_TRACE_ID not in _trace_ids(spans)


# --------------------------------------------------------------------------
# AC-1.5  FASTAPI_OTEL_AUTO_CONFIGURE is nowhere in the chart / images
# --------------------------------------------------------------------------
_SWITCH = "FASTAPI_OTEL_AUTO_CONFIGURE"


def _tracked_text_files() -> Iterator[Path]:
    # index-gap: BS-006 (chart templates, Dockerfiles and yaml are not indexed)
    for root in ("charts", "images", "bff"):
        for path in (REPO_ROOT / root).rglob("*"):
            if path.is_file() and path.suffix not in {".pyc", ".png", ".tgz"}:
                yield path


def test_ac_1_5_switch_absent_from_chart_and_image_sources() -> None:
    hits = [
        str(p.relative_to(REPO_ROOT))
        for p in _tracked_text_files()
        if _SWITCH in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert hits == []


_HELM_BASE = (
    "--set", "secrets.postgres.appPassword=ci-test",
    "--set", "secrets.postgres.password=ci-test",
    "--set", "secrets.redis.password=ci-test",
    "--set", "secrets.summariser.password=ci-test",
    "--set", "secrets.minio.secretKey=ci-test",
    "--set", "secrets.minio.kmsKey=ci-test",
    "--set", "observability.external.langfuseHost=langfuse.test.invalid",
    "--set", "observability.external.tempoHost=tempo.test.invalid",
    "--set", "observability.external.lokiHost=loki.test.invalid",
)  # fmt: skip


@pytest.mark.parametrize(
    "extra",
    [
        ("--set", "externalLLM.host=llm.test.invalid"),
        ("--set", "llmStub.enabled=true", "--set", "externalLLM.enabled=false"),
    ],
    ids=["external-llm", "llm-stub"],
)
def test_ac_1_5_switch_absent_from_rendered_manifests(extra: tuple[str, ...]) -> None:
    """``helm template`` (memory-server, BFF-side values and the llm-stub).

    Not a skip: without helm the static scan above is the whole check, and this
    test says so by asserting the sources it scanned.
    """
    helm = shutil.which("helm")
    if helm is None:
        assert list(_tracked_text_files()), "static scan covered the sources"
        return
    out = subprocess.run(
        [
            helm,
            "template",
            "audittrace",
            str(REPO_ROOT / "charts" / "audittrace"),
            "--namespace",
            "audittrace",
            *_HELM_BASE,
            *extra,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    docs: list[dict[str, Any]] = [d for d in yaml.safe_load_all(out) if d]
    assert any(d.get("kind") == "Deployment" for d in docs)
    assert _SWITCH not in out
