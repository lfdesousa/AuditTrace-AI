"""MODEL-MMPROJ-PERMANENT ADDENDUM-1 -- explicit error when the model server has no vision projector.

A llama-server started without ``--mmproj`` answers an ``image_url`` content part with
HTTP 500 and the body below (measured on the deployed build). That state is reachable:
``AUDITTRACE_MODEL_ROUTES`` can route a request to a server that has no projector. The
proxy must turn that ONE rejection, matched by identity, into the same OpenAI error
shape it already uses, as a client error (HTTP 400): the request is correctable by the
client. The audit row keeps the upstream's original status and text. Any other upstream
500 is byte-identical to today.

Through the real HTTP route with a stub upstream (real ``httpx.Response`` objects; the
streamed one is stream-backed, so ``.text`` raises ``ResponseNotRead`` until ``aread()``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.test_chat_failure_audit import _latest_interaction
from tests.test_chat_proxy import (
    _FakeAsyncClient,
    _patch_async_client,
    _patch_tool_loop_client,
)
from tests.test_chat_unsupported_content_error import _flip_mode, _RealRespCtx, _req
from tests.test_openai_compatibility import _parse_sse_error_frame

#: The exact body llama-server returned without a projector (probe, same build).
LLAMA_IMAGE_500_TEXT = '{"error":{"code":500,"message":"image input is not supported - hint: if this is unexpected, you may need to provide the mmproj","type":"server_error"}}'
LLAMA_IMAGE_500_BODY: dict[str, Any] = json.loads(LLAMA_IMAGE_500_TEXT)
UPSTREAM_MESSAGE: str = LLAMA_IMAGE_500_BODY["error"]["message"]
LLAMA_OTHER_500_BODY: dict[str, Any] = {
    "error": {"code": 500, "message": "out of memory", "type": "server_error"}
}
LLAMA_AUDIO_500_BODY: dict[str, Any] = {
    "error": {
        "code": 500,
        "message": UPSTREAM_MESSAGE.replace("image input", "audio input"),
        "type": "server_error",
    }
}

EXPECTED_ERROR = {
    "message": (
        "The model server selected for this request does not accept image input. "
        "Choose a model that accepts images, or remove the image from the message."
    ),
    "type": "invalid_request_error",
    "param": "messages",
    "code": "image_input_unsupported",
    "status": 400,
}
GENERIC_500_FRAME = {
    "message": "llama-server returned status 500",
    "type": "api_error",
    "param": None,
    "code": "upstream_error",
    "status": 502,
}
B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg=="
IMAGE_MESSAGES = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "What does this picture show?"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{B64}"},
            },
        ],
    }
]
_dumps = json.dumps
GUIDE = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "guides"
    / "librechat-attachments-support.md"
)


class _UpstreamErrorClient(_FakeAsyncClient):
    """Upstream that answers ``status`` with ``body`` on POST and on stream."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__()
        self._status = status
        self._body = body
        self.stream_calls: list[dict[str, Any]] = []

    async def post(self, url, json=None, **kwargs):  # type: ignore[no-untyped-def,override]
        self.post_calls.append({"url": url, "json": json, "kwargs": kwargs})
        return httpx.Response(self._status, json=self._body, request=_req(url))

    def stream(self, method, url, json=None, **kwargs):  # type: ignore[no-untyped-def,override]
        self.stream_calls.append({"url": url, "json": json})
        resp = httpx.Response(
            self._status,
            stream=httpx.ByteStream(_dumps(self._body).encode()),
            headers={"content-type": "application/json"},
            request=_req(url),
        )
        return _RealRespCtx(resp)


@pytest.fixture
def tools_mode(monkeypatch):  # type: ignore[no-untyped-def]
    yield from _flip_mode(monkeypatch, "tools")


@pytest.fixture
def inject_mode(monkeypatch):  # type: ignore[no-untyped-def]
    yield from _flip_mode(monkeypatch, "inject")


def _post(client: Any, *, stream: bool, model: str = "mistral") -> Any:
    return client.post(
        "/v1/chat/completions",
        json={
            "model": model,
            "messages": IMAGE_MESSAGES,
            "project": "AuditTrace",
            "stream": stream,
        },
    )


def _run(
    client: Any, mode: str, stream: bool, status: int, body: dict[str, Any]
) -> Any:
    fake = _UpstreamErrorClient(status, body)
    if mode == "tools":
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            return _post(client, stream=stream), fake
    with _patch_async_client(fake):
        return _post(client, stream=stream), fake


