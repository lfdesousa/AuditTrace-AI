"""AT-NONSTREAM-UPSTREAM-STATUS -- the non-stream arms answer an upstream error as an error.

On ``POST /v1/chat/completions`` with ``stream: false``, in both memory modes, a reply from
the model server that is not a 2xx must (1) reach the client as an HTTP error an OpenAI
client can read, never as HTTP 200 carrying an error object, (2) be audited as ONE
``failed`` / ``upstream_error`` row whose ``error_detail`` is
``status=<code>: <upstream text[:400]>`` and whose ``trace_id`` is the reply's, and
(3) be logged as exactly ONE WARNING carrying the upstream text. A 2xx reply that is not
a completion (not JSON, not an object, or carrying an ``error`` object) is the same
failure. The proxy's own 502/504 replies are an OpenAI error object plus ``detail`` and
name no upstream URL; a relayed reply keeps the upstream's ``Retry-After``.

The stream arms get the same rule for a 2xx reply that is not a complete stream (no
completion chunk, an ``error`` object in the stream, or no ``data: [DONE]``): one SSE
error frame, one failed row, one WARNING -- never a ``success`` row with no frame.

Through the real HTTP route; the stub upstream returns real ``httpx.Response`` objects.
Every status assertion is exact. The three module constants below are the operator's
rulings (Q1, Q2, Q3 of the operator question list); nothing else in this file depends on them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from audittrace.config import get_settings
from audittrace.db.models import InteractionRecord
from audittrace.dependencies import get_postgres_factory
from audittrace.identity import SENTINEL_SUBJECT
from audittrace.routes._memory_tool_loop import _TOOL_PARSE_ERROR_RETRIES
from tests.test_chat_image_input_unsupported_error import (
    IMAGE_MESSAGES,
    LLAMA_IMAGE_500_BODY,
)
from tests.test_chat_proxy import (
    _FakeAsyncClient,
    _patch_async_client,
    _patch_tool_loop_client,
)
from tests.test_chat_tools_nonstream_upstream_audit import PARSE_500, _SeqRealClient
from tests.test_chat_unsupported_content_error import (  # noqa: F401 (fixtures)
    LLAMA_UNSUPPORTED_BODY,
    UPLOAD_TO_PROVIDER_MESSAGES,
    _RealRespCtx,
    inject_mode,
    tools_mode,
)
from tests.test_openai_compatibility import _parse_sse_error_frame

#: Operator ruling Q1: an unmatched upstream 5xx with a relayable body is relayed with
#: the upstream's own status (True) or answered 502 (False).
RELAY_5XX: bool = False
#: Operator ruling Q2: the upstream statuses that speak about the proxy's own credentials
#: towards the model server or the network in between; never relayed, answered 502.
#: Empty = relay them too.
HOP_CREDENTIAL_STATUSES: frozenset[int] = frozenset({401, 403, 407, 511})
#: Operator ruling Q3: only a body carrying an ``error`` object is relayed (True), or any
#: JSON object (False).
RELAY_REQUIRES_ERROR_OBJECT: bool = True

MODEL = "audittrace-default"
PROMPT = "Summarise the quarterly numbers."
PLAIN: list[dict[str, Any]] = [{"role": "user", "content": PROMPT}]
MODES = ["tools", "inject"]
CUT = 400
TRACE_ID = re.compile(r"[0-9a-f]{32}")
LOOP_LOGGER = "audittrace.routes._memory_tool_loop"
STREAM_LOGGER = "audittrace.routes._stream_reply"
SSE = {"content-type": "text/event-stream"}
ERRORS_LOGGER = "audittrace.routes._upstream_errors"


def _err(code: int, message: str, type_: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "type": type_}}


# llama-server's own bodies where it has one (llama.cpp tools/server).
CONTEXT_400 = _err(
    400,
    "request (170000 tokens) exceeds the available context size (163840 tokens), "
    "try increasing it",
    "exceed_context_size_error",
)
SERVER_500 = _err(500, "CUDA error: out of memory", "server_error")
LONG_400 = _err(400, "m" * 600, "invalid_request_error")
DETAIL_ONLY = {"detail": [{"loc": ["body", "messages"], "msg": "field required"}]}
ENVELOPE_ON_200 = {
    "error": {
        "message": "upstream_error: model returned malformed response",
        "type": "api_error",
        "param": None,
        "code": "upstream_error",
        "status": 502,
    }
}
HTML_404 = b"<html><body>upstream not found</body></html>"
HTML_502 = b"<html><body>502 Bad Gateway</body></html>"

#: id -> (upstream status, raw body). Every one is an UNMATCHED reply that is not a
#: completion: a non-2xx, or a 2xx that is not JSON, not an object, or an error object.
UNMATCHED: dict[str, tuple[int, bytes]] = {
    "400-context": (400, json.dumps(CONTEXT_400).encode()),
    "401-key": (401, json.dumps(_err(401, "Invalid API Key", "auth_error")).encode()),
    "402": (402, json.dumps(_err(402, "payment", "invalid_request_error")).encode()),
    "403": (403, json.dumps(_err(403, "forbidden", "permission_error")).encode()),
    "404": (404, json.dumps(_err(404, "no such model", "not_found_error")).encode()),
    "407": (407, json.dumps(_err(407, "proxy auth", "auth_error")).encode()),
    "408": (408, json.dumps(_err(408, "request timeout", "timeout_error")).encode()),
    "409": (409, json.dumps(_err(409, "conflict", "conflict_error")).encode()),
    "422-detail-only": (422, json.dumps(DETAIL_ONLY).encode()),
    "429": (429, json.dumps(_err(429, "rate limited", "rate_limit_error")).encode()),
    "500-error-object": (500, json.dumps(SERVER_500).encode()),
    "500-detail-only": (500, b'{"detail": "Internal Server Error"}'),
    "501": (501, json.dumps(_err(501, "not supported", "not_supported")).encode()),
    "503-loading": (
        503,
        json.dumps(_err(503, "Loading model", "unavailable_error")).encode(),
    ),
    "511": (511, json.dumps(_err(511, "network auth", "server_error")).encode()),
    "520-unregistered": (
        520,
        json.dumps(_err(520, "unknown error", "server_error")).encode(),
    ),
    "302-redirect": (302, json.dumps(_err(302, "moved", "redirect")).encode()),
    "404-html": (404, HTML_404),
    "502-html": (502, HTML_502),
    "500-json-array": (500, b'["not", "an", "object"]'),
    "200-html": (200, b"<html><body>it works, but it is no completion</body></html>"),
    "200-json-array": (
        200,
        b'["a", "2xx", "array", "is", "no", "completion", "either"]',
    ),
    "200-error-object": (200, json.dumps(ENVELOPE_ON_200).encode()),
}


class _RaisingCtx:
    """A stream context whose entry raises (the upstream never answers)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> httpx.Response:
        raise self._exc

    async def __aexit__(self, *args: object) -> bool:
        return False


