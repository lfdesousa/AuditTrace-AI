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
    DATA_URL,
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
    # an extra key inside a known media holder (the holder keeps only its schema keys)
    "audio-holder-extra": {
        "type": "input_audio",
        "input_audio": {"data": "AAAA", "format": "wav", "blob": SHORT},
    },
    "video-holder-extra": {
        "type": "input_video",
        "input_video": {"data": "AAAA", "blob": SHORT},
    },
    "file-holder-extra": {
        "type": "file",
        "file": {"filename": "n.pdf", "file_data": "AAAA", "blob": SHORT},
    },
    "image-holder-extra": {
        "type": "image_url",
        "image_url": {"url": DATA_URL, "detail": "low", "blob": SHORT},
    },
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


#: shapes of a whole message ``content`` that the OpenAI schema does not allow (a string or a
#: list of part objects only): each carries the canary and must be redacted whole.
CONTAINERS: dict[str, Any] = {
    "content-is-a-dict": {"type": "custom_blob", "blob": SHORT},
    "content-is-a-number": 12345678901234567890,
    "unknown-part-one-list-deeper": [
        {"type": "text", "text": QUESTION},
        [{"type": "custom_blob", "blob": SHORT}],
    ],
    "bare-string-element": [{"type": "text", "text": QUESTION}, SHORT],
    "null-element": [{"type": "text", "text": QUESTION}, None, SHORT],
}


_BLOB = {"type": "custom_blob", "blob": SHORT}
_Q = {"role": "user", "content": QUESTION}
#: whole ``messages`` lists the schema does not allow (a message without a role, a message
#: that is not an object): the canary must reach no telemetry site although the route may
#: answer them with an error of its own (outside this change).
MESSAGE_SHAPES: dict[str, list[Any]] = {
    "roleless-list": [{"content": [{"type": "text", "text": QUESTION}, _BLOB]}, _Q],
    "roleless-dict-content": [{"content": _BLOB}, _Q],
    "roleless-last": [_Q, {"content": [_BLOB]}],
    "non-object-list": [_Q, [_BLOB]],
    "non-object-str": [_Q, SHORT],
    "non-object-number-list": [_Q, [1, [SHORT]]],
    # a message object: only the OpenAI message keys are traced, any other key is a marker
    "roleless-blob-key": [{"blob": SHORT}, _Q],
    "roleless-parts-key": [{"parts": [_BLOB]}, _Q],
    "role-bearing-extra-key": [{"role": "user", "content": QUESTION, "blob": SHORT}],
    "list-content-extra-key": [
        {"role": "user", "content": [{"type": "text", "text": QUESTION}], "blob": SHORT}
    ],
    # the route turns a [key, value] pair into {key: value} (dict(m)) before the model call
    "mangled-pairs": [_Q, [["blob", SHORT]]],
    "mangled-pairs-first": [[["blob", SHORT]], _Q],
    "non-object-only": [[_BLOB]],
    "non-object-str-only": [SHORT],
}


def _container(content: Any) -> list[dict[str, Any]]:
    return [{"role": "user", "content": content}]


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