async def _assert_failed_row_keeps_upstream_text() -> None:
    row = await _latest_interaction()
    assert row is not None
    assert row.status == "failed"
    assert row.failure_class == "upstream_error"
    assert row.error_detail is not None
    assert row.error_detail.startswith("status=500: ")
    assert UPSTREAM_MESSAGE in row.error_detail
    assert B64 not in row.error_detail
    assert row.session_id
    assert row.user_id


# ───────────────────────── RED: the explicit error ─────────────────────────


class TestStreamArms:
    async def test_tools_stream_frame_is_explicit(self, client, tools_mode) -> None:  # type: ignore[no-untyped-def]
        response, _ = _run(client, "tools", True, 500, LLAMA_IMAGE_500_BODY)
        assert response.status_code == 200
        text = response.content.decode()
        assert _parse_sse_error_frame(text)["error"] == EXPECTED_ERROR
        assert text.rstrip().endswith("[DONE]")
        assert B64 not in text
        await _assert_failed_row_keeps_upstream_text()

    async def test_inject_stream_frame_is_explicit(self, client, inject_mode) -> None:  # type: ignore[no-untyped-def]
        response, _ = _run(client, "inject", True, 500, LLAMA_IMAGE_500_BODY)
        assert response.status_code == 200
        text = response.content.decode()
        assert _parse_sse_error_frame(text)["error"] == EXPECTED_ERROR
        assert text.rstrip().endswith("[DONE]")
        assert B64 not in text
        await _assert_failed_row_keeps_upstream_text()


class TestNonStreamArms:
    async def test_tools_non_stream_is_an_explicit_400(
        self, client, tools_mode
    ) -> None:  # type: ignore[no-untyped-def]
        response, _ = _run(client, "tools", False, 500, LLAMA_IMAGE_500_BODY)
        assert response.status_code == 400
        assert response.json() == {"error": EXPECTED_ERROR}
        assert B64 not in response.text
        await _assert_failed_row_keeps_upstream_text()

    async def test_inject_non_stream_is_an_explicit_400(
        self, client, inject_mode
    ) -> None:  # type: ignore[no-untyped-def]
        response, _ = _run(client, "inject", False, 500, LLAMA_IMAGE_500_BODY)
        assert response.status_code == 400
        assert response.json() == {"error": EXPECTED_ERROR}
        assert B64 not in response.text
        await _assert_failed_row_keeps_upstream_text()


class TestExactlyOneWarningCarriesTheUpstreamText:
    @pytest.mark.parametrize(
        ("mode", "stream"),
        [("tools", True), ("tools", False), ("inject", True), ("inject", False)],
    )
    async def test_one_warning_and_no_image_data_in_logs(  # type: ignore[no-untyped-def]
        self, client, request, caplog, mode, stream
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        with caplog.at_level(logging.WARNING):
            _run(client, mode, stream, 500, LLAMA_IMAGE_500_BODY)
        hits = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and UPSTREAM_MESSAGE in r.getMessage()
        ]
        assert len(hits) == 1, [r.getMessage()[:100] for r in caplog.records]
        assert hits[0].name.startswith("audittrace.routes.")
        assert all(
            B64 not in r.getMessage()
            for r in caplog.records
            if r.levelno >= logging.WARNING
        )


# ───────────────── RED: the matcher is identity, the body is pinned ─────────────────


def _mod() -> Any:
    from audittrace.routes import _upstream_errors

    return _upstream_errors


def _text(code: object, message: str) -> str:
    return json.dumps({"error": {"code": code, "message": message}})


