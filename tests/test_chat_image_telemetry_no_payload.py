"""LIBRECHAT-IMAGE-SLICE AC-3 -- image bytes leave the process in ONE place only.

Measured live (trace 5c04872063658f34558efaaeb3642a98, 2026-11-14): the span
``sovereign-chat-request`` carried the head of the base64 image in ``input.value`` and
``langfuse.observation.input``. The image's payload may reach the model server and
nothing else: no span attribute, no log record, no Langfuse ingestion body.

An identity pin, not a list of sinks: the suite-wide redaction net already witnesses
EVERY ended span (``Span.end``) and EVERY dispatched log record
(``Logger.callHandlers``); this test reads those two witnesses plus every POST the
route made, and looks for a slice of the payload.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any

import pytest

from tests import conftest as net
from tests._image_slice import (
    ARM_IDS,
    ARMS,
    CANARY,
    PNG_BYTES,
    PNG_SHA256,
    flip_mode,
    image_messages,
    non_upstream_posts,
    ok_fake,
    post_chat,
    upstream_bodies,
)
from tests.test_chat_image_input_unsupported_error import (
    LLAMA_IMAGE_500_BODY,
    _UpstreamErrorClient,
)


@pytest.fixture(params=ARMS, ids=ARM_IDS)
def arm(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, bool]]:
    mode, stream = request.param
    for _ in flip_mode(monkeypatch, mode):
        yield mode, stream


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


def _spans() -> list[dict[str, Any]]:
    return [json.loads(s) for s in net._SPAN_END_SNAPSHOTS]


#: a data-URL marker followed by payload (a marker followed by a redaction mark is fine)
_PAYLOAD_RE = re.compile(r"base64,[A-Za-z0-9+/=]{16,}")


def _leaks(texts: list[str]) -> list[str]:
    return [t[:120] for t in texts if CANARY in t or _PAYLOAD_RE.search(t)]


class TestPayloadReachesTheModelServerOnly:
    def test_no_span_carries_the_image_payload(
        self, client: Any, arm: tuple[str, bool], langfuse_on: None, debug_logs: None
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        response = post_chat(client, fake, stream=stream, messages=image_messages())
        assert response.status_code == 200
        assert CANARY in json.dumps(upstream_bodies(fake)), "control: upstream got it"
        names = sorted({s["name"] for s in _spans()})
        assert any("chat" in n for n in names), f"control: chat spans ended: {names}"
        leaking = sorted({s["name"] for s in _spans() if _leaks([json.dumps(s)])})
        assert leaking == []

    def test_no_log_record_carries_the_image_payload(
        self, client: Any, arm: tuple[str, bool], langfuse_on: None, debug_logs: None
    ) -> None:
        mode, stream = arm
        post_chat(
            client, ok_fake(mode, stream), stream=stream, messages=image_messages()
        )
        records = list(net._LOG_RECORD_SNAPSHOTS)
        assert any("chat" in r for r in records), "control: chat log records seen"
        assert _leaks(records) == []

    def test_no_langfuse_ingestion_body_carries_the_image_payload(
        self, client: Any, arm: tuple[str, bool], langfuse_on: None
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        post_chat(client, fake, stream=stream, messages=image_messages())
        posts = non_upstream_posts(fake)
        assert len(posts) == 1, "control: one Langfuse ingestion POST"
        assert _leaks(posts) == []
        assert PNG_SHA256 in posts[0], "the trace input names the image by its digest"

    def test_failed_turn_does_not_leak_either(
        self, client: Any, arm: tuple[str, bool], langfuse_on: None, debug_logs: None
    ) -> None:
        mode, stream = arm
        fake = _UpstreamErrorClient(500, LLAMA_IMAGE_500_BODY)
        post_chat(client, fake, stream=stream, messages=image_messages())
        assert _leaks([json.dumps(s) for s in _spans()]) == []
        assert _leaks(list(net._LOG_RECORD_SNAPSHOTS)) == []
        assert _leaks(non_upstream_posts(fake)) == []


class TestSpanNamesTheImageWithoutItsPayload:
    def test_span_attributes_count_bytes_and_digests(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        post_chat(
            client, ok_fake(mode, stream), stream=stream, messages=image_messages()
        )
        tagged = [
            s["attributes"]
            for s in _spans()
            if "audittrace.input.image.count" in s["attributes"]
        ]
        assert len(tagged) >= 1
        for attrs in tagged:
            assert attrs["audittrace.input.image.count"] == 1
            assert attrs["audittrace.input.image.bytes_total"] == PNG_BYTES
            assert list(attrs["audittrace.input.image.sha256"]) == [PNG_SHA256]

    def test_text_only_turn_sets_no_image_attribute(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        post_chat(
            client,
            ok_fake(mode, stream),
            stream=stream,
            messages=[{"role": "user", "content": "hello"}],
        )
        assert "POST /v1/chat/completions" in {s["name"] for s in _spans()}, "control"
        keys = {k for s in _spans() for k in s["attributes"]}
        assert [k for k in keys if k.startswith("audittrace.input.image.")] == []
