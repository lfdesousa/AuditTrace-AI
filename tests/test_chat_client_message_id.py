"""CONSOLE-AUDIT-EVIDENCE WU-A -- ``X-AuditTrace-Message-Id`` is persisted on the row.

Through the real ``POST /v1/chat/completions`` route with a stub upstream, on the four arms
(inject / tools x non-stream / stream), and on the refused-request and async paths.

G1 row value equals the header; G2 grid (absent, empty, 256, ``../x``, ``.``, ``..``,
``abc\\n``, ``a\\nb``, template) -> NULL, never a 4xx; G3 the enqueued async payload carries
it; AC12 an unresolved ``{{LIBRECHAT_BODY_MESSAGEID}}`` -> NULL; G18 the ``/v1`` response is
byte-identical with and without the header.

Neuters (each alone): G1 delete the header read or the ContextVar bind -> every ``test_row_
carries_the_header`` cell RED; G2 accept any string -> the grid RED; G3 drop it from the
``kwargs`` of ``_persist_or_enqueue`` -> ``test_async_persist_carries_client_message_id`` RED;
G18 append the id to the response body -> ``test_v1_response_identical_*`` RED.
"""

from __future__ import annotations

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
    image_messages,
    ok_fake,
    post_chat,
)

HEADER = "X-AuditTrace-Message-Id"
MID = "b7b3f4e0-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
TEXT = [{"role": "user", "content": QUESTION}]


@pytest.fixture(params=ARMS, ids=ARM_IDS)
def arm(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, bool]]:
    mode, stream = request.param
    for _ in flip_mode(monkeypatch, mode):
        yield mode, stream


async def _rows() -> list[InteractionRecord]:
    pg = get_postgres_factory()
    async with pg.get_session_factory()() as db:
        result = await db.execute(
            select(InteractionRecord).order_by(InteractionRecord.id)
        )
        return list(result.scalars().all())


class TestG1RowCarriesTheHeader:
    async def test_row_carries_the_header(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        response = post_chat(
            client,
            ok_fake(mode, stream),
            stream=stream,
            messages=TEXT,
            headers={HEADER: MID},
        )
        assert response.status_code == 200
        rows = await _rows()
        assert [r.client_message_id for r in rows] == [MID]
        assert rows[0].status == "success"

    async def test_a_refused_request_row_carries_it_too(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        response = post_chat(
            client,
            ok_fake(mode, stream),
            stream=stream,
            messages=image_messages("http://127.0.0.1/x.png"),
            headers={HEADER: MID},
        )
        assert response.status_code == 400
        rows = await _rows()
        assert [(r.status, r.client_message_id) for r in rows] == [("failed", MID)]

    async def test_no_binding_bleeds_into_the_next_request(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        post_chat(
            client,
            ok_fake(mode, stream),
            stream=stream,
            messages=TEXT,
            headers={HEADER: MID},
        )
        post_chat(client, ok_fake(mode, stream), stream=stream, messages=TEXT)
        assert [r.client_message_id for r in await _rows()] == [MID, None]


GRID = {
    "absent": None,
    "empty": "",
    "256": "x" * 256,
    "dotdot-x": "../x",
    "dot": ".",
    "dotdot": "..",
    "trailing-lf": "abc\n",
    "inner-lf": "a\nb",
    "template": "{{LIBRECHAT_BODY_MESSAGEID}}",
}


class TestG2AbsentOrInvalidIsNullAndNeverA4xx:
    @pytest.mark.parametrize("name", sorted(GRID))
    @pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
    async def test_grid(
        self,
        client: Any,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        stream: bool,
    ) -> None:
        value = GRID[name]
        headers = {} if value is None else {HEADER: value}
        for _ in flip_mode(monkeypatch, "inject"):
            response = post_chat(
                client,
                ok_fake("inject", stream),
                stream=stream,
                messages=TEXT,
                headers=headers,
            )
        assert response.status_code == 200
        assert [r.client_message_id for r in await _rows()] == [None]


class TestG18TheV1ResponseIsByteIdentical:
    @pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
    async def test_v1_response_identical_with_and_without_the_header(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, stream: bool
    ) -> None:
        for _ in flip_mode(monkeypatch, "inject"):
            without = post_chat(
                client, ok_fake("inject", stream), stream=stream, messages=TEXT
            )
            with_header = post_chat(
                client,
                ok_fake("inject", stream),
                stream=stream,
                messages=TEXT,
                headers={HEADER: MID},
            )
        assert without.status_code == with_header.status_code == 200
        assert without.content == with_header.content
        assert MID.encode() not in with_header.content
        volatile = {"date", "x-trace-id"}  # per-request by design
        assert {k: v for k, v in without.headers.items() if k not in volatile} == {
            k: v for k, v in with_header.headers.items() if k not in volatile
        }
        assert set(without.headers) == set(with_header.headers)
        assert MID not in str(dict(with_header.headers))

    @pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
    async def test_an_image_turn_is_byte_identical_too(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, stream: bool
    ) -> None:
        """AC2 (A5): the identity also covers a turn that carries an image."""
        for _ in flip_mode(monkeypatch, "inject"):
            without = post_chat(
                client,
                ok_fake("inject", stream),
                stream=stream,
                messages=image_messages(),
            )
            with_header = post_chat(
                client,
                ok_fake("inject", stream),
                stream=stream,
                messages=image_messages(),
                headers={HEADER: MID},
            )
        assert without.status_code == with_header.status_code == 200
        assert without.content == with_header.content
        rows = await _rows()
        assert [r.client_message_id for r in rows] == [None, MID]
        assert rows[0].input_media == rows[1].input_media


class TestG3AsyncPersistCarriesIt:
    @pytest.fixture
    def _async_persist_on(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        from audittrace import config as config_mod

        config_mod.get_settings.cache_clear()
        monkeypatch.setenv("AUDITTRACE_ASYNC_PERSIST_ENABLED", "true")
        yield
        config_mod.get_settings.cache_clear()

    async def test_async_persist_carries_client_message_id(
        self, client: Any, _async_persist_on: None
    ) -> None:
        from fakeredis.aioredis import FakeRedis

        from audittrace.routes._message_id_bind import bound_message_id
        from audittrace.routes.chat import _persist_or_enqueue
        from audittrace.services import async_persist as ap
        from tests.test_async_persist import _settings

        redis = FakeRedis(decode_responses=True)
        ap.install_test_producer(
            ap.AsyncPersistProducer(settings=_settings(), redis=redis)
        )
        try:
            with bound_message_id(MID):
                result = await _persist_or_enqueue(
                    persist_mode="async",
                    pending_tool_calls=None,
                    project="P",
                    source="curl",
                    question="q",
                    answer="a",
                    prompt_tokens=1,
                    completion_tokens=1,
                    session_id="s",
                    model="m",
                    user_id="u",
                )
        finally:
            ap.reset_for_tests()
        assert result is None
        entries = await redis.xrange("test:persist:stream", "-", "+")
        assert len(entries) == 1
        kwargs, _tcs, _ts = ap.deserialise_record(entries[0][1])
        assert kwargs["client_message_id"] == MID
        # ... and the consumer's `_persist_interaction(**kwargs)` writes it:
        from audittrace.routes.chat import _persist_interaction

        await _persist_interaction(**kwargs)
        assert [r.client_message_id for r in await _rows()] == [MID]
