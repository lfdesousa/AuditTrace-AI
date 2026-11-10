"""AT-UNSUPPORTED-CONTENT-ERROR — RED set (spec 2026-11-09).

When llama-server answers HTTP 400 with the body
``{"error":{"code":400,"message":"unsupported content[].type","type":"invalid_request_error"}}``
the proxy must return the SAME OpenAI error shape it uses today, with an
explicit, actionable ``message`` and a stable ``code``, on every path
(inject/tools x stream/non-stream). The audit row keeps the upstream's
original text. An UNRELATED upstream 400 is byte-identical to today.

Through the real HTTP route with a stub upstream (real ``httpx.Response``
objects so ``raise_for_status`` / ``aread`` / ``.text`` behave as in
production; the streamed stub is stream-backed so ``.text`` raises
``ResponseNotRead`` until ``aread()``, as a real socket response does).
"""

from __future__ import annotations

import json

import httpx
import pytest

from tests.test_chat_failure_audit import _latest_interaction
from tests.test_chat_proxy import (
    _FakeAsyncClient,
    _ok_chat_response,
    _patch_async_client,
    _patch_tool_loop_client,
)
from tests.test_openai_compatibility import (
    OPENAI_ERROR_REQUIRED_KEYS,
    _parse_sse_error_frame,
)

LLAMA_UNSUPPORTED_BODY = {
    "error": {
        "code": 400,
        "message": "unsupported content[].type",
        "type": "invalid_request_error",
    }
}
LLAMA_UNRELATED_400_BODY = {
    "error": {
        "code": 400,
        "message": "Invalid value for 'messages': expected a non-empty array",
        "type": "invalid_request_error",
    }
}
EXPECTED_CODE = "unsupported_content_type"
EXPECTED_TYPE = "invalid_request_error"
EXPECTED_MESSAGE = (
    "This model can't receive files or images directly in the message "
    "(unsupported content part type 'file'). Use 'Upload as Text' instead."
)
FILE_DATA = "data:text/plain;base64,aGVsbG8gd29ybGQ="  # never echoed back
FILENAME = "notes-2026-11-09.txt"
UPLOAD_TO_PROVIDER_MESSAGES = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "Summarise the attached file."},
            {"type": "file", "file": {"filename": FILENAME, "file_data": FILE_DATA}},
        ],
    }
]
UPLOAD_AS_TEXT_MESSAGES = [
    {
        "role": "user",
        "content": (
            'Attached document(s):\n```md\n# "notes.txt"\nThe meeting is on Thursday.\n```\n\n'
            "When is the meeting?"
        ),
    }
]


def _req(url: str = "http://llama.test/v1/chat/completions") -> httpx.Request:
    return httpx.Request("POST", url)


class _Upstream400Client(_FakeAsyncClient):
    """Upstream that answers HTTP 400 with ``body`` on POST and on stream."""

    def __init__(self, body: dict) -> None:
        super().__init__()
        self._body = body
        self.stream_calls: list[dict] = []

    async def post(self, url, json=None, **kwargs):  # type: ignore[override]
        self.last_post_url = url
        self.last_post_json = json
        self.post_calls.append({"url": url, "json": json, "kwargs": kwargs})
        return httpx.Response(400, json=self._body, request=_req(url))

    def stream(self, method, url, json=None, **kwargs):  # type: ignore[override]
        self.last_stream_json = json
        self.stream_calls.append({"url": url, "json": json})
        raw = _json_bytes(self._body)
        resp = httpx.Response(
            400,
            stream=httpx.ByteStream(raw),
            headers={"content-type": "application/json"},
            request=_req(url),
        )
        return _RealRespCtx(resp)


class _RealRespCtx:
    def __init__(self, resp: httpx.Response) -> None:
        self._resp = resp

    async def __aenter__(self) -> httpx.Response:
        return self._resp

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self._resp.aclose()
        return False


def _json_bytes(body: dict) -> bytes:
    return json.dumps(body).encode()


def _flip_mode(monkeypatch, mode: str):
    from audittrace import config as config_mod

    config_mod.get_settings.cache_clear()
    monkeypatch.setenv("AUDITTRACE_MEMORY_MODE", mode)
    yield
    config_mod.get_settings.cache_clear()


@pytest.fixture
def tools_mode(monkeypatch):
    yield from _flip_mode(monkeypatch, "tools")


@pytest.fixture
def inject_mode(monkeypatch):
    yield from _flip_mode(monkeypatch, "inject")


def _post(client, messages, *, stream: bool):
    return client.post(
        "/v1/chat/completions",
        json={
            "model": "audittrace-default",
            "messages": messages,
            "project": "AuditTrace",
            "stream": stream,
        },
    )


def _assert_explicit_error(err: dict, *, status_ext: int = 400) -> None:
    assert OPENAI_ERROR_REQUIRED_KEYS.issubset(err.keys()), sorted(err.keys())
    assert err["message"] == EXPECTED_MESSAGE
    assert err["code"] == EXPECTED_CODE
    assert err["type"] == EXPECTED_TYPE
    assert err["param"] == "messages"
    assert err["status"] == status_ext


def _assert_no_echo(text: str) -> None:
    assert FILE_DATA not in text
    assert "aGVsbG8gd29ybGQ" not in text
    assert FILENAME not in text


