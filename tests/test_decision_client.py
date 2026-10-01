"""#459 WU-459-1 client behaviour: T2/T3/T5/T6/T13/T15/T16 + request shape."""

from __future__ import annotations

import asyncio
import json
import math

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from audittrace.services.decision.client import LlamaCppDecisionClient
from audittrace.services.decision.distribution import renormalise
from audittrace.services.decision.errors import ERROR_CODES, result_for
from audittrace.services.decision.result import DecisionResult
from tests.fakes.decision_server import (
    ALIAS,
    DEFAULT_TOP,
    FakeLlama,
    letter_top,
    make_settings,
    sleep_then,
)

STATE = "User asked about the retention policy."
Q = "memory_layer_v1"


async def _decide(
    fake: FakeLlama, state: str = STATE, **settings: object
) -> DecisionResult:
    async with LlamaCppDecisionClient(
        make_settings(**settings), transport=fake.transport
    ) as client:
        return await client.decide(state, Q)


# ------------------------------------------------------------------ happy path


async def test_ok_result_values() -> None:
    fake = FakeLlama()
    r = await _decide(fake, decision_temperature=1.0)
    ids = [ord(c) for c in "ABCDE"]
    raw = {t["id"]: t["logprob"] for t in DEFAULT_TOP}
    want = renormalise(raw, ids, 1.0)
    assert (r.result, r.error_code) == ("ok", None)
    assert r.allowed_ids == tuple(ids)
    assert r.raw_top_logprobs == raw
    assert r.distribution == tuple(want)
    assert r.choice_index == 1  # B
    assert r.confidence == max(want)
    assert r.entropy is not None and r.entropy > 0
    assert r.latency_ms >= 0
    assert r.template_id == "tev1-choice-v1"
    assert r.question_id == Q
    assert r.decision_model == "tev1-test-model"
    assert r.server_model_file == "tev1-test.gguf"  # basename only
    assert r.server_model_alias == ALIAS
    assert r.runtime == "llama.cpp"
    assert r.runtime_version == "b-test"
    assert (r.backend, r.quantisation) == ("vulkan", "f16")
    assert r.temperature == 1.0


async def test_request_body_is_exactly_the_specified_shape() -> None:
    fake = FakeLlama()
    r = await _decide(fake)
    (req,) = fake.calls("POST", "/completion")
    body = json.loads(req.content)
    assert set(body) == {
        "prompt",
        "n_predict",
        "n_probs",
        "temperature",
        "cache_prompt",
    }
    assert isinstance(body["prompt"], list)
    assert all(isinstance(i, int) for i in body["prompt"])
    assert body["n_predict"] == 1
    assert body["n_probs"] == 20  # max(5 options, 20)
    assert body["temperature"] == 0
    assert body["cache_prompt"] is False
    assert r.sampler_params == {k: v for k, v in body.items() if k != "prompt"}
    assert r.n_probs == 20


async def test_tokenize_calls_always_state_parse_special_explicitly() -> None:
    fake = FakeLlama()
    await _decide(fake)
    for req in fake.calls("POST", "/tokenize"):
        assert isinstance(json.loads(req.content)["parse_special"], bool)


async def test_decide_uses_the_configured_temperature_not_the_runtime() -> None:
    fake = FakeLlama()
    hot = await _decide(fake, decision_temperature=4.0)
    cold = await _decide(FakeLlama(), decision_temperature=0.25)
    assert hot.temperature == 4.0 and cold.temperature == 0.25
    assert hot.choice_index == cold.choice_index == 1


async def test_t2_client_confidence_strictly_decreases_with_temperature() -> None:
    confs = []
    picks = []
    for temp in (0.25, 0.5, 0.7, 1.0, 1.5, 4.0):
        r = await _decide(FakeLlama(), decision_temperature=temp)
        assert r.confidence is not None
        confs.append(r.confidence)
        picks.append(r.choice_index)
    assert len(set(picks)) == 1
    assert all(b < a for a, b in zip(confs, confs[1:], strict=False)), confs


# ------------------------------------------------------------- T3: id matching


