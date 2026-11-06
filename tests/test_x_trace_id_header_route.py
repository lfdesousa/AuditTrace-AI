"""X-TRACE-ID-HEADER (v1.31.0 WP-4), INTEGRATION layer: the real
``create_app()`` stack (CORS, ``TraceIdHeaderMiddleware``, the FastAPI
instrumentor, the real auth dependencies, the 500 handler) answering the 26
request classes of ``tests/_x_trace_id_header_support.CASES``; a raw-ASGI driver
for the SSE ordering; a subprocess for the laptop ``init_telemetry`` mode. The
unit layer is ``test_x_trace_id_header.py``; the two layers are not mixed.

Spec ``2026-10-17-SPEC-x-trace-id-header.md`` + gate 5438c34e (C1-C7). Cell names
(AC1 ... AC-R7) are the spec's.

Mocks declared: ``InMemorySpanExporter`` and ``NoOpTracerProvider`` (OTel SDK
doubles, installed through ``instrument_app(tracer_provider=)`` on a FRESH app so
the set-once global provider is never touched, gate C3); synthetic routes added
to the real app; nothing in the product is patched. No clock, no network.

Neuters (each run ALONE, RED recorded in the build record):
  N1  remove ``app.add_middleware(TraceIdHeaderMiddleware)``
  N2  remove the ``headers=`` kwarg of the 500 handler
  N3  the middleware emits the all-zero id when invalid
  N4  the middleware stamps a hardcoded value
  N6  the middleware keeps a pre-existing header (first writer wins)
  N7  the middleware drops/rewrites other headers
  N8  register the middleware inside ``if cors_origins:``
  N10 register the middleware BEFORE the CORS block (inner to CORS)
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.middleware.cors import CORSMiddleware

from audittrace.server import create_app
from audittrace.trace_id_header import TraceIdHeaderMiddleware
from tests._x_trace_id_header_support import (
    CASES,
    CASES_BY_NAME,
    CORS_ORIGIN,
    INBOUND_TRACE,
    PRESET_VALUE,
    Case,
    Harness,
    isolated_state,
)

TRACE_SHAPE = re.compile(r"^[0-9a-f]{32}$")
IDS = [c.name for c in CASES]


@pytest.fixture(scope="module")
def sdk() -> Iterator[Harness]:
    with isolated_state():
        yield Harness("sdk")


@pytest.fixture(scope="module")
def off() -> Iterator[Harness]:
    with isolated_state():
        yield Harness("off")


def _one_request(h: Harness, case: Case) -> tuple[Any, list[str]]:
    """Send ``case`` with a clean exporter; return (response, SERVER trace ids)."""
    assert h.exporter is not None
    h.exporter.clear()
    h.seen.clear()
    response = h.request(case)
    return response, h.server_span_trace_ids()


# ---------------------------------------------------------------------------
# AC1: 26 classes, exactly one header, equal to the single SERVER span
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_ac1_each_class_carries_exactly_one_header_equal_to_its_server_span(
    sdk: Harness, case: Case
) -> None:
    """AC1 / D2 / D3. For every response class Starlette can emit here: the status
    is the expected one, there is exactly one ``x-trace-id`` (32 lowercase hex),
    exactly one SERVER span was exported, and the header equals its trace id; a
    synthetic route also saw the SAME id through ``current_trace_id_hex()``; the
    500 envelope's ``error.trace_id`` equals the header (R3).

    Neuters: N1 -> RED on 25 rows, GREEN on ``unhandled_500_envelope`` (the handler
    stamps itself); N2 -> RED on that one row only; N4 -> RED everywhere;
    N6 -> RED on ``preset_header_replaced``.
    """
    response, servers = _one_request(sdk, case)
    assert response.status_code == case.status
    values = response.headers.get_list("x-trace-id")
    assert len(values) == 1, values
    assert TRACE_SHAPE.match(values[0])
    assert servers == [values[0]]
    if case.seen is not None:
        assert sdk.seen[case.seen] == values[0]
    if case.name == "unhandled_500_envelope":
        assert response.json()["error"]["trace_id"] == values[0]


def test_ac1_the_pre_existing_header_is_replaced_not_kept_nor_duplicated(
    sdk: Harness,
) -> None:
    """Gate C2 through the real stack: a route that sets ``X-Trace-Id`` itself
    ends up with the server span's id (a single header), never ``PRESET_VALUE``.

    Neuter: N6 (keep) -> RED.
    """
    response, servers = _one_request(sdk, CASES_BY_NAME["preset_header_replaced"])
    assert response.headers.get_list("x-trace-id") == servers
    assert PRESET_VALUE not in response.headers.get_list("x-trace-id")


FORGED = "d" * 32


def test_nb3_a_forged_inbound_x_trace_id_is_never_echoed(sdk: Harness) -> None:
    """WP-4 NB-3 (closed by WP-6). A caller-supplied REQUEST ``X-Trace-Id`` is
    ignored: the response header equals the exported SERVER span's id and differs
    from the forged value, and the route itself saw the server's id.

    Neuter: make the middleware echo the inbound ``x-trace-id`` request header
    (read it from ``scope["headers"]``) -> RED.
    """
    case = Case(
        "forged_inbound_x_trace_id",
        "GET",
        "/__x/json",
        200,
        headers={"X-Trace-Id": FORGED},
        seen="json",
    )
    response, servers = _one_request(sdk, case)
    values = response.headers.get_list("x-trace-id")
    assert len(servers) == 1
    assert values == servers
    assert values[0] != FORGED
    assert sdk.seen["json"] == values[0]


def test_ac1_distinct_requests_get_distinct_ids(sdk: Harness) -> None:
    """The header is per-request, not a process constant.

    Neuter: N4 (hardcoded) -> RED.
    """
    case = CASES_BY_NAME["route_200_json"]
    first = sdk.request(case).headers["x-trace-id"]
    second = sdk.request(case).headers["x-trace-id"]
    assert first != second


# ---------------------------------------------------------------------------
# AC2: tracing disabled
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_ac2_tracing_disabled_no_header_on_any_class(off: Harness, case: Case) -> None:
    """AC2 / D4. Proxy (non-recording) provider: the context is invalid, so the
    header is ABSENT on every class - including the 500 handler's response and
    the classes that carry an inbound ``traceparent`` (TRACEPARENT-TRUST keeps it
    as a link only, so it cannot make the context valid). Not all-zero.

    ``preset_header_replaced`` is INCLUDED since WP-6 (WP-4 NB-4): with no valid
    span a route-set value is stripped too - the header only ever carries a value
    the server minted (D4).

    Neuters: N3 (all-zero when invalid) -> RED; N4 (hardcoded) -> RED;
    N-NB4 (forward a route-set value when the context is invalid) -> RED on
    ``preset_header_replaced`` only.
    """
    response = off.request(case)
    assert response.status_code == case.status
    assert response.headers.get_list("x-trace-id") == []


def test_ac2_the_500_envelope_trace_id_is_null_when_tracing_is_off(
    off: Harness,
) -> None:
    """absent header <-> ``trace_id: null`` <-> NULL column: one statement from
    one function (D4)."""
    response = off.request(CASES_BY_NAME["unhandled_500_envelope"])
    assert response.json()["error"]["trace_id"] is None
    assert off.seen["boom"] is None


# ---------------------------------------------------------------------------
# AC3: the laptop default (init_telemetry(otlp_endpoint=""))
# ---------------------------------------------------------------------------
_LAPTOP_CHILD = textwrap.dedent(
    """
    import json
    from fastapi.testclient import TestClient
    from audittrace import telemetry
    telemetry.init_telemetry(
        service_name="x-trace-id-laptop-child",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=True,
    )
    from audittrace.server import create_app
    app = create_app()
    seen = {}

    @app.get("/__x/json")
    async def _json():
        seen["v"] = telemetry.current_trace_id_hex()
        return {"ok": True}

    # no ``with``: the lifespan (Postgres/Chroma/Redis) is not started
    r = TestClient(app, raise_server_exceptions=False).get("/__x/json")
    print(json.dumps({"headers": r.headers.get_list("x-trace-id"),
                      "seen": seen["v"], "status": r.status_code}))
    """
)


def test_ac3_laptop_default_init_telemetry_sets_the_header_equal_to_the_route_id() -> (
    None
):
    """AC3. The ``init_telemetry(otlp_endpoint="")`` mode is global and set-once, so
    it runs in a SUBPROCESS (reason stated, gate C3): the real bootstrap + the real
    ``create_app()`` -> the header is present, 32-hex, and equals the id the route
    saw. (The coverage of the middleware comes from the in-process cells.)

    Neuter: N1 -> RED.
    """
    proc = subprocess.run(  # noqa: S603 - fixed argv, the test interpreter
        [sys.executable, "-c", _LAPTOP_CHILD],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["status"] == 200
    assert len(result["headers"]) == 1
    assert TRACE_SHAPE.match(result["headers"][0])
    assert result["headers"] == [result["seen"]]


# ---------------------------------------------------------------------------
# AC4 / AC-R4: SSE ordering, preserved headers, identical frames
# ---------------------------------------------------------------------------
async def _asgi_messages(
    h: Harness, path: str, headers: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Drive the app with a raw ASGI scope and return every sent message in order."""
    sent: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }

    requested = False

    async def receive() -> dict[str, Any]:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        # A streaming response listens for ``http.disconnect`` concurrently; a
        # receive that always returns at once would spin. Block until cancelled.
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await h.app(scope, receive, send)
    return sent


