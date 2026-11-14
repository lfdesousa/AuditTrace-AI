"""Unit tests for ``audittrace.routes._upstream_errors`` (gate r2 carry S1/S2/S3).

Complements the route-level RED-1 / RED-2 files with the cases the gate
named as otherwise un-reddenable neuters:

* a 500 whose body ``code`` is 400 must NOT match (reddens if the status
  check is dropped from ``match_unsupported_content``);
* a ``code`` of ``"400"`` / ``400.0`` must NOT match (reddens if the code is
  coerced with ``int(code)`` / ``str(code) == "400"``).
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from audittrace.routes import _upstream_errors as ue

MSG = ue.LLAMA_UNSUPPORTED_CONTENT_TYPE_MESSAGE
PAYLOAD = {
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "x"}, {"type": "file"}]}
    ]
}


def _text(code: object, message: str = MSG) -> str:
    return json.dumps({"error": {"code": code, "message": message}})


class TestStatusIsPartOfTheTextMatch:
    def test_status_500_with_body_code_400_does_not_match(self):
        # S1(a): only the status check reddens this; the body alone matches.
        assert ue.match_unsupported_content(500, _text(400)) is False

    def test_status_400_with_matching_body_matches(self):
        assert ue.match_unsupported_content(400, _text(400)) is True

    def test_image_class_500_message_does_not_match(self):
        body = _text(500, "image input is not supported - hint: provide an mmproj")
        assert ue.match_unsupported_content(500, body) is False
        assert ue.match_unsupported_content(400, body) is False


class TestCodeIsAnIntNotCoerced:
    @pytest.mark.parametrize("code", ["400", 400.0, True, None])
    def test_non_int_codes_do_not_match(self, code):
        # S1(b): `int(code)` / `str(code) == "400"` coercion would match "400"/400.0.
        assert ue.match_unsupported_content_body(json.loads(_text(code))) is False


class TestClassify:
    def test_hit_has_body_detail_and_exactly_one_warning(self, caplog):
        text = _text(400)
        with caplog.at_level(logging.WARNING, logger=ue.logger.name):
            hit = ue.classify_unsupported_content(400, text, PAYLOAD)
        assert hit is not None
        assert hit.error_body["error"]["code"] == ue.UNSUPPORTED_CONTENT_CODE
        assert hit.error_detail == "status=400: " + text
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and MSG in warnings[0].getMessage()

    def test_miss_returns_none_and_logs_nothing(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=ue.logger.name):
            assert ue.classify_unsupported_content(400, "{}", PAYLOAD) is None
        assert caplog.records == []

    def test_body_matcher_is_silent(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=ue.logger.name):
            assert ue.match_unsupported_content_body(json.loads(_text(400))) is True
        assert caplog.records == []

    def test_error_detail_is_bounded(self):
        assert (
            len(ue.error_detail_from_text(400, "x" * 1000)) == len("status=400: ") + 400
        )


class TestClassifyStatusError:
    def _exc(self, response: httpx.Response) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", "http://llama/v1/chat/completions")
        return httpx.HTTPStatusError("boom", request=request, response=response)

    def test_read_response_is_classified(self):
        response = httpx.Response(400, text=_text(400))
        hit = ue.classify_status_error(self._exc(response), PAYLOAD)
        assert hit is not None and "'file'" in hit.error_body["error"]["message"]

    def test_unread_stream_is_no_match_not_a_new_failure(self):
        response = httpx.Response(400, stream=httpx.ByteStream(_text(400).encode()))
        assert ue.classify_status_error(self._exc(response), PAYLOAD) is None

    def test_unrelated_400_is_no_match(self):
        response = httpx.Response(
            400, json={"error": {"code": 400, "message": "other"}}
        )
        assert ue.classify_status_error(self._exc(response), PAYLOAD) is None


class TestStatusErrorDetail:
    """``status_error_detail``: upstream text, else ``str(exc)`` (operator Q3, C1)."""

    def _exc(self, response: httpx.Response) -> httpx.HTTPStatusError:
        return httpx.HTTPStatusError(
            "boom " + "u" * 600, request=response.request, response=response
        )

    def test_unread_streamed_500_falls_back_to_str_exc_without_raising(self):
        # C1: reading .text on an unread stream raises ResponseNotRead; the
        # suppress turns that into the str(exc) fallback. Deleting the
        # contextlib.suppress(httpx.ResponseNotRead) reddens this test.
        response = httpx.Response(
            500,
            stream=httpx.ByteStream(b"never read"),
            request=httpx.Request("POST", "http://l/v1"),
        )
        exc = self._exc(response)
        assert ue.status_error_detail(exc) == "status=500: " + str(exc)[:400]

    def test_read_body_is_preferred_and_cut_at_400(self):
        response = httpx.Response(
            400, text="r" * 601, request=httpx.Request("POST", "http://l/v1")
        )
        detail = ue.status_error_detail(self._exc(response))
        assert detail == "status=400: " + "r" * 400
        assert len(detail) == len("status=400: ") + 400

    def test_empty_body_falls_back_to_str_exc(self):
        response = httpx.Response(
            500, text="", request=httpx.Request("POST", "http://l/v1")
        )
        exc = self._exc(response)
        assert ue.status_error_detail(exc) == "status=500: " + str(exc)[:400]


class TestHelpers:
    def test_detail_from_hit_or_prefers_the_hit(self):
        hit = ue.classify_unsupported_content(400, _text(400), PAYLOAD)
        assert ue.detail_from_hit_or(hit, "fallback") == hit.error_detail  # type: ignore[union-attr]
        assert ue.detail_from_hit_or(None, "fallback") == "fallback"

    def test_frame_or_picks_the_hit_frame_else_the_default(self):
        hit = ue.classify_unsupported_content(400, _text(400), PAYLOAD)
        assert hit is not None
        assert ue.frame_or(hit, b"generic") == ue.emit_sse_error_frame_from_body(
            hit.error_body
        )
        assert ue.frame_or(None, b"generic") == b"generic"

    def test_sse_frame_is_data_line_then_done(self):
        body = ue.unsupported_content_error_body(PAYLOAD)
        frame = ue.emit_sse_error_frame_from_body(body)
        assert frame == ("data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n").encode()

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"messages": "x"},
            {"messages": ["x", {"content": 3}]},
            {"messages": [{"content": ["x"]}]},
        ],
    )
    def test_malformed_payloads_name_no_type(self, payload):
        assert ue.unsupported_part_types(payload) == []

    def test_trailing_newline_type_is_not_named(self):
        # A2: fullmatch, not match+$ — "file\n" must not reach the message.
        payload = {"messages": [{"content": [{"type": "file\n"}]}]}
        assert ue.unsupported_part_types(payload) == []


class TestMovedArmHelpers:
    def test_stream_detail_hit_else_upstream_text(self):
        response = httpx.Response(
            500, json={"x": 1}, request=httpx.Request("POST", "http://l/v1")
        )
        exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
        assert ue.stream_detail(None, exc) == "status=500: " + response.text
        hit = ue.classify_unsupported_content(400, _text(400), PAYLOAD)
        assert ue.stream_detail(hit, exc) == hit.error_detail  # type: ignore[union-attr]

    def test_stream_detail_empty_body_falls_back_to_str_exc(self):
        response = httpx.Response(
            500, text="", request=httpx.Request("POST", "http://l/v1")
        )
        exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
        assert ue.stream_detail(None, exc) == "status=500: boom"

    async def test_read_error_body_reads_only_on_400_and_above(self):
        req = httpx.Request("POST", "http://l/v1")
        err = httpx.Response(400, stream=httpx.ByteStream(b"{}"), request=req)
        ok = httpx.Response(200, stream=httpx.ByteStream(b"{}"), request=req)
        await ue.read_error_body(err)
        await ue.read_error_body(ok)
        assert err.text == "{}"
        with pytest.raises(httpx.ResponseNotRead):
            ok.text  # noqa: B018

    def test_client_error_response_is_a_400_with_the_hit_body(self):
        hit = ue.classify_unsupported_content(400, _text(400), PAYLOAD)
        assert hit is not None
        resp = ue.client_error_response(hit)
        assert resp.status_code == 400
        assert json.loads(resp.body) == hit.error_body