@pytest.mark.parametrize(
    "decoy_first", [True, False], ids=["decoy-before", "decoy-after"]
)
async def test_t3_matches_by_token_id_not_stripped_string(decoy_first: bool) -> None:
    true_b = {"id": ord("B"), "token": "B", "logprob": -0.9}
    # Same string after strip, different id, and a HIGHER logprob.
    decoy = {"id": 99999, "token": " B", "logprob": -0.05}
    rest = letter_top({"A": -3.0, "C": -2.5, "D": -1.4, "E": -4.0})
    pair = [decoy, true_b] if decoy_first else [true_b, decoy]
    fake = FakeLlama(top=rest + pair)
    r = await _decide(fake)
    assert r.result == "ok"
    ids = [ord(c) for c in "ABCDE"]
    want = renormalise(
        {**{t["id"]: t["logprob"] for t in rest}, ord("B"): -0.9}, ids, 1.0
    )
    assert r.distribution == tuple(want)
    assert r.raw_top_logprobs is not None
    assert r.raw_top_logprobs[ord("B")] == -0.9
    assert r.raw_top_logprobs[99999] == -0.05  # decoy kept as returned, never merged


# ----------------------------------------------------------- T6: disabled / T5


async def test_t6_disabled_means_no_network_at_all() -> None:
    fake = FakeLlama()
    s = make_settings(decision_url="", memory_routing_mode="off")
    async with LlamaCppDecisionClient(s, transport=fake.transport) as client:
        r = await client.decide(STATE, Q)
    assert (r.result, r.error_code) == ("unavailable", "disabled")
    assert fake.requests == []
    assert r.distribution is None and r.choice_index is None


