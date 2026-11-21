"""LIBRECHAT-IMAGE-SLICE ADDENDUM-1 AC-8 -- no media payload in telemetry, whatever the part type.

Gate r1 S2, measured on the image-only prototype: an ``input_audio`` part (raw base64, no
``data:`` prefix) and a ``file`` part still reached span attributes and the Langfuse
ingestion body. The RECORD stays images-only (operator Q3); the telemetry rule does not:
a media payload may reach the model server and nothing else.

An identity pin over the same three witnesses as the image test (every ended span, every
dispatched log record, every POST that did not go to the model server), for four shapes.
The payloads are built here from fixed bytes; a leak is any 24-character window of the
payload (so a truncated head, tail or middle counts).
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

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
from tests.test_chat_image_input_unsupported_error import (
    LLAMA_IMAGE_500_BODY,
    _UpstreamErrorClient,
)

#: 768 fixed bytes -> 1024 base64 characters, no padding, every alphabet character.
PAYLOAD = base64.b64encode(bytes(range(256)) * 3).decode()
WINDOW = 24
_WINDOWS = [PAYLOAD[i : i + WINDOW] for i in range(0, len(PAYLOAD) - WINDOW + 1, 8)]

SHAPES: dict[str, dict[str, Any]] = {
    "audio-raw-base64": {
        "type": "input_audio",
        "input_audio": {"data": PAYLOAD, "format": "wav"},
    },
    "file-data-url": {
        "type": "file",
        "file": {
            "filename": "note.pdf",
            "file_data": "data:application/pdf;base64," + PAYLOAD,
        },
    },
    "file-raw-base64": {
        "type": "file",
        "file": {"filename": "note.pdf", "file_data": PAYLOAD},
    },
    "unknown-part-data-url": {
        "type": "custom_blob",
        "blob": {"u": "data:application/octet-stream;base64," + PAYLOAD},
    },
}


def _messages(shape: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [{"type": "text", "text": QUESTION}, SHAPES[shape]],
        }
    ]


def _leaks(texts: list[str]) -> list[str]:
    return [t[:100] for t in texts if any(w in t for w in _WINDOWS)]


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


def _spans() -> list[str]:
    return list(net._SPAN_END_SNAPSHOTS)


class TestNoMediaPayloadInTelemetry:
    def test_no_span_carries_the_payload(
        self,
        client: Any,
        arm: tuple[str, bool],
        shape: str,
        langfuse_on: None,
        debug_logs: None,
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        response = post_chat(client, fake, stream=stream, messages=_messages(shape))
        assert response.status_code == 200
        assert PAYLOAD in json.dumps(upstream_bodies(fake)), "control: upstream got it"
        names = sorted({json.loads(s)["name"] for s in _spans()})
        assert any("chat" in n for n in names), f"control: chat spans ended: {names}"
        leaking = sorted({json.loads(s)["name"] for s in _spans() if _leaks([s])})
        assert leaking == []

    def test_no_log_record_carries_the_payload(
        self,
        client: Any,
        arm: tuple[str, bool],
        shape: str,
        langfuse_on: None,
        debug_logs: None,
    ) -> None:
        mode, stream = arm
        post_chat(
            client, ok_fake(mode, stream), stream=stream, messages=_messages(shape)
        )
        records = list(net._LOG_RECORD_SNAPSHOTS)
        assert any("chat" in r for r in records), "control: chat log records seen"
        assert _leaks(records) == []

    def test_no_langfuse_ingestion_body_carries_the_payload(
        self, client: Any, arm: tuple[str, bool], shape: str, langfuse_on: None
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        post_chat(client, fake, stream=stream, messages=_messages(shape))
        posts = non_upstream_posts(fake)
        assert len(posts) == 1, "control: one Langfuse ingestion POST"
        assert QUESTION in posts[0], "control: the trace input still holds the text"
        assert _leaks(posts) == []
        assert "[REDACTED:media" in posts[0], "the trace says a payload was removed"

    def test_failed_turn_does_not_leak_either(
        self,
        client: Any,
        arm: tuple[str, bool],
        shape: str,
        langfuse_on: None,
        debug_logs: None,
    ) -> None:
        mode, stream = arm
        fake = _UpstreamErrorClient(500, LLAMA_IMAGE_500_BODY)
        post_chat(client, fake, stream=stream, messages=_messages(shape))
        assert len(_spans()) > 0, "control: spans were witnessed"
        assert _leaks(_spans()) == []
        assert _leaks(list(net._LOG_RECORD_SNAPSHOTS)) == []
        assert _leaks(non_upstream_posts(fake)) == []


#: shorter than the chokepoint's run threshold: only redaction BY PART TYPE removes it
SHORT = PAYLOAD[:96]
TYPED_SHORT: dict[str, dict[str, Any]] = {
    "input_audio": {
        "type": "input_audio",
        "input_audio": {"data": SHORT, "format": "wav"},
    },
    "file": {"type": "file", "file": {"filename": "note.pdf", "file_data": SHORT}},
}


class TestRedactionByPartType:
    @pytest.mark.parametrize("kind", sorted(TYPED_SHORT))
    def test_a_short_payload_is_replaced_in_the_trace_input(
        self, client: Any, arm: tuple[str, bool], kind: str, langfuse_on: None
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": QUESTION}, TYPED_SHORT[kind]],
            }
        ]
        post_chat(client, fake, stream=stream, messages=messages)
        assert SHORT in json.dumps(upstream_bodies(fake)), "control: upstream got it"
        posts = non_upstream_posts(fake)
        assert len(posts) == 1, "control: one Langfuse ingestion POST"
        assert SHORT not in posts[0]
        assert f"[REDACTED:media {kind} chars={len(SHORT)}]" in posts[0]
        entry = [
            json.loads(s)["attributes"].get("langfuse.observation.input", "")
            for s in _spans()
            if json.loads(s)["name"] == "POST /v1/chat/completions"
        ]
        assert len(entry) >= 1, "control: the entry span ended"
        assert all(QUESTION in e for e in entry), (
            "control: the entry tag holds the text"
        )
        assert [e[:80] for e in entry if SHORT in e] == []


class TestTheRecordStaysImagesOnly:
    """Operator Q3: audio and file parts are not recorded by this slice."""

    def test_the_model_server_still_receives_the_part_unchanged(
        self, client: Any, arm: tuple[str, bool], shape: str
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        post_chat(client, fake, stream=stream, messages=_messages(shape))
        bodies = upstream_bodies(fake)
        assert len(bodies) == 1
        user = [m for m in bodies[0]["messages"] if m.get("role") == "user"]
        assert user == _messages(shape)

    def test_no_image_attribute_and_no_image_record(
        self, client: Any, arm: tuple[str, bool], shape: str
    ) -> None:
        mode, stream = arm
        post_chat(
            client, ok_fake(mode, stream), stream=stream, messages=_messages(shape)
        )
        listing = client.get("/interactions?limit=1")
        assert listing.status_code == 200
        row = listing.json()["interactions"][0]
        assert "input_media" in row and row["input_media"] is None
        keys = {k for s in _spans() for k in json.loads(s)["attributes"]}
        assert [k for k in keys if k.startswith("audittrace.input.image.")] == []


class TestScrubChokepoint:
    """The export-time chokepoint itself, on strings (truncated values included)."""

    def test_a_long_base64_run_is_dropped_and_digests_survive(self) -> None:
        from audittrace.logging_config import scrub_text

        sha256_hex = "8cf8513dd0fb04730604640ba5a61a5277b0bb0f3f446b5047b7ad17438e0732"
        sha512_hex = sha256_hex * 2
        text = f'{{"data": "{PAYLOAD[:600]}", "sha256": "{sha256_hex}", "x": "{sha512_hex}"}}'
        out = scrub_text(text)
        assert _leaks([out]) == []
        assert "[REDACTED:media]" in out
        assert sha256_hex in out and sha512_hex in out, "digests are not payloads"

    def test_prose_and_short_values_are_untouched(self) -> None:
        from audittrace.logging_config import scrub_text

        text = (
            "What does this picture show? id="
            + PAYLOAD[:128]
            + " path=/v1/chat/completions"
        )
        assert scrub_text(text) == text
