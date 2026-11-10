"""AT-UNSUPPORTED-CONTENT-ERROR — RED-2 (ADDENDUM-1, 2026-11-09).

Closes gate r1 S3 (the matcher is EQUALITY: near-misses at status 400 must
not match, so widening ``==`` to ``in`` / ``startswith`` / a regex reddens
these), B1 (the parsed-body matcher the tools x non-stream arm uses), A1
(part types de-duplicated, capped), S4 (exactly one WARNING line carries the
upstream text on every matched path, and no WARNING-or-higher record carries
request content) and S6 (the public guide exists, is linked, and carries no
internal identifier or banned word).

Imports of the sibling module ``audittrace.routes._upstream_errors`` are
INSIDE the tests so each case fails on its own today (ImportError: cannot
import name ``_upstream_errors``)
and collection never fails as a whole. Route cases reuse the RED-1 stubs
(real ``httpx.Response`` objects) by import; RED-1 must sit beside this file.
"""

# ruff: noqa: F811  (fixtures imported from the RED-1 module are re-requested by name)
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from tests.test_chat_proxy import _patch_async_client, _patch_tool_loop_client
from tests.test_chat_unsupported_content_error import (  # noqa: F401 - fixtures re-exported
    EXPECTED_MESSAGE,
    FILE_DATA,
    FILENAME,
    LLAMA_UNSUPPORTED_BODY,
    UPLOAD_TO_PROVIDER_MESSAGES,
    _post,
    _Upstream400Client,
    inject_mode,
    tools_mode,
)

UPSTREAM_TEXT = "unsupported content[].type"
B64 = "aGVsbG8gd29ybGQ"
NEAR_MISSES = [
    "unsupported content[].type: file",  # suffix: `startswith` would match
    "Unsupported content[].type",  # case: a case-folded compare would match
    "the request has unsupported content[].type",  # prefix: `in` would match
    "unsupported content[].type\n",  # trailing whitespace: `strip()` would match
]
GUIDE = (
    Path(__file__).resolve().parents[1] / "docs/guides/librechat-attachments-support.md"
)
GUIDE_LINK_FROM = GUIDE.parent / "per-chat-session-recall.md"
BANNED_IN_PUBLIC_GUIDE = (
    "LC-10a",
    "lc10a",
    "LCFCP",
    "FORK-UPSTREAM-SYNC",
    "private RAG",
    "leak",
    "probe",
    "another user's",
)


def _mod():
    from audittrace.routes import _upstream_errors

    return _upstream_errors


def _body(message: str, code: int | str = 400) -> dict:
    return {
        "error": {"code": code, "message": message, "type": "invalid_request_error"}
    }


def _payload(*types: object) -> dict:
    parts: list[dict] = [{"type": "text", "text": "Summarise the attached file."}]
    for t in types:
        parts.append(
            {"type": t, "file": {"filename": FILENAME, "file_data": FILE_DATA}}
        )
    return {
        "model": "audittrace-default",
        "messages": [{"role": "user", "content": parts}],
    }


# ───────────── S3: the text matcher (status + raw body) is equality ─────────────


class TestTextMatcherIsEquality:
    def test_exact_text_at_400_matches(self):
        assert (
            _mod().match_unsupported_content(400, json.dumps(_body(UPSTREAM_TEXT)))
            is True
        )

    @pytest.mark.parametrize("message", NEAR_MISSES)
    def test_near_miss_text_at_400_does_not_match(self, message):
        assert (
            _mod().match_unsupported_content(400, json.dumps(_body(message))) is False
        )

    def test_exact_text_at_500_does_not_match(self):
        assert (
            _mod().match_unsupported_content(500, json.dumps(_body(UPSTREAM_TEXT, 500)))
            is False
        )

    def test_non_json_body_does_not_match(self):
        assert (
            _mod().match_unsupported_content(400, "<html>400 Bad Request</html>")
            is False
        )


# ───────── B1: the parsed-body matcher (tools x non-stream arm) is equality ─────────


class TestParsedBodyMatcherIsEquality:
    def test_parsed_body_exact_matches(self):
        assert _mod().match_unsupported_content_body(_body(UPSTREAM_TEXT)) is True

    @pytest.mark.parametrize("message", NEAR_MISSES)
    def test_parsed_body_near_miss_does_not_match(self, message):
        assert _mod().match_unsupported_content_body(_body(message)) is False

    def test_parsed_body_code_500_does_not_match(self):
        assert _mod().match_unsupported_content_body(_body(UPSTREAM_TEXT, 500)) is False

    def test_parsed_body_code_as_string_does_not_match(self):
        assert (
            _mod().match_unsupported_content_body(_body(UPSTREAM_TEXT, "400")) is False
        )

    @pytest.mark.parametrize(
        "body", [[], "x", {"error": "x"}, {"error": {"message": UPSTREAM_TEXT}}]
    )
    def test_parsed_body_wrong_shape_does_not_match(self, body):
        assert _mod().match_unsupported_content_body(body) is False

    def test_error_detail_from_parsed_body_is_compact_json(self):
        detail = _mod().error_detail_from_body(LLAMA_UNSUPPORTED_BODY)
        assert detail == (
            'status=400: {"error":{"code":400,"message":"unsupported content[].type",'
            '"type":"invalid_request_error"}}'
        )
        assert len(detail) <= 12 + 400

    def test_error_detail_from_text_keeps_upstream_text(self):
        text = json.dumps(LLAMA_UNSUPPORTED_BODY)
        assert _mod().error_detail_from_text(400, text) == "status=400: " + text