def _failing(path: str, make) -> FakeLlama:  # type: ignore[no-untyped-def]
    async def on(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == path:
            return make(request)
        return None

    return FakeLlama(on_request=on)


def _boom(exc: Exception):  # type: ignore[no-untyped-def]
    def make(request: httpx.Request) -> httpx.Response:
        raise exc

    return make


CASES = [
    (
        "connect",
        "/props",
        _boom(httpx.ConnectError("refused")),
        "unavailable",
        "connect_error",
    ),
    (
        "connect-completion",
        "/completion",
        _boom(httpx.ConnectError("x")),
        "unavailable",
        "connect_error",
    ),
    (
        "read-error",
        "/completion",
        _boom(httpx.ReadError("x")),
        "unavailable",
        "connect_error",
    ),
    (
        "read-timeout",
        "/completion",
        _boom(httpx.ReadTimeout("x")),
        "timeout",
        "timeout",
    ),
    (
        "connect-timeout",
        "/props",
        _boom(httpx.ConnectTimeout("x")),
        "timeout",
        "timeout",
    ),
    (
        "401",
        "/completion",
        lambda r: httpx.Response(401, json={"e": 1}),
        "error",
        "http_status",
    ),
    (
        "500",
        "/completion",
        lambda r: httpx.Response(500, text="boom"),
        "error",
        "http_status",
    ),
    ("props-500", "/props", lambda r: httpx.Response(500), "error", "http_status"),
    (
        "tokenize-503",
        "/tokenize",
        lambda r: httpx.Response(503),
        "error",
        "http_status",
    ),
    (
        "malformed-json",
        "/completion",
        lambda r: httpx.Response(200, text="{not json"),
        "error",
        "malformed_response",
    ),
    (
        "props-malformed-json",
        "/props",
        lambda r: httpx.Response(200, text="<html>"),
        "error",
        "malformed_response",
    ),
    (
        "props-not-object",
        "/props",
        lambda r: httpx.Response(200, json=[1]),
        "error",
        "malformed_response",
    ),
    (
        "props-missing-field",
        "/props",
        lambda r: httpx.Response(
            200, json={"model_path": "/a/b.gguf", "model_alias": ALIAS}
        ),
        "error",
        "malformed_response",
    ),
    (
        "tokenize-shape",
        "/tokenize",
        lambda r: httpx.Response(200, json={"tokens": "x"}),
        "error",
        "malformed_response",
    ),
    (
        "tokenize-bool",
        "/tokenize",
        lambda r: httpx.Response(200, json={"tokens": [True]}),
        "error",
        "malformed_response",
    ),
    (
        "tokenize-not-object",
        "/tokenize",
        lambda r: httpx.Response(200, json=[1]),
        "error",
        "malformed_response",
    ),
    (
        "missing-probs",
        "/completion",
        lambda r: httpx.Response(200, json={"content": "B"}),
        "error",
        "missing_probabilities",
    ),
    (
        "empty-probs",
        "/completion",
        lambda r: httpx.Response(200, json={"completion_probabilities": []}),
        "error",
        "missing_probabilities",
    ),
    (
        "no-top",
        "/completion",
        lambda r: httpx.Response(200, json={"completion_probabilities": [{"id": 1}]}),
        "error",
        "missing_probabilities",
    ),
    (
        "empty-top",
        "/completion",
        lambda r: httpx.Response(
            200, json={"completion_probabilities": [{"top_logprobs": []}]}
        ),
        "error",
        "missing_probabilities",
    ),
    (
        "probs-not-object",
        "/completion",
        lambda r: httpx.Response(200, json=[1]),
        "error",
        "missing_probabilities",
    ),
    (
        "entry-not-object",
        "/completion",
        lambda r: httpx.Response(
            200, json={"completion_probabilities": [{"top_logprobs": [3]}]}
        ),
        "error",
        "malformed_response",
    ),
    (
        "entry-bad-id",
        "/completion",
        lambda r: httpx.Response(
            200,
            json={
                "completion_probabilities": [
                    {"top_logprobs": [{"id": "x", "logprob": -1.0}]}
                ]
            },
        ),
        "error",
        "malformed_response",
    ),
    (
        "entry-bool-id",
        "/completion",
        lambda r: httpx.Response(
            200,
            json={
                "completion_probabilities": [
                    {"top_logprobs": [{"id": True, "logprob": -1.0}]}
                ]
            },
        ),
        "error",
        "malformed_response",
    ),
    (
        "entry-bad-logprob",
        "/completion",
        lambda r: httpx.Response(
            200,
            json={
                "completion_probabilities": [
                    {"top_logprobs": [{"id": 1, "logprob": "x"}]}
                ]
            },
        ),
        "error",
        "malformed_response",
    ),
    (
        "entry-bool-logprob",
        "/completion",
        lambda r: httpx.Response(
            200,
            json={
                "completion_probabilities": [
                    {"top_logprobs": [{"id": 1, "logprob": True}]}
                ]
            },
        ),
        "error",
        "malformed_response",
    ),
    (
        "entry-non-finite",
        "/completion",
        lambda r: httpx.Response(
            200,
            content=b'{"completion_probabilities":[{"top_logprobs":[{"id":1,"logprob":-Infinity}]}]}',
        ),
        "error",
        "malformed_response",
    ),
    (
        "no-allowed-in-top",
        "/completion",
        lambda r: httpx.Response(
            200,
            json={
                "completion_probabilities": [
                    {"top_logprobs": [{"id": 1000, "logprob": -0.1}]}
                ]
            },
        ),
        "error",
        "no_allowed_token_in_top",
    ),
    (
        "unexpected-exception",
        "/completion",
        _boom(RuntimeError("secret internal text")),
        "error",
        "malformed_response",
    ),
]


@pytest.mark.parametrize(
    ("path", "make", "result", "code"),
    [c[1:] for c in CASES],
    ids=[c[0] for c in CASES],
)
async def test_t5_fail_open_matrix_never_raises(path, make, result, code) -> None:  # type: ignore[no-untyped-def]
    r = await _decide(_failing(path, make))
    assert (r.result, r.error_code) == (result, code)
    assert r.distribution is None and r.choice_index is None and r.confidence is None
    assert r.raw_top_logprobs is None and r.allowed_ids is None
    assert r.error_code in ERROR_CODES
    assert result_for(code) == result


async def test_t5_non_single_token_option_fails_closed() -> None:
    def tok(text: str, ps: bool) -> list[int]:
        from tests.fakes.decision_server import fake_tokenize

        return [1, 2] if text == "C" else fake_tokenize(text, ps)

    r = await _decide(FakeLlama(tokenize=tok))
    assert (r.result, r.error_code) == ("error", "option_not_single_token")


async def test_t5_non_single_token_option_makes_no_completion_call() -> None:
    from tests.fakes.decision_server import fake_tokenize

    fake = FakeLlama(tokenize=lambda t, ps: [] if t == "A" else fake_tokenize(t, ps))
    r = await _decide(fake)
    assert r.error_code == "option_not_single_token"
    assert fake.calls("POST", "/completion") == []


@pytest.mark.parametrize(
    "missing", ["<|im_start|>", "<|im_end|>", "<think>", "</think>"]
)
async def test_t5_unresolved_template_token(missing: str) -> None:
    from tests.fakes.decision_server import fake_tokenize

    def tok(text: str, ps: bool) -> list[int]:
        return [7, 8] if (text == missing and ps) else fake_tokenize(text, ps)

    fake = FakeLlama(tokenize=tok)
    r = await _decide(fake)
    assert (r.result, r.error_code) == ("error", "template_token_unresolved")
    assert fake.calls("POST", "/completion") == []


@pytest.mark.parametrize("bad_state", [None, 3, b"x", ["x"]])
async def test_invalid_state_type_is_invalid_input_without_network(
    bad_state: object,
) -> None:
    fake = FakeLlama()
    async with LlamaCppDecisionClient(
        make_settings(), transport=fake.transport
    ) as client:
        r = await client.decide(bad_state, Q)  # type: ignore[arg-type]
    assert (r.result, r.error_code) == ("error", "invalid_input")
    assert fake.requests == []


@pytest.mark.parametrize(
    "qid", ["", "nope", "memory_layer_v2", None, ["memory_layer_v1"]]
)
async def test_unknown_question_id_is_invalid_input_without_network(
    qid: object,
) -> None:
    fake = FakeLlama()
    async with LlamaCppDecisionClient(
        make_settings(), transport=fake.transport
    ) as client:
        r = await client.decide(STATE, qid)  # type: ignore[arg-type]
    assert (r.result, r.error_code) == ("error", "invalid_input")
    assert r.question_id is None  # untrusted caller text is not echoed
    assert fake.requests == []


# ----------------------------------------------------------- T13: identity


async def test_t13_alias_mismatch_blocks_inference() -> None:
    fake = FakeLlama(alias="X")
    r = await _decide(fake, decision_model_alias="Y")
    assert (r.result, r.error_code) == ("error", "model_identity_mismatch")
    assert fake.calls("POST", "/completion") == []
    assert fake.calls("POST", "/tokenize") == []
    # The row still records what the server reported.
    assert r.server_model_alias == "X"
    assert r.server_model_file == "tev1-test.gguf"
    assert r.runtime_version == "b-test"


async def test_t13_props_is_requested_with_get_never_post() -> None:
    fake = FakeLlama()
    await _decide(fake)
    props = [r for r in fake.requests if r.url.path == "/props"]
    assert [r.method for r in props] == ["GET"]
    assert all(r.method == "POST" for r in fake.requests if r.url.path != "/props")


async def test_identity_and_token_tables_are_fetched_once_per_client() -> None:
    fake = FakeLlama()
    async with LlamaCppDecisionClient(make_settings(), transport=fake.transport) as c:
        a = await c.decide(STATE, Q)
        n_tokenize_first = len(fake.calls("POST", "/tokenize"))
        b = await c.decide("another state", Q)
    assert a.result == b.result == "ok"
    assert len(fake.calls("GET", "/props")) == 1
    # second decide adds exactly ONE tokenize call (the user content).
    assert len(fake.calls("POST", "/tokenize")) == n_tokenize_first + 1
    assert len(fake.calls("POST", "/completion")) == 2


async def test_failed_identity_is_not_cached() -> None:
    fake = FakeLlama(alias="X")
    async with LlamaCppDecisionClient(
        make_settings(decision_model_alias="Y"), transport=fake.transport
    ) as c:
        await c.decide(STATE, Q)
        fake.alias = "Y"
        r = await c.decide(STATE, Q)
    assert r.result == "ok"
    assert len(fake.calls("GET", "/props")) == 2


async def test_server_model_file_is_basename_for_posix_and_windows_paths() -> None:
    for path in ("/home/u/m/model.gguf", "C:\\Users\\u\\m\\model.gguf", "model.gguf"):
        r = await _decide(FakeLlama(model_path=path))
        assert r.server_model_file == "model.gguf"


# ------------------------------------------------------------ auth header (P10)


async def test_api_key_sent_as_bearer_on_every_request() -> None:
    fake = FakeLlama()
    await _decide(fake, decision_api_key="k-secret-123")
    assert fake.requests
    assert {r.headers.get("authorization") for r in fake.requests} == {
        "Bearer k-secret-123"
    }


async def test_no_authorization_header_when_no_key() -> None:
    fake = FakeLlama()
    await _decide(fake)
    assert all("authorization" not in r.headers for r in fake.requests)


# --------------------------------------------------------- T15 / T16: control


async def test_t15_cancellation_propagates() -> None:
    started = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/completion":
            started.set()
            await asyncio.sleep(30)
        return None

    fake = FakeLlama(on_request=hang)
    async with LlamaCppDecisionClient(
        make_settings(decision_timeout_ms=60000), transport=fake.transport
    ) as client:
        task = asyncio.create_task(client.decide(STATE, Q))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_t16_total_budget_spans_all_calls_and_skips_completion() -> None:
    async def slow(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/tokenize":
            await asyncio.sleep(0.15)  # 11+ tokenize calls >> 0.4 s budget
        return None

    fake = FakeLlama(on_request=slow)
    r = await _decide(fake, decision_timeout_ms=400)
    assert (r.result, r.error_code) == ("timeout", "timeout")
    assert fake.calls("POST", "/completion") == []
    assert r.latency_ms < 2000  # bounded, not N x per-call


async def test_total_budget_when_completion_is_the_slow_call() -> None:
    fake = FakeLlama(
        on_request=lambda req: (
            sleep_then(5) if req.url.path == "/completion" else _none()
        )
    )
    r = await _decide(fake, decision_timeout_ms=300)
    assert (r.result, r.error_code) == ("timeout", "timeout")


async def _none() -> None:
    return None


async def test_latency_ms_reports_elapsed_wall_time() -> None:
    fake = FakeLlama(
        on_request=lambda req: sleep_then(0.12) if req.url.path == "/props" else _none()
    )
    r = await _decide(fake)
    assert r.result == "ok"
    assert 100 <= r.latency_ms < 2000


# ------------------------------------------------------------------- telemetry


def _provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


async def test_span_is_child_of_current_context_with_only_allowed_attributes() -> None:
    provider, exporter = _provider()
    tracer = provider.get_tracer("t")
    fake = FakeLlama()
    async with LlamaCppDecisionClient(
        make_settings(), transport=fake.transport, tracer=tracer
    ) as client:
        with tracer.start_as_current_span("parent") as parent:
            r = await client.decide(STATE, Q)
    spans = {s.name: s for s in exporter.get_finished_spans()}
    child = spans["decision.decide"]
    assert child.parent is not None
    assert child.parent.span_id == parent.get_span_context().span_id
    assert child.context.trace_id == parent.get_span_context().trace_id
    assert dict(child.attributes or {}) == {
        "template_id": "tev1-choice-v1",
        "input_sha256": r.input_sha256,
        "result": "ok",
        "choice_index": 1,
        "confidence": r.confidence,
        "latency_ms": r.latency_ms,
        "decision_model_digest_configured": "ab" * 32,
    }


async def test_span_on_failure_omits_unknown_attributes() -> None:
    provider, exporter = _provider()
    fake = FakeLlama()
    async with LlamaCppDecisionClient(
        make_settings(decision_url="", memory_routing_mode="off"),
        transport=fake.transport,
        tracer=provider.get_tracer("t"),
    ) as client:
        await client.decide(STATE, Q)
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes or {})
    assert attrs["result"] == "unavailable" and attrs["error_code"] == "disabled"
    assert "choice_index" not in attrs and "confidence" not in attrs
    assert "input_sha256" not in attrs


async def test_default_tracer_is_the_global_otel_tracer() -> None:
    fake = FakeLlama()
    async with LlamaCppDecisionClient(
        make_settings(), transport=fake.transport
    ) as client:
        assert (await client.decide(STATE, Q)).result == "ok"
    assert trace.get_tracer(__name__) is not None


# --------------------------------------------------------------- misc surface


async def test_repr_of_client_has_no_key_or_settings() -> None:
    async with LlamaCppDecisionClient(
        make_settings(decision_api_key="k-secret-123"), transport=FakeLlama().transport
    ) as c:
        text = repr(c)
    assert "k-secret-123" not in text and "Settings" not in text
    assert "decision.test" in text


async def test_trailing_slash_in_url_is_normalised() -> None:
    fake = FakeLlama()
    r = await _decide(fake, decision_url="http://decision.test/")
    assert r.result == "ok"
    assert {q.url.path for q in fake.requests} == {"/props", "/tokenize", "/completion"}


async def test_distribution_sums_to_one_and_confidence_is_max() -> None:
    r = await _decide(FakeLlama(top=letter_top({"A": -0.1, "B": -0.1, "C": -9.0})))
    assert r.distribution is not None
    assert math.isclose(sum(r.distribution), 1.0, abs_tol=1e-12)
    assert r.choice_index == 0  # tie -> first
    assert r.confidence == max(r.distribution)


def test_error_vocabulary_is_closed() -> None:
    from audittrace.services.decision import _llama_http as llama
    from audittrace.services.decision.errors import DecisionError

    assert ERROR_CODES == {
        "disabled",
        "connect_error",
        "timeout",
        "http_status",
        "malformed_response",
        "missing_probabilities",
        "option_not_single_token",
        "template_token_unresolved",
        "no_allowed_token_in_top",
        "model_identity_mismatch",
        "invalid_input",
    }
    with pytest.raises(ValueError, match="unknown error_code"):
        DecisionError("made_up")
    assert DecisionError("timeout").error_code == "timeout"
    assert llama.map_transport_error(RuntimeError("x")) is None
    assert llama.map_transport_error(httpx.ReadTimeout("x")) == "timeout"
    assert llama.map_transport_error(httpx.ConnectError("x")) == "connect_error"
    assert {result_for(c) for c in ERROR_CODES} == {"unavailable", "timeout", "error"}
    assert result_for("disabled") == "unavailable"
