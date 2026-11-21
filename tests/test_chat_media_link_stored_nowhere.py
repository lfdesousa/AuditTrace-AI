"""LIBRECHAT-IMAGE-SLICE ADDENDUM-2 AC-12 (gate r2 B1) -- a refused media link is stored nowhere.

Gate r2 measured, on the prototype of ADDENDUM-1, that a link in the ``url`` slot of an
``input_audio`` / ``input_video`` part was refused (HTTP 400) and still written to the span
attribute ``langfuse.observation.input`` of ``POST /v1/chat/completions``: the caller-chosen
internal target reached the trace store. This file gives AC-12 the assertions AC-11 has for
images: the target is absent from the response, from every column of the audit row, from every
ended span, from every dispatched log record and from every body the route posted anywhere.

Two rules are pinned, and neither is a list of hosts:

* the SLOT rule: ``url`` is a payload slot of ``input_audio`` / ``input_video`` like ``data``
  (the model server reads ``data``, else ``url``), so it is replaced whatever it holds;
* the LINK rule: a string the link guard refuses on is replaced wherever it sits in a part
  that is neither ``text`` nor ``image_url``. Guard and redaction share ONE predicate, and the
  identity is asserted: the safe copy of a refused request never trips the guard.

Every witness carries its own canary, so a hit cannot be an address the suite itself uses.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from audittrace.db.models import InteractionRecord
from audittrace.dependencies import get_postgres_factory
from tests import conftest as net
from tests._image_slice import ARM_IDS, ARMS, QUESTION, flip_mode, ok_fake, post_chat

MEDIA_LINK_CODE = "media_link_not_supported"
CANARY = "a2canary"

#: id -> (part type, the part); every link holds CANARY in its host or path
WITNESSES: dict[str, dict[str, Any]] = {
    "video-url": {
        "type": "input_video",
        "input_video": {"url": f"http://169.254.169.254/{CANARY}/meta-data/"},
    },
    "audio-url": {
        "type": "input_audio",
        "input_audio": {"url": f"https://{CANARY}.default.svc/api", "format": "wav"},
    },
    "video-data": {
        "type": "input_video",
        "input_video": {"data": f"http://10.42.0.1:11436/{CANARY}/v1/models"},
    },
    "audio-data": {
        "type": "input_audio",
        "input_audio": {"data": f"http://127.0.0.1:11435/{CANARY}", "format": "wav"},
    },
    "video-url-file-scheme": {
        "type": "input_video",
        "input_video": {"url": f"file:///etc/{CANARY}/passwd"},
    },
    "audio-url-upper-case-leading-space": {
        "type": "input_audio",
        "input_audio": {"url": f" HTTP://{CANARY}.internal.test/x", "format": "wav"},
    },
    "file-file_data": {
        "type": "file",
        "file": {"filename": "n.pdf", "file_data": f"http://{CANARY}.test/n.pdf"},
    },
    "file-filename": {
        "type": "file",
        "file": {"filename": f"http://{CANARY}.test/name.pdf", "file_data": "QUJD"},
    },
    "unknown-part-nested": {
        "type": "custom_blob",
        "blob": {"sources": [{"u": f"https://{CANARY}.test/deep"}]},
    },
    "unknown-part-file-scheme": {
        "type": "custom_blob",
        "blob": {"u": f"file:///srv/{CANARY}/secret"},
    },
    "file-filename-upper-case-leading-space": {
        "type": "file",
        "file": {"filename": f" HtTpS://{CANARY}.test/n.pdf", "file_data": "QUJD"},
    },
    "video-link-beside-the-holder": {
        "type": "input_video",
        "input_video": {"data": "QUJD"},
        "url": f"http://{CANARY}.test/top-level",
    },
}
#: the slots gate r2 named; the model server reads ``data``, else ``url``
GATE_SLOTS = [
    (kind, slot) for kind in ("input_audio", "input_video") for slot in ("data", "url")
]


@pytest.fixture(params=ARMS, ids=ARM_IDS)
def arm(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, bool]]:
    mode, stream = request.param
    for _ in flip_mode(monkeypatch, mode):
        yield mode, stream


@pytest.fixture
def debug_logs() -> Iterator[None]:
    """The witnesses see DEBUG records too: a leak at any level counts."""
    root = logging.getLogger()
    before = root.level
    root.setLevel(logging.DEBUG)
    yield
    root.setLevel(before)


async def _rows() -> list[InteractionRecord]:
    pg = get_postgres_factory()
    async with pg.get_session_factory()() as db:
        result = await db.execute(
            select(InteractionRecord).order_by(InteractionRecord.id)
        )
        return list(result.scalars().all())


def _messages(part: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"role": "user", "content": [{"type": "text", "text": QUESTION}, part]}]


def _emitted() -> dict[str, list[str]]:
    spans = [json.loads(s) for s in net._SPAN_END_SNAPSHOTS]
    return {
        "span attributes": sorted(
            {
                f"{s['name']} :: {k}"
                for s in spans
                for k, v in s["attributes"].items()
                if CANARY in str(v)
            }
        ),
        "log records": [r[:80] for r in net._LOG_RECORD_SNAPSHOTS if CANARY in r],
    }


class TestRefusedMediaLinkIsStoredNowhere:
    @pytest.mark.parametrize("witness", sorted(WITNESSES))
    async def test_the_target_is_in_no_response_row_span_log_or_post(
        self, client: Any, arm: tuple[str, bool], witness: str, debug_logs: None
    ) -> None:
        mode, stream = arm
        fake = ok_fake(mode, stream)
        response = post_chat(
            client, fake, stream=stream, messages=_messages(WITNESSES[witness])
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == MEDIA_LINK_CODE
        assert CANARY not in response.text
        rows = await _rows()
        assert len(rows) == 1
        assert (rows[0].status, rows[0].failure_class) == ("failed", "request_refused")
        assert rows[0].error_detail == f"{MEDIA_LINK_CODE}: message 0 part 1"
        assert rows[0].input_media is None
        columns = {
            c.name: str(getattr(rows[0], c.name)) for c in rows[0].__table__.columns
        }
        assert [name for name, value in columns.items() if CANARY in value] == []
        assert len(net._SPAN_END_SNAPSHOTS) > 0, "control: spans were witnessed"
        assert len(net._LOG_RECORD_SNAPSHOTS) > 0, "control: log records were witnessed"
        emitted = _emitted()
        assert emitted["span attributes"] == []
        assert emitted["log records"] == []
        posted = [json.dumps(c.get("json"), default=str) for c in fake.post_calls]
        assert [p[:80] for p in posted if CANARY in p] == []

    def test_control_the_witness_text_is_visible_to_the_net(
        self, client: Any, arm: tuple[str, bool], debug_logs: None
    ) -> None:
        """A link in a TEXT part is not media: it is forwarded and it is visible, so the
        zeros above are not a blind net."""
        mode, stream = arm
        text = f"see http://{CANARY}.test/in-text"
        response = post_chat(
            client,
            ok_fake(mode, stream),
            stream=stream,
            messages=[{"role": "user", "content": [{"type": "text", "text": text}]}],
        )
        assert response.status_code == 200
        assert _emitted()["span attributes"] != []


class TestTheSafeCopy:
    """The pure half: what telemetry is given, for a request that is refused or not."""

    @pytest.mark.parametrize("witness", sorted(WITNESSES))
    def test_guard_and_redaction_share_one_predicate(self, witness: str) -> None:
        from audittrace.media.link_guard import find_media_link
        from audittrace.media.redaction import redact_media_payloads

        messages = _messages(WITNESSES[witness])
        assert find_media_link(messages) == (0, 1)
        safe = redact_media_payloads(messages, None)
        assert find_media_link(safe) is None
        assert CANARY not in json.dumps(safe)
        assert CANARY in json.dumps(messages), "the request itself is not modified"

    @pytest.mark.parametrize(("kind", "slot"), GATE_SLOTS)
    @pytest.mark.parametrize("value", ["QUJDRA==", f"http://{CANARY}.test/x"])
    def test_data_and_url_are_payload_slots_whatever_they_hold(
        self, kind: str, slot: str, value: str
    ) -> None:
        from audittrace.media.redaction import redact_media_payloads

        part = {"type": kind, kind: {slot: value, "format": "wav"}}
        safe = redact_media_payloads(_messages(part), None)
        assert safe[0]["content"][1] == {
            "type": kind,
            kind: {
                slot: f"[REDACTED:media {kind} chars={len(value)}]",
                "format": "wav",
            },
        }

    def test_text_and_image_parts_are_not_touched_by_the_link_rule(self) -> None:
        from audittrace.media.redaction import redact_media_payloads

        content = [
            {"type": "text", "text": f"see http://{CANARY}.test/in-text"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ]
        messages = [{"role": "user", "content": content}]
        assert redact_media_payloads(messages, None) == messages
