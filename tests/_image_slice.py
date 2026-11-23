"""LIBRECHAT-IMAGE-SLICE -- shared fixtures of the image-slice tests (not a test module).

One PNG, sent as an OpenAI ``image_url`` content part (a base64 data URL), through the
real ``POST /v1/chat/completions`` route on the four arms (inject / tools x non-stream /
stream) with a stub upstream. ``PNG_SHA256`` and ``PNG_BYTES`` are literals measured with
coreutils (``base64 -d | sha256sum`` / ``wc -c``), a different instrument from the code
under test.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from tests.test_chat_proxy import (
    _FakeAsyncClient,
    _is_ingestion_url,
    _ok_chat_response,
    _patch_async_client,
    _patch_tool_loop_client,
    _SequencedClient,
    _SequencedStreamClient,
    _sse_content_lines,
    _tools_mode_response_text,
)

PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg=="
PNG_SHA256 = "8cf8513dd0fb04730604640ba5a61a5277b0bb0f3f446b5047b7ad17438e0732"
PNG_BYTES = 70
DATA_URL = f"data:image/png;base64,{PNG_B64}"
#: A slice of the payload long enough to be unmistakable, short enough to survive any cap.
CANARY = PNG_B64[8:56]
QUESTION = "What does this picture show?"
ARMS = [
    ("inject", False),
    ("inject", True),
    ("tools", False),
    ("tools", True),
]
ARM_IDS = ["inject-nonstream", "inject-stream", "tools-nonstream", "tools-stream"]


def image_messages(url: str = DATA_URL) -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": QUESTION},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }
    ]


def recorded_item(message_index: int = 0, part_index: int = 1) -> dict[str, Any]:
    return {
        "kind": "image",
        "message_index": message_index,
        "part_index": part_index,
        "role": "user",
        "source": "data_uri",
        "status": "recorded",
        "media_type": "image/png",
        "bytes": PNG_BYTES,
        "sha256": PNG_SHA256,
    }


EXPECTED_RECORD: dict[str, Any] = {"schema": 1, "count": 1, "items": [recorded_item()]}


def flip_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> Iterator[None]:
    from audittrace import config as config_mod

    config_mod.get_settings.cache_clear()
    monkeypatch.setenv("AUDITTRACE_MEMORY_MODE", mode)
    yield
    config_mod.get_settings.cache_clear()


def ok_fake(mode: str, stream: bool) -> _FakeAsyncClient:
    if mode == "tools":
        if stream:
            return _SequencedStreamClient(
                [_sse_content_lines(["It ", "is a ", "dot."])]
            )
        return _SequencedClient([_tools_mode_response_text("It is a dot.")])
    if stream:
        return _FakeAsyncClient(
            stream_lines=_sse_content_lines(["It ", "is a ", "dot."])
        )
    return _FakeAsyncClient(post_json=_ok_chat_response("It is a dot."))


def post_chat(
    client: Any,
    fake: _FakeAsyncClient,
    *,
    stream: bool,
    messages: list[dict[str, Any]],
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> Any:
    with _patch_async_client(fake), _patch_tool_loop_client(fake):
        return client.post(
            "/v1/chat/completions",
            json={
                "model": "audittrace-default",
                "messages": messages,
                "project": "AuditTrace",
                "stream": stream,
                **(extra or {}),
            },
            headers=headers or {},
        )


def upstream_bodies(fake: _FakeAsyncClient) -> list[dict[str, Any]]:
    """Every JSON body the route sent to the model server (never Langfuse)."""
    bodies = [
        c["json"] for c in fake.post_calls if not _is_ingestion_url(str(c["url"]))
    ]
    bodies += [c["json"] for c in getattr(fake, "stream_calls", [])]
    if not getattr(fake, "stream_calls", []) and fake.last_stream_json is not None:
        bodies.append(fake.last_stream_json)
    return [b for b in bodies if isinstance(b, dict)]


def non_upstream_posts(fake: _FakeAsyncClient) -> list[str]:
    """Serialised bodies of every POST that did NOT go to the model server."""
    return [
        json.dumps(c["json"], default=str)
        for c in fake.post_calls
        if _is_ingestion_url(str(c["url"]))
    ]
