"""LIBRECHAT-IMAGE-SLICE ADDENDUM-1 AC-11 / AC-12 -- no caller-chosen URL reaches the model server.

Gate r1 security observation: the model server (llama-server 360e1349f, ``handle_media``,
server-common.cpp) downloads any media URL that starts with ``http`` itself, with no host
check. A chat-scoped caller can therefore make the model HOST issue a GET to an address of
the caller's choosing, internal ones included (SSRF). The refusal of a linked image
(operator Q1) closes that door for ``image_url`` parts: the request is answered 400 before
ANY upstream call.

ADDENDUM-1 measured that the same fetch is reachable through ``input_audio`` and
``input_video`` parts (their ``data`` / ``url`` slot goes to the same ``handle_media``);
``TestOtherMediaLinkIsRefused`` pins the closure of those, CONDITIONAL on operator OQ-1.

The rule is an identity, not a blocklist of hosts: the only accepted image source is an
inline ``data:`` URL, so no host form can be "missed". The forms below are witnesses.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from audittrace.db.models import InteractionRecord
from audittrace.dependencies import get_postgres_factory
from tests import conftest as net
from tests._image_slice import (
    ARM_IDS,
    ARMS,
    QUESTION,
    flip_mode,
    image_messages,
    ok_fake,
    post_chat,
)

#: every way of naming an internal or caller-chosen target that ``handle_media`` would
#: fetch (prefix ``http``) or open (``file://``), plus near-misses it would decode as base64
INTERNAL_FORMS = [
    "http://127.0.0.1:11435/props",
    "http://localhost/metrics",
    "http://[::1]:8200/v1/sys/health",
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "http://2130706433/",
    "http://0x7f000001/",
    "http://0177.0.0.1/",
    "http://10.42.0.1:11436/v1/models",
    "http://memory-server.audittrace.svc.cluster.local:8765/health",
    "https://kubernetes.default.svc/api",
    "http://user:pw@intranet.example.test/x.png",
    "https://images.example.test/a.png#data:image/png;base64,AAAA",
    "httpx://not-a-scheme-but-starts-with-http/",
    "HTTP://127.0.0.1/upper-case-scheme",
    " http://127.0.0.1/leading-space",
    "file:///etc/passwd",
    "//127.0.0.1/scheme-relative",
    "gopher://127.0.0.1:6379/_INFO",
]
LINK_CODE = "image_url_not_supported"
MEDIA_LINK_CODE = "media_link_not_supported"


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


def _outbound(fake: Any) -> list[str]:
    """EVERY outbound call the route made through the HTTP client, of any kind."""
    calls = [str(c.get("url", "")) for c in fake.post_calls]
    calls += ["stream" for _ in getattr(fake, "stream_calls", [])]
    if fake.last_stream_json is not None:
        calls.append("stream")
    if fake.last_get_url is not None:
        calls.append(str(fake.last_get_url))
    return calls


class TestLinkedImageNeverReachesTheModelServer:
    @pytest.mark.parametrize("url", INTERNAL_FORMS)
    async def test_refused_before_any_upstream_call(
        self, client: Any, arm: tuple[str, bool], url: str
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        response = post_chat(client, fake, stream=stream, messages=image_messages(url))
        assert response.status_code == 400
        assert response.json()["error"]["code"] == LINK_CODE
        assert _outbound(fake) == [], "no outbound call of any kind"
        rows = await _rows()
        assert len(rows) == 1
        assert (rows[0].status, rows[0].failure_class) == ("failed", "request_refused")
        target = url.strip().split("//", 1)[-1][:12]
        assert target not in response.text
        stored = json.dumps(
            {c.name: str(getattr(rows[0], c.name)) for c in rows[0].__table__.columns}
        )
        assert target not in stored
        emitted = list(net._SPAN_END_SNAPSHOTS) + list(net._LOG_RECORD_SNAPSHOTS)
        assert len(emitted) > 0, "control: spans and log records were witnessed"
        assert [e[:80] for e in emitted if url.strip() in e] == []

    def test_control_an_inline_image_does_reach_the_model_server(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        response = post_chat(client, fake, stream=stream, messages=image_messages())
        assert response.status_code == 200
        assert len(_outbound(fake)) >= 1


OTHER_MEDIA_LINKS = [
    ("input_video", "data", "http://169.254.169.254/latest/meta-data/"),
    ("input_video", "url", "http://127.0.0.1:11435/props"),
    ("input_audio", "data", "http://10.42.0.1:11436/v1/models"),
    ("input_audio", "url", "https://kubernetes.default.svc/api"),
    ("input_video", "url", "file:///etc/passwd"),
]


class TestOtherMediaLinkIsRefused:
    """CONDITIONAL on operator OQ-1 (ADDENDUM-1 section 9)."""

    @pytest.mark.parametrize(("kind", "slot", "url"), OTHER_MEDIA_LINKS)
    async def test_refused_before_any_upstream_call(
        self, client: Any, arm: tuple[str, bool], kind: str, slot: str, url: str
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": QUESTION},
                    {"type": kind, kind: {slot: url, "format": "wav"}},
                ],
            }
        ]
        response = post_chat(client, fake, stream=stream, messages=messages)
        assert response.status_code == 400
        error = response.json()["error"]
        assert (error["code"], error["type"], error["param"], error["status"]) == (
            MEDIA_LINK_CODE,
            "invalid_request_error",
            "messages",
            400,
        )
        assert _outbound(fake) == [], "no outbound call of any kind"
        rows = await _rows()
        assert len(rows) == 1
        assert (rows[0].status, rows[0].failure_class) == ("failed", "request_refused")
        assert rows[0].error_detail == f"{MEDIA_LINK_CODE}: message 0 part 1"
        assert url.split("//", 1)[-1][:12] not in response.text

    @pytest.mark.parametrize("kind", ["input_audio", "input_video"])
    def test_control_inline_base64_is_still_forwarded(
        self, client: Any, arm: tuple[str, bool], kind: str
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": QUESTION},
                    {"type": kind, kind: {"data": "QUJDRA==", "format": "wav"}},
                ],
            }
        ]
        response = post_chat(client, fake, stream=stream, messages=messages)
        assert response.status_code == 200
        assert len(_outbound(fake)) >= 1
