"""Tests for chat completions proxy — memory augmentation + llama-server forward (ADR-018, ADR-024).

The proxy is dict pass-through (ADR-024): every inbound field reaches
llama-server unchanged except ``messages`` which gets memory injected
into the system entry. These tests assert that pass-through holds for
``tools``, ``tool_call_id``, and streamed ``delta.tool_calls`` — the
regression that triggered ADR-024.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from audittrace.identity import SENTINEL_SUBJECT
from audittrace.routes.chat import (
    _apply_thinking_mode,
    _compute_session_id,
    _resolve_project,
    _resolve_thinking,
)
from audittrace.services.context_builder import PROFILE_SECTION_HEADER

# ──────────────────────────── httpx.AsyncClient fakes ────────────────────────
#
# The route uses ``async with httpx.AsyncClient(...) as client`` for both
# streaming and non-streaming branches. We patch the class on the route module
# and have its constructor return a stateful fake that records inbound payloads
# so assertions can verify pass-through behaviour.


class _FakeAsyncClient:
    """Async-context-manager fake mimicking the slice of httpx.AsyncClient
    the chat route uses (post, get, stream)."""

    def __init__(
        self,
        *,
        post_json: dict | None = None,
        stream_lines: list[str] | None = None,
        get_json: dict | None = None,
        post_exc: Exception | None = None,
    ) -> None:
        self._post_json = post_json
        self._stream_lines = stream_lines or []
        self._get_json = get_json
        self._post_exc = post_exc
        self.last_post_json: dict | None = None
        self.last_stream_json: dict | None = None
        self.last_post_url: str | None = None
        # Full call history — useful when a single request fans out to multiple
        # POSTs (LLM upstream + Langfuse ingestion).
        self.post_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, **kwargs):
        self.last_post_url = url
        self.last_post_json = json
        self.post_calls.append({"url": url, "json": json, "kwargs": kwargs})
        if self._post_exc is not None:
            raise self._post_exc
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = self._post_json
        return resp

    async def get(self, url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = self._get_json
        return resp

    def stream(self, method, url, json=None, **kwargs):
        self.last_stream_json = json
        return _FakeStreamCtx(self._stream_lines)


class _FakeStreamCtx:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self):
        return _FakeStreamResponse(self._lines)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeStreamResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def _patch_async_client(fake: _FakeAsyncClient):
    """Patch httpx.AsyncClient so its constructor returns ``fake``."""
    return patch(
        "audittrace.routes.chat.httpx.AsyncClient",
        return_value=fake,
    )


class _SequencedClient(_FakeAsyncClient):
    """``_FakeAsyncClient`` subclass that returns a different JSON body on
    each successive POST. Used by tools-mode integration tests where one
    chat request fans out to N llama-server POSTs inside the tool-call
    loop."""

    def __init__(self, responses: list[dict]):
        super().__init__()
        self._responses = responses
        self._i = 0

    async def post(self, url, json=None, **kwargs):
        self.last_post_url = url
        self.last_post_json = json
        self.post_calls.append({"url": url, "json": json, "kwargs": kwargs})
        body = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = body
        return resp


def _patch_tool_loop_client(fake):
    """Patch the httpx.AsyncClient used INSIDE the memory tool-call loop.

    The loop imports httpx at module level in
    ``audittrace.routes._memory_tool_loop`` so patching
    ``routes.chat.httpx`` is not enough in tools mode — we also have to
    patch the loop module's own httpx reference.
    """
    return patch(
        "audittrace.routes._memory_tool_loop.httpx.AsyncClient",
        return_value=fake,
    )


# ── Streaming fakes for the tools-mode per-turn streaming loop (#299) ────────
#
# The tools-mode streaming path (``_stream_memory_tool_loop``) opens one
# ``client.stream(...)`` per loop turn and forwards content deltas live. These
# fakes return a DIFFERENT set of SSE lines per ``.stream()`` call (one per
# turn) and, on the buffered tool-parse-500 fallback, a ``.post()`` body.


def _sse_line(obj: dict) -> str:
    """Encode one OpenAI ``chat.completion.chunk`` dict as an SSE data line."""
    import json as _j

    return "data: " + _j.dumps(obj)


def _sse_content_lines(pieces: list[str], model: str = "qwen3.5-35b") -> list[str]:
    """SSE data lines streaming *pieces* of content then finish_reason=stop."""
    lines = [
        _sse_line(
            {
                "id": "cmpl-s",
                "created": 1,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": p}}],
            }
        )
        for p in pieces
    ]
    lines.append(
        _sse_line(
            {
                "id": "cmpl-s",
                "created": 1,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 8,
                    "total_tokens": 28,
                },
            }
        )
    )
    lines.append("data: [DONE]")
    return lines


def _sse_tool_call_lines(
    tool_name: str, args_json: str, call_id: str = "call_1", model: str = "qwen3.5-35b"
) -> list[str]:
    """SSE data lines for a streamed tool_calls turn (think text + tool_call)."""
    return [
        _sse_line(
            {
                "id": "cmpl-s",
                "created": 1,
                "model": model,
                "choices": [
                    {"index": 0, "delta": {"content": "<think>recall</think>"}}
                ],
            }
        ),
        _sse_line(
            {
                "id": "cmpl-s",
                "created": 1,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": args_json,
                                    },
                                }
                            ]
                        },
                    }
                ],
            }
        ),
        _sse_line(
            {
                "id": "cmpl-s",
                "created": 1,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
        ),
        "data: [DONE]",
    ]


class _SeqStreamResponse:
    def __init__(self, lines, status=200, body=None):
        self._lines = lines
        self.status_code = status
        self._body = body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "upstream error", request=MagicMock(), response=self
            )
        return None

    async def aread(self):
        return b""

    def json(self):
        return self._body

    @property
    def text(self):
        import json as _j

        return _j.dumps(self._body)

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _SeqStreamCtx:
    def __init__(self, lines, status=200, body=None):
        self._lines = lines
        self._status = status
        self._body = body

    async def __aenter__(self):
        return _SeqStreamResponse(self._lines, self._status, self._body)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SequencedStreamClient(_FakeAsyncClient):
    """``_FakeAsyncClient`` whose ``.stream()`` returns a different SSE
    line-set per call (one per loop turn), plus ``.post()`` for the buffered
    tool-parse-500 fallback. ``stream_status``/``stream_bodies`` (parallel to
    ``stream_turns``) drive error/fallback turns."""

    def __init__(
        self,
        stream_turns: list[list[str]],
        post_responses: list[dict] | None = None,
        stream_status: list[int] | None = None,
        stream_bodies: list[dict] | None = None,
    ):
        super().__init__()
        self._turns = stream_turns
        self._si = 0
        self._post_responses = post_responses or []
        self._pi = 0
        self._stream_status = stream_status or []
        self._stream_bodies = stream_bodies or []
        self.stream_calls: list[dict] = []

    def stream(self, method, url, json=None, **kwargs):
        self.last_stream_json = json
        self.stream_calls.append({"url": url, "json": json})
        idx = min(self._si, len(self._turns) - 1)
        lines = self._turns[idx]
        status = self._stream_status[idx] if idx < len(self._stream_status) else 200
        body = self._stream_bodies[idx] if idx < len(self._stream_bodies) else None
        self._si += 1
        return _SeqStreamCtx(lines, status, body)

    async def post(self, url, json=None, **kwargs):
        self.last_post_url = url
        self.last_post_json = json
        self.post_calls.append({"url": url, "json": json, "kwargs": kwargs})
        body = self._post_responses[min(self._pi, len(self._post_responses) - 1)]
        self._pi += 1
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = body
        return resp


def _parse_sse_deltas(raw: str) -> list[dict]:
    """Parse an SSE body into the list of decoded ``data:`` JSON chunks
    (skipping ``[DONE]`` and keep-alive comment frames)."""
    import json as _j

    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            continue
        try:
            out.append(_j.loads(payload))
        except _j.JSONDecodeError:
            continue
    return out


class _RaisingStreamCtx:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RaisingStreamClient(_FakeAsyncClient):
    """``.stream()`` raises *exc* when the streaming context is entered —
    models llama-server being unreachable / timing out / blowing up mid-open."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    def stream(self, method, url, json=None, **kwargs):
        return _RaisingStreamCtx(self._exc)