class TestMatcherIsEquality:
    def test_the_measured_body_matches_at_500(self) -> None:
        assert _mod().match_image_input_unsupported(500, LLAMA_IMAGE_500_TEXT) is True

    @pytest.mark.parametrize(
        "message",
        [
            "image input is not supported",
            UPSTREAM_MESSAGE + "\n",
            UPSTREAM_MESSAGE.capitalize(),
            "error: " + UPSTREAM_MESSAGE,
            UPSTREAM_MESSAGE.replace("image input", "audio input"),
        ],
    )
    def test_near_misses_do_not_match(self, message: str) -> None:
        assert _mod().match_image_input_unsupported(500, _text(500, message)) is False

    def test_the_status_is_part_of_the_match(self) -> None:
        assert _mod().match_image_input_unsupported(400, LLAMA_IMAGE_500_TEXT) is False
        assert _mod().match_image_input_unsupported(502, LLAMA_IMAGE_500_TEXT) is False

    @pytest.mark.parametrize("code", ["500", 500.0, True, None, 400])
    def test_the_body_code_is_the_int_500(self, code: object) -> None:
        text = _text(code, UPSTREAM_MESSAGE)
        assert _mod().match_image_input_unsupported(500, text) is False

    @pytest.mark.parametrize("text", ["", "not json", "[]", '"x"', '{"error":"x"}'])
    def test_shapes_that_are_not_the_error_object_do_not_match(self, text: str) -> None:
        assert _mod().match_image_input_unsupported(500, text) is False

    def test_the_file_matcher_is_not_widened(self) -> None:
        """PIN (green before and after): the two rejections stay two matchers."""
        assert _mod().match_unsupported_content(500, LLAMA_IMAGE_500_TEXT) is False
        assert _mod().match_unsupported_content(400, LLAMA_IMAGE_500_TEXT) is False


class TestErrorBody:
    def test_whole_body_is_pinned_envelope_included(self) -> None:
        assert _mod().image_input_unsupported_error_body() == {"error": EXPECTED_ERROR}

    def test_code_is_a_module_constant(self) -> None:
        assert _mod().IMAGE_INPUT_UNSUPPORTED_CODE == EXPECTED_ERROR["code"]
        assert _mod().LLAMA_IMAGE_INPUT_UNSUPPORTED_MESSAGE == UPSTREAM_MESSAGE

    def test_message_names_no_model_and_no_file_hint(self) -> None:
        built = _mod().image_input_unsupported_error_body()["error"]["message"]
        message = str(built).lower()
        assert message, "the code built an empty message"
        for word in ("mistral", "qwen", "llama", "mmproj", "upload as text"):
            assert word not in message

    def test_no_second_class_in_the_module(self) -> None:
        import ast

        tree = ast.parse(Path(_mod().__file__).read_text())
        classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        assert classes == ["UnsupportedContentHit"]

    def test_public_guide_names_the_code(self) -> None:
        assert "image_input_unsupported" in GUIDE.read_text()


# ──────────── PIN (GREEN before and after): everything else is unchanged ────────────


class TestOtherUpstream500Unchanged:
    @pytest.mark.parametrize("body", [LLAMA_OTHER_500_BODY, LLAMA_AUDIO_500_BODY])
    @pytest.mark.parametrize("mode", ["tools", "inject"])
    async def test_stream_frame_is_the_generic_one(  # type: ignore[no-untyped-def]
        self, client, request, mode, body
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        response, _ = _run(client, mode, True, 500, body)
        assert response.status_code == 200
        frame = _parse_sse_error_frame(response.content.decode())["error"]
        assert frame == GENERIC_500_FRAME

    async def test_tools_non_stream_is_the_502_detail(self, client, tools_mode) -> None:  # type: ignore[no-untyped-def]
        response, _ = _run(client, "tools", False, 500, LLAMA_OTHER_500_BODY)
        assert response.status_code == 502
        assert response.json() == {"detail": "llama-server returned 500"}

    async def test_inject_non_stream_returns_the_upstream_body(  # type: ignore[no-untyped-def]
        self, client, inject_mode
    ) -> None:
        """TODAY'S BEHAVIOUR, NOT ENDORSED: an unmatched upstream 500 on the inject
        non-stream arm is returned as HTTP 200 with the upstream's error object,
        which no OpenAI client can read as a completion. Pinned only so that THIS
        change does not move it; the defect has its own backlog item
        (AT-INJECT-NONSTREAM-UPSTREAM-ERROR-200)."""
        response, _ = _run(client, "inject", False, 500, LLAMA_OTHER_500_BODY)
        assert response.status_code == 200
        assert response.json() == LLAMA_OTHER_500_BODY
        row = await _latest_interaction()
        assert row is not None and row.status == "failed"

    async def test_request_is_forwarded_unmodified(self, client, tools_mode) -> None:  # type: ignore[no-untyped-def]
        """ADR-024: a response-side change only."""
        _, fake = _run(client, "tools", True, 500, LLAMA_IMAGE_500_BODY)
        sent = fake.stream_calls[0]["json"]["messages"]
        user_turn = [m for m in sent if m.get("role") == "user"][-1]
        assert user_turn["content"] == IMAGE_MESSAGES[0]["content"]