class _Upstream(_FakeAsyncClient):
    """Model-server stub: ``status`` + ``raw`` on POST and on stream, or raises."""

    def __init__(
        self,
        status: int = 200,
        raw: bytes = b"",
        raises: Exception | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.status, self._raw, self._raises = status, raw, raises
        ct = "application/json" if raw[:1] in (b"{", b"[") else "text/html"
        self._headers = {"content-type": ct, **(headers or {})}

    @property
    def text(self) -> str:
        return self._raw.decode()

    async def post(self, url: str, json: Any = None, **kwargs: Any) -> httpx.Response:  # type: ignore[override]
        self.post_calls.append({"url": url, "json": json, "kwargs": kwargs})
        if self._raises is not None:
            raise self._raises
        return httpx.Response(
            self.status,
            content=self._raw,
            headers=self._headers,
            request=httpx.Request("POST", url),
        )

    async def get(self, url: str, **kwargs: Any) -> Any:  # type: ignore[override]
        if self._raises is not None:
            raise self._raises
        return await super().get(url, **kwargs)

    def stream(self, method: str, url: str, json: Any = None, **kwargs: Any) -> Any:  # type: ignore[override]
        if self._raises is not None:
            return _RaisingCtx(self._raises)
        return _RealRespCtx(
            httpx.Response(
                self.status,
                stream=httpx.ByteStream(self._raw),
                headers=self._headers,
                request=httpx.Request("POST", str(url)),
            )
        )


def _json_upstream(status: int, body: dict[str, Any]) -> _Upstream:
    return _Upstream(status, json.dumps(body).encode())


def _sid(request: pytest.FixtureRequest) -> str:
    """A session id unique to this test case (its node id, hashed)."""
    return "ns-" + hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:24]


