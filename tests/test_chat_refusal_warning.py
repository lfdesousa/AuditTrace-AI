"""LIBRECHAT-IMAGE-SLICE round 2 (review A5) -- the refusal logs ONE WARNING: reason and position only.

Spec section 3.4.4: "The refusal's single WARNING names the reason and the position only."
Through the real route, four arms, for the three reasons: exactly one WARNING record from the
refusal, naming the reason and the message / part position, and no part of the link or of
the damaged payload.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from tests._image_slice import (
    ARM_IDS,
    ARMS,
    QUESTION,
    flip_mode,
    ok_fake,
    post_chat,
)

TARGET = "intranet-canary.example.test/secret-path"
CASES = {
    "image_url_not_supported": {
        "type": "image_url",
        "image_url": {"url": f"https://{TARGET}/a.png"},
    },
    "image_data_invalid": {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,@@@canary-payload"},
    },
    "media_link_not_supported": {
        "type": "input_video",
        "input_video": {"url": f"http://{TARGET}/v.mp4"},
    },
}


@pytest.fixture(params=ARMS, ids=ARM_IDS)
def arm(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, bool]]:
    mode, stream = request.param
    for _ in flip_mode(monkeypatch, mode):
        yield mode, stream


@pytest.mark.parametrize("reason", sorted(CASES))
def test_one_warning_with_the_reason_and_the_position_only(
    client: Any,
    arm: tuple[str, bool],
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mode, stream = arm
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": QUESTION}, CASES[reason]],
        }
    ]
    with caplog.at_level(logging.DEBUG):
        response = post_chat(
            client, ok_fake(mode, stream), stream=stream, messages=messages
        )
    assert response.status_code == 400
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "refus" in r.getMessage().lower()
    ]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records][-5:]
    text = warnings[0].getMessage()
    assert reason in text
    assert "message 0 part 1" in text
    assert "canary" not in text and TARGET not in text