# ──────────── A1 / AC-5: part types are named, de-duplicated and capped ────────────


class TestUnsupportedPartTypes:
    def test_dedupes_and_keeps_first_seen_order(self):
        assert _mod().unsupported_part_types(_payload("file", "file", "image_url")) == [
            "file",
            "image_url",
        ]

    def test_caps_at_three_types(self):
        assert _mod().unsupported_part_types(_payload("a", "b", "c", "d")) == [
            "a",
            "b",
            "c",
        ]

    @pytest.mark.parametrize("bad", ["x" * 33, {"a": 1}, "", "file name", None, 7])
    def test_unnamed_types_are_dropped(self, bad):
        assert _mod().unsupported_part_types(_payload(bad)) == []

    def test_text_only_and_string_content_give_empty(self):
        assert _mod().unsupported_part_types(_payload()) == []
        assert (
            _mod().unsupported_part_types(
                {"messages": [{"role": "user", "content": "hi"}]}
            )
            == []
        )

    def test_message_names_one_type(self):
        err = _mod().unsupported_content_error_body(_payload("file"))["error"]
        assert err["message"] == EXPECTED_MESSAGE

    def test_message_names_two_types_in_one_parenthetical(self):
        err = _mod().unsupported_content_error_body(_payload("file", "image_url"))[
            "error"
        ]
        assert err["message"] == (
            "This model can't receive files or images directly in the message "
            "(unsupported content part type 'file', 'image_url'). Use 'Upload as Text' instead."
        )

    def test_message_without_a_readable_type(self):
        err = _mod().unsupported_content_error_body(_payload({"a": 1}))["error"]
        assert err["message"] == (
            "This model can't receive files or images directly in the message "
            "(unsupported content part type). Use 'Upload as Text' instead."
        )
        assert FILENAME not in json.dumps(err) and B64 not in json.dumps(err)


# ─────── S4: one WARNING carries the upstream text; no request content in logs ───────


def _warnings_with_upstream_text(records) -> list[logging.LogRecord]:
    return [
        r
        for r in records
        if r.levelno == logging.WARNING and UPSTREAM_TEXT in r.getMessage()
    ]


def _assert_no_echo_at_warning_or_above(records) -> None:
    for r in records:
        if r.levelno >= logging.WARNING:
            msg = r.getMessage()
            assert FILE_DATA not in msg and B64 not in msg and FILENAME not in msg, msg[
                :200
            ]


class TestWarningLineAndNoEchoInLogs:
    async def test_tools_stream_one_warning_carries_upstream_text(
        self, client, tools_mode, caplog
    ):
        fake = _Upstream400Client(LLAMA_UNSUPPORTED_BODY)
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=True)
        hits = _warnings_with_upstream_text(caplog.records)
        assert len(hits) == 1, [
            r.getMessage()[:120] for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert hits[0].name.startswith("audittrace.")
        _assert_no_echo_at_warning_or_above(caplog.records)

    async def test_inject_stream_one_warning_carries_upstream_text(
        self, client, inject_mode, caplog
    ):
        fake = _Upstream400Client(LLAMA_UNSUPPORTED_BODY)
        with _patch_async_client(fake):
            _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=True)
        hits = _warnings_with_upstream_text(caplog.records)
        assert len(hits) == 1, [
            r.getMessage()[:120] for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert hits[0].name.startswith("audittrace.")
        _assert_no_echo_at_warning_or_above(caplog.records)

    async def test_inject_non_stream_one_warning_carries_upstream_text(
        self, client, inject_mode, caplog
    ):
        fake = _Upstream400Client(LLAMA_UNSUPPORTED_BODY)
        with _patch_async_client(fake):
            _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=False)
        hits = _warnings_with_upstream_text(caplog.records)
        assert len(hits) == 1, [
            r.getMessage()[:120] for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert hits[0].name.startswith("audittrace.")
        _assert_no_echo_at_warning_or_above(caplog.records)

    async def test_tools_non_stream_loop_warning_is_the_one_line(
        self, client, tools_mode, caplog
    ):
        """PIN (GREEN before and after): the loop's own WARNING is the one line on this arm."""
        fake = _Upstream400Client(LLAMA_UNSUPPORTED_BODY)
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=False)
        hits = _warnings_with_upstream_text(caplog.records)
        assert len(hits) == 1, [
            r.getMessage()[:120] for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert hits[0].name == "audittrace.routes._memory_tool_loop"
        _assert_no_echo_at_warning_or_above(caplog.records)


# ───────────── S6: the public guide exists, is linked, and is clean ─────────────


class TestPublicAttachmentsGuide:
    def test_guide_exists_and_names_both_upload_paths(self):
        text = GUIDE.read_text(encoding="utf-8")
        assert "Upload as Text" in text
        assert "Upload to Provider" in text
        assert "unsupported_content_type" in text

    def test_guide_has_no_internal_identifier_or_banned_word(self):
        low = GUIDE.read_text(encoding="utf-8").lower()
        assert [w for w in BANNED_IN_PUBLIC_GUIDE if w.lower() in low] == []

    def test_guide_is_linked_from_the_session_recall_guide(self):
        assert GUIDE.name in GUIDE_LINK_FROM.read_text(encoding="utf-8")