@pytest.fixture(params=sorted(CONTAINERS))
def container(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(params=sorted(MESSAGE_SHAPES))
def message_shape(request: pytest.FixtureRequest) -> str:
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


class TestKnownHoldersKeepTheirSchemaKeys:
    @pytest.mark.parametrize(
        ("kind", "keys"),
        [
            ("input_audio", {"data", "format"}),
            ("input_video", {"data", "url", "format"}),
            ("file", {"file_data", "file_id", "filename"}),
            ("image_url", {"url", "detail"}),
        ],
    )
    def test_the_schema_keys_pass_and_any_other_key_is_a_marker(
        self, kind: str, keys: set[str]
    ) -> None:
        holder = {k: "v" for k in keys} | {"blob": SHORT}
        out = redact_media_payloads(_user({"type": kind, kind: holder}), None)
        safe = out[0]["content"][1][kind]
        assert SHORT not in json.dumps(out)
        assert safe["blob"].startswith(f"[REDACTED:media {kind} key str chars=96")
        assert set(safe) == keys | {"blob"}
        from audittrace.media.redaction import _PAYLOAD_SLOTS

        plain = keys - set(_PAYLOAD_SLOTS.get(kind, ()))
        assert plain, "control: the kind has schema keys that are not payload slots"
        assert all(safe[k] == "v" for k in plain), (
            "the schema keys are traced as written"
        )

    def test_the_input_is_not_mutated(self) -> None:
        part = {"type": "file", "file": {"filename": "n", "blob": SHORT}}
        before = copy.deepcopy(part)
        redact_media_payloads(_user(part), None)
        assert part == before


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

    @pytest.mark.parametrize(
        ("message", "marker"),
        [
            ("bare", "[REDACTED:media message str chars=4]"),
            ([1, 2], "[REDACTED:media message list items=2]"),
            (7, "[REDACTED:media message int]"),
            (None, "[REDACTED:media message NoneType]"),
        ],
        ids=["str", "list", "int", "none"],
    )
    def test_a_message_that_is_not_an_object_is_replaced_whole(
        self, message: Any, marker: str
    ) -> None:
        assert redact_media_payloads([message], None) == [{"redacted": marker}]

    @pytest.mark.parametrize("name", sorted(CONTAINERS))
    def test_a_content_shape_the_schema_does_not_allow_is_redacted_whole(
        self, name: str
    ) -> None:
        out = redact_media_payloads(_container(CONTAINERS[name]), None)
        assert SHORT not in json.dumps(out)
        assert "[REDACTED:media" in json.dumps(out)

    @pytest.mark.parametrize(
        ("element", "marker"),
        [
            ("bare", "[REDACTED:media unknown str chars=4]"),
            (3, "[REDACTED:media unknown int]"),
            (None, "[REDACTED:media unknown NoneType]"),
            ([1, 2], "[REDACTED:media unknown list items=2]"),
        ],
        ids=["str", "int", "none", "list"],
    )
    def test_a_list_element_that_is_not_an_object_is_an_unknown_part(
        self, element: Any, marker: str
    ) -> None:
        out = redact_media_payloads(_container([element]), None)
        assert out[0]["content"] == [{"type": "unknown", "redacted": marker}]

    def test_the_container_marker_records_the_type_and_the_size(self) -> None:
        out = redact_media_payloads(_container({"a": 1, "b": 2}), None)
        assert out[0]["content"] == "[REDACTED:media content dict keys=2]"
        out = redact_media_payloads(_container(7), None)
        assert out[0]["content"] == "[REDACTED:media content int]"

    def test_text_and_a_null_content_pass_as_they_are(self) -> None:
        """A string is text, traced as written; null is legal on a tool-calling assistant."""
        for content in ("plain words", None):
            assert redact_media_payloads(_container(content), None)[0]["content"] == (
                content
            )

    def test_a_dict_content_is_not_mutated(self) -> None:
        messages = _container(copy.deepcopy(CONTAINERS["content-is-a-dict"]))
        before = copy.deepcopy(messages)
        redact_media_payloads(messages, None)
        assert messages == before

    @pytest.mark.parametrize("kind", ["input_audio", "input_video", "file"])
    def test_a_known_media_part_keeps_its_shape_and_loses_its_payload(
        self, kind: str
    ) -> None:
        slot = "file_data" if kind == "file" else "data"
        extra = {"format": "wav"} if kind != "file" else {"filename": "n.pdf"}
        part = {"type": kind, kind: {slot: SHORT, **extra}}
        out = redact_media_payloads(_user(part), None)[0]["content"][1]
        assert out == {
            "type": kind,
            kind: {slot: f"[REDACTED:media {kind} chars=96]", **extra},
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
            any(
                m in s["attributes"].get("langfuse.observation.input", "")
                for m in ("[REDACTED:media", "[image sha256=")
            )
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
        assert "[REDACTED:media" in posts[0] or "[image sha256=" in posts[0]


class TestContainerShapesOnFourArms:
    """The review's finding: a part outside a list of objects reached every site."""

    def test_no_span_log_or_ingestion_body_carries_it_and_upstream_gets_it(
        self,
        client: Any,
        arm: tuple[str, bool],
        container: str,
        langfuse_on: None,
        debug_logs: None,
    ) -> None:
        mode, stream = arm
        messages = _container(CONTAINERS[container])
        fake = ok_fake(mode, stream)
        response = post_chat(client, fake, stream=stream, messages=messages)
        assert response.status_code == 200
        sent = [b["messages"][-1] for b in upstream_bodies(fake)]
        assert sent and all(m == messages[-1] for m in sent), "ADR-024: unchanged"
        spans = [json.loads(s) for s in net._SPAN_END_SNAPSHOTS]
        assert any(s["name"] == "POST /v1/chat/completions" for s in spans), "control"
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
        assert SHORT not in posts[0]
        assert "[REDACTED:media" in posts[0]


class TestMessageShapesOnFourArms:
    """Review of the fail-closed round: a message without a role, a message that is no object."""

    def test_no_telemetry_site_carries_it(
        self,
        client: Any,
        arm: tuple[str, bool],
        message_shape: str,
        langfuse_on: None,
        debug_logs: None,
    ) -> None:
        mode, stream = arm
        messages = MESSAGE_SHAPES[message_shape]
        before = copy.deepcopy(messages)
        fake = ok_fake(mode, stream)
        try:
            post_chat(client, fake, stream=stream, messages=messages)
        except Exception:
            pass  # the route's own error for such a request is not under test here
        assert messages == before, "the input is never mutated"
        spans = [json.loads(s) for s in net._SPAN_END_SNAPSHOTS]
        assert any(s["name"] == "POST /v1/chat/completions" for s in spans), "control"
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
        assert [p[:100] for p in non_upstream_posts(fake) if SHORT in p] == []

    def test_a_role_less_message_reaches_the_model_server_unchanged(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        messages = MESSAGE_SHAPES["roleless-list"]
        fake = ok_fake(mode, stream)
        response = post_chat(client, fake, stream=stream, messages=messages)
        assert response.status_code == 200
        sent = [m for b in upstream_bodies(fake) for m in b["messages"]]
        assert messages[0] in sent, "ADR-024: forwarded as it came"


#: top-level request fields that are not OpenAI chat-completions fields
REQUEST_EXTRAS: dict[str, dict[str, Any]] = {
    "input-nested-unknown-part": {"input": [[_BLOB]]},
    "blob-field": {"blob_field": SHORT},
    "prediction-content-part": {"prediction": {"type": "content", "content": [_BLOB]}},
}


@pytest.fixture(params=sorted(REQUEST_EXTRAS))
def request_extra(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class TestRequestFieldsOnFourArms:
    """An unknown top-level field of the request body is rendered as a marker."""

    def test_no_telemetry_site_carries_it_and_upstream_gets_it(
        self,
        client: Any,
        arm: tuple[str, bool],
        request_extra: str,
        langfuse_on: None,
        debug_logs: None,
    ) -> None:
        mode, stream = arm
        extra = REQUEST_EXTRAS[request_extra]
        before = copy.deepcopy(extra)
        fake = ok_fake(mode, stream)
        response = post_chat(client, fake, stream=stream, messages=[_Q], extra=extra)
        assert response.status_code == 200
        assert extra == before
        for key, value in extra.items():
            assert all(b.get(key) == value for b in upstream_bodies(fake)), key
        spans = [json.loads(s) for s in net._SPAN_END_SNAPSHOTS]
        assert any(s["name"] == "chat-extract-query" for s in spans), "control"
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
        assert [p[:100] for p in non_upstream_posts(fake) if SHORT in p] == []


class TestMessageKeysAreAnAllowList:
    @pytest.mark.parametrize(
        "key",
        [
            "role",
            "content",
            "name",
            "tool_calls",
            "tool_call_id",
            "refusal",
            "function_call",
            "audio",
        ],
    )
    def test_the_openai_message_keys_pass(self, key: str) -> None:
        value: Any = "v" if key != "tool_calls" else [{"id": "c", "type": "function"}]
        out = redact_media_payloads([{key: value}], None)
        assert out == [{key: value}]

    def test_any_other_key_is_a_marker_and_its_name_is_bounded(self) -> None:
        message = {"role": "user", "content": "q", "blob": SHORT, SHORT: "x", "n": 3}
        out = redact_media_payloads([message], None)[0]
        assert out["blob"] == "[REDACTED:media message key str chars=96]"
        assert out["unknown"] == "[REDACTED:media message key str chars=1]"
        assert out["n"] == "[REDACTED:media message key int]"
        assert SHORT not in json.dumps(out)
        assert (out["role"], out["content"]) == ("user", "q")

    def test_tool_call_arguments_are_text_traced_as_written(self) -> None:
        calls = [{"id": "c", "function": {"name": "f", "arguments": '{"q": 1}'}}]
        out = redact_media_payloads([{"role": "assistant", "tool_calls": calls}], None)
        assert out[0]["tool_calls"] == calls


class TestLogCallRedactsByParameterName:
    """The DEBUG lines and the span input/output of a decorated function, without a route.

    A positional argument has no key the renderer could see, so ``log_call`` redacts a
    ``messages`` list, a ``payload`` dict and the list a ``messages`` function returns.
    """

    def _run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[list[dict[str, Any]], list[Any]]:
        import logging as _logging

        from audittrace import telemetry
        from audittrace.logging_config import log_call

        written: list[dict[str, Any]] = []
        monkeypatch.setattr(
            telemetry,
            "set_current_span_attributes",
            lambda attrs: written.append(dict(attrs)),
        )
        log = _logging.getLogger("test.log_call.by_name")
        records: list[Any] = []

        class _Grab(_logging.Handler):
            def emit(self, record: _logging.LogRecord) -> None:
                records.append(record)

        handler = _Grab(_logging.DEBUG)
        log.addHandler(handler)
        log.setLevel(_logging.DEBUG)

        @log_call(logger=log)
        def merge(
            messages: list[Any], context: str, payload: dict[str, Any]
        ) -> list[Any]:
            return [dict(m) if isinstance(m, list) else m for m in messages]

        try:
            merge(
                [{"role": "user", "content": QUESTION}, [["blob", SHORT]], SHORT],
                "ctx",
                {"model": "m", "input": [[_BLOB]], "blob_field": SHORT},
            )
        finally:
            log.removeHandler(handler)
        return written, records

    def test_span_input_and_output_carry_no_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        written, _ = self._run(monkeypatch)
        text = json.dumps(written)
        assert "input.value" in text and "output.value" in text, "control"
        assert QUESTION in text, "control: text is traced"
        assert SHORT not in text and "custom_blob" not in text.replace("[REDACTED", "")

    def test_the_debug_lines_carry_no_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, records = self._run(monkeypatch)
        lines = [json.dumps(r.__dict__, default=str) for r in records]
        assert any("OUTPUT" in str(r.getMessage()) for r in records), "control"
        assert any("INPUT" in str(r.getMessage()) for r in records), "control"
        assert [ln[:80] for ln in lines if SHORT in ln] == []


class TestTheQueryIsReadLikeTheRedaction:
    """Review A-6: a bare string in a content list is not text; the query ignores it too.

    OpenAI's schema: ``content`` is a string or a list of part OBJECTS. The redaction treats
    any other element as an unknown part; the retrieval query reads only ``text`` parts that
    are objects, so the two agree and the element reaches neither.
    """

    def test_only_text_objects_make_the_query(self) -> None:
        from audittrace.routes.chat import _extract_query

        for content, expected in (
            ([{"type": "text", "text": "q"}, SHORT, None, 3, [SHORT]], "q"),
            ({"type": "text", "text": SHORT}, ""),
            ("plain", "plain"),
        ):
            payload = {"messages": [{"role": "user", "content": content}]}
            assert _extract_query(payload) == expected, content


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