def _call(
    client: TestClient,
    fake: _Upstream,
    session_id: str,
    *,
    stream: bool = False,
    messages: list[dict[str, Any]] | None = None,
) -> httpx.Response:
    with _patch_async_client(fake), _patch_tool_loop_client(fake):
        return client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": messages or PLAIN,
                "project": "AuditTrace",
                "stream": stream,
            },
            headers={"X-Session-Id": session_id},
        )


async def _only_row(session_id: str) -> InteractionRecord:
    """The ONE interaction row of the turn (never zero, never two)."""
    async with get_postgres_factory().get_session_factory()() as db:
        result = await db.execute(
            select(InteractionRecord).where(InteractionRecord.session_id == session_id)
        )
        rows = list(result.scalars().all())
    assert len(rows) == 1, [(r.status, r.failure_class) for r in rows]
    return rows[0]


async def _assert_failed_row(
    session_id: str,
    status: int,
    text: str,
    response: httpx.Response | None,
    question: str = PROMPT,
) -> None:
    row = await _only_row(session_id)
    assert row.status == "failed"
    assert row.failure_class == "upstream_error"
    assert row.answer == ""
    assert row.error_detail == f"status={status}: {text[:CUT]}"
    # EU AI Act Art 12 traceability fields, exact
    assert row.user_id == SENTINEL_SUBJECT
    assert row.model == MODEL
    assert row.question == question
    assert row.trace_id is not None and TRACE_ID.fullmatch(row.trace_id)
    if response is not None:
        assert response.headers["x-trace-id"] == row.trace_id


def _assert_one_warning(
    caplog: pytest.LogCaptureFixture, mode: str, status: int, text: str
) -> None:
    """Exactly ONE WARNING carries the upstream text, from the arm's logger."""
    carrying = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and text[:40] in r.getMessage()
    ]
    assert len(carrying) == 1, f"{len(carrying)} WARNING+ lines carry the upstream text"
    record = carrying[0]
    assert record.levelno == logging.WARNING
    if mode == "tools" and status < 500:
        expected = (LOOP_LOGGER, f"llama-server client error HTTP {status} — upstream")
    elif mode == "tools":
        expected = (LOOP_LOGGER, f"llama-server upstream error HTTP {status} — body: ")
    else:
        expected = (
            ERRORS_LOGGER,
            f"llama-server upstream error HTTP {status} — body: ",
        )
    assert (record.name, record.getMessage()[: len(expected[1])]) == expected


def _gateway(status: int, code: str, message: str) -> dict[str, Any]:
    """The proxy's own 502/504 body: the SSE frame's error object plus ``detail``."""
    return {
        "error": {
            "message": message,
            "type": "api_error",
            "param": None,
            "code": code,
            "status": status,
        },
        "detail": message,
    }


def _upstream_502(status: int) -> dict[str, Any]:
    return _gateway(502, "upstream_error", f"llama-server returned {status}")


def _expected_reply(status: int, raw: bytes) -> tuple[int, Any]:
    """The reply the rulings give an unmatched non-2xx upstream reply."""
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    relayable = 400 <= status <= 499 or (RELAY_5XX and 500 <= status <= 599)
    shaped = isinstance(body, dict) and (
        isinstance(body.get("error"), dict) or not RELAY_REQUIRES_ERROR_OBJECT
    )
    if relayable and shaped and status not in HOP_CREDENTIAL_STATUSES:
        return status, body
    return 502, _upstream_502(status)


