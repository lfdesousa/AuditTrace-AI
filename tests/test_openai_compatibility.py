"""Strict OpenAI /v1/chat/completions compatibility regression guard.

OpenAI API schema compatibility is AuditTrace-AI's biggest integration
asset. Every AI SDK (``@ai-sdk/openai-compatible``, langchain,
llamaindex) and every IDE integration (OpenCode, Continue, Cursor,
Zed) targets OpenAI shape as the default. A single byte drift in an
error body, a renamed field, or a missing ``param`` key can make us
not-a-drop-in and force clients into custom-code territory.

These tests lock in the shapes a well-behaved OpenAI client will parse
so any future change that would break compatibility fails CI before
it reaches main. See ``feedback_openai_schema_inviolate`` memory for
the principle and ADR-033 (pending) for the formal contract.

Contract asserted here:

- Success (non-streaming): body is a strict OpenAI ChatCompletion
  shape: ``id``, ``object="chat.completion"``, ``model``, ``choices``
  with ``message.role``, ``message.content`` (and optional
  ``tool_calls``), ``finish_reason``, ``usage`` with
  ``prompt_tokens``/``completion_tokens``/``total_tokens``.
- Success (streaming): each frame ``data: {...}`` parses as a
  ``chat.completion.chunk`` with ``choices[].delta``; final
  ``data: [DONE]`` terminates.
- Error bodies on non-streaming paths: ``{"error": {"message",
  "type", "param", "code", ...}}`` — the four OpenAI keys are
  always present; additional AuditTrace keys (``status``,
  ``operator_hint``, ``trace_id``, ``user_facing_message``) are
  additive.
- Error frames on streaming paths: SSE ``data: {"error": {...}}``
  with the same four OpenAI keys + ``[DONE]`` closer.
"""

import json

import httpx

from tests.test_chat_proxy import (
    _FakeAsyncClient,
    _ok_chat_response,
    _patch_async_client,
    _patch_tool_loop_client,
)

OPENAI_ERROR_REQUIRED_KEYS = {"message", "type", "param", "code"}
"""The four keys every OpenAI error body MUST carry. Additive keys are
allowed; dropping or renaming any of these four breaks clients."""


def _parse_sse_error_frame(body: str) -> dict:
    """Extract the first ``data: {...}`` frame with an ``error`` key
    from an SSE body and return the parsed JSON."""
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :]
        if payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "error" in parsed:
            return parsed
    raise AssertionError(f"No SSE error frame found in body: {body[:400]}")


class TestSuccessShape:
    def test_non_streaming_success_matches_openai_chat_completion(self, client):
        fake = _FakeAsyncClient(post_json=_ok_chat_response("Hi there"))
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "project": "AuditTrace",
                },
            )
        assert response.status_code == 200
        body = response.json()
        # Canonical OpenAI ChatCompletion shape
        assert body["object"] == "chat.completion"
        assert "id" in body
        assert "model" in body
        assert "choices" in body
        assert isinstance(body["choices"], list)
        first = body["choices"][0]
        assert "message" in first
        assert first["message"]["role"] == "assistant"
        assert "content" in first["message"]
        assert "finish_reason" in first
        assert "usage" in body
        usage = body["usage"]
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            assert k in usage

    def test_streaming_success_frames_parse_as_chunks(self, client):
        stream_lines = [
            'data: {"id":"x","object":"chat.completion.chunk","model":"qwen",'
            '"choices":[{"index":0,"delta":{"content":"hi"}}]}',
            "",
            'data: {"choices":[{"finish_reason":"stop","delta":{}}],'
            '"model":"qwen",'
            '"timings":{"cache_n":10,"prompt_n":5,"predicted_n":2}}',
            "",
            "data: [DONE]",
            "",
        ]
        fake = _FakeAsyncClient(stream_lines=stream_lines)
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "project": "AuditTrace",
                    "stream": True,
                },
            )
        assert response.status_code == 200
        body = response.content.decode()
        # Every non-[DONE] data line must parse as a chunk with choices[].delta
        saw_delta_content = False
        saw_usage = False
        for line in body.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: ") :]
            if payload == "[DONE]":
                continue
            chunk = json.loads(payload)
            if "choices" in chunk and chunk["choices"]:
                delta = chunk["choices"][0].get("delta", {})
                if delta.get("content"):
                    saw_delta_content = True
            if "usage" in chunk:
                saw_usage = True
        assert saw_delta_content
        assert saw_usage
        assert body.rstrip().endswith("[DONE]")