async def _assert_failed_row_keeps_upstream_text() -> None:
    row = await _latest_interaction()
    assert row is not None
    assert row.status == "failed"
    assert row.failure_class == "upstream_error"
    assert row.error_detail is not None
    assert row.error_detail.startswith("status=400: ")
    assert "unsupported content[].type" in row.error_detail
    _assert_no_echo(row.error_detail)
    assert row.session_id
    assert row.user_id


# ───────────────────────── RED: the explicit error ─────────────────────────


class TestUnsupportedContentExplicitError:
    async def test_tools_stream_frame_is_explicit(self, client, tools_mode):
        fake = _Upstream400Client(LLAMA_UNSUPPORTED_BODY)
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=True)
        assert response.status_code == 200
        body = response.content.decode()
        err = _parse_sse_error_frame(body)["error"]
        _assert_explicit_error(err)
        assert body.rstrip().endswith("[DONE]")
        _assert_no_echo(body)
        await _assert_failed_row_keeps_upstream_text()

    async def test_tools_non_stream_body_is_explicit_400(self, client, tools_mode):
        fake = _Upstream400Client(LLAMA_UNSUPPORTED_BODY)
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=False)
        assert response.status_code == 400
        err = response.json()["error"]
        _assert_explicit_error(err)
        _assert_no_echo(response.text)
        await _assert_failed_row_keeps_upstream_text()

    async def test_inject_stream_frame_is_explicit(self, client, inject_mode):
        fake = _Upstream400Client(LLAMA_UNSUPPORTED_BODY)
        with _patch_async_client(fake):
            response = _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=True)
        assert response.status_code == 200
        body = response.content.decode()
        err = _parse_sse_error_frame(body)["error"]
        _assert_explicit_error(err)
        assert body.rstrip().endswith("[DONE]")
        _assert_no_echo(body)
        await _assert_failed_row_keeps_upstream_text()

    async def test_inject_non_stream_body_is_explicit_400(self, client, inject_mode):
        fake = _Upstream400Client(LLAMA_UNSUPPORTED_BODY)
        with _patch_async_client(fake):
            response = _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=False)
        assert response.status_code == 400
        err = response.json()["error"]
        _assert_explicit_error(err)
        _assert_no_echo(response.text)
        await _assert_failed_row_keeps_upstream_text()

    async def test_request_is_forwarded_unmodified(self, client, tools_mode):
        """ADR-024: the explicit error is a response-side change only."""
        fake = _Upstream400Client(LLAMA_UNSUPPORTED_BODY)
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=True)
        sent = fake.stream_calls[0]["json"]["messages"]
        user_turn = [m for m in sent if m.get("role") == "user"][-1]
        assert user_turn["content"] == UPLOAD_TO_PROVIDER_MESSAGES[0]["content"]


# ──────────── PIN (GREEN before and after): unrelated 400 unchanged ────────────


class TestUnrelatedUpstream400Unchanged:
    async def test_tools_stream_unrelated_400_frame_unchanged(self, client, tools_mode):
        fake = _Upstream400Client(LLAMA_UNRELATED_400_BODY)
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=True)
        assert response.status_code == 200
        err = _parse_sse_error_frame(response.content.decode())["error"]
        assert err == {
            "message": "llama-server returned status 400",
            "type": "api_error",
            "param": None,
            "code": "upstream_error",
            "status": 502,
        }

    async def test_inject_stream_unrelated_400_frame_unchanged(
        self, client, inject_mode
    ):
        fake = _Upstream400Client(LLAMA_UNRELATED_400_BODY)
        with _patch_async_client(fake):
            response = _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=True)
        assert response.status_code == 200
        err = _parse_sse_error_frame(response.content.decode())["error"]
        assert err == {
            "message": "llama-server returned status 400",
            "type": "api_error",
            "param": None,
            "code": "upstream_error",
            "status": 502,
        }

    async def test_inject_non_stream_unrelated_400_body_unchanged(
        self, client, inject_mode
    ):
        fake = _Upstream400Client(LLAMA_UNRELATED_400_BODY)
        with _patch_async_client(fake):
            response = _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=False)
        assert response.status_code == 200
        assert response.json() == LLAMA_UNRELATED_400_BODY
        row = await _latest_interaction()
        assert row is not None and row.status == "failed"

    async def test_tools_non_stream_unrelated_400_body_unchanged(
        self, client, tools_mode
    ):
        fake = _Upstream400Client(LLAMA_UNRELATED_400_BODY)
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=False)
        assert response.status_code == 200
        assert response.json() == LLAMA_UNRELATED_400_BODY
        row = await _latest_interaction()
        assert row is not None
        print(f"DIAG tools-non-stream unrelated-400 row.status={row.status!r}")


# ─────────────── PIN (GREEN before and after): Upload as Text passes ───────────────


class TestUploadAsTextPassesThrough:
    async def test_inject_non_stream_attached_document_text_reaches_upstream(
        self, client, inject_mode
    ):
        fake = _FakeAsyncClient(post_json=_ok_chat_response("Thursday."))
        with _patch_async_client(fake):
            response = _post(client, UPLOAD_AS_TEXT_MESSAGES, stream=False)
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "Thursday."
        sent_user = [m for m in fake.last_post_json["messages"] if m["role"] == "user"][
            -1
        ]
        assert sent_user["content"] == UPLOAD_AS_TEXT_MESSAGES[0]["content"]
        row = await _latest_interaction()
        assert row is not None and row.status == "success"