async def test_ac4_sse_header_rides_response_start_before_the_first_frame(
    sdk: Harness,
) -> None:
    """AC4 / F4. The header is on the FIRST message (``http.response.start``),
    before any SSE frame; the id observed inside the generator equals it and
    equals the exported SERVER span; ``Cache-Control`` / ``X-Accel-Buffering``
    survive next to exactly one ``x-trace-id`` (AC-R4); the frames are the
    generator's own.

    Neuters: N1 -> RED; stamping a fresh uuid -> the equality RED.
    """
    assert sdk.exporter is not None
    sdk.exporter.clear()
    sdk.seen.clear()
    messages = await _asgi_messages(sdk, "/__x/sse")
    start = messages[0]
    assert start["type"] == "http.response.start"
    headers = [(k, v) for k, v in start["headers"]]
    ids = [v.decode() for k, v in headers if k == b"x-trace-id"]
    assert len(ids) == 1
    assert ids == [sdk.seen["sse"]] == sdk.server_span_trace_ids()
    assert (b"cache-control", b"no-cache") in headers
    assert (b"x-accel-buffering", b"no") in headers
    frames = b"".join(m.get("body", b"") for m in messages[1:])
    assert frames == f"data: {ids[0]}\n\ndata: [DONE]\n\n".encode()


