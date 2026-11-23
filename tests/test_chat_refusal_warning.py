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
from sqlalchemy import select

from audittrace.db.models import InteractionRecord
from audittrace.dependencies import get_postgres_factory
from audittrace.identity import sentinel_user_context
from tests._image_slice import (
    ARM_IDS,
    ARMS,
    QUESTION,
    flip_mode,
    ok_fake,
    post_chat,
)

_BARE = logging.LogRecord("x", logging.WARNING, "f", 1, "m", None, None)
#: the identity fields the service's logging adds to EVERY record (traceability)
SERVICE_FIELDS = frozenset({"message", "session_id", "span_id", "trace_id", "user_id"})
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
async def test_one_warning_with_the_reason_and_the_position_only(
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
    record = warnings[0]
    detail = f"{reason}: message 0 part 1"
    # The EXACT text and the exact arguments: anything appended (a URL, a host, a
    # payload, the reply body, a dropped position) changes one of these.
    assert record.getMessage() == f"chat request refused: {detail}"
    assert record.msg == "chat request refused: %s"
    assert record.args == (detail,)
    # No extra field carrying payload: only the attributes a bare LogRecord has plus the
    # ones the service's own logging filters add for every record.
    assert set(vars(record)) - set(vars(_BARE)) <= SERVICE_FIELDS, sorted(
        set(vars(record)) - set(vars(_BARE)) - SERVICE_FIELDS
    )
    assert "canary" not in repr(vars(record)) and TARGET not in repr(vars(record))
    # The identity fields are the BOUND identity: the caller's user and the turn's session,
    # the same ones the audit row of this refusal carries (traceability, EU AI Act Art. 12).
    async with get_postgres_factory().get_session_factory()() as db:
        rows = list((await db.execute(select(InteractionRecord))).scalars().all())
    assert len(rows) == 1
    assert record.user_id == rows[0].user_id == sentinel_user_context().user_id
    assert record.session_id == rows[0].session_id
    assert rows[0].session_id
