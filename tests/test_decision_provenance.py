"""#459 WU-459-1 provenance row: T7 (exact key set + values), T9, S2 constants."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime

import httpx
import pytest

from audittrace.routes._memory_tool_loop import PendingToolCall
from audittrace.services.decision.client import LlamaCppDecisionClient
from audittrace.services.decision.distribution import renormalise
from audittrace.services.decision.provenance import (
    ARGS_KEYS,
    GRANTED_SCOPE,
    PROVENANCE,
    TOOL_NAME,
    build_args,
    build_pending_tool_call,
)
from audittrace.services.decision.result import DecisionResult
from tests.fakes.decision_server import (
    DEFAULT_TOP,
    FakeLlama,
    fake_tokenize,
    letter_top,
    make_settings,
)

Q = "memory_layer_v1"
STATE = "User asked about the retention policy."

# ONE literal key set (SF-3). T7 asserts EQUALITY with exactly this set.
EXPECTED_KEYS = {
    "template_id",
    "template_sha256",
    "upstream_chat_template_sha256",
    "question_id",
    "question_sha256",
    "options_sha256",
    "input_sha256",
    "allowed_ids",
    "decision_model",
    "decision_model_digest_configured",
    "server_model_file",
    "server_model_alias",
    "runtime",
    "runtime_version",
    "backend",
    "quantisation",
    "temperature",
    "sampler_params",
    "n_probs",
    "raw_top_logprobs",
    "distribution",
    "choice_index",
    "confidence",
    "entropy",
    "latency_ms",
    "mode",
    "acted",
    "result",
    "error_code",
}


async def _result(fake: FakeLlama | None = None, **settings: object) -> DecisionResult:
    fake = fake or FakeLlama()
    async with LlamaCppDecisionClient(
        make_settings(**settings), transport=fake.transport
    ) as client:
        return await client.decide(STATE, Q)


def _args(pending: PendingToolCall) -> dict[str, object]:
    loaded: dict[str, object] = json.loads(pending.args)
    return loaded


def _ref_dist(top: dict[int, float], ids: list[int], temp: float) -> list[float]:
    w = [math.exp(top[i] / temp) for i in ids]
    total = math.fsum(w)
    return [x / total for x in w]


# ------------------------------------------------------------------------ S2


def test_s2_row_identity_constants_by_value() -> None:
    assert TOOL_NAME == "system_one_route"
    assert PROVENANCE == "decision"
    assert GRANTED_SCOPE == "system:decision"
    assert GRANTED_SCOPE != ""  # "" means "refused" in the audit model
    assert len(PROVENANCE) <= 16  # tool_calls.provenance is String(16)


async def test_s2_pending_tool_call_carries_all_three_identity_values() -> None:
    p = build_pending_tool_call(
        await _result(), user_id="u-1", agent_type="agent-x", mode="shadow"
    )
    assert isinstance(p, PendingToolCall)
    assert p.tool_name == "system_one_route"
    assert p.provenance == "decision"
    assert p.granted_scope == "system:decision"
    assert p.phase is None and p.downstream_server is None and p.downstream_tool is None
    assert p.args_digest is None and p.result_digest is None


# ------------------------------------------------------------------------ T7


def test_args_keys_constant_equals_the_literal_set() -> None:
    assert set(ARGS_KEYS) == EXPECTED_KEYS
    assert len(ARGS_KEYS) == len(EXPECTED_KEYS) == 29


async def test_t7_ok_row_keys_equal_literal_set_and_every_value_by_value() -> None:
    r = await _result(decision_temperature=0.7)
    started = r.started_at
    p = build_pending_tool_call(r, user_id="u-1", agent_type="agent-x", mode="shadow")
    args = _args(p)
    assert set(args) == EXPECTED_KEYS  # equality, not "contains"

    ids = [ord(c) for c in "ABCDE"]
    top = {t["id"]: t["logprob"] for t in DEFAULT_TOP}
    dist = _ref_dist(top, ids, 0.7)
    rendered_ids = fake_tokenize(
        "<|im_start|>user\nState:\n"
        + STATE
        + "\n\nQuestion: Which memory layer is most "
        "relevant to answer this?\nOptions:\nA. none\nB. episodic (decision records)\n"
        "C. procedural (skills)\nD. conversational (session history)\n"
        "E. semantic (documents)\nAnswer with the option letter only."
        "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n",
        True,
    )
    assert (
        args["input_sha256"]
        == hashlib.sha256(
            json.dumps(rendered_ids, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert args["template_id"] == "tev1-choice-v1"
    assert args["template_sha256"] == (
        "350a4b6a1fc100542c479749a49d2a67b9f26027859cf8631837f48b4623dd71"
    )
    assert args["upstream_chat_template_sha256"] == (
        "d78de6bee4c952ca3145eb161921560a6ede7b59a34e7e4be815f3c5386b4364"
    )
    assert args["question_id"] == "memory_layer_v1"
    assert (
        args["question_sha256"]
        == hashlib.sha256(
            b"Which memory layer is most relevant to answer this?"
        ).hexdigest()
    )
    assert (
        args["options_sha256"]
        == hashlib.sha256(
            json.dumps(
                [
                    "none",
                    "episodic (decision records)",
                    "procedural (skills)",
                    "conversational (session history)",
                    "semantic (documents)",
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    assert args["allowed_ids"] == ids
    assert args["decision_model"] == "tev1-test-model"
    assert args["decision_model_digest_configured"] == "ab" * 32
    assert args["server_model_file"] == "tev1-test.gguf"
    assert args["server_model_alias"] == "tev1-test"
    assert args["runtime"] == "llama.cpp"
    assert args["runtime_version"] == "b-test"
    assert args["backend"] == "vulkan"
    assert args["quantisation"] == "f16"
    assert args["temperature"] == 0.7
    assert args["sampler_params"] == {
        "n_predict": 1,
        "n_probs": 20,
        "temperature": 0,
        "cache_prompt": False,
    }
    assert args["n_probs"] == 20
    assert args["raw_top_logprobs"] == {str(k): v for k, v in top.items()}
    got_dist = args["distribution"]
    assert isinstance(got_dist, list)
    for g, w in zip(got_dist, dist, strict=True):
        assert abs(g - w) <= 1e-12
    assert args["choice_index"] == 1
    assert args["confidence"] == max(got_dist)
    assert args["entropy"] == pytest.approx(
        -sum(x * math.log(x) for x in dist), abs=1e-12
    )
    assert args["latency_ms"] == r.latency_ms
    assert args["mode"] == "shadow"
    assert args["acted"] is False
    assert args["result"] == "ok"
    assert args["error_code"] is None

    assert p.user_id == "u-1" and p.agent_type == "agent-x"
    assert p.started_at == started and isinstance(p.started_at, datetime)
    assert p.duration_ms == r.latency_ms
    assert p.error is None
    assert p.result_summary == "result=ok choice_index=1"


async def test_t7_args_is_canonical_strict_json() -> None:
    p = build_pending_tool_call(
        await _result(), user_id="u", agent_type="a", mode="shadow"
    )
    assert p.args == json.dumps(
        json.loads(p.args), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def test_args_reject_nan_rather_than_emit_invalid_json() -> None:
    r = DecisionResult(
        result="ok",
        error_code=None,
        started_at=datetime.now(),
        latency_ms=1,
        confidence=float("nan"),
    )
    with pytest.raises(ValueError, match="JSON"):
        build_pending_tool_call(r, user_id="u", agent_type="a", mode="shadow")


FAILURES = [
    ("disabled", {"decision_url": "", "memory_routing_mode": "off"}, None),
    ("props-500", {}, ("/props", httpx.Response(500))),
    ("completion-500", {}, ("/completion", httpx.Response(500, text="x"))),
    ("mismatch", {"decision_model_alias": "other"}, None),
]


@pytest.mark.parametrize(
    ("name", "settings", "fail"), FAILURES, ids=[f[0] for f in FAILURES]
)
async def test_t7_failure_rows_have_every_key_with_null_for_unknowns(
    name: str, settings: dict[str, object], fail: tuple[str, httpx.Response] | None
) -> None:
    async def on(request: httpx.Request) -> httpx.Response | None:
        return fail[1] if fail and request.url.path == fail[0] else None

    r = await _result(FakeLlama(on_request=on), **settings)
    assert r.result != "ok"
    p = build_pending_tool_call(r, user_id="u", agent_type="a", mode="shadow")
    args = _args(p)
    assert set(args) == EXPECTED_KEYS  # equality, every time
    assert args["result"] == r.result and args["error_code"] == r.error_code
    assert args["error_code"] is not None
    for unknown in (
        "distribution",
        "choice_index",
        "confidence",
        "entropy",
        "raw_top_logprobs",
        "allowed_ids",
    ):
        assert args[unknown] is None, unknown
    assert (
        args["decision_model_digest_configured"] == "ab" * 32
    )  # config is always known
    assert args["temperature"] == 1.0
    assert p.error == r.error_code
    assert p.result_summary == f"result={r.result}"
    assert p.tool_name == TOOL_NAME and p.granted_scope == GRANTED_SCOPE


async def test_failure_row_after_server_identity_known_records_it() -> None:
    r = await _result(
        FakeLlama(on_request=_fail_path("/completion", httpx.Response(500))),
    )
    args = _args(build_pending_tool_call(r, user_id="u", agent_type="a", mode="shadow"))
    assert args["server_model_alias"] == "tev1-test"
    assert args["runtime_version"] == "b-test"
    assert args["input_sha256"] is not None
    assert args["question_id"] == Q and args["sampler_params"] is not None


def _fail_path(path: str, response: httpx.Response):  # type: ignore[no-untyped-def]
    async def on(request: httpx.Request) -> httpx.Response | None:
        return response if request.url.path == path else None

    return on


async def test_args_is_not_truncated_even_when_large() -> None:
    top = letter_top({"A": -1.0, "B": -2.0, "C": -3.0, "D": -4.0, "E": -5.0}) + [
        {"id": 10_000 + i, "token": f"t{i}", "logprob": -9.0 - i / 1000}
        for i in range(600)
    ]
    r = await _result(FakeLlama(top=top))
    p = build_pending_tool_call(r, user_id="u", agent_type="a", mode="shadow")
    assert len(p.args) > 10_000  # far beyond the 1000-char result_summary cut
    assert len(_args(p)["raw_top_logprobs"]) == 605  # type: ignore[arg-type]


# ------------------------------------------------------------------------ T9


@pytest.mark.parametrize("temp", [0.25, 0.7, 1.0, 1.5, 4.0])
async def test_t9_recompute_from_the_rows_own_fields_equals_recorded_exactly(
    temp: float,
) -> None:
    r = await _result(decision_temperature=temp)
    args = _args(build_pending_tool_call(r, user_id="u", agent_type="a", mode="shadow"))
    raw = {int(k): v for k, v in args["raw_top_logprobs"].items()}  # type: ignore[attr-defined]
    recomputed = renormalise(raw, args["allowed_ids"], args["temperature"])  # type: ignore[arg-type]
    assert recomputed == args["distribution"]  # EXACT: same implementation
    assert args["temperature"] == temp


async def test_t9_recorded_temperature_is_the_client_one_not_the_runtime_one() -> None:
    args = _args(
        build_pending_tool_call(
            await _result(decision_temperature=1.5),
            user_id="u",
            agent_type="a",
            mode="shadow",
        )
    )
    assert args["temperature"] == 1.5
    assert args["sampler_params"]["temperature"] == 0  # type: ignore[index]


# ----------------------------------------------------------- mode / acted guards


async def test_mode_must_be_shadow_or_acting() -> None:
    r = await _result()
    for ok in ("shadow", "acting"):
        assert (
            _args(build_pending_tool_call(r, user_id="u", agent_type="a", mode=ok))[
                "mode"
            ]
            == ok
        )
    for bad in ("off", "", "SHADOW", "triage"):
        with pytest.raises(ValueError, match="mode"):
            build_pending_tool_call(r, user_id="u", agent_type="a", mode=bad)


async def test_acted_true_is_refused_for_a_shadow_row() -> None:
    r = await _result()
    with pytest.raises(ValueError, match="acted"):
        build_pending_tool_call(
            r, user_id="u", agent_type="a", mode="shadow", acted=True
        )
    acting = build_pending_tool_call(
        r, user_id="u", agent_type="a", mode="acting", acted=True
    )
    assert _args(acting)["acted"] is True


async def test_build_args_is_pure_and_does_not_mutate_the_result() -> None:
    r = await _result()
    before = replace(r)
    a1 = build_args(r, mode="shadow", acted=False)
    a1["raw_top_logprobs"]["x"] = 1.0  # type: ignore[index]
    assert r == before
    assert "x" not in (
        build_args(r, mode="shadow", acted=False)["raw_top_logprobs"] or {}
    )
