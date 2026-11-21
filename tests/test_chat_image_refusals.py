"""LIBRECHAT-IMAGE-SLICE AC-5 -- an image the audit cannot identify is refused, explicitly.

The model server (llama-server 360e1349f, ``handle_media``) DOWNLOADS an ``http...`` image
URL itself and decodes base64 leniently (it stops at the first character outside the
alphabet). In both cases the bytes the model reads are not bytes this service can hash.
Such a part is refused before the model server is called: HTTP 400 exactly, an explicit
OpenAI-shaped error, a ``failed`` audit row that names the part without its content.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from audittrace.db.models import InteractionRecord
from audittrace.dependencies import get_postgres_factory
from tests import conftest as net
from tests._image_slice import (
    ARM_IDS,
    ARMS,
    DATA_URL,
    PNG_B64,
    QUESTION,
    flip_mode,
    image_messages,
    ok_fake,
    post_chat,
    recorded_item,
    upstream_bodies,
)

REMOTE_URL = "https://images.example.test/private/cat.png?sig=SECRET-SIGNATURE"
URL_ERROR = {
    "message": (
        "An image in this message is a link. This service only accepts images sent "
        "inline as a base64 data URL (data:image/...;base64,...); it does not fetch "
        "images from a URL. Attach the image file itself."
    ),
    "type": "invalid_request_error",
    "param": "messages",
    "code": "image_url_not_supported",
    "status": 400,
}
DATA_ERROR = {
    "message": (
        "An image in this message is not a valid base64 data URL "
        "(data:image/<type>;base64,<data>). Attach the image again."
    ),
    "type": "invalid_request_error",
    "param": "messages",
    "code": "image_data_invalid",
    "status": 400,
}
NOT_A_DATA_URL = [
    REMOTE_URL,
    "http://127.0.0.1:9/x.png",
    "file:///etc/passwd",
    PNG_B64,
    "DATA:image/png;base64," + PNG_B64,
]
BAD_DATA_URL = [
    "data:image/png;base64," + PNG_B64[:40] + " " + PNG_B64[40:],
    "data:image/png;base64," + PNG_B64[:40] + "\n" + PNG_B64[40:],
    "data:image/png;base64," + PNG_B64 + "!trailing",
    "data:image/png;base64," + PNG_B64.replace("+", "-").replace("/", "_") + "-_",
    "data:image/png;base64,",
    "data:image/png," + PNG_B64,
    "data:text/plain;base64," + PNG_B64,
    "data:image/png;base64," + PNG_B64 + "," + PNG_B64,
    "data:image/png;base64,A",
]


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


def _refused_item(url: str, source: str, reason: str) -> dict[str, Any]:
    return {
        "kind": "image",
        "message_index": 0,
        "part_index": 1,
        "role": "user",
        "source": source,
        "status": "refused",
        "reason": reason,
        "url_chars": len(url),
        "url_sha256": hashlib.sha256(url.encode()).hexdigest(),
    }


class TestLinkedImageIsRefused:
    @pytest.mark.parametrize("url", NOT_A_DATA_URL)
    async def test_exact_400_no_upstream_call_and_a_failed_row(
        self, client: Any, arm: tuple[str, bool], url: str
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        response = post_chat(client, fake, stream=stream, messages=image_messages(url))
        assert response.status_code == 400
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"error": URL_ERROR}
        assert upstream_bodies(fake) == []
        rows = await _rows()
        assert len(rows) == 1
        row = rows[0]
        assert (row.status, row.failure_class) == ("failed", "request_refused")
        assert row.question == QUESTION
        assert row.error_detail == "image_url_not_supported: message 0 part 1"
        assert json.loads(str(getattr(row, "input_media", None))) == {
            "schema": 1,
            "count": 1,
            "items": [_refused_item(url, "url", "image_url_not_supported")],
        }

    async def test_the_link_itself_is_stored_and_returned_nowhere(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        response = post_chat(
            client,
            ok_fake(mode, stream),
            stream=stream,
            messages=image_messages(REMOTE_URL),
        )
        assert response.status_code == 400
        assert "SECRET-SIGNATURE" not in response.text
        assert "images.example.test" not in response.text
        row = (await _rows())[0]
        stored = json.dumps(
            {c.name: str(getattr(row, c.name)) for c in row.__table__.columns}
        )
        assert "SECRET-SIGNATURE" not in stored
        assert "images.example.test" not in stored
        emitted = list(net._SPAN_END_SNAPSHOTS) + list(net._LOG_RECORD_SNAPSHOTS)
        assert len(emitted) > 0, "control: spans and log records were witnessed"
        assert [e[:80] for e in emitted if "SECRET-SIGNATURE" in e] == []


class TestUndecodableImageIsRefused:
    @pytest.mark.parametrize("url", BAD_DATA_URL)
    async def test_exact_400_no_upstream_call_and_a_failed_row(
        self, client: Any, arm: tuple[str, bool], url: str
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        response = post_chat(client, fake, stream=stream, messages=image_messages(url))
        assert response.status_code == 400
        assert response.json() == {"error": DATA_ERROR}
        assert upstream_bodies(fake) == []
        row = (await _rows())[0]
        assert (row.status, row.failure_class) == ("failed", "request_refused")
        assert row.error_detail == "image_data_invalid: message 0 part 1"
        assert json.loads(str(getattr(row, "input_media", None))) == {
            "schema": 1,
            "count": 1,
            "items": [_refused_item(url, "data_uri", "image_data_invalid")],
        }

    @pytest.mark.parametrize(
        "image_url", [None, "a string", {}, {"url": 7}, {"url": ""}]
    )
    async def test_a_part_without_a_url_string_is_refused_as_invalid(
        self, client: Any, arm: tuple[str, bool], image_url: Any
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        part: dict[str, Any] = {"type": "image_url"}
        if image_url is not None:
            part["image_url"] = image_url
        messages = [
            {"role": "user", "content": [{"type": "text", "text": QUESTION}, part]}
        ]
        response = post_chat(client, fake, stream=stream, messages=messages)
        assert response.status_code == 400
        assert response.json() == {"error": DATA_ERROR}
        assert upstream_bodies(fake) == []
        item = json.loads(str(getattr((await _rows())[0], "input_media", None)))[
            "items"
        ][0]
        assert (item["source"], item["status"], item["reason"]) == (
            "missing",
            "refused",
            "image_data_invalid",
        )


class TestOneRefusedPartRefusesTheTurn:
    async def test_good_and_bad_parts_are_both_on_the_row(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": DATA_URL}},
                    {"type": "image_url", "image_url": {"url": REMOTE_URL}},
                ],
            }
        ]
        response = post_chat(client, fake, stream=stream, messages=messages)
        assert response.status_code == 400
        assert response.json() == {"error": URL_ERROR}
        assert upstream_bodies(fake) == []
        record = json.loads(str(getattr((await _rows())[0], "input_media", None)))
        assert record["count"] == 2
        assert record["items"][0] == recorded_item(0, 0)
        assert record["items"][1]["status"] == "refused"


GUIDE = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "guides"
    / "librechat-attachments-support.md"
)


class TestPublicGuide:
    def test_names_the_record_and_both_refusal_codes(self) -> None:
        text = GUIDE.read_text(encoding="utf-8")
        for needle in (
            "input_media",
            "sha256",
            "image_url_not_supported",
            "image_data_invalid",
        ):
            assert needle in text, needle

    def test_the_images_row_no_longer_says_later(self) -> None:
        rows = [
            ln
            for ln in GUIDE.read_text(encoding="utf-8").splitlines()
            if ln.startswith("| Images (`image_url` parts")
        ]
        assert len(rows) == 1
        assert rows[0].rstrip().endswith("| **supported** |")
        assert "later" not in rows[0]


class TestAcceptedShapesStayAccepted:
    """Controls: green before and after."""

    @pytest.mark.parametrize(
        "url",
        [
            DATA_URL,
            "data:image/png;base64," + PNG_B64.rstrip("="),
            "data:image/jpeg;base64," + PNG_B64,
            "data:image/svg+xml;charset=utf-8;base64," + PNG_B64,
        ],
    )
    def test_reaches_the_model_server_unchanged(
        self, client: Any, arm: tuple[str, bool], url: str
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        response = post_chat(client, fake, stream=stream, messages=image_messages(url))
        assert response.status_code == 200
        bodies = upstream_bodies(fake)
        assert len(bodies) == 1
        user = [m for m in bodies[0]["messages"] if m.get("role") == "user"]
        assert user == image_messages(url)
