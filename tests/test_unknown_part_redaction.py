"""#474 F-d -- a content part of a type the service does not know is redacted whole.

OpenAI's chat content-part types are ``text``, ``image_url``, ``input_audio``, ``file``
and (assistant side) ``refusal``; the model server also reads ``input_video``. The
service knows exactly those. A part of any other type (``custom_blob``, a future type, a
non-string ``type``) is NOT trusted to be text: for telemetry and ingestion its payload
is replaced by a placeholder that keeps the type name (when it is a short plain
identifier) and the number of keys, and nothing else.

What is pinned here, and what each pin is the counterpart of:

* the redaction function itself (allow-list, fail-closed shapes, input never mutated);
* the five witnesses of the route on all four arms (inject / tools x stream / non-stream):
  every ended span attribute, every DEBUG log record, the Langfuse ingestion POST body;
  and, for the ``sovereign-chat-request`` attributes (an ended span only under a live
  Langfuse client), the function that builds them;
* ADR-024: the model server receives the part byte for byte.

The payload is 96 base64 characters, under the export-time 128-character run rule, so
only redaction by part type can remove it.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from audittrace.media.redaction import redact_media_payloads
from tests import conftest as net
from tests._image_slice import (
    ARM_IDS,
    ARMS,
    QUESTION,
    flip_mode,
    non_upstream_posts,
    ok_fake,
    post_chat,
    upstream_bodies,
)

#: 72 bytes -> 96 base64 characters
SHORT = base64.b64encode(bytes(range(72))).decode()

SHAPES: dict[str, dict[str, Any]] = {
    "data-key": {"type": "custom_blob", "data": SHORT},
    "nested-one": {"type": "custom_blob", "blob": {"u": SHORT}},
    "nested-list": {"type": "custom_blob", "blobs": [[SHORT]]},
    "future-type": {"type": "input_hologram", "input_hologram": {"data": SHORT}},
    # known media types: the payload of ANY length never reaches telemetry (R-1 closed)
    "audio-short": {
        "type": "input_audio",
        "input_audio": {"data": SHORT, "format": "wav"},
    },
    "video-short": {"type": "input_video", "input_video": {"data": SHORT}},
    "file-short": {"type": "file", "file": {"filename": "n.pdf", "file_data": SHORT}},
    "audio-url-safe": {
        "type": "input_audio",
        "input_audio": {
            "data": SHORT.replace("+", "-").replace("/", "_") + "-_",
            "format": "wav",
        },
    },
}


def _user(part: Any) -> list[dict[str, Any]]:
    return [{"role": "user", "content": [{"type": "text", "text": QUESTION}, part]}]


@pytest.fixture(params=ARMS, ids=ARM_IDS)
def arm(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, bool]]:
    mode, stream = request.param
    for _ in flip_mode(monkeypatch, mode):
        yield mode, stream


@pytest.fixture(params=sorted(SHAPES))
def shape(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def langfuse_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from audittrace import config as config_mod

    monkeypatch.setenv("AUDITTRACE_LANGFUSE_HOST", "http://lf.test")
    monkeypatch.setenv("AUDITTRACE_LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("AUDITTRACE_LANGFUSE_SECRET_KEY", "sk-test")
    config_mod.get_settings.cache_clear()
    yield
    config_mod.get_settings.cache_clear()


@pytest.fixture
def debug_logs() -> Iterator[None]:
    root = logging.getLogger()
    before = root.level
    root.setLevel(logging.DEBUG)
    yield
    root.setLevel(before)


class TestTheRedactionFunction:
    @pytest.mark.parametrize("kind", ["text", "refusal"])
    def test_text_bearing_parts_pass_through_unchanged(self, kind: str) -> None:
        part = {"type": kind, kind: "plain words"}
        assert redact_media_payloads(_user(part), None)[0]["content"][1] == part

    def test_an_image_url_part_without_a_record_keeps_its_shape(self) -> None:
        part = {"type": "image_url", "image_url": {"url": "x", "detail": "low"}}
        assert redact_media_payloads(_user(part), None)[0]["content"][1] == part

    def test_a_part_of_an_unknown_type_keeps_its_type_and_key_count_only(self) -> None:
        out = redact_media_payloads(_user(SHAPES["data-key"]), None)
        assert out[0]["content"][1] == {
            "type": "custom_blob",
            "redacted": "[REDACTED:media custom_blob keys=2]",
        }

    @pytest.mark.parametrize(
        "part",
        [
            {"type": SHORT, "data": "x"},
            {"type": 7, "data": SHORT},
            {"type": None, "data": SHORT},
            {"type": ["input_audio"], "data": SHORT},
            {"data": SHORT},
            {"type": "", "data": SHORT},
            {"type": "a" * 33, "data": SHORT},
            {"type": "has space", "data": SHORT},
        ],
        ids=[
            "type-is-the-payload",
            "int-type",
            "null-type",
            "list-type",
            "no-type",
            "empty-type",
            "overlong-type",
            "not-an-identifier",
        ],
    )
    def test_a_type_that_is_not_a_short_identifier_is_not_kept(
        self, part: dict[str, Any]
    ) -> None:
        out = redact_media_payloads(_user(part), None)[0]["content"][1]
        assert SHORT not in json.dumps(out)
        assert out["type"] == "unknown"
        assert set(out) == {"type", "redacted"}

    @pytest.mark.parametrize(
        "part",
        [
            {"type": "text", "text": "hi", "data": SHORT},
            {"type": "text", "text": {"nested": SHORT}},
            {"type": "refusal", "refusal": ["x"], "x": SHORT},
            {"type": "image_url", "image_url": {"url": "x"}, "blob": SHORT},
            {"type": "input_audio", "input_audio": {"data": "x"}, "blob": SHORT},
        ],
        ids=["text-extra", "text-nested", "refusal-bad", "image-extra", "audio-extra"],
    )
    def test_a_known_type_carrying_something_else_is_redacted_too(
        self, part: dict[str, Any]
    ) -> None:
        out = redact_media_payloads(_user(part), None)[0]["content"][1]
        assert SHORT not in json.dumps(out)

    def test_the_input_is_never_mutated(self) -> None:
        messages = _user(copy.deepcopy(SHAPES["nested-one"]))
        before = copy.deepcopy(messages)
        redact_media_payloads(messages, None)
        assert messages == before

    def test_deeply_nested_unknown_parts_do_not_raise(self) -> None:
        deep: Any = SHORT
        for _ in range(5000):
            deep = [deep]
        out = redact_media_payloads(_user({"type": "custom_blob", "b": deep}), None)
        assert out[0]["content"][1]["type"] == "custom_blob"

    def test_non_dict_parts_and_messages_are_left_to_the_other_guards(self) -> None:
        messages: list[Any] = ["x", {"role": "user", "content": ["bare", 3]}]
        assert redact_media_payloads(messages, None) == messages

    @pytest.mark.parametrize("kind", ["input_audio", "input_video", "file"])
    def test_a_known_media_part_keeps_its_shape_and_loses_its_payload(
        self, kind: str
    ) -> None:
        slot = "file_data" if kind == "file" else "data"
        part = {"type": kind, kind: {slot: SHORT, "format": "wav"}}
        out = redact_media_payloads(_user(part), None)[0]["content"][1]
        assert out == {
            "type": kind,
            kind: {slot: f"[REDACTED:media {kind} chars=96]", "format": "wav"},
        }


class TestTheRouteOnFourArms:
    def test_no_span_log_or_ingestion_body_carries_it_and_upstream_gets_it(
        self,
        client: Any,
        arm: tuple[str, bool],
        shape: str,
        langfuse_on: None,
        debug_logs: None,
    ) -> None:
        mode, stream = arm
        part = SHAPES[shape]
        fake = ok_fake(mode, stream)
        response = post_chat(client, fake, stream=stream, messages=_user(part))
        assert response.status_code == 200
        sent = [b["messages"][-1]["content"][-1] for b in upstream_bodies(fake)]
        assert sent and all(p == part for p in sent), "ADR-024: byte for byte"
        spans = [json.loads(s) for s in net._SPAN_END_SNAPSHOTS]
        entry = [s for s in spans if s["name"] == "POST /v1/chat/completions"]
        assert entry, "control: the entry span ended"
        assert all(
            "[REDACTED:media" in s["attributes"].get("langfuse.observation.input", "")
            for s in entry
        ), "control: the entry tag shows a payload was removed"
        if not stream:
            calls = [s for s in spans if s["name"] == "llm.chat.completions"]
            assert calls, "control: the model-call span ended"
            assert all(QUESTION in s["attributes"]["input.value"] for s in calls)
        carrying = sorted(
            {
                f"{s['name']} :: {k}"
                for s in spans
                for k, v in s["attributes"].items()
                if SHORT in str(v)
            }
        )
        assert carrying == []
        records = list(net._LOG_RECORD_SNAPSHOTS)
        assert any("chat" in r for r in records), "control: chat log records seen"
        assert [r[:100] for r in records if SHORT in r] == []
        posts = non_upstream_posts(fake)
        assert len(posts) == 1, "control: one Langfuse ingestion POST"
        assert QUESTION in posts[0], "control: the trace input still holds the text"
        assert SHORT not in posts[0]
        assert "[REDACTED:media" in posts[0]


class TestTheRequestSpanAttributes:
    """``sovereign-chat-request`` is an ended span only under a live Langfuse client."""

    def test_the_input_value_the_function_builds_holds_no_payload(
        self, monkeypatch: pytest.MonkeyPatch, shape: str
    ) -> None:
        from audittrace.routes import chat

        written: list[dict[str, Any]] = []
        monkeypatch.setattr(
            chat.telemetry,
            "set_current_span_attributes",
            lambda attrs: written.append(dict(attrs)),
        )
        payload = {"model": "m", "messages": _user(SHAPES[shape])}
        before = copy.deepcopy(payload)
        chat._set_genai_request_attributes(payload, "q", "s", "src", "u")
        assert len(written) == 1
        value = written[0]["input.value"]
        assert QUESTION in value, "control: the text is traced"
        assert SHORT not in value
        assert "[REDACTED:media" in value
        assert payload == before, "the payload itself is untouched"