# ─────────────────────────────── Session id ─────────────────────────────────


class TestSessionId:
    """Deterministic session_id grouping (port from ADR-014).

    Phase 2 (DESIGN §15): ``user_id`` is mixed into the hash so two users
    with identical ``(source, date, first_message)`` never produce the
    same session id.
    """

    def test_session_id_is_stable_for_same_input(self):
        a = _compute_session_id("opencode", "Hello world", "user-1")
        b = _compute_session_id("opencode", "Hello world", "user-1")
        assert a == b

    def test_session_id_changes_with_source(self):
        a = _compute_session_id("opencode", "Hello world", "user-1")
        b = _compute_session_id("continue", "Hello world", "user-1")
        assert a != b

    def test_session_id_changes_with_first_message(self):
        a = _compute_session_id("opencode", "Hello world", "user-1")
        b = _compute_session_id("opencode", "Different question", "user-1")
        assert a != b

    def test_session_id_changes_with_user_id(self):
        """Phase 2 contract: same source+message under different users
        must produce distinct session ids."""
        a = _compute_session_id("opencode", "Hello world", "user-alice")
        b = _compute_session_id("opencode", "Hello world", "user-bob")
        assert a != b

    def test_session_id_includes_source_and_date(self):
        sid = _compute_session_id("opencode", "Hello", "user-1")
        today = date.today().isoformat()
        assert sid.startswith(f"opencode-{today}-")
        assert len(sid.split("-")[-1]) == 64  # full sha256 hex (256 bits, backlog #04)


