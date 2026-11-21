"""LIBRECHAT-IMAGE-SLICE round 2 (review F1) -- a deeply nested media part never crashes the route.

``json.loads`` accepts a media part nested thousands of levels deep. The walks of
``audittrace.media`` (the link guard and the safe copy) must cover EVERY level, in linear
time, and never raise: a recursive walk raised ``RecursionError`` at about 600 levels,
which answered 500 and wrote no audit row for a request the parent release answered and
audited. No depth cap is allowed either: a cap would let a link nested one level deeper
reach the model host's fetch.

Through the real route, four arms, with the parent release's behaviour as the control:
a nested part with no link is forwarded unchanged, answered 200, one ``success`` row; the
same part with a link at the bottom is refused 400 ``media_link_not_supported`` before any
upstream call, one ``failed`` / ``request_refused`` row, the link stored nowhere.
The request body is built as TEXT: the test client's own encoder cannot nest this deep.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from audittrace.db.models import InteractionRecord
from audittrace.dependencies import get_postgres_factory
from tests._image_slice import (
    ARM_IDS,
    ARMS,
    QUESTION,
    flip_mode,
    ok_fake,
    upstream_bodies,
)
from tests.test_chat_proxy import _patch_async_client, _patch_tool_loop_client

DEPTHS = [600, 5000]
LINK = "http://127.0.0.1:11435/props"
CANARY_HOST = "127.0.0.1:11435"


@pytest.fixture(params=ARMS, ids=ARM_IDS)
def arm(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, bool]]:
    mode, stream = request.param
    for _ in flip_mode(monkeypatch, mode):
        yield mode, stream


def _nested(leaf: str, depth: int) -> str:
    return "[" * depth + leaf + "]" * depth


def _body(part_json: str, stream: bool) -> bytes:
    text = json.dumps(QUESTION)
    return (
        '{"model":"audittrace-default","project":"AuditTrace","stream":'
        + ("true" if stream else "false")
        + ',"messages":[{"role":"user","content":[{"type":"text","text":'
        + text
        + "},"
        + part_json
        + "]}]}"
    ).encode()


def _video(leaf: str, depth: int) -> str:
    return (
        '{"type":"input_video","input_video":{"data":"QUJD","meta":'
        + _nested(leaf, depth)
        + "}}"
    )


def _unknown(leaf: str, depth: int) -> str:
    return '{"type":"custom_blob","blob":' + _nested(leaf, depth) + "}"


SHAPES = {"input_video": _video, "unknown_part": _unknown}


async def _rows() -> list[InteractionRecord]:
    async with get_postgres_factory().get_session_factory()() as db:
        return list((await db.execute(select(InteractionRecord))).scalars().all())


def _post(client: Any, fake: Any, body: bytes) -> Any:
    with _patch_async_client(fake), _patch_tool_loop_client(fake):
        return client.post(
            "/v1/chat/completions",
            content=body,
            headers={"content-type": "application/json"},
        )


@pytest.mark.parametrize("depth", DEPTHS)
@pytest.mark.parametrize("shape", sorted(SHAPES))
class TestDeepPartWithoutLinkIsForwarded:
    async def test_200_one_success_row_and_the_part_reaches_the_model_server(
        self, client: Any, arm: tuple[str, bool], shape: str, depth: int
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        response = _post(client, fake, _body(SHAPES[shape]('"x"', depth), stream))
        assert response.status_code == 200
        rows = await _rows()
        assert [r.status for r in rows] == ["success"]
        assert rows[0].input_media is None
        assert len(upstream_bodies(fake)) >= 1, "control: the model server was called"


@pytest.mark.parametrize("depth", DEPTHS)
@pytest.mark.parametrize("shape", sorted(SHAPES))
class TestDeepLinkIsStillRefused:
    async def test_400_no_upstream_call_one_failed_row_and_the_link_stored_nowhere(
        self, client: Any, arm: tuple[str, bool], shape: str, depth: int
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        response = _post(client, fake, _body(SHAPES[shape](f'"{LINK}"', depth), stream))
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "media_link_not_supported"
        assert upstream_bodies(fake) == [], "refused before any upstream call"
        assert [c for c in fake.post_calls if "ingestion" not in str(c["url"])] == []
        rows = await _rows()
        assert [(r.status, r.failure_class) for r in rows] == [
            ("failed", "request_refused")
        ]
        assert rows[0].error_detail == "media_link_not_supported: message 0 part 1"
        stored = json.dumps(
            [r.__dict__.get(k) for r in rows for k in ("question", "error_detail")]
        )
        assert CANARY_HOST not in stored
        assert CANARY_HOST not in response.text