class TestEveryUnmatchedReply:
    """One request, one error reply, one failed row, one WARNING -- for every shape."""

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("case", list(UNMATCHED))
    async def test_reply_row_and_warning(
        self,
        client: TestClient,
        request: pytest.FixtureRequest,
        caplog: pytest.LogCaptureFixture,
        mode: str,
        case: str,
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        status, raw = UNMATCHED[case]
        fake, sid = _Upstream(status, raw), _sid(request)
        with caplog.at_level(logging.WARNING):
            response = _call(client, fake, sid)
        assert (response.status_code, response.json()) == _expected_reply(status, raw)
        assert response.headers["content-type"] == "application/json"
        assert "www-authenticate" not in response.headers
        assert len(fake.post_calls) == 1
        await _assert_failed_row(sid, status, fake.text, response)
        _assert_one_warning(caplog, mode, status, fake.text)


class TestAnchorsThatNoRulingMoves:
    """Literal expectations: the three replies no answer to Q1-Q3 changes."""

    @pytest.mark.parametrize("mode", MODES)
    async def test_400_error_object_is_relayed_with_status_400(
        self, client: TestClient, request: pytest.FixtureRequest, mode: str
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        response = _call(client, _json_upstream(400, CONTEXT_400), _sid(request))
        assert response.status_code == 400
        assert response.json() == CONTEXT_400

    @pytest.mark.parametrize("mode", MODES)
    async def test_html_reply_is_502(
        self, client: TestClient, request: pytest.FixtureRequest, mode: str
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        response = _call(client, _Upstream(404, HTML_404), _sid(request))
        assert response.status_code == 502
        assert response.json() == _upstream_502(404)

    @pytest.mark.parametrize("mode", MODES)
    async def test_redirect_is_502_never_relayed(
        self, client: TestClient, request: pytest.FixtureRequest, mode: str
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        fake = _json_upstream(302, _err(302, "moved", "redirect"))
        response = _call(client, fake, _sid(request))
        assert response.status_code == 502
        assert response.json() == _upstream_502(302)

    @pytest.mark.parametrize("mode", MODES)
    async def test_detail_is_cut_at_400_chars(
        self, client: TestClient, request: pytest.FixtureRequest, mode: str
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        sid = _sid(request)
        response = _call(client, _json_upstream(400, LONG_400), sid)
        assert response.status_code == 400
        row = await _only_row(sid)
        assert row.error_detail is not None
        assert len(row.error_detail) == len("status=400: ") + CUT


class TestInjectAuditRowIsAlwaysWritten:
    """Row-only assertions (EU AI Act Art 12): the two inject shapes that are not audited
    as failures today, whatever the client is told."""

    async def test_inject_non_json_reply_writes_one_failed_row(
        self,
        client: TestClient,
        request: pytest.FixtureRequest,
        inject_mode: None,  # noqa: F811
    ) -> None:
        fake, sid = _Upstream(404, HTML_404), _sid(request)
        with contextlib.suppress(ValueError):  # today the route raises on the body
            _call(client, fake, sid)
        await _assert_failed_row(sid, 404, fake.text, None)

    async def test_inject_json_without_error_key_is_not_a_success_row(
        self,
        client: TestClient,
        request: pytest.FixtureRequest,
        inject_mode: None,  # noqa: F811
    ) -> None:
        body = {"detail": "Internal Server Error"}
        fake, sid = _json_upstream(500, body), _sid(request)
        _call(client, fake, sid)
        await _assert_failed_row(sid, 500, fake.text, None)


class TestRetryAfter:
    """A relayed reply keeps the upstream's well-formed ``Retry-After``; a 502 has none."""

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize(
        "value", ["7", "Wed, 21 Oct 2026 07:28:00 GMT"], ids=["seconds", "http-date"]
    )
    async def test_retry_after_is_relayed_with_a_relayed_429(
        self, client: TestClient, request: pytest.FixtureRequest, mode: str, value: str
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        fake = _Upstream(429, UNMATCHED["429"][1], headers={"retry-after": value})
        response = _call(client, fake, _sid(request))
        assert response.status_code == 429
        assert response.headers["retry-after"] == value

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("value", ["soon", "-1", "7, 8", "1e3"])
    async def test_a_malformed_retry_after_is_not_relayed(
        self, client: TestClient, request: pytest.FixtureRequest, mode: str, value: str
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        fake = _Upstream(429, UNMATCHED["429"][1], headers={"retry-after": value})
        response = _call(client, fake, _sid(request))
        assert response.status_code == 429
        assert "retry-after" not in response.headers

    @pytest.mark.parametrize("mode", MODES)
    async def test_retry_after_follows_the_503_only_when_the_503_is_relayed(
        self, client: TestClient, request: pytest.FixtureRequest, mode: str
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        status, raw = UNMATCHED["503-loading"]
        fake = _Upstream(status, raw, headers={"retry-after": "7"})
        response = _call(client, fake, _sid(request))
        expected_status, _ = _expected_reply(status, raw)
        assert response.status_code == expected_status
        assert response.headers.get("retry-after") == ("7" if RELAY_5XX else None)

    @pytest.mark.parametrize("mode", MODES)
    async def test_a_relayed_reply_without_the_header_gets_none(
        self, client: TestClient, request: pytest.FixtureRequest, mode: str
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        response = _call(client, _Upstream(*UNMATCHED["429"]), _sid(request))
        assert response.status_code == 429
        assert "retry-after" not in response.headers


class TestGatewayRepliesNameNoUpstream:
    """The proxy's own 502/504: an OpenAI error object plus ``detail``, and no URL."""

    @pytest.mark.parametrize("mode", MODES)
    async def test_unreachable_is_the_502_error_object(
        self, client: TestClient, request: pytest.FixtureRequest, mode: str
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        fake = _Upstream(raises=httpx.ConnectError("All connection attempts failed"))
        response = _call(client, fake, _sid(request))
        assert response.status_code == 502
        assert response.json() == _gateway(
            502, "upstream_unreachable", "llama-server unreachable"
        )
        assert "://" not in response.text
        row = await _only_row(_sid(request))
        assert (row.status, row.failure_class) == ("failed", "upstream_unreachable")
        assert row.trace_id == response.headers["x-trace-id"]

    @pytest.mark.parametrize("mode", MODES)
    async def test_timeout_is_the_504_error_object(
        self, client: TestClient, request: pytest.FixtureRequest, mode: str
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        fake = _Upstream(raises=httpx.ReadTimeout("read timed out"))
        response = _call(client, fake, _sid(request))
        seconds = get_settings().llama_chunk_timeout
        assert response.status_code == 504
        assert response.json() == _gateway(
            504, "proxy_timeout", f"llama-server timeout after {seconds}s"
        )
        row = await _only_row(_sid(request))
        assert (row.status, row.failure_class) == ("failed", "proxy_timeout")

    @pytest.mark.parametrize("mode", MODES)
    async def test_stream_unreachable_frame_names_no_url(
        self, client: TestClient, request: pytest.FixtureRequest, mode: str
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        fake = _Upstream(raises=httpx.ConnectError("All connection attempts failed"))
        response = _call(client, fake, _sid(request), stream=True)
        assert response.status_code == 200
        frame = _parse_sse_error_frame(response.content.decode())
        assert frame == {
            "error": _gateway(502, "upstream_unreachable", "llama-server unreachable")[
                "error"
            ]
        }
        assert "://" not in response.text

    @pytest.mark.parametrize(
        ("exc", "status", "code", "message"),
        [
            (
                httpx.ConnectError("All connection attempts failed"),
                502,
                "upstream_unreachable",
                "llama-server unreachable",
            ),
            (
                httpx.ReadTimeout("read timed out"),
                504,
                "proxy_timeout",
                "llama-server timeout",
            ),
        ],
        ids=["unreachable", "timeout"],
    )
    async def test_models_route_gateway_reply_names_no_url(
        self, client: TestClient, exc: Exception, status: int, code: str, message: str
    ) -> None:
        with _patch_async_client(_Upstream(raises=exc)):
            response = client.get("/v1/models")
        assert response.status_code == status
        assert response.json() == _gateway(status, code, message)
        assert "://" not in response.text


class TestToolsStreamFallbackGetsTheSameRule:
    """The buffered fallback of the tools stream arm shares the POST helper: a 2xx
    reply carrying an ``error`` object is a failed turn there too."""

    async def test_fallback_200_error_object_is_an_error_frame_and_a_failed_row(
        self,
        client: TestClient,
        tools_mode: None,  # noqa: F811
    ) -> None:
        raw = json.dumps(ENVELOPE_ON_200).encode()
        fake = _SeqRealClient(streams=[PARSE_500], posts=[(200, raw)])
        sid = "ns-stream-fallback-200-error-object"
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": MODEL,
                    "messages": PLAIN,
                    "project": "AuditTrace",
                    "stream": True,
                },
                headers={"X-Session-Id": sid},
            )
        assert response.status_code == 200
        assert _parse_sse_error_frame(response.content.decode())["error"] == {
            "message": "llama-server returned status 200",
            "type": "api_error",
            "param": None,
            "code": "upstream_error",
            "status": 502,
        }
        await _assert_failed_row(sid, 200, raw.decode(), None)


class TestEveryReturnPointOfThePostHelper:
    """The tools arm's POST helper returns a reply at three points (first POST, the
    same-payload retry, the no-tools fallback): at each, only a completion passes."""

    @pytest.mark.parametrize(
        "parse_500s",
        [1, 1 + _TOOL_PARSE_ERROR_RETRIES],
        ids=["retry-point", "fallback-point"],
    )
    async def test_a_200_error_object_after_tool_parse_500s_is_a_failed_turn(
        self,
        client: TestClient,
        request: pytest.FixtureRequest,
        tools_mode: None,  # noqa: F811
        parse_500s: int,
    ) -> None:
        raw = json.dumps(ENVELOPE_ON_200).encode()
        fake = _SeqRealClient(posts=[PARSE_500] * parse_500s + [(200, raw)])
        sid = _sid(request)
        with _patch_async_client(fake), _patch_tool_loop_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": MODEL,
                    "messages": PLAIN,
                    "project": "AuditTrace",
                    "stream": False,
                },
                headers={"X-Session-Id": sid},
            )
        assert len(fake.post_calls) == parse_500s + 1
        assert response.status_code == 502
        assert response.json() == _upstream_502(200)
        await _assert_failed_row(sid, 200, raw.decode(), response)


def _data(obj: Any) -> bytes:
    return ("data: " + json.dumps(obj) + "\n\n").encode()


CHUNK = _data(
    {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}],
    }
)
FINISH = _data(
    {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
)
DONE = b"data: [DONE]\n\n"
STREAM_ERROR_MESSAGE = "slot crashed mid-generation"
#: llama-server's shape for a failure after the stream started: an ``error`` object in a
#: ``data:`` frame, and no ``[DONE]`` after it.
STREAM_ERROR = _data(_err(500, STREAM_ERROR_MESSAGE, "server_error"))
COMPLETION_JSON = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"}}],
}
GENERIC_STREAM_FRAME = {
    "error": {
        "message": "llama-server returned status 200",
        "type": "api_error",
        "param": None,
        "code": "upstream_error",
        "status": 502,
    }
}
#: id -> (raw upstream body, response headers). Each is an HTTP 200 reply to a streamed
#: turn that is NOT a complete stream.
NOT_A_STREAM: dict[str, tuple[bytes, dict[str, str]]] = {
    "json-error-object": (json.dumps(ENVELOPE_ON_200).encode(), {}),
    "json-completion": (json.dumps(COMPLETION_JSON).encode(), {}),
    "html": (b"<html><body>not a stream</body></html>", {}),
    "long-html": (b"<html>" + b"x" * 600 + b"</html>", {}),
    "empty": (b"", SSE),
    "sse-unparseable-then-done": (b"data: {not json\n\n" + DONE, SSE),
    "sse-non-object-then-done": (b"data: [1, 2]\n\n" + DONE, SSE),
    "sse-done-only": (DONE, SSE),
    "sse-error-first": (STREAM_ERROR, SSE),
    "sse-error-then-done": (STREAM_ERROR + DONE, SSE),
    "sse-content-then-error": (CHUNK + STREAM_ERROR, SSE),
    "sse-content-no-done": (CHUNK + FINISH, SSE),
}


def _head(raw: bytes) -> str:
    """What the audit row keeps of a reply that was no stream: its first 400 characters."""
    for stop in (STREAM_ERROR, b"data: [1, 2]\n"):  # the check stops reading there
        if stop in raw:
            raw = raw.split(stop)[0] + stop
    text = "".join(line + "\n" for line in raw.decode().splitlines())
    return text[:CUT].strip() or "(empty reply)"


def _error_frames(sse: str) -> list[Any]:
    frames = []
    for line in sse.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            with contextlib.suppress(ValueError):
                frame = json.loads(line[6:])
                if isinstance(frame, dict) and "error" in frame:
                    frames.append(frame)
    return frames


class TestStreamReplyThatIsNotAStream:
    """Both stream arms: an HTTP 200 reply that is no complete stream is a failed turn."""

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("case", list(NOT_A_STREAM))
    async def test_one_error_frame_one_failed_row_one_warning(
        self,
        client: TestClient,
        request: pytest.FixtureRequest,
        caplog: pytest.LogCaptureFixture,
        mode: str,
        case: str,
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        raw, headers = NOT_A_STREAM[case]
        fake, sid = _Upstream(200, raw, headers=headers), _sid(request)
        with caplog.at_level(logging.WARNING):
            response = _call(client, fake, sid, stream=True)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = response.content.decode()
        assert _error_frames(body) == [GENERIC_STREAM_FRAME]
        assert body.endswith("data: [DONE]\n\n")
        if case.startswith("sse-error") or case == "sse-content-then-error":
            assert STREAM_ERROR_MESSAGE not in body
        row = await _only_row(sid)
        assert (row.status, row.failure_class) == ("failed", "upstream_error")
        assert row.answer == ""
        assert row.error_detail == f"status=200: {_head(raw)}"
        assert (row.user_id, row.model, row.question) == (
            SENTINEL_SUBJECT,
            MODEL,
            PROMPT,
        )
        assert row.trace_id == response.headers["x-trace-id"]
        lines = [
            r.getMessage()
            for r in caplog.records
            if r.levelno >= logging.WARNING and r.name == STREAM_LOGGER
        ]
        assert len(lines) == 1
        assert lines[0].startswith(
            "llama-server stream reply HTTP 200 is not a complete stream ("
        )

    @pytest.mark.parametrize("mode", MODES)
    async def test_a_complete_stream_is_still_a_success(
        self, client: TestClient, request: pytest.FixtureRequest, mode: str
    ) -> None:
        """GREEN before and after: chunks, then ``[DONE]``."""
        request.getfixturevalue(f"{mode}_mode")
        fake, sid = _Upstream(200, CHUNK + FINISH + DONE, headers=SSE), _sid(request)
        response = _call(client, fake, sid, stream=True)
        body = response.content.decode()
        assert response.status_code == 200
        assert _error_frames(body) == []
        assert body.endswith("data: [DONE]\n\n")
        row = await _only_row(sid)
        assert (row.status, row.failure_class, row.answer) == ("success", None, "Hel")


class TestPinsThatDoNotMove:
    """GREEN before and after: what this change must leave alone."""

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize(
        ("status", "body", "messages", "code"),
        [
            (
                400,
                LLAMA_UNSUPPORTED_BODY,
                UPLOAD_TO_PROVIDER_MESSAGES,
                "unsupported_content_type",
            ),
            (500, LLAMA_IMAGE_500_BODY, IMAGE_MESSAGES, "image_input_unsupported"),
        ],
        ids=["file-part", "image-part"],
    )
    async def test_recognised_rejection_keeps_its_400_its_row_and_its_warning(
        self,
        client: TestClient,
        request: pytest.FixtureRequest,
        caplog: pytest.LogCaptureFixture,
        mode: str,
        status: int,
        body: dict[str, Any],
        messages: list[dict[str, Any]],
        code: str,
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        fake, sid = _json_upstream(status, body), _sid(request)
        with caplog.at_level(logging.WARNING):
            response = _call(client, fake, sid, messages=messages)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == code
        row = await _only_row(sid)
        assert (row.status, row.failure_class) == ("failed", "upstream_error")
        assert row.error_detail == f"status={status}: {fake.text[:CUT]}"
        assert row.trace_id == response.headers["x-trace-id"]
        if mode == "tools":
            _assert_one_warning(caplog, mode, status, fake.text)
        else:  # the inject arm keeps the classification line for a recognised reply
            lines = [
                r.getMessage()
                for r in caplog.records
                if r.levelno >= logging.WARNING and r.name == ERRORS_LOGGER
            ]
            assert len(lines) == 1
            assert lines[0].startswith(
                f"llama-server rejected a content part: HTTP {status}, upstream body: "
            )

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize(
        ("status", "body"), [(400, CONTEXT_400), (500, SERVER_500)], ids=["400", "500"]
    )
    async def test_stream_arms_keep_http_200_and_the_generic_frame(
        self,
        client: TestClient,
        request: pytest.FixtureRequest,
        mode: str,
        status: int,
        body: dict[str, Any],
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        fake, sid = _json_upstream(status, body), _sid(request)
        response = _call(client, fake, sid, stream=True)
        assert response.status_code == 200
        assert _parse_sse_error_frame(response.content.decode())["error"] == {
            "message": f"llama-server returned status {status}",
            "type": "api_error",
            "param": None,
            "code": "upstream_error",
            "status": 502,
        }
        await _assert_failed_row(sid, status, fake.text, None)

    async def test_inject_request_is_forwarded_unmodified(
        self,
        client: TestClient,
        request: pytest.FixtureRequest,
        inject_mode: None,  # noqa: F811
    ) -> None:
        """ADR-024: a response-side change only."""
        fake = _json_upstream(400, CONTEXT_400)
        _call(client, fake, _sid(request), messages=UPLOAD_TO_PROVIDER_MESSAGES)
        sent = fake.post_calls[0]["json"]["messages"]
        user_turn = [m for m in sent if m.get("role") == "user"][-1]
        assert user_turn["content"] == UPLOAD_TO_PROVIDER_MESSAGES[0]["content"]