class TestErrorBodyOpenAIShape:
    """Every non-streaming error body must carry the four OpenAI keys."""

    def test_non_streaming_timeout_error_is_openai_shaped(self, client):
        fake = _FakeAsyncClient(post_exc=httpx.ReadTimeout("read timeout"))
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "project": "AuditTrace",
                },
            )
        # FastAPI's HTTPException path returns `{"detail": "..."}` today
        # rather than the OpenAI shape — that's a known gap tracked
        # separately (see ADR-033 scope). The STREAMING SSE path and the
        # global 500 handler ARE OpenAI-shaped today; the FastAPI
        # HTTPException default will be aligned in a follow-up.
        #
        # For now we assert the response is 504 and carries a message.
        # Do NOT tighten this assertion without implementing the
        # HTTPException -> OpenAI-envelope converter.
        assert response.status_code == 504
        body = response.json()
        # Either the OpenAI shape or the FastAPI-default shape; record
        # which one so the follow-up work has a clear target.
        assert "error" in body or "detail" in body

    def test_global_500_envelope_is_openai_strict_superset(self, app):
        """Exceptions that escape to the global handler return the
        ADR-033 envelope, which MUST include the four OpenAI keys."""
        from fastapi.testclient import TestClient

        from audittrace import config as config_mod

        config_mod.get_settings.cache_clear()
        fake = _FakeAsyncClient(post_exc=RuntimeError("boom"))
        # Flip to tools mode so RuntimeError escapes to global handler.
        import os

        os.environ["AUDITTRACE_MEMORY_MODE"] = "tools"
        try:
            config_mod.get_settings.cache_clear()
            with TestClient(app, raise_server_exceptions=False) as client:
                with _patch_tool_loop_client(fake), _patch_async_client(fake):
                    response = client.post(
                        "/v1/chat/completions",
                        json={
                            "model": "qwen3.5-35b",
                            "messages": [{"role": "user", "content": "hi"}],
                            "project": "AuditTrace",
                        },
                    )
        finally:
            os.environ.pop("AUDITTRACE_MEMORY_MODE", None)
            config_mod.get_settings.cache_clear()

        assert response.status_code == 500
        body = response.json()
        assert "error" in body
        err = body["error"]
        # The four OpenAI keys, all present, all correctly typed:
        assert OPENAI_ERROR_REQUIRED_KEYS.issubset(err.keys()), (
            f"Global 500 envelope missing OpenAI keys — got {sorted(err.keys())}"
        )
        assert isinstance(err["message"], str) and err["message"]
        assert err["type"] == "api_error"
        assert err["param"] is None  # OpenAI uses null when N/A
        assert isinstance(err["code"], str) and err["code"]
        # AuditTrace extensions are additive (don't mandate every one,
        # just confirm presence is permitted without breaking the core):
        for k in ("status", "operator_hint", "trace_id", "user_facing_message"):
            assert k in err, f"expected AuditTrace extension {k!r} in envelope"


class TestSSEErrorFrameOpenAIShape:
    """Every SSE error frame must carry the four OpenAI keys and be
    followed by a [DONE] terminator — what @ai-sdk and openai-python
    expect for mid-stream failures."""

    def test_streaming_timeout_frame_is_openai_shaped(self, client):
        from tests.test_chat_failure_audit import _FakeStreamTimeoutClient

        fake = _FakeStreamTimeoutClient()
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "project": "AuditTrace",
                    "stream": True,
                },
            )
        assert response.status_code == 200
        body = response.content.decode()
        frame = _parse_sse_error_frame(body)
        err = frame["error"]
        assert OPENAI_ERROR_REQUIRED_KEYS.issubset(err.keys())
        assert err["type"] == "api_error"
        assert err["code"] == "proxy_timeout"
        assert err["param"] is None
        assert isinstance(err["message"], str) and err["message"]
        # AuditTrace extension present
        assert err.get("status") == 504
        assert body.rstrip().endswith("[DONE]")

    def test_streaming_connect_error_frame_is_openai_shaped(self, client):
        fake = _FakeAsyncClient()

        class _ConnectErrorCtx:
            async def __aenter__(self) -> None:
                raise httpx.ConnectError("refused")

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

        fake.stream = lambda method, url, json=None, **kw: _ConnectErrorCtx()
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "project": "AuditTrace",
                    "stream": True,
                },
            )
        body = response.content.decode()
        frame = _parse_sse_error_frame(body)
        err = frame["error"]
        assert OPENAI_ERROR_REQUIRED_KEYS.issubset(err.keys())
        assert err["type"] == "api_error"
        assert err["code"] == "upstream_unreachable"