class _FakeRequest:
    """Minimal stand-in for fastapi.Request — only ``.headers.get`` is used."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


class TestResolveProject:
    """ADR-029 project tag precedence: header → metadata.project → body.project → default."""

    def test_header_wins_over_metadata_and_body(self):
        req = _FakeRequest({"x-project": "FromHeader"})
        payload = {"project": "FromBody", "metadata": {"project": "FromMetadata"}}
        assert _resolve_project(req, payload) == "FromHeader"

    def test_metadata_wins_when_no_header(self):
        req = _FakeRequest()
        payload = {"project": "FromBody", "metadata": {"project": "FromMetadata"}}
        assert _resolve_project(req, payload) == "FromMetadata"

    def test_body_project_used_when_only_body_set(self):
        req = _FakeRequest()
        assert _resolve_project(req, {"project": "FromBody"}) == "FromBody"

    def test_default_returned_when_nothing_set(self):
        req = _FakeRequest()
        assert _resolve_project(req, {}) == "default"

    def test_whitespace_trimmed_from_header(self):
        req = _FakeRequest({"x-project": "   Spaced  "})
        assert _resolve_project(req, {}) == "Spaced"

    def test_empty_header_falls_through_to_next_tier(self):
        req = _FakeRequest({"x-project": "   "})
        assert _resolve_project(req, {"project": "FromBody"}) == "FromBody"

    def test_non_string_metadata_project_ignored(self):
        req = _FakeRequest()
        payload = {"metadata": {"project": 42}, "project": "FromBody"}
        assert _resolve_project(req, payload) == "FromBody"


# ─────────────────────────── Non-streaming proxy ────────────────────────────


def _ok_chat_response(answer: str = "Hello!") -> dict:
    return {
        "id": "cmpl-test",
        "object": "chat.completion",
        "model": "qwen3.5-35b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class TestChatProxy:
    """Test the /v1/chat/completions endpoint with memory augmentation."""

    def test_chat_proxy_augments_system_message(self, client):
        fake = _FakeAsyncClient(post_json=_ok_chat_response())
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [
                        {"role": "system", "content": "You are a helpful assistant."},
                        {"role": "user", "content": "What is KV cache?"},
                    ],
                },
            )

        assert response.status_code == 200
        assert fake.last_post_json is not None
        system_msg = fake.last_post_json["messages"][0]
        assert system_msg["role"] == "system"
        assert (
            PROFILE_SECTION_HEADER in system_msg["content"]
        )  # memory context injected
        assert "You are a helpful assistant." in system_msg["content"]

    def test_chat_proxy_creates_system_message_when_missing(self, client):
        fake = _FakeAsyncClient(post_json=_ok_chat_response("Hi!"))
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 200
        msgs = fake.last_post_json["messages"]
        assert msgs[0]["role"] == "system"
        assert PROFILE_SECTION_HEADER in msgs[0]["content"]

    def test_chat_proxy_passes_through_openai_fields(self, client):
        """temperature, top_p, max_tokens forwarded to llama-server."""
        fake = _FakeAsyncClient(post_json=_ok_chat_response("ok"))
        with _patch_async_client(fake):
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "temperature": 0.3,
                    "top_p": 0.9,
                    "max_tokens": 500,
                },
            )

        body = fake.last_post_json
        assert body["temperature"] == 0.3
        assert body["top_p"] == 0.9
        assert body["max_tokens"] == 500

    def test_chat_proxy_uses_context_query_over_last_message(self, client):
        fake = _FakeAsyncClient(post_json=_ok_chat_response("ok"))
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "context_query": "KV cache compression",
                },
            )

        assert response.status_code == 200

    def test_chat_proxy_returns_error_when_llama_unreachable(self, client):
        import httpx

        fake = _FakeAsyncClient(post_exc=httpx.ConnectError("Connection refused"))
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        assert response.status_code == 502
        detail = response.json()["detail"].lower()
        assert "llama-server" in detail or "unreachable" in detail

    # ───────────── ADR-024 regression: tool calls pass-through ─────────────

    def test_chat_proxy_forwards_tools_field(self, client):
        """Inbound 'tools' must reach llama-server unchanged. Pre-ADR-024 the
        Pydantic schema silently dropped this field, breaking tool calling."""
        fake = _FakeAsyncClient(post_json=_ok_chat_response("ok"))
        tools_payload = [
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "Run a shell command",
                    "parameters": {
                        "type": "object",
                        "properties": {"cmd": {"type": "string"}},
                        "required": ["cmd"],
                    },
                },
            }
        ]
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "loc count?"}],
                    "tools": tools_payload,
                    "tool_choice": "auto",
                },
            )

        assert response.status_code == 200
        forwarded = fake.last_post_json
        assert forwarded["tools"] == tools_payload
        assert forwarded["tool_choice"] == "auto"

    def test_chat_proxy_preserves_tool_call_id_on_followup(self, client):
        """A follow-up turn carrying tool_result must preserve role='tool'
        and tool_call_id so llama-server can correlate the response."""
        fake = _FakeAsyncClient(post_json=_ok_chat_response("12345"))
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [
                        {"role": "user", "content": "loc count?"},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_abc",
                                    "type": "function",
                                    "function": {
                                        "name": "bash",
                                        "arguments": '{"cmd":"wc -l"}',
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "call_abc",
                            "name": "bash",
                            "content": "12345",
                        },
                    ],
                },
            )

        assert response.status_code == 200
        forwarded = fake.last_post_json["messages"]
        # System message inserted at 0; original messages start at index 1
        assistant_turn = next(m for m in forwarded if m.get("role") == "assistant")
        assert assistant_turn["tool_calls"][0]["id"] == "call_abc"
        assert (
            assistant_turn["tool_calls"][0]["function"]["arguments"]
            == '{"cmd":"wc -l"}'
        )
        tool_turn = next(m for m in forwarded if m.get("role") == "tool")
        assert tool_turn["tool_call_id"] == "call_abc"
        assert tool_turn["name"] == "bash"
        assert tool_turn["content"] == "12345"

    def test_models_endpoint_proxies_llama(self, client):
        models_response = {
            "object": "list",
            "data": [{"id": "qwen3.5", "object": "model", "owned_by": "llamacpp"}],
        }
        fake = _FakeAsyncClient(get_json=models_response)
        with _patch_async_client(fake):
            response = client.get("/v1/models")

        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "list"
        assert body["data"][0]["id"] == "qwen3.5"

    # ──────────────────────────── Streaming branch ─────────────────────────

    def test_chat_proxy_streams_when_stream_true(self, client):
        """When stream=true, response must be SSE chunks (not JSON) and include
        a synthetic usage chunk derived from llama.cpp timings before [DONE]."""
        stream_lines = [
            'data: {"choices":[{"delta":{"content":"Hel"}}]}',
            "",
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            "",
            'data: {"choices":[{"finish_reason":"stop","delta":{}}],'
            '"model":"qwen3.5",'
            '"timings":{"cache_n":700,"prompt_n":260,"predicted_n":2}}',
            "",
            "data: [DONE]",
            "",
        ]
        fake = _FakeAsyncClient(stream_lines=stream_lines)
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert "event-stream" in response.headers.get("content-type", "")
        body = response.content.decode()
        assert "Hel" in body
        assert "lo" in body
        assert "[DONE]" in body

        import json as _json

        usage_lines = [
            line
            for line in body.split("\n")
            if '"usage"' in line and "[DONE]" not in line
        ]
        assert usage_lines, "synthetic usage chunk missing from stream"
        usage_chunk = _json.loads(usage_lines[-1].removeprefix("data: "))
        assert usage_chunk["usage"]["prompt_tokens"] == 960  # cache_n + prompt_n
        assert usage_chunk["usage"]["completion_tokens"] == 2
        assert usage_chunk["usage"]["total_tokens"] == 962
        assert body.rfind("[DONE]") > body.rfind('"usage"')

    async def test_chat_proxy_streams_tool_call_deltas(self, client):
        """ADR-024 regression: streamed delta.tool_calls must be (a) forwarded
        byte-equal so OpenCode sees them, (b) accumulated by index so the
        persisted answer reflects the tool call, not an empty string."""
        from audittrace.db.models import InteractionRecord
        from audittrace.dependencies import get_postgres_factory

        stream_lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_xyz",'
            '"type":"function","function":{"name":"bash","arguments":"{\\"cmd"}}]}}]}',
            "",
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":"\\":\\"wc -l\\"}"}}]}}]}',
            "",
            'data: {"choices":[{"finish_reason":"tool_calls","delta":{}}],'
            '"model":"qwen3.5",'
            '"timings":{"cache_n":100,"prompt_n":50,"predicted_n":12}}',
            "",
            "data: [DONE]",
            "",
        ]
        fake = _FakeAsyncClient(stream_lines=stream_lines)
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5",
                    "messages": [{"role": "user", "content": "loc count?"}],
                    "stream": True,
                    "project": "AuditTrace",
                },
            )

        assert response.status_code == 200
        body = response.content.decode()
        # (a) byte-equal forward of tool_call deltas
        assert '"tool_calls"' in body
        assert "call_xyz" in body
        assert "bash" in body
        # And the synthetic usage chunk + DONE still come last
        assert "[DONE]" in body

        # (b) persisted answer includes the rendered tool_call line so the
        # audit trail is meaningful even when the model emits zero text content
        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            rows = (await db.execute(select(InteractionRecord))).scalars().all()
        assert rows, "interaction was not persisted"
        latest = rows[-1]
        assert "[tool_call]" in latest.answer
        assert "bash" in latest.answer
        assert "wc -l" in latest.answer

    async def test_chat_proxy_persists_interaction(self, client):
        """A successful chat completion writes a row to interactions."""
        from audittrace.db.models import InteractionRecord
        from audittrace.dependencies import get_postgres_factory

        fake = _FakeAsyncClient(
            post_json={
                "id": "cmpl-test",
                "model": "qwen3.5-35b",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hi there!"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                },
            }
        )
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "Persist this"}],
                    "project": "AuditTrace",
                },
            )

        assert response.status_code == 200
        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            rows = (await db.execute(select(InteractionRecord))).scalars().all()
        assert len(rows) >= 1
        latest = rows[-1]
        assert latest.project == "AuditTrace"
        assert latest.question
        assert latest.answer == "Hi there!"
        assert latest.prompt_tokens == 12
        assert latest.completion_tokens == 4
        assert latest.session_id
        assert latest.model == "qwen3.5-35b"
        # Phase 2: every audit row carries the sentinel user_id in bypass mode.
        assert latest.user_id == SENTINEL_SUBJECT

    def test_chat_proxy_with_project(self, client):
        fake = _FakeAsyncClient(post_json=_ok_chat_response("ok"))
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "What about KV cache?"}],
                    "project": "AuditTrace",
                },
            )

        assert response.status_code == 200

    async def test_chat_proxy_x_project_header_wins_over_body(self, client):
        """ADR-029: X-Project header is authoritative over body.project."""
        from audittrace.db.models import InteractionRecord
        from audittrace.dependencies import get_postgres_factory

        fake = _FakeAsyncClient(post_json=_ok_chat_response("ok"))
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                headers={"X-Project": "HeaderProject"},
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "project": "BodyProject",
                },
            )
        assert response.status_code == 200
        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            row = (
                (
                    await db.execute(
                        select(InteractionRecord).order_by(InteractionRecord.id.desc())
                    )
                )
                .scalars()
                .first()
            )
        assert row is not None
        assert row.project == "HeaderProject"

    async def test_chat_proxy_metadata_project_used_when_no_header(self, client):
        """ADR-029: body.metadata.project is the second-tier source."""
        from audittrace.db.models import InteractionRecord
        from audittrace.dependencies import get_postgres_factory

        fake = _FakeAsyncClient(post_json=_ok_chat_response("ok"))
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "metadata": {"project": "FromMetadata"},
                },
            )
        assert response.status_code == 200
        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            row = (
                (
                    await db.execute(
                        select(InteractionRecord).order_by(InteractionRecord.id.desc())
                    )
                )
                .scalars()
                .first()
            )
        assert row is not None
        assert row.project == "FromMetadata"

    async def test_chat_proxy_defaults_project_when_none_provided(self, client):
        """ADR-029: project defaults to "default" (not "unknown") when caller omits it."""
        from audittrace.db.models import InteractionRecord
        from audittrace.dependencies import get_postgres_factory

        fake = _FakeAsyncClient(post_json=_ok_chat_response("ok"))
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert response.status_code == 200
        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            row = (
                (
                    await db.execute(
                        select(InteractionRecord).order_by(InteractionRecord.id.desc())
                    )
                )
                .scalars()
                .first()
            )
        assert row is not None
        assert row.project == "default"

    # ───────── Edge cases & error paths (chat.py coverage) ─────────────────

    def test_chat_proxy_rejects_invalid_json(self, client):
        """Bad JSON body returns 400 (not 500)."""
        response = client.post(
            "/v1/chat/completions",
            content=b"not-a-json-document",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert "Invalid JSON" in response.json()["detail"]

    def test_chat_proxy_rejects_missing_messages(self, client):
        """Missing messages list returns 422."""
        response = client.post(
            "/v1/chat/completions",
            json={"model": "qwen3.5-35b"},
        )
        assert response.status_code == 422

    async def test_chat_proxy_renders_tool_calls_in_non_streaming(self, client):
        """Non-streaming branch must render tool_calls into the persisted answer."""
        from audittrace.db.models import InteractionRecord
        from audittrace.dependencies import get_postgres_factory

        fake = _FakeAsyncClient(
            post_json={
                "id": "cmpl-test",
                "model": "qwen3.5-35b",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_xyz",
                                    "type": "function",
                                    "function": {
                                        "name": "bash",
                                        "arguments": '{"cmd":"wc -l"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
        )
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "loc count?"}],
                    "project": "AuditTrace",
                },
            )

        assert response.status_code == 200
        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            rows = (await db.execute(select(InteractionRecord))).scalars().all()
        latest = rows[-1]
        assert "[tool_call]" in latest.answer
        assert "bash" in latest.answer
        assert "wc -l" in latest.answer

    def test_chat_proxy_streams_error_on_upstream_connect_failure(self, client):
        """Streaming branch yields a JSON error chunk + DONE on upstream failure."""
        import httpx as _httpx

        class _ExplodingClient(_FakeAsyncClient):
            def stream(self, method, url, json=None, **kwargs):
                raise _httpx.ConnectError("upstream down")

        fake = _ExplodingClient()
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        body = response.content.decode()
        assert "llama-server unreachable" in body
        assert "[DONE]" in body

    def test_chat_proxy_records_langfuse_output_when_configured(
        self, client, monkeypatch
    ):
        """When Langfuse is configured, _record_langfuse_output POSTs to the
        ingestion endpoint with the captured trace_id + answer.

        After making the helper async (ADR-024 follow-up), the ingestion call
        flows through the same patched httpx.AsyncClient as the LLM call, so
        the test inspects the fake's post_calls history for the ingestion URL.
        """
        from audittrace import config as config_mod

        config_mod.get_settings.cache_clear()
        monkeypatch.setenv("AUDITTRACE_LANGFUSE_HOST", "http://lf.test")
        monkeypatch.setenv("AUDITTRACE_LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("AUDITTRACE_LANGFUSE_SECRET_KEY", "sk-test")

        fake = _FakeAsyncClient(post_json=_ok_chat_response("hello"))

        with (
            _patch_async_client(fake),
            patch(
                "audittrace.routes.chat._lf_get_client",
                return_value=MagicMock(get_current_trace_id=lambda: "trace-test-1"),
            ),
            patch("audittrace.routes.chat._LANGFUSE_AVAILABLE", True),
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        config_mod.get_settings.cache_clear()

        assert response.status_code == 200
        ingestion_calls = [
            c for c in fake.post_calls if "/api/public/ingestion" in c["url"]
        ]
        assert ingestion_calls, "Langfuse ingestion endpoint was not called"
        call = ingestion_calls[0]
        assert call["url"].startswith("http://lf.test")
        body = call["json"]["batch"][0]["body"]
        assert body["id"] == "trace-test-1"
        assert body["output"] == "hello"
        assert body["metadata"]["finish_reason"] == "stop"
        # Phase 2.1: input + userId must be present so Langfuse UI stops
        # rendering the trace as "undefined" (EU AI Act Art. 12 recon).
        assert body.get("input") == [{"role": "user", "content": "Hi"}]
        assert body.get("userId"), "userId must be set on the trace body"

    def test_chat_proxy_langfuse_truncates_long_message_history(
        self, client, monkeypatch
    ):
        """_LANGFUSE_INPUT_MESSAGE_CAP caps the ingested input at 50 turns.

        A rogue or adversarial caller sending a million-message conversation
        must not push that full transcript into Langfuse — keeping the cap
        bounded protects the observability pipeline from payload bloat.
        """
        from audittrace import config as config_mod

        config_mod.get_settings.cache_clear()
        monkeypatch.setenv("AUDITTRACE_LANGFUSE_HOST", "http://lf.test")
        monkeypatch.setenv("AUDITTRACE_LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("AUDITTRACE_LANGFUSE_SECRET_KEY", "sk-test")

        long_messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
            for i in range(120)
        ]
        fake = _FakeAsyncClient(post_json=_ok_chat_response("ok"))

        with (
            _patch_async_client(fake),
            patch(
                "audittrace.routes.chat._lf_get_client",
                return_value=MagicMock(get_current_trace_id=lambda: "trace-cap-1"),
            ),
            patch("audittrace.routes.chat._LANGFUSE_AVAILABLE", True),
        ):
            response = client.post(
                "/v1/chat/completions",
                json={"model": "qwen3.5-35b", "messages": long_messages},
            )

        config_mod.get_settings.cache_clear()

        assert response.status_code == 200
        ingestion_calls = [
            c for c in fake.post_calls if "/api/public/ingestion" in c["url"]
        ]
        assert ingestion_calls
        body = ingestion_calls[0]["json"]["batch"][0]["body"]
        captured = body["input"]
        assert len(captured) == 50
        # The cap keeps the HEAD of the conversation (oldest turns) —
        # consistent with how Langfuse renders "the first N messages the
        # model saw" for reconstructibility, not the most-recent window.
        assert captured[0] == {"role": "user", "content": "turn 0"}
        assert captured[-1]["content"] == "turn 49"

    def test_chat_proxy_streams_error_on_upstream_status(self, client):
        """Streaming branch yields error chunk + DONE on upstream HTTP 4xx/5xx."""
        import httpx as _httpx

        class _StatusErrorClient(_FakeAsyncClient):
            def stream(self, method, url, json=None, **kwargs):
                resp = MagicMock()
                resp.status_code = 503

                class _Ctx:
                    async def __aenter__(self):
                        bad = _FakeStreamResponse([])
                        bad.raise_for_status = MagicMock(  # type: ignore[method-assign]
                            side_effect=_httpx.HTTPStatusError(
                                "boom", request=MagicMock(), response=resp
                            )
                        )
                        return bad

                    async def __aexit__(self, *a):
                        return False

                return _Ctx()

        fake = _StatusErrorClient()
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        body = response.content.decode()
        assert "503" in body
        assert "[DONE]" in body

    def test_chat_proxy_returns_504_on_upstream_timeout(self, client):
        """Non-streaming branch maps TimeoutException to 504."""
        import httpx as _httpx

        fake = _FakeAsyncClient(post_exc=_httpx.TimeoutException("slow"))
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )
        assert response.status_code == 504

    def test_extract_query_handles_multipart_content(self, client):
        """_extract_query should join text parts when content is a list."""
        from audittrace.routes.chat import _extract_query

        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is "},
                        {"type": "image_url", "image_url": {"url": "..."}},
                        {"type": "text", "text": "this?"},
                    ],
                }
            ]
        }
        assert _extract_query(payload) == "what is  this?"

    def test_models_endpoint_returns_502_on_unreachable(self, client):
        """list_models surfaces ConnectError as 502 (not 500)."""
        import httpx as _httpx

        class _ExplodingGetClient(_FakeAsyncClient):
            async def get(self, url, **kwargs):
                raise _httpx.ConnectError("nope")

        fake = _ExplodingGetClient()
        with _patch_async_client(fake):
            response = client.get("/v1/models")
        assert response.status_code == 502


# ──────────────────── ADR-025 — memory_mode=tools integration ───────────────
# End-to-end tests that flip AUDITTRACE_MEMORY_MODE=tools and exercise the
# full chat_completions handler via the TestClient fixture. These prove:
#
#   - Inject mode (default) is unchanged — covered by the 20+ tests above.
#   - Tools mode routes through the memory tool-call loop, returns a
#     final body (or synthesized SSE), persists the interaction with
#     user_id, and flushes ToolCall audit rows after the interaction lands.


def _tools_mode_response_text(text: str = "done") -> dict:
    return {
        "id": "cmpl-test",
        "object": "chat.completion",
        "model": "qwen3.5-35b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
    }


def _tools_mode_tool_call_response(
    tool_name: str, args_json: str, call_id: str = "call_1"
) -> dict:
    return {
        "id": "cmpl-test",
        "object": "chat.completion",
        "model": "qwen3.5-35b",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": args_json,
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class TestToolsModeIntegration:
    """Full chat_completions flow with AUDITTRACE_MEMORY_MODE=tools."""

    def _flip_to_tools_mode(self, monkeypatch):
        """Flip the global settings to tools mode for the duration of one
        test. The @lru_cache on get_settings means we have to clear it
        before and after."""
        from audittrace import config as config_mod

        config_mod.get_settings.cache_clear()
        monkeypatch.setenv("AUDITTRACE_MEMORY_MODE", "tools")
        yield
        config_mod.get_settings.cache_clear()

    @pytest.fixture
    def _tools_mode(self, monkeypatch):
        """Flip AUDITTRACE_MEMORY_MODE=tools for the duration of a test."""
        yield from self._flip_to_tools_mode(monkeypatch)

    async def test_tools_mode_trivial_prompt_single_llama_call(
        self, client, _tools_mode
    ):
        """A prompt that doesn't trigger any tool_calls results in exactly
        one POST to llama-server and no ToolCall audit rows."""
        from audittrace.db.models import InteractionRecord, ToolCall
        from audittrace.dependencies import get_postgres_factory

        fake = _SequencedClient([_tools_mode_response_text("Just text.")])
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hello"}],
                    "project": "AuditTrace",
                },
            )

        assert response.status_code == 200
        # Exactly one POST inside the loop
        assert len(fake.post_calls) == 1
        # The first POST carries the ambient context in the system message
        system_msg = fake.post_calls[0]["json"]["messages"][0]
        assert system_msg["role"] == "system"
        assert PROFILE_SECTION_HEADER in system_msg["content"]
        # And the memory tools are advertised in the tools array
        tools_arr = fake.post_calls[0]["json"].get("tools") or []
        tool_names = {t["function"]["name"] for t in tools_arr}
        assert {
            "recall_decisions",
            "recall_skills",
            "recall_recent_sessions",
            "recall_semantic",
        }.issubset(tool_names)

        # Interaction persisted with user_id, no ToolCall rows (trivial).
        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            interactions = (await db.execute(select(InteractionRecord))).scalars().all()
            tool_calls_rows = (await db.execute(select(ToolCall))).scalars().all()
        assert len(interactions) >= 1
        assert interactions[-1].user_id == SENTINEL_SUBJECT
        assert tool_calls_rows == []

    async def test_tools_mode_memory_prompt_fires_loop_and_audits(
        self, client, _tools_mode
    ):
        """Prompt that triggers recall_decisions: TWO POSTs, final text
        answer, ToolCall audit row written with user_id + granted_scope."""
        from audittrace.db.models import InteractionRecord, ToolCall
        from audittrace.dependencies import get_postgres_factory

        fake = _SequencedClient(
            [
                _tools_mode_tool_call_response(
                    "recall_decisions", '{"query": "cache compression"}'
                ),
                _tools_mode_response_text("Based on ADR-009: 75% reduction."),
            ]
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "what about KV cache?"}],
                    "project": "AuditTrace",
                },
            )

        assert response.status_code == 200
        assert len(fake.post_calls) == 2
        # Final response body carries the second iteration's text
        body = response.json()
        assert "ADR-009" in body["choices"][0]["message"]["content"]

        # Interaction row persisted + ONE ToolCall audit row
        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            interactions = (await db.execute(select(InteractionRecord))).scalars().all()
            tool_calls_rows = (await db.execute(select(ToolCall))).scalars().all()
        assert len(interactions) >= 1
        latest_interaction = interactions[-1]
        assert latest_interaction.user_id == SENTINEL_SUBJECT

        assert len(tool_calls_rows) == 1
        tc_row = tool_calls_rows[0]
        assert tc_row.tool_name == "recall_decisions"
        assert tc_row.user_id == SENTINEL_SUBJECT
        assert tc_row.granted_scope == "memory:episodic:read"
        assert tc_row.error is None
        assert tc_row.interaction_id == latest_interaction.id

    def test_tools_mode_streams_content_token_by_token(self, client, _tools_mode):
        """#299: stream=true must stream the answer as MULTIPLE content
        deltas (real token-by-token), not one buffered chunk. The pieces
        concatenate to the full answer and the stream ends with [DONE]."""
        fake = _SequencedStreamClient([_sse_content_lines(["Hel", "lo ", "world"])])
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert "event-stream" in response.headers.get("content-type", "")
        raw = response.content.decode()
        assert "[DONE]" in raw
        chunks = _parse_sse_deltas(raw)
        # Multiple content deltas — the whole point of #299 (was one chunk).
        content_pieces = [
            c["choices"][0]["delta"]["content"]
            for c in chunks
            if c.get("choices") and c["choices"][0].get("delta", {}).get("content")
        ]
        assert content_pieces == ["Hel", "lo ", "world"]
        # Exactly one streaming POST (single terminal turn).
        assert len(fake.stream_calls) == 1
        # A finish_reason=stop frame is present.
        assert any(
            c.get("choices") and c["choices"][0].get("finish_reason") == "stop"
            for c in chunks
        )

    async def test_tools_mode_streaming_persists_streamed_answer(
        self, client, _tools_mode
    ):
        """ADR-049 reconstructibility: the persisted answer equals exactly
        what was streamed to the user (single generation, no double-gen)."""
        from audittrace.db.models import InteractionRecord
        from audittrace.dependencies import get_postgres_factory

        fake = _SequencedStreamClient(
            [_sse_content_lines(["The ", "answer ", "is 42."])]
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "q"}],
                    "stream": True,
                },
            )
        assert response.status_code == 200
        _ = response.content  # drain the stream so the generator finalises

        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            interactions = (await db.execute(select(InteractionRecord))).scalars().all()
        assert len(interactions) >= 1
        latest = interactions[-1]
        assert latest.answer == "The answer is 42."
        assert latest.user_id == SENTINEL_SUBJECT

    async def test_tools_mode_streaming_through_memory_tool_loop(
        self, client, _tools_mode
    ):
        """A memory tool turn streams (think forwarded, tool_call swallowed),
        the tool executes + audits, then the final answer streams. The
        internal memory tool_call must NOT leak to the client as tool_calls."""
        from audittrace.db.models import ToolCall
        from audittrace.dependencies import get_postgres_factory

        fake = _SequencedStreamClient(
            [
                _sse_tool_call_lines(
                    "recall_decisions", '{"query": "cache compression"}'
                ),
                _sse_content_lines(["Based on ", "ADR-009."]),
            ]
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "what about KV cache?"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        raw = response.content.decode()
        chunks = _parse_sse_deltas(raw)
        # TWO streaming turns (tool turn + answer turn).
        assert len(fake.stream_calls) == 2
        # The memory tool_call was NOT forwarded to the client.
        assert not any(
            c.get("choices") and c["choices"][0].get("delta", {}).get("tool_calls")
            for c in chunks
        )
        # The final answer streamed.
        joined = "".join(
            c["choices"][0]["delta"].get("content", "")
            for c in chunks
            if c.get("choices") and c["choices"][0].get("delta")
        )
        assert "ADR-009." in joined
        # The memory tool executed and was audited.
        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            tool_calls_rows = (await db.execute(select(ToolCall))).scalars().all()
        assert len(tool_calls_rows) == 1
        assert tool_calls_rows[0].tool_name == "recall_decisions"

    def test_tools_mode_streaming_forwards_external_tool_call(
        self, client, _tools_mode
    ):
        """An external (non-memory) tool call is forwarded to the client so
        the agentic loop (OpenCode) can execute it, with finish_reason
        tool_calls — the proxy does not try to run it itself."""
        fake = _SequencedStreamClient(
            [_sse_tool_call_lines("read_file", '{"path": "/etc/hosts"}')]
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "read a file"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        chunks = _parse_sse_deltas(response.content.decode())
        # Exactly one streaming turn — the external call terminates the loop.
        assert len(fake.stream_calls) == 1
        # The external tool_call IS forwarded to the client.
        forwarded = [
            tc
            for c in chunks
            if c.get("choices")
            for tc in (c["choices"][0].get("delta", {}).get("tool_calls") or [])
        ]
        assert len(forwarded) == 1
        assert forwarded[0]["function"]["name"] == "read_file"
        assert any(
            c.get("choices") and c["choices"][0].get("finish_reason") == "tool_calls"
            for c in chunks
        )

    def test_tools_mode_streaming_tool_parse_500_falls_back(self, client, _tools_mode):
        """Resilience port: a tool-parse 500 on the streamed turn falls back
        to the buffered retry/no-tools path and still streams an answer."""
        parse_err_body = {
            "error": {
                "code": 500,
                "type": "server_error",
                "message": (
                    "Failed to parse tool call arguments as JSON: "
                    "[json.exception.parse_error.101]"
                ),
            }
        }
        fake = _SequencedStreamClient(
            stream_turns=[["data: [DONE]"]],  # unused — status 500 short-circuits
            stream_status=[500],
            stream_bodies=[parse_err_body],
            post_responses=[_tools_mode_response_text("Recovered answer.")],
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "trigger parse error"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        raw = response.content.decode()
        assert "Recovered answer." in raw
        assert "[DONE]" in raw
        # Fallback used the buffered .post() path at least once.
        assert len(fake.post_calls) >= 1

    def test_tools_mode_streams_synthesized_sse_when_stream_true(
        self, client, _tools_mode
    ):
        """Smoke: stream=true returns an event-stream that carries the
        answer text and terminates with [DONE] (back-compat wire shape)."""
        fake = _SequencedStreamClient([_sse_content_lines(["Streamed text."])])
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert "event-stream" in response.headers.get("content-type", "")
        body = response.content.decode()
        assert "Streamed text." in body
        assert "[DONE]" in body

    async def test_tools_mode_streaming_connect_error_persists_failed(
        self, client, _tools_mode
    ):
        """A connect error mid-stream emits an SSE error frame and persists a
        failed-interaction audit row (same contract as the inject path)."""
        import httpx

        from audittrace.db.models import InteractionRecord
        from audittrace.dependencies import get_postgres_factory

        fake = _RaisingStreamClient(httpx.ConnectError("down"))
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
        assert response.status_code == 200
        raw = response.content.decode()
        assert "unreachable" in raw
        assert "[DONE]" in raw
        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            interactions = (await db.execute(select(InteractionRecord))).scalars().all()
        assert interactions[-1].status == "failed"

    def test_tools_mode_streaming_timeout_emits_error_frame(self, client, _tools_mode):
        """A read timeout mid-stream emits the 504 idle-timeout error frame."""
        import httpx

        fake = _RaisingStreamClient(httpx.ReadTimeout("slow"))
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
        assert response.status_code == 200
        assert "idle timeout" in response.content.decode()

    def test_tools_mode_streaming_generic_exception_emits_error_frame(
        self, client, _tools_mode
    ):
        """An unexpected error mid-stream emits the internal-error frame."""
        fake = _RaisingStreamClient(ValueError("boom"))
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
        assert response.status_code == 200
        assert "Internal error during streaming." in response.content.decode()

    def test_tools_mode_streaming_non_parse_500_emits_502(self, client, _tools_mode):
        """A non-tool-parse upstream 500 surfaces a 502 error frame (no
        retry/fallback — only the Qwen tool-args parse error falls back)."""
        fake = _SequencedStreamClient(
            stream_turns=[["data: [DONE]"]],
            stream_status=[500],
            stream_bodies=[{"error": {"message": "CUDA OOM"}}],
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
        assert response.status_code == 200
        assert "status 500" in response.content.decode()
        # No buffered fallback attempted for a non-parse error.
        assert fake.post_calls == []

    async def test_tools_mode_streaming_fallback_executes_memory_tool(
        self, client, _tools_mode
    ):
        """tool-parse 500 whose buffered fallback returns a MEMORY tool_call:
        execute it, then keep streaming the next turn to a final answer."""
        from audittrace.db.models import ToolCall
        from audittrace.dependencies import get_postgres_factory

        parse_err = {
            "error": {
                "message": "Failed to parse tool call arguments as JSON: "
                "[json.exception.parse_error.101]"
            }
        }
        fake = _SequencedStreamClient(
            stream_turns=[["unused"], _sse_content_lines(["Final answer."])],
            stream_status=[500, 200],
            stream_bodies=[parse_err, None],
            post_responses=[
                _tools_mode_tool_call_response("recall_decisions", '{"query": "x"}')
            ],
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
        assert response.status_code == 200
        raw = response.content.decode()
        assert "Final answer." in raw
        # The buffered fallback executed the memory tool and audited it.
        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            tool_calls_rows = (await db.execute(select(ToolCall))).scalars().all()
        assert len(tool_calls_rows) == 1
        assert tool_calls_rows[0].tool_name == "recall_decisions"

    def test_tools_mode_streaming_repeated_signature_exits_early(
        self, client, _tools_mode
    ):
        """ADR-030: identical memory tool_calls two turns running → the
        streaming loop exits early instead of spinning to the cap."""
        fake = _SequencedStreamClient(
            [
                _sse_tool_call_lines("recall_decisions", '{"query": "x"}', "c1"),
                _sse_tool_call_lines("recall_decisions", '{"query": "x"}', "c2"),
            ]
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
        assert response.status_code == 200
        # Two streamed turns, then early exit — not the 5-iteration cap.
        assert len(fake.stream_calls) == 2

    def test_tools_mode_streaming_hits_iteration_cap(
        self, client, _tools_mode, monkeypatch
    ):
        """Distinct memory tool_calls every turn → the loop stops at the
        configured iteration cap and still closes the stream cleanly."""
        from audittrace import config as config_mod

        config_mod.get_settings.cache_clear()
        monkeypatch.setenv("AUDITTRACE_MEMORY_TOOL_LOOP_MAX_ITERATIONS", "2")

        fake = _SequencedStreamClient(
            [
                _sse_tool_call_lines("recall_decisions", '{"query": "a"}', "c1"),
                _sse_tool_call_lines("recall_decisions", '{"query": "b"}', "c2"),
                _sse_tool_call_lines("recall_decisions", '{"query": "c"}', "c3"),
            ]
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
        assert response.status_code == 200
        assert "[DONE]" in response.content.decode()
        # Exactly the cap — 2 streamed turns, not 3.
        assert len(fake.stream_calls) == 2

    def test_tools_mode_records_langfuse_output_when_configured(
        self, client, _tools_mode, monkeypatch
    ):
        """Tools-mode must create a Langfuse trace and push the LLM output
        via ``_record_langfuse_output`` — same contract as inject mode.

        Regression guard for the bug where tools-mode shipped with no
        Langfuse instrumentation at all, so every prompt/reply went dark
        in the Langfuse UI even though the proxy kept persisting rows to
        Postgres.
        """
        from audittrace import config as config_mod

        config_mod.get_settings.cache_clear()
        monkeypatch.setenv("AUDITTRACE_LANGFUSE_HOST", "http://lf.test")
        monkeypatch.setenv("AUDITTRACE_LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("AUDITTRACE_LANGFUSE_SECRET_KEY", "sk-test")
        # _tools_mode fixture already flipped AUDITTRACE_MEMORY_MODE=tools

        fake = _SequencedClient([_tools_mode_response_text("Answer in tools mode.")])
        with (
            _patch_tool_loop_client(fake),
            _patch_async_client(fake),
            patch(
                "audittrace.routes.chat._lf_get_client",
                return_value=MagicMock(get_current_trace_id=lambda: "trace-tools-1"),
            ),
            patch("audittrace.routes.chat._LANGFUSE_AVAILABLE", True),
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "what time is it?"}],
                    "project": "AuditTrace",
                },
            )

        config_mod.get_settings.cache_clear()

        assert response.status_code == 200

        # The ingestion POST flows through the same patched client as the
        # llama-server POST (both go via chat.py's httpx.AsyncClient).
        ingestion_calls = [
            c for c in fake.post_calls if "/api/public/ingestion" in c["url"]
        ]
        assert ingestion_calls, (
            "tools-mode did not POST to Langfuse /api/public/ingestion — "
            "prompts and replies will be invisible in the Langfuse UI"
        )
        call = ingestion_calls[0]
        assert call["url"].startswith("http://lf.test")
        body = call["json"]["batch"][0]["body"]
        assert body["id"] == "trace-tools-1"
        assert body["output"] == "Answer in tools mode."
        assert body["metadata"]["finish_reason"] == "stop"
        assert body["metadata"]["prompt_tokens"] == 20
        assert body["metadata"]["completion_tokens"] == 8

    async def test_tools_mode_passes_through_external_tool_call(
        self, client, _tools_mode
    ):
        """A `bash` tool call from the client must reach the client
        unchanged — the proxy cannot execute bash, and the loop's
        external-tool exit branch must kick in. Zero ToolCall audit
        rows because no memory tools were dispatched."""
        from audittrace.db.models import ToolCall
        from audittrace.dependencies import get_postgres_factory

        bash_response = {
            "id": "cmpl-test",
            "object": "chat.completion",
            "model": "qwen3.5-35b",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_bash",
                                "type": "function",
                                "function": {
                                    "name": "bash",
                                    "arguments": '{"cmd": "ls /tmp"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
        fake = _SequencedClient([bash_response])
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "ls /tmp"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "description": "Run a shell command",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "cmd": {"type": "string"},
                                    },
                                },
                            },
                        }
                    ],
                },
            )

        assert response.status_code == 200
        body = response.json()
        # The bash tool_call passes through the proxy untouched
        assert (
            body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "bash"
        )
        # Zero memory tool_calls rows — nothing was dispatched
        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            tool_calls_rows = (await db.execute(select(ToolCall))).scalars().all()
        assert tool_calls_rows == []


# ──────────────────── ADR-034: X-Thinking header ─────────────────────────


class TestResolveThinking:
    """ADR-034: parse X-Thinking header → deep | fast | None (auto)."""

    def test_deep(self):
        req = _FakeRequest({"x-thinking": "deep"})
        assert _resolve_thinking(req) == "deep"

    def test_fast(self):
        req = _FakeRequest({"x-thinking": "fast"})
        assert _resolve_thinking(req) == "fast"

    def test_auto_explicit(self):
        req = _FakeRequest({"x-thinking": "auto"})
        assert _resolve_thinking(req) is None

    def test_absent_returns_none(self):
        req = _FakeRequest({})
        assert _resolve_thinking(req) is None

    def test_case_insensitive(self):
        req = _FakeRequest({"x-thinking": "DEEP"})
        assert _resolve_thinking(req) == "deep"

    def test_whitespace_stripped(self):
        req = _FakeRequest({"x-thinking": "  fast  "})
        assert _resolve_thinking(req) == "fast"


class TestApplyThinkingMode:
    """ADR-034: inject chat_template_kwargs.enable_thinking into payload."""

    def test_deep_sets_enable_thinking_true(self):
        payload: dict = {}
        _apply_thinking_mode(payload, "deep")
        assert payload["chat_template_kwargs"]["enable_thinking"] is True

    def test_fast_sets_enable_thinking_false(self):
        payload: dict = {}
        _apply_thinking_mode(payload, "fast")
        assert payload["chat_template_kwargs"]["enable_thinking"] is False

    def test_none_leaves_payload_untouched(self):
        payload: dict = {"messages": []}
        _apply_thinking_mode(payload, None)
        assert "chat_template_kwargs" not in payload

    def test_preserves_existing_chat_template_kwargs(self):
        payload: dict = {"chat_template_kwargs": {"some_other_flag": 42}}
        _apply_thinking_mode(payload, "deep")
        assert payload["chat_template_kwargs"]["some_other_flag"] == 42
        assert payload["chat_template_kwargs"]["enable_thinking"] is True
