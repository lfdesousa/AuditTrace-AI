"""LIBRECHAT-IMAGE-SLICE AC-1, AC-2, AC-4 -- the audit row records every image part.

Live finding 2026-11-14 (interaction 19268): a chat turn that carried an image stored
only the question and the answer. EU AI Act Art 12 needs the input to be identifiable:
the row must say that an image was sent, its declared media type, its decoded size and
the sha256 of its decoded bytes -- never the bytes, never base64.

Through the real HTTP route with a stub upstream, on the four arms.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from audittrace.db.models import InteractionRecord
from audittrace.dependencies import get_postgres_factory
from audittrace.integrity import verify_content_hash
from tests._image_slice import (
    ARM_IDS,
    ARMS,
    CANARY,
    DATA_URL,
    EXPECTED_RECORD,
    PNG_B64,
    QUESTION,
    flip_mode,
    image_messages,
    ok_fake,
    post_chat,
    recorded_item,
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


async def _rows() -> list[InteractionRecord]:
    pg = get_postgres_factory()
    async with pg.get_session_factory()() as db:
        result = await db.execute(
            select(InteractionRecord).order_by(InteractionRecord.id)
        )
        return list(result.scalars().all())


def _media(row: InteractionRecord) -> Any:
    raw = getattr(row, "input_media", "<no such column>")
    return raw if raw is None or raw == "<no such column>" else json.loads(raw)


class TestSuccessRowRecordsTheImage:
    async def test_row_carries_type_size_sha256_count_and_position(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        response = post_chat(
            client, ok_fake(mode, stream), stream=stream, messages=image_messages()
        )
        assert response.status_code == 200
        rows = await _rows()
        assert len(rows) == 1
        assert rows[0].status == "success"
        assert rows[0].question == QUESTION
        assert _media(rows[0]) == EXPECTED_RECORD

    async def test_stored_text_is_canonical_json_without_payload(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        post_chat(
            client, ok_fake(mode, stream), stream=stream, messages=image_messages()
        )
        row = (await _rows())[0]
        raw = getattr(row, "input_media", None)
        assert isinstance(raw, str)
        assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
        for column in InteractionRecord.__table__.columns:
            value = getattr(row, column.name)
            assert CANARY not in str(value), column.name
            assert "base64," not in str(value), column.name

    async def test_row_hash_covers_the_image_record(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        post_chat(
            client, ok_fake(mode, stream), stream=stream, messages=image_messages()
        )
        row = (await _rows())[0]
        assert _media(row) == EXPECTED_RECORD
        assert verify_content_hash(row) is True
        tampered = {c.name: getattr(row, c.name) for c in row.__table__.columns}
        tampered["input_media"] = str(tampered["input_media"]).replace(
            '"bytes":', '"bytes":1'
        )
        assert verify_content_hash(type("Row", (), tampered)()) is False


class TestEveryImageOfTheRequestIsRecorded:
    async def test_two_messages_three_images_in_document_order(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        part = {"type": "image_url", "image_url": {"url": DATA_URL}}
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [part, {"type": "text", "text": "first"}]},
            {"role": "assistant", "content": "a dot"},
            {
                "role": "user",
                "content": [{"type": "text", "text": QUESTION}, part, part],
            },
        ]
        post_chat(client, ok_fake(mode, stream), stream=stream, messages=messages)
        assert _media((await _rows())[0]) == {
            "schema": 1,
            "count": 3,
            "items": [recorded_item(0, 0), recorded_item(2, 1), recorded_item(2, 2)],
        }


class TestTextOnlyTurnIsUntouched:
    async def test_no_image_means_null_and_a_verifying_hash(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        post_chat(
            client,
            ok_fake(mode, stream),
            stream=stream,
            messages=[{"role": "user", "content": "hello"}],
        )
        row = (await _rows())[0]
        assert _media(row) is None
        assert verify_content_hash(row) is True


class TestFailedRowRecordsTheImageToo:
    async def test_no_projector_row_keeps_upstream_text_and_the_image_record(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        fake = _UpstreamErrorClient(500, LLAMA_IMAGE_500_BODY)
        post_chat(client, fake, stream=stream, messages=image_messages())
        row = (await _rows())[0]
        assert row.status == "failed"
        assert row.failure_class == "upstream_error"
        assert _media(row) == EXPECTED_RECORD


class TestAsyncPersistCarriesTheImageRecord:
    async def test_enqueued_kwargs_hold_the_record_not_the_payload(
        self, client: Any, arm: tuple[str, bool], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from audittrace import config as config_mod
        from audittrace.services import async_persist

        mode, stream = arm
        monkeypatch.setenv("AUDITTRACE_ASYNC_PERSIST_ENABLED", "true")
        config_mod.get_settings.cache_clear()
        seen: list[dict[str, str]] = []

        class _Producer:
            async def enqueue(
                self, *, kwargs: dict[str, Any], pending_tool_calls: Any = None
            ) -> str:
                seen.append(
                    async_persist.serialise_record(
                        kwargs=kwargs, pending_tool_calls=pending_tool_calls
                    )
                )
                return "1-0"

        async_persist.install_test_producer(_Producer())  # type: ignore[arg-type]
        try:
            post_chat(
                client,
                ok_fake(mode, stream),
                stream=stream,
                messages=image_messages(),
                headers={"X-Persist-Mode": "async"},
            )
        finally:
            async_persist.reset_for_tests()
            config_mod.get_settings.cache_clear()
        assert len(seen) == 1
        record = json.loads(seen[0]["record_json"])
        assert json.loads(record["input_media"]) == EXPECTED_RECORD
        assert CANARY not in json.dumps(seen[0])


class TestDigestRunsOffTheEventLoop:
    """Decoding and hashing megabytes is CPU work; ``/health`` shares the event loop.
    Proven by thread identity, never by a clock."""

    def test_off_the_event_loop(
        self, client: Any, arm: tuple[str, bool], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import base64
        import hashlib
        import threading

        from audittrace.routes import chat as chat_mod

        mode, stream = arm
        raw = base64.b64decode(PNG_B64)
        hashed_on: list[int] = []
        loop_thread: list[int] = []
        real_sha256 = hashlib.sha256
        real_resolve = chat_mod._resolve_project

        def _sha256(data: Any = b"", **kwargs: Any) -> Any:
            if bytes(data) == raw:
                hashed_on.append(threading.get_ident())
            return real_sha256(data, **kwargs)

        def _resolve(*args: Any, **kwargs: Any) -> Any:
            loop_thread.append(threading.get_ident())
            return real_resolve(*args, **kwargs)

        monkeypatch.setattr(hashlib, "sha256", _sha256)
        monkeypatch.setattr(chat_mod, "_resolve_project", _resolve)
        post_chat(
            client, ok_fake(mode, stream), stream=stream, messages=image_messages()
        )
        assert len(loop_thread) == 1, "control: the handler ran once, on the loop"
        assert hashed_on, "the decoded image is hashed with hashlib.sha256(<bytes>)"
        assert loop_thread[0] not in hashed_on


class TestAuditApiExposesTheRecord:
    async def test_get_interactions_returns_the_record_as_an_object(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        post_chat(
            client, ok_fake(mode, stream), stream=stream, messages=image_messages()
        )
        post_chat(
            client,
            ok_fake(mode, stream),
            stream=stream,
            messages=[{"role": "user", "content": "hello"}],
        )
        listing = client.get("/interactions?limit=5")
        assert listing.status_code == 200
        newest, oldest = listing.json()["interactions"][:2]
        assert oldest["input_media"] == EXPECTED_RECORD
        assert "input_media" in newest and newest["input_media"] is None
        assert CANARY not in listing.text


class TestV1BytesUnchanged:
    """Controls: green before and after. The image part reaches the model server
    exactly as sent, and the client's reply is exactly the model server's."""

    def test_upstream_receives_the_image_part_unchanged(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        post_chat(client, fake, stream=stream, messages=image_messages())
        bodies = upstream_bodies(fake)
        assert len(bodies) == 1
        user = [m for m in bodies[0]["messages"] if m.get("role") == "user"]
        assert user == image_messages()
        assert (
            user[0]["content"][1]["image_url"]["url"]
            == f"data:image/png;base64,{PNG_B64}"
        )

    def test_inject_non_stream_reply_is_the_upstream_reply(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.test_chat_proxy import _ok_chat_response

        for _ in flip_mode(monkeypatch, "inject"):
            fake = ok_fake("inject", False)
            response = post_chat(client, fake, stream=False, messages=image_messages())
            text_only = post_chat(
                client,
                ok_fake("inject", False),
                stream=False,
                messages=[{"role": "user", "content": QUESTION}],
            )
        assert response.json() == _ok_chat_response("It is a dot.")
        assert response.content == text_only.content
        assert sorted(response.headers.keys()) == sorted(text_only.headers.keys())
