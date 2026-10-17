"""#459 WU-459-1 T8 (no text leak) and T11 (the secret is never logged)."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from audittrace.services.decision.client import LlamaCppDecisionClient
from audittrace.services.decision.provenance import build_pending_tool_call
from audittrace.services.decision.questions import QUESTIONS, Question
from audittrace.services.decision.result import DecisionResult
from tests.fakes.decision_server import FakeLlama, make_settings

Q = "memory_layer_v1"
CANARY_STATE = "CANARY-STATE-7f3a91"
CANARY_QUESTION = "CANARY-QUESTION-52bc10"
CANARY_OPTION = "CANARY-OPTION-e08d44"
CANARIES = (CANARY_STATE, CANARY_QUESTION, CANARY_OPTION)
KEY = "KEYCANARY-9d2e6b1c-secret"


@pytest.fixture
def canary_registry(monkeypatch: pytest.MonkeyPatch) -> Question:
    base = QUESTIONS[Q]
    q = Question(Q, f"{CANARY_QUESTION}?", (*base.options[:-1], CANARY_OPTION))
    monkeypatch.setattr(
        "audittrace.services.decision.client.get_question", lambda _q: q
    )
    return q


def _echo(path: str, make):  # type: ignore[no-untyped-def]
    """Fail ``path`` with a response/exception that ECHOES the prompt."""

    async def on(request: httpx.Request) -> httpx.Response | None:
        if request.url.path != path:
            return None
        return make(request)

    return on


def _echo_body(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text=request.content.decode() + CANARY_STATE)


def _echo_malformed(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="{" + request.content.decode())


def _echo_exception(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"cannot connect: {request.content.decode()}")


def _echo_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout(f"timed out: {request.content.decode()}")


def _echo_runtime(request: httpx.Request) -> httpx.Response:
    raise RuntimeError(f"boom {request.content.decode()} {CANARY_STATE}")


SCENARIOS = {
    "ok": FakeLlama(),
    "completion-500-echo": FakeLlama(on_request=_echo("/completion", _echo_body)),
    "tokenize-500-echo": FakeLlama(on_request=_echo("/tokenize", _echo_body)),
    "malformed-echo": FakeLlama(on_request=_echo("/completion", _echo_malformed)),
    "timeout-echo": FakeLlama(on_request=_echo("/completion", _echo_timeout)),
    "connect-echo": FakeLlama(on_request=_echo("/completion", _echo_exception)),
    "unexpected-echo": FakeLlama(on_request=_echo("/completion", _echo_runtime)),
}


def _scenario(name: str) -> FakeLlama:
    # Fresh instance per test (the shared dict above only names the recipes).
    template = SCENARIOS[name]
    return FakeLlama(on_request=template.on_request)


def _provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _serialise_spans(exporter: InMemorySpanExporter) -> str:
    out = []
    for span in exporter.get_finished_spans():
        out.append(span.name)
        out.append(repr(dict(span.attributes or {})))
        out.append(repr([(e.name, dict(e.attributes or {})) for e in span.events]))
        out.append(str(span.status.description or ""))
    return "\n".join(out)


def _debug_logs(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(
        f"{r.name}|{r.getMessage()}|{r.exc_text or ''}" for r in caplog.records
    )


async def _run_scenario(
    fake: FakeLlama,
    caplog: pytest.LogCaptureFixture,
    **settings: object,
) -> DecisionResult:
    """Run one decide() with canaries everywhere and assert NO surface has one."""
    caplog.set_level(logging.DEBUG)
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.DEBUG)
    provider, exporter = _provider()
    async with LlamaCppDecisionClient(
        make_settings(**settings),
        transport=fake.transport,
        tracer=provider.get_tracer("t"),
    ) as client:
        result = await client.decide(f"{CANARY_STATE} please", Q)
    pending = build_pending_tool_call(
        result, user_id="u", agent_type="a", mode="shadow"
    )
    surfaces = {
        "args": pending.args,
        "result_summary": pending.result_summary or "",
        "error": pending.error or "",
        "repr(result)": repr(result),
        "repr(pending)": repr(pending),
        "spans": _serialise_spans(exporter),
        "logs": _debug_logs(caplog),
    }
    for surface, text in surfaces.items():
        for canary in CANARIES:
            assert canary not in text, (surface, canary)
    return result


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_t8_no_canary_in_row_spans_or_logs_on_any_path(
    name: str, canary_registry: Question, caplog: pytest.LogCaptureFixture
) -> None:
    result = await _run_scenario(_scenario(name), caplog)
    # Sanity: the scenario really ran the way its name says.
    assert (result.result == "ok") == (name == "ok")


# --- B2: the /tokenize call carries the TEXT, and so does the deadline path.

_TEXT_BEARING = "/tokenize"


def _on_user_text_call(action):  # type: ignore[no-untyped-def]
    """Run ``action`` ONLY on the /tokenize call whose body carries the canary."""

    async def on(request: httpx.Request) -> httpx.Response | None:
        if (
            request.url.path == _TEXT_BEARING
            and CANARY_STATE in request.content.decode()
        ):
            return await action(request)
        return None

    return on


def _raising(make):  # type: ignore[no-untyped-def]
    async def action(request: httpx.Request) -> httpx.Response:
        raise make(request.content.decode())

    return action


# Every exception class the client handles, raised on the text-bearing call,
# each ECHOING the request body in its message.
TOKENIZE_EXCEPTIONS = {
    "connect-error": (
        lambda body: httpx.ConnectError(f"refused {body}"),
        "unavailable",
    ),
    "read-error": (lambda body: httpx.ReadError(f"read {body}"), "unavailable"),
    "write-error": (lambda body: httpx.WriteError(f"write {body}"), "unavailable"),
    "remote-protocol": (
        lambda body: httpx.RemoteProtocolError(f"proto {body}"),
        "unavailable",
    ),
    "unsupported-protocol": (
        lambda body: httpx.UnsupportedProtocol(f"scheme {body}"),
        "unavailable",
    ),
    "read-timeout": (lambda body: httpx.ReadTimeout(f"slow {body}"), "timeout"),
    "connect-timeout": (lambda body: httpx.ConnectTimeout(f"slow {body}"), "timeout"),
    "pool-timeout": (lambda body: httpx.PoolTimeout(f"pool {body}"), "timeout"),
    "decoding-error": (lambda body: httpx.DecodingError(f"decode {body}"), "error"),
    "too-many-redirects": (
        lambda body: httpx.TooManyRedirects(f"loop {body}"),
        "error",
    ),
    "runtime-error": (lambda body: RuntimeError(f"bug {body}"), "error"),
    "builtin-timeout": (lambda body: TimeoutError(f"deadline {body}"), "timeout"),
}


@pytest.mark.parametrize("name", list(TOKENIZE_EXCEPTIONS))
async def test_t8_exception_on_the_text_bearing_tokenize_call(
    name: str, canary_registry: Question, caplog: pytest.LogCaptureFixture
) -> None:
    make, want = TOKENIZE_EXCEPTIONS[name]
    fake = FakeLlama(on_request=_on_user_text_call(_raising(make)))
    result = await _run_scenario(fake, caplog)
    assert result.result == want
    # Anti-vacuity: the failing request really carried the canary text.
    failing = [
        r
        for r in fake.requests
        if r.url.path == "/tokenize" and CANARY_STATE in r.content.decode()
    ]
    assert len(failing) == 1


async def _http_500(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text=request.content.decode())


async def _malformed(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="{" + request.content.decode())


@pytest.mark.parametrize(
    ("name", "action", "want"),
    [
        ("tokenize-500-echo", _http_500, "error"),
        ("tokenize-malformed-echo", _malformed, "error"),
    ],
)
async def test_t8_error_response_on_the_text_bearing_tokenize_call(
    name: str,
    action,
    want: str,
    canary_registry: Question,
    caplog: pytest.LogCaptureFixture,
) -> None:  # type: ignore[no-untyped-def]
    fake = FakeLlama(on_request=_on_user_text_call(action))
    result = await _run_scenario(fake, caplog)
    assert result.result == want


def _slow(path: str, seconds: float):  # type: ignore[no-untyped-def]
    async def on(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == path:
            await asyncio.sleep(seconds)
        return None

    return on


@pytest.mark.parametrize("slow_path", ["/props", "/tokenize", "/completion"])
async def test_t8_total_deadline_expiry_leaks_nothing(
    slow_path: str, canary_registry: Question, caplog: pytest.LogCaptureFixture
) -> None:
    """The production timeout path (one total budget): logs at DEBUG, canaries
    in the state, question and options, the deadline expiring mid-call."""
    fake = FakeLlama(on_request=_slow(slow_path, 5.0))
    result = await _run_scenario(fake, caplog, decision_timeout_ms=150)
    assert (result.result, result.error_code) == ("timeout", "timeout")
    if slow_path == "/tokenize":
        # The text-bearing call itself was in flight when the deadline hit
        # (the first /tokenize call, a template special, already hangs).
        assert fake.calls("POST", "/tokenize")
    assert fake.calls("POST", "/completion") == [] or slow_path == "/completion"


async def test_t8_deadline_expiring_exactly_on_the_text_bearing_call(
    canary_registry: Question, caplog: pytest.LogCaptureFixture
) -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"tokens": [1]})

    fake = FakeLlama(on_request=_on_user_text_call(hang))
    result = await _run_scenario(fake, caplog, decision_timeout_ms=300)
    assert (result.result, result.error_code) == ("timeout", "timeout")
    assert any(
        CANARY_STATE in r.content.decode() for r in fake.calls("POST", "/tokenize")
    )  # the canary-bearing request was sent and was the one in flight


async def test_t8_canaries_are_really_part_of_the_tokenised_content(
    canary_registry: Question,
) -> None:
    """Anti-vacuity: the tokenize call DID carry the canary text."""
    fake = FakeLlama()
    async with LlamaCppDecisionClient(make_settings(), transport=fake.transport) as c:
        await c.decide(CANARY_STATE, Q)
    sent = " ".join(r.content.decode() for r in fake.calls("POST", "/tokenize"))
    for canary in CANARIES:
        assert canary in sent


# ------------------------------------------------------------------------ T11


@pytest.mark.parametrize("scenario", ["ok", "completion-401", "props-401"])
async def test_t11_api_key_never_logged_recorded_or_in_a_repr(
    scenario: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.DEBUG)
    provider, exporter = _provider()
    path = {"completion-401": "/completion", "props-401": "/props"}.get(scenario)
    fake = FakeLlama(
        on_request=_echo(path, lambda r: httpx.Response(401, json={"e": "no"}))
        if path
        else None
    )
    async with LlamaCppDecisionClient(
        make_settings(decision_api_key=KEY),
        transport=fake.transport,
        tracer=provider.get_tracer("t"),
    ) as client:
        result = await client.decide("some state", Q)
        client_repr = repr(client)
    assert (result.error_code == "http_status") == (path is not None)
    pending = build_pending_tool_call(
        result, user_id="u", agent_type="a", mode="shadow"
    )
    # The key was really sent (otherwise the absence below proves nothing).
    assert fake.requests[0].headers["authorization"] == f"Bearer {KEY}"
    surfaces = {
        "logs": _debug_logs(caplog),
        "spans": _serialise_spans(exporter),
        "repr(result)": repr(result),
        "repr(client)": client_repr,
        "repr(pending)": repr(pending),
        "args": pending.args,
        "summary+error": f"{pending.result_summary}{pending.error}",
    }
    for surface, text in surfaces.items():
        assert KEY not in text, surface


def test_settings_repr_contains_the_plain_str_key_by_design() -> None:
    """Recorded, pre-existing pattern (like langfuse_secret_key): Settings must
    never be logged. This pins the fact so nobody assumes SecretStr."""
    assert KEY in repr(make_settings(decision_api_key=KEY))
