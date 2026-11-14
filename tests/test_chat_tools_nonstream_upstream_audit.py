# ruff: noqa: F811
"""AT-TOOLS-NONSTREAM-UPSTREAM-4XX-AUDIT — RED set v2 (spec 2026-11-10 + ADDENDUM-1).

v2 = the v1 set (R1-R6, P1, P2 unchanged) + ADDENDUM-1: a failed turn keeps its
memory tool_calls rows (K1-K4), the tools stream buffered fallback surfaces an
upstream 4xx (F1), every return point of the retry helper raises on a non-2xx
(L2-L4), no file part is echoed (N1), and the
streamed detail prefers the upstream text (D1-D4, superseding v1 S1/S2).

memory_mode=tools (deployed), non-stream: today an UNMATCHED upstream 4xx from
llama-server (e.g. ``exceed_context_size_error``) is persisted as
``status=success`` with an empty answer, because the loop RETURNS the 4xx
body and ``_handle_tools_mode`` reads it as a final answer. After the fix every
upstream non-2xx on this arm records ``failed`` / ``upstream_error`` /
``status=<code>: <upstream text[:400]>``.

Also in scope (pins that are GREEN before and after):
* AT-B1-AUDIT-ROW-IDENTITY-PIN — session_id / user_id / model / question on
  the tools non-stream failed row.
* AT-STREAM-5XX-DETAIL-BOUND-PIN — the ``[:400]`` cut on the streamed 5xx
  ``error_detail`` (both stream arms).

Through the real HTTP route with a stub upstream returning REAL
``httpx.Response`` objects (so ``.json()`` / ``.text`` / ``raise_for_status``
behave as in production).
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from sqlalchemy import select

from audittrace.db.models import ToolCall
from audittrace.dependencies import get_postgres_factory
from audittrace.identity import SENTINEL_SUBJECT
from audittrace.routes.chat import FAILURE_UPSTREAM_ERROR, _emit_sse_error_frame
from tests.test_chat_failure_audit import _latest_interaction
from tests.test_chat_proxy import (
    _FakeAsyncClient,
    _patch_async_client,
    _patch_tool_loop_client,
    _sse_tool_call_lines,
    _tools_mode_tool_call_response,
)
from tests.test_chat_unsupported_content_error import (  # noqa: F401 (fixtures)
    LLAMA_UNRELATED_400_BODY,
    LLAMA_UNSUPPORTED_BODY,
    UPLOAD_TO_PROVIDER_MESSAGES,
    _assert_no_echo,
    _RealRespCtx,
    inject_mode,
    tools_mode,
)

# llama-server build 10288: server-common.cpp ERROR_TYPE_EXCEED_CONTEXT_SIZE ->
# "exceed_context_size_error", HTTP 400; text from server-context.cpp:3211.
LLAMA_CONTEXT_EXCEEDED_BODY = {
    "error": {
        "code": 400,
        "message": (
            "request (170000 tokens) exceeds the available context size "
            "(163840 tokens), try increasing it"
        ),
        "type": "exceed_context_size_error",
    }
}
LLAMA_500_BODY = {
    "error": {
        "code": 500,
        "message": "CUDA error: out of memory",
        "type": "server_error",
    }
}
NON_JSON_404_TEXT = "<html><body>upstream not found</body></html>"
LONG_MESSAGE = "m" * 600
LLAMA_LONG_400_BODY = {
    "error": {"code": 400, "message": LONG_MESSAGE, "type": "invalid_request_error"}
}
DETAIL_PREFIX_LEN = len("status=400: ")
DETAIL_CUT = 400
SESSION_HEADER = "sess-4xx-audit-0001"
PROMPT = "Summarise the quarterly numbers."
MODEL = "audittrace-default"
LONG_URL = "http://llama.test/" + "x" * 600 + "/v1/chat/completions"


def _req(url: str) -> httpx.Request:
    return httpx.Request("POST", url)


class _UpstreamStatusClient(_FakeAsyncClient):
    """Upstream answering ``status`` with ``raw`` bytes on POST and on stream."""

    def __init__(
        self,
        status: int,
        raw: bytes,
        content_type: str = "application/json",
        url: str | None = None,
    ) -> None:
        super().__init__()
        self._status = status
        self._raw = raw
        self._ct = content_type
        self._url = url
        self.stream_calls: list[dict] = []

    async def post(self, url, json=None, **kwargs):  # type: ignore[override]
        self.last_post_url = url
        self.last_post_json = json
        self.post_calls.append({"url": url, "json": json, "kwargs": kwargs})
        return httpx.Response(
            self._status,
            content=self._raw,
            headers={"content-type": self._ct},
            request=_req(self._url or url),
        )

    def stream(self, method, url, json=None, **kwargs):  # type: ignore[override]
        self.last_stream_json = json
        self.stream_calls.append({"url": url, "json": json})
        resp = httpx.Response(
            self._status,
            stream=httpx.ByteStream(self._raw),
            headers={"content-type": self._ct},
            request=_req(self._url or url),
        )
        return _RealRespCtx(resp)


def _json_client(status: int, body: dict) -> _UpstreamStatusClient:
    return _UpstreamStatusClient(status, json.dumps(body).encode())


def _post(client, messages, *, stream: bool):
    return client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": messages,
            "project": "AuditTrace",
            "stream": stream,
        },
        headers={"X-Session-Id": SESSION_HEADER},
    )


PLAIN_MESSAGES = [{"role": "user", "content": PROMPT}]


async def _failed_upstream_row(status: int):
    row = await _latest_interaction()
    assert row is not None
    print(f"DIAG row.status={row.status!r} failure_class={row.failure_class!r}")
    assert row.status == "failed"
    assert row.failure_class == "upstream_error"
    assert row.answer == ""
    assert row.error_detail is not None
    assert row.error_detail.startswith(f"status={status}: ")
    return row


def _assert_identity(row, question: str) -> None:
    # AT-B1-AUDIT-ROW-IDENTITY-PIN (Art 12 traceability fields, exact)
    assert row.session_id == SESSION_HEADER
    assert row.user_id == SENTINEL_SUBJECT
    assert row.model == MODEL
    assert row.question == question


# ───────────── RED: unmatched upstream non-2xx on tools × non-stream ─────────────


class TestToolsNonStreamUnmatchedUpstreamErrorIsAudited:
    async def test_context_exceeded_400_records_failed_upstream_error(
        self, client, tools_mode
    ):
        fake = _json_client(400, LLAMA_CONTEXT_EXCEEDED_BODY)
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, PLAIN_MESSAGES, stream=False)
        # Client-visible behaviour KEPT: HTTP 200 + the upstream body verbatim.
        assert response.status_code == 200
        assert response.json() == LLAMA_CONTEXT_EXCEEDED_BODY
        row = await _failed_upstream_row(400)
        assert "exceeds the available context size" in row.error_detail
        _assert_identity(row, PROMPT)

    async def test_unrelated_400_records_failed_upstream_error(
        self, client, tools_mode
    ):
        fake = _json_client(400, LLAMA_UNRELATED_400_BODY)
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=False)
        assert response.status_code == 200
        assert response.json() == LLAMA_UNRELATED_400_BODY
        row = await _failed_upstream_row(400)
        assert LLAMA_UNRELATED_400_BODY["error"]["message"] in row.error_detail
        _assert_identity(row, "Summarise the attached file.")

    async def test_non_json_404_records_upstream_error_not_internal(
        self, client, tools_mode
    ):
        fake = _UpstreamStatusClient(404, NON_JSON_404_TEXT.encode(), "text/html")
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, PLAIN_MESSAGES, stream=False)
        print(f"DIAG client status={response.status_code} body={response.text[:120]!r}")
        row = await _failed_upstream_row(404)
        assert "upstream not found" in row.error_detail
        _assert_identity(row, PROMPT)
        # A body that is not JSON cannot be returned as a completion: same
        # class as the 5xx arm (502), no longer the ADR-033 500 envelope.
        assert response.status_code == 502
        assert response.json() == {"detail": "llama-server returned 404"}

    async def test_unmatched_400_detail_is_cut_at_400_chars(self, client, tools_mode):
        fake = _json_client(400, LLAMA_LONG_400_BODY)
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, PLAIN_MESSAGES, stream=False)
        assert response.status_code == 200
        row = await _failed_upstream_row(400)
        assert len(row.error_detail) == DETAIL_PREFIX_LEN + DETAIL_CUT

    async def test_unmatched_400_one_warning_carries_the_body(
        self, client, tools_mode, caplog
    ):
        # one-WARNING rule (#445 ADDENDUM-1 §5): the loop's line is THE line
        # on this arm; the fix must not add a second one.
        fake = _json_client(400, LLAMA_CONTEXT_EXCEEDED_BODY)
        with (
            caplog.at_level(logging.WARNING),
            _patch_async_client(fake),
            _patch_tool_loop_client(fake),
        ):
            _post(client, PLAIN_MESSAGES, stream=False)
        carrying = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING
            and "exceeds the available context size" in r.getMessage()
        ]
        assert len(carrying) == 1
        assert carrying[0].name == "audittrace.routes._memory_tool_loop"


class TestLoopRaisesForEveryNon2xx:
    async def test_loop_4xx_raises_http_status_error(self):
        # Supersedes test_memory_tool_loop.py::test_4xx_passes_through_unchanged
        # (quoted in the spec): the retry helper returns ONLY a 2xx response.
        from audittrace.identity import sentinel_user_context
        from audittrace.routes._memory_tool_loop import run_memory_tool_loop

        fake = _json_client(400, LLAMA_CONTEXT_EXCEEDED_BODY)
        with _patch_tool_loop_client(fake), pytest.raises(httpx.HTTPStatusError) as ei:
            await run_memory_tool_loop(
                llama_url="http://llama.test/v1/chat/completions",
                payload={"model": MODEL, "messages": PLAIN_MESSAGES, "tools": []},
                user_context=sentinel_user_context(),
                session_id="sess-loop-4xx",
                max_iterations=5,
            )
        assert ei.value.response.status_code == 400
        assert ei.value.response.json() == LLAMA_CONTEXT_EXCEEDED_BODY
        assert len(fake.post_calls) == 1


# ───────────── PINS (GREEN before and after) ─────────────


class TestToolsNonStreamPins:
    async def test_non_parse_500_is_502_and_failed_row(self, client, tools_mode):
        fake = _json_client(500, LLAMA_500_BODY)
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, PLAIN_MESSAGES, stream=False)
        assert response.status_code == 502
        assert response.json() == {"detail": "llama-server returned 500"}
        row = await _failed_upstream_row(500)
        assert "out of memory" in row.error_detail
        _assert_identity(row, PROMPT)

    async def test_matched_unsupported_content_row_identity(self, client, tools_mode):
        # AT-B1-AUDIT-ROW-IDENTITY-PIN on the #445 arm (explicit 400 kept).
        fake = _json_client(400, LLAMA_UNSUPPORTED_BODY)
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=False)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unsupported_content_type"
        row = await _failed_upstream_row(400)
        assert "unsupported content[].type" in row.error_detail
        _assert_identity(row, "Summarise the attached file.")


class TestStreamDetailPrefersUpstreamText:
    # ADDENDUM-1 (operator Q3): the streamed error_detail carries the upstream
    # BODY TEXT, cut at 400; str(exc) only when the body is empty. Supersedes
    # the v1 pins S1/S2 (which pinned str(exc) for a non-empty body).
    STR_EXC_START = "status=500: Server error '500 Internal Server Error' for url 'http://llama.test/xxxx"
    LONG_500 = json.dumps(
        {"error": {"code": 500, "message": LONG_MESSAGE, "type": "server_error"}}
    ).encode()

    async def _detail(self, client, raw: bytes, *, tools: bool) -> str:
        fake = _UpstreamStatusClient(500, raw, url=LONG_URL)
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, PLAIN_MESSAGES, stream=True)
        assert response.status_code == 200
        assert "data: [DONE]" in response.text
        row = await _failed_upstream_row(500)
        print(f"DIAG detail[:60]={row.error_detail[:60]!r} len={len(row.error_detail)}")
        assert len(row.error_detail) == DETAIL_PREFIX_LEN + DETAIL_CUT
        return row.error_detail

    async def test_tools_stream_5xx_detail_is_upstream_text_cut_at_400(
        self, client, tools_mode
    ):
        detail = await self._detail(client, self.LONG_500, tools=True)
        assert detail == "status=500: " + self.LONG_500.decode()[:DETAIL_CUT]

    async def test_inject_stream_5xx_detail_is_upstream_text_cut_at_400(
        self, client, inject_mode
    ):
        detail = await self._detail(client, self.LONG_500, tools=False)
        assert detail == "status=500: " + self.LONG_500.decode()[:DETAIL_CUT]

    async def test_tools_stream_5xx_empty_body_falls_back_to_str_exc(
        self, client, tools_mode
    ):
        detail = await self._detail(client, b"", tools=True)
        assert detail.startswith(self.STR_EXC_START)

    async def test_inject_stream_5xx_empty_body_falls_back_to_str_exc(
        self, client, inject_mode
    ):
        detail = await self._detail(client, b"", tools=False)
        assert detail.startswith(self.STR_EXC_START)


# ───────────── ADDENDUM-1: sequenced upstream (real httpx.Response per call) ─────────────

LLAMA_TOOL_PARSE_500_BODY = {
    "error": {
        "code": 500,
        "type": "server_error",
        "message": (
            "Failed to parse tool call arguments as JSON: "
            "[json.exception.parse_error.101]"
        ),
    }
}
RECALL_ARGS = '{"query": "quarterly numbers"}'
RECALL_POST = (
    200,
    json.dumps(
        _tools_mode_tool_call_response("recall_decisions", RECALL_ARGS)
    ).encode(),
)
RECALL_STREAM = (
    200,
    ("\n".join(_sse_tool_call_lines("recall_decisions", RECALL_ARGS)) + "\n").encode(),
)
CTX_400 = (400, json.dumps(LLAMA_CONTEXT_EXCEEDED_BODY).encode())
OOM_500 = (500, json.dumps(LLAMA_500_BODY).encode())
PARSE_500 = (500, json.dumps(LLAMA_TOOL_PARSE_500_BODY).encode())
REDIRECT_302 = (302, b"")
URL = "http://llama.test/v1/chat/completions"


class _SeqRealClient(_FakeAsyncClient):
    """Upstream answering each POST / stream call with the next ``(status, raw)``."""

    def __init__(
        self,
        posts: list[tuple[int, bytes]] | None = None,
        streams: list[tuple[int, bytes]] | None = None,
    ) -> None:
        super().__init__()
        self._posts = list(posts or [])
        self._streams = list(streams or [])
        self.stream_calls: list[dict] = []

    async def post(self, url, json=None, **kwargs):  # type: ignore[override]
        self.post_calls.append({"url": url, "json": json, "kwargs": kwargs})
        status, raw = self._posts[len(self.post_calls) - 1]
        return httpx.Response(
            status,
            content=raw,
            headers={"content-type": "application/json"},
            request=_req(URL),
        )

    def stream(self, method, url, json=None, **kwargs):  # type: ignore[override]
        self.stream_calls.append({"url": url, "json": json})
        status, raw = self._streams[len(self.stream_calls) - 1]
        resp = httpx.Response(
            status,
            stream=httpx.ByteStream(raw),
            headers={"content-type": "application/json"},
            request=_req(URL),
        )
        return _RealRespCtx(resp)


async def _tool_call_rows() -> list[ToolCall]:
    pg = get_postgres_factory()
    async with pg.get_session_factory()() as db:
        return list((await db.execute(select(ToolCall))).scalars().all())


async def _assert_one_recall_row_on_failed_turn(status: int) -> None:
    rows = await _tool_call_rows()
    print(f"DIAG tool_call_rows={len(rows)} names={[r.tool_name for r in rows]}")
    row = await _failed_upstream_row(status)
    assert len(rows) == 1
    assert rows[0].tool_name == "recall_decisions"
    assert rows[0].interaction_id == row.id
    assert rows[0].user_id == SENTINEL_SUBJECT
    assert rows[0].granted_scope == "memory:episodic:read"


class TestFailedTurnKeepsToolCallRows:
    # ADDENDUM-1 (operator Q4): the memory tool_calls of a turn that then fails
    # on an upstream non-2xx are kept, linked to the FAILED interaction row.
    async def test_non_stream_recall_then_400_keeps_tool_call_row(
        self, client, tools_mode
    ):
        fake = _SeqRealClient(posts=[RECALL_POST, CTX_400])
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, PLAIN_MESSAGES, stream=False)
        assert len(fake.post_calls) == 2
        assert response.status_code == 200
        assert response.json() == LLAMA_CONTEXT_EXCEEDED_BODY
        await _assert_one_recall_row_on_failed_turn(400)

    async def test_non_stream_recall_then_500_keeps_tool_call_row(
        self, client, tools_mode
    ):
        fake = _SeqRealClient(posts=[RECALL_POST, OOM_500])
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, PLAIN_MESSAGES, stream=False)
        assert len(fake.post_calls) == 2
        assert response.status_code == 502
        await _assert_one_recall_row_on_failed_turn(500)

    async def test_stream_recall_then_400_keeps_tool_call_row(self, client, tools_mode):
        fake = _SeqRealClient(streams=[RECALL_STREAM, CTX_400])
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, PLAIN_MESSAGES, stream=True)
        assert len(fake.stream_calls) == 2
        assert response.status_code == 200
        await _assert_one_recall_row_on_failed_turn(400)

    async def test_stream_fallback_recall_then_400_keeps_tool_call_row(
        self, client, tools_mode
    ):
        fake = _SeqRealClient(streams=[RECALL_STREAM, PARSE_500], posts=[CTX_400])
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, PLAIN_MESSAGES, stream=True)
        assert (len(fake.stream_calls), len(fake.post_calls)) == (2, 1)
        assert response.status_code == 200
        await _assert_one_recall_row_on_failed_turn(400)


class TestToolsStreamFallbackUpstream4xx:
    # ADDENDUM-1 (operator Q5): streamed tool-parse 500, then the buffered
    # fallback POST gets an upstream 4xx. Before: an empty SUCCESS stream.
    # After: the generic upstream_error frame + [DONE], and a failed row.
    async def test_fallback_400_emits_upstream_error_frame_and_failed_row(
        self, client, tools_mode
    ):
        fake = _SeqRealClient(streams=[PARSE_500], posts=[CTX_400])
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, PLAIN_MESSAGES, stream=True)
        print(f"DIAG sse={response.content!r}")
        assert (len(fake.stream_calls), len(fake.post_calls)) == (1, 1)
        assert response.status_code == 200
        assert response.content == _emit_sse_error_frame(
            FAILURE_UPSTREAM_ERROR, "llama-server returned status 400", 502
        )
        row = await _failed_upstream_row(400)
        assert row.error_detail == "status=400: " + CTX_400[1].decode()
        _assert_identity(row, PROMPT)


class TestLoopRaisesAtEveryReturnPoint:
    # AC-2 / gate A3: the retry helper has THREE return points; each returns
    # only a 2xx. The pin is is_success, not "< 400" and not "< 500".
    async def _raises(self, posts: list[tuple[int, bytes]]) -> tuple[int, int]:
        from audittrace.identity import sentinel_user_context
        from audittrace.routes._memory_tool_loop import run_memory_tool_loop

        fake = _SeqRealClient(posts=posts)
        with _patch_tool_loop_client(fake), pytest.raises(httpx.HTTPStatusError) as ei:
            await run_memory_tool_loop(
                llama_url=URL,
                payload={"model": MODEL, "messages": PLAIN_MESSAGES, "tools": []},
                user_context=sentinel_user_context(),
                session_id="sess-loop-points",
                max_iterations=5,
            )
        return ei.value.response.status_code, len(fake.post_calls)

    async def test_loop_302_raises_http_status_error(self):
        assert await self._raises([REDIRECT_302]) == (302, 1)

    async def test_retry_point_4xx_raises_http_status_error(self):
        assert await self._raises([PARSE_500, CTX_400]) == (400, 2)

    async def test_fallback_point_4xx_raises_http_status_error(self):
        from audittrace.routes._memory_tool_loop import _TOOL_PARSE_ERROR_RETRIES

        posts = [PARSE_500] * (1 + _TOOL_PARSE_ERROR_RETRIES) + [CTX_400]
        assert await self._raises(posts) == (400, _TOOL_PARSE_ERROR_RETRIES + 2)


class TestNoEchoOfFileParts:
    async def test_unmatched_400_row_does_not_echo_file_parts(self, client, tools_mode):
        # AC-6 (ADDENDUM-1): the proxy writes no request content of its own
        # into the row; the upstream text here names no file part.
        fake = _json_client(400, LLAMA_UNRELATED_400_BODY)
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=False)
        row = await _failed_upstream_row(400)
        _assert_no_echo(row.error_detail)
        _assert_no_echo(response.text)