# ---------------------------------------------------------------------------
# AC-R7 / F6: additive only - bodies and every other header unchanged
# ---------------------------------------------------------------------------
def _masked(body: bytes, own_id: str) -> bytes:
    return body.replace(own_id.encode(), b"<TRACE>")


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_ac_r7_the_header_is_the_only_difference_bodies_masked_identical(
    sdk: Harness, case: Case
) -> None:
    """AC-R7 / F6 / A4. Against a build WITHOUT the middleware (same app, same
    instrumentation), every class has the same status, the same other headers
    and byte-identical bodies after masking the request's OWN id (only the 500
    envelope and the synthetic SSE frame embed it, by construction: the 500
    envelope is pre-existing product behaviour, the SSE id is this probe's own
    generator). Byte lengths are equal. Scope: the matrix classes (a real
    ``/v1/chat/completions`` body carries per-run ``chatcmpl`` ids; the OpenAI
    compatibility suites cover that route).

    Neuter: N7 (drop/rewrite another header or a body frame) -> RED.
    """
    with_mw, with_ids = _one_request(sdk, case)
    baseline_h = _baseline()
    without_mw, base_ids = _one_request(baseline_h, case)
    assert len(with_ids) == len(base_ids) == 1
    assert with_mw.status_code == without_mw.status_code
    assert _other_headers(with_mw) == _other_headers(without_mw)
    assert _masked(with_mw.content, with_ids[0]) == _masked(
        without_mw.content, base_ids[0]
    )
    assert len(with_mw.content) == len(without_mw.content)
    if not case.body_embeds_id:
        assert with_mw.content == without_mw.content


def _other_headers(response: Any) -> list[tuple[str, str]]:
    """Every response header except the one under test and the length."""
    return sorted(
        (k, v)
        for k, v in response.headers.multi_items()
        if k.lower() not in {"x-trace-id", "content-length"}
    )


_BASELINES: dict[str, Harness] = {}


def _baseline() -> Harness:
    """One baseline harness (no middleware) shared by the module's cells."""
    if "h" not in _BASELINES:
        _BASELINES["h"] = Harness("sdk", without_middleware=True)
    return _BASELINES["h"]


@pytest.fixture(scope="module", autouse=True)
def _drop_baseline() -> Iterator[None]:
    yield
    _BASELINES.clear()


