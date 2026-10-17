"""#459 WU-459-1 T4 (golden template) + question registry + splice order."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from audittrace.services.decision import template as tpl
from audittrace.services.decision.questions import QUESTIONS, get_question

GOLDEN = json.loads(
    (
        Path(__file__).parent / "fixtures/decision/golden_chat_template_tev1.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.parametrize(
    "case", GOLDEN["cases"], ids=["X", "state-q-options", "unicode"]
)
def test_t4_golden_cases_byte_identical_and_hash_matches(case: dict[str, str]) -> None:
    rendered = tpl.wrap_chat_template(case["content"])
    assert rendered == case["rendered"]
    assert tpl.rendered_sha256(rendered) == case["sha256"]


def test_t4_render_choice_prompt_equals_golden_case_2() -> None:
    case = GOLDEN["cases"][1]
    rendered = tpl.render_choice_prompt("hello", "q?", ["one", "two"])
    assert rendered == case["rendered"]
    assert tpl.rendered_sha256(rendered) == case["sha256"]
    assert tpl.build_user_content("hello", "q?", ["one", "two"]) == case["content"]


def test_template_ends_with_empty_think_block() -> None:
    rendered = tpl.render_choice_prompt("s", "q", ["a", "b"])
    assert rendered.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")


def test_template_identity_pair_is_pinned() -> None:
    """Changing the template without bumping TEMPLATE_ID must fail here."""
    assert tpl.TEMPLATE_ID == "tev1-choice-v1"
    assert (
        tpl.TEMPLATE_SHA256
        == "350a4b6a1fc100542c479749a49d2a67b9f26027859cf8631837f48b4623dd71"
    )
    assert (
        tpl.TEMPLATE_SHA256 == hashlib.sha256(tpl.TEMPLATE_SOURCE.encode()).hexdigest()
    )


def test_upstream_chat_template_hash_is_the_measured_constant() -> None:
    assert (
        tpl.UPSTREAM_CHAT_TEMPLATE_SHA256
        == "d78de6bee4c952ca3145eb161921560a6ede7b59a34e7e4be815f3c5386b4364"
    )


@pytest.mark.parametrize("count", [0, 1, 25, 40])
def test_option_count_bounds_raise(count: int) -> None:
    with pytest.raises(ValueError, match="options"):
        tpl.render_choice_prompt("s", "q", ["x"] * count)


@pytest.mark.parametrize("count", [2, 24])
def test_option_count_bounds_accept_edges(count: int) -> None:
    out = tpl.build_user_content("s", "q", [f"o{i}" for i in range(count)])
    assert f"\n{tpl.LETTERS[count - 1]}. o{count - 1}\n" in out


def test_input_sha256_is_canonical_json_of_the_id_list() -> None:
    want = hashlib.sha256(b"[1,2,3]").hexdigest()
    assert tpl.input_sha256([1, 2, 3]) == want
    assert tpl.input_sha256([1, 2, 3]) != tpl.input_sha256([3, 2, 1])


def test_splice_places_special_ids_only_at_template_positions() -> None:
    special = {
        tpl.IM_START: 1,
        tpl.IM_END: 2,
        tpl.THINK_OPEN: 3,
        tpl.THINK_CLOSE: 4,
    }
    pieces = {
        tpl.USER_ROLE: [10],
        tpl.NEWLINE: [11],
        tpl.ASSISTANT_ROLE: [12],
        tpl.THINK_BODY: [13],
        tpl.AFTER_THINK: [13],
    }
    ids = tpl.splice_prompt_ids(special, pieces, [100, 101])
    assert ids == [1, 10, 100, 101, 2, 11, 1, 12, 3, 13, 4, 13]


def test_registry_has_exactly_memory_layer_v1_with_five_fixed_options() -> None:
    assert set(QUESTIONS) == {"memory_layer_v1"}
    q = QUESTIONS["memory_layer_v1"]
    assert q.question_id == "memory_layer_v1"
    assert q.text == "Which memory layer is most relevant to answer this?"
    assert q.options == (
        "none",
        "episodic (decision records)",
        "procedural (skills)",
        "conversational (session history)",
        "semantic (documents)",
    )
    assert q.question_sha256 == hashlib.sha256(q.text.encode()).hexdigest()
    assert (
        q.options_sha256
        == hashlib.sha256(
            json.dumps(list(q.options), separators=(",", ":")).encode()
        ).hexdigest()
    )


def test_registry_is_read_only() -> None:
    with pytest.raises(TypeError):
        QUESTIONS["x"] = QUESTIONS["memory_layer_v1"]  # type: ignore[index]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "nope",
        "MEMORY_LAYER_V1",
        None,
        3,
        b"memory_layer_v1",
        ["memory_layer_v1"],
        {"a": 1},
    ],
)
def test_get_question_unknown_or_non_str_is_none(bad: object) -> None:
    assert get_question(bad) is None
    assert get_question("memory_layer_v1") is QUESTIONS["memory_layer_v1"]