# ---------------------------------------------------------------------------
# AC6 / AC6b: CHARACTERISATION (A27). NOT a contract.
# ---------------------------------------------------------------------------
def test_ac6_characterisation_an_inbound_traceparent_does_not_choose_the_header(
    sdk: Harness,
) -> None:
    """AC6, a CHARACTERISATION test (gate C1; v1.31 spec A27): it documents what
    the server does today and is not a contract that the caller may choose the id.

    The X-TRACE-ID spec predicted the header would equal the inbound id (F12).
    TRACEPARENT-TRUST (merged since) made the server MINT the trace identity: the
    inbound ``traceparent`` survives only as a span link. Measured at this
    commit: the header is the MINTED id, differs from the inbound trace id, and
    equals the exported SERVER span. The security unit that inverted AC6 has
    landed; this cell now pins the inverted behaviour.

    Neuter: reverting ``install_trace_context_policy`` (TRACEPARENT-TRUST N1)
    makes the header equal the inbound id -> RED.
    """
    case = CASES_BY_NAME["inbound_traceparent"]
    response, servers = _one_request(sdk, case)
    value = response.headers["x-trace-id"]
    assert value != INBOUND_TRACE
    assert servers == [value]


def test_ac6b_characterisation_an_unsampled_inbound_flag_does_not_drop_the_trace(
    sdk: Harness,
) -> None:
    """AC6b, a CHARACTERISATION test (gate C1). The spec predicted: inbound flags
    ``00`` -> header present, equal to the inbound id, ZERO spans exported (the
    caller opts the request out of telemetry while the durable row keeps a
    valid-looking id). Measured here: the server span is the root and is sampled
    regardless of the caller's flag, so the header is the minted id, differs from
    the inbound one, and exactly one SERVER span is exported.

    Neuter: reverting TRACEPARENT-TRUST (default composite propagator) -> the
    inbound flags decide sampling -> 0 spans -> RED.
    """
    case = Case(
        "unsampled_inbound",
        "GET",
        "/__x/json",
        200,
        headers={"traceparent": f"00-{INBOUND_TRACE}-00f067aa0ba902b7-00"},
        seen="json",
    )
    response, servers = _one_request(sdk, case)
    value = response.headers["x-trace-id"]
    assert value != INBOUND_TRACE
    assert servers == [value]


# ---------------------------------------------------------------------------
# AC-R5 / wiring: CORS exposure, placement, unconditional registration
# ---------------------------------------------------------------------------
def test_ac_r5_an_allowed_origin_sees_the_header_exposed_and_present(
    sdk: Harness,
) -> None:
    """AC-R5 / F7. For an allowed origin the ACTUAL response carries
    ``access-control-expose-headers: X-Trace-Id`` AND the header itself (the
    exposure is now truthful); the preflight (class ``cors_preflight_options``)
    stays 200 and is stamped too (AC1 row).
    """
    case = Case("cors_actual", "GET", "/__x/json", 200, headers={"Origin": CORS_ORIGIN})
    response, servers = _one_request(sdk, case)
    assert "X-Trace-Id" in response.headers["access-control-expose-headers"]
    assert response.headers.get_list("x-trace-id") == servers


def _user_middleware(app: Any) -> list[type]:
    return [m.cls for m in app.user_middleware]


def test_wiring_the_middleware_is_outer_to_cors() -> None:
    """D2: ``add_middleware`` is outer-last, so the middleware's index in
    ``user_middleware`` (outermost first) is BELOW CORS's, which is why the
    preflight response is stamped.

    Neuter: N10 (registered before the CORS block) -> RED.
    """
    with isolated_state():
        classes = _user_middleware(create_app())
    assert classes.index(TraceIdHeaderMiddleware) < classes.index(CORSMiddleware)


def test_wiring_the_middleware_is_registered_even_with_cors_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The middleware does not depend on ``cors_origins``: with CORS disabled
    (production-safe BFF default, an empty list) it is still registered and
    the response is still stamped.

    Neuter: N8 (registered inside ``if cors_origins:``) -> RED.
    """
    from audittrace import config as config_mod

    with isolated_state():
        monkeypatch.setenv("AUDITTRACE_CORS_ORIGINS", "[]")
        config_mod.get_settings.cache_clear()
        harness = Harness("sdk")
        classes = _user_middleware(harness.app)
        response, servers = _one_request(harness, CASES_BY_NAME["route_200_json"])
    assert CORSMiddleware not in classes
    assert TraceIdHeaderMiddleware in classes
    assert response.headers.get_list("x-trace-id") == servers


# ---------------------------------------------------------------------------
# bonus pin: the inline 500-handler copy is gone (D1/D3)
# ---------------------------------------------------------------------------
def test_the_500_handler_envelope_keys_are_unchanged(sdk: Harness) -> None:
    """R3: the ADR-033 envelope keys are exactly the pre-existing eight (the
    header is additive; the body shape does not move)."""
    response = sdk.request(CASES_BY_NAME["unhandled_500_envelope"])
    assert sorted(response.json()["error"]) == sorted(
        [
            "code",
            "message",
            "operator_hint",
            "param",
            "status",
            "trace_id",
            "type",
            "user_facing_message",
        ]
    )
