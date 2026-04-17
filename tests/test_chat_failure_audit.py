"""Regression tests for migration 007 / ADR-033 seed.

Every upstream failure on ``POST /v1/chat/completions`` must produce
an ``interactions`` row with ``status='failed'`` and an appropriate
``failure_class``. Prior to 2026-04-16, ``httpx.ReadTimeout`` on the
streaming + tools-mode paths escaped without reaching
``_persist_interaction`` — 10 confirmed 500s across 2026-04-14 and
2026-04-15 left zero audit rows in Postgres. See the forensic plan
at ``~/.claude/plans/reflective-discovering-platypus.md`` for the
full investigation.

These tests cover every upstream-failure path on the chat hot loop:
streaming + tools-mode + non-streaming × (ReadTimeout, generic
Exception), plus the success path (so ``duration_ms`` regression is
covered for good rows too).
"""

import httpx
import pytest

from audittrace.db.models import InteractionRecord
from audittrace.dependencies import get_postgres_factory
from audittrace.identity import SENTINEL_SUBJECT
from tests.test_chat_proxy import (
    _FakeAsyncClient,
    _ok_chat_response,
    _patch_async_client,
    _patch_tool_loop_client,
    _tools_mode_response_text,
)


class _StreamTimeoutCtx:
    """Async-stream context manager that raises ``httpx.ReadTimeout`` on
    entry — simulates the upstream proxy never sending the first SSE
    line within ``llama_proxy_timeout``.
    """

    async def __aenter__(self) -> None:
        raise httpx.ReadTimeout("Read timed out while opening stream")

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _StreamGenericExcCtx:
    """Async-stream context manager that raises a non-httpx exception on
    entry — the ``except Exception`` branch in the streaming generator
    is the safety net.
    """

    async def __aenter__(self) -> None:
        raise RuntimeError("unexpected streaming failure")

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeStreamTimeoutClient(_FakeAsyncClient):
    def stream(self, method, url, json=None, **kwargs):  # type: ignore[override]
        return _StreamTimeoutCtx()


class _FakeStreamGenericExcClient(_FakeAsyncClient):
    def stream(self, method, url, json=None, **kwargs):  # type: ignore[override]
        return _StreamGenericExcCtx()


def _latest_interaction() -> InteractionRecord | None:
    pg = get_postgres_factory()
    with pg.get_session_factory()() as db:
        return db.query(InteractionRecord).order_by(InteractionRecord.id.desc()).first()


class TestStreamingFailureAudit:
    """Streaming inject path — the only path where the SSE error frame is
    yielded from inside the generator because HTTP headers have already
    been sent by the time the upstream failure occurs."""

    def test_streaming_timeout_persists_failed_row_with_sse_error_frame(self, client):
        fake = _FakeStreamTimeoutClient()
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "very long think"}],
                    "project": "AuditTrace",
                    "stream": True,
                },
            )

        # Stream opens OK; the failure surfaces as an error frame in the body.
        assert response.status_code == 200
        body = response.content.decode()
        # OpenAI-shape error frame: {error: {message, type, param, code, ...}}
        assert '"type": "api_error"' in body
        assert '"code": "proxy_timeout"' in body
        assert '"status": 504' in body
        assert "[DONE]" in body

        row = _latest_interaction()
        assert row is not None, "no interaction row written on timeout"
        assert row.status == "failed"
        assert row.failure_class == "proxy_timeout"
        assert row.answer == ""
        assert row.error_detail
        assert row.duration_ms is not None
        assert row.duration_ms >= 0
        assert row.user_id == SENTINEL_SUBJECT

    def test_streaming_generic_exception_persists_internal_error_row(self, client):
        fake = _FakeStreamGenericExcClient()
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "boom"}],
                    "project": "AuditTrace",
                    "stream": True,
                },
            )

        assert response.status_code == 200
        body = response.content.decode()
        assert '"type": "api_error"' in body
        assert '"code": "internal_error"' in body
        assert "[DONE]" in body

        row = _latest_interaction()
        assert row is not None
        assert row.status == "failed"
        assert row.failure_class == "internal_error"
        assert row.duration_ms is not None

    def test_streaming_connect_error_now_persists_failed_row(self, client):
        """Prior to migration 007 the ConnectError path yielded an error
        frame but did NOT persist a row. The audit taxonomy now requires
        a row for every failure class, including this one."""
        fake = _FakeAsyncClient()

        class _ConnectErrorCtx:
            async def __aenter__(self) -> None:
                raise httpx.ConnectError("connection refused")

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

        assert response.status_code == 200
        body = response.content.decode()
        assert '"type": "api_error"' in body
        assert '"code": "upstream_unreachable"' in body
        assert "unreachable" in body
        assert "[DONE]" in body

        row = _latest_interaction()
        assert row is not None
        assert row.status == "failed"
        assert row.failure_class == "upstream_unreachable"


class TestNonStreamingFailureAudit:
    """Non-streaming inject path — already returned 504/502 pre-migration;
    this confirms the interaction row now lands too, and carries the
    correct failure_class."""

    def test_non_streaming_timeout_returns_504_and_persists_failed_row(self, client):
        fake = _FakeAsyncClient(post_exc=httpx.ReadTimeout("Read timed out on POST"))
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "short"}],
                    "project": "AuditTrace",
                },
            )

        assert response.status_code == 504
        assert "timeout" in response.json()["detail"].lower()

        row = _latest_interaction()
        assert row is not None
        assert row.status == "failed"
        assert row.failure_class == "proxy_timeout"
        assert row.error_detail
        assert row.duration_ms is not None

    def test_non_streaming_connect_error_returns_502_and_persists_row(self, client):
        fake = _FakeAsyncClient(post_exc=httpx.ConnectError("no route"))
        with _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "project": "AuditTrace",
                },
            )

        assert response.status_code == 502
        row = _latest_interaction()
        assert row is not None
        assert row.status == "failed"
        assert row.failure_class == "upstream_unreachable"


class TestToolsModeFailureAudit:
    """ADR-025 tools mode — previously the single biggest audit hole.
    Every iteration of the tool loop is a non-streaming httpx POST and
    none were wrapped. An upstream timeout on any iteration bypassed
    both ``_persist_interaction`` and ``_flush_pending_tool_calls``."""

    def _flip_to_tools_mode(self, monkeypatch):
        from audittrace import config as config_mod

        config_mod.get_settings.cache_clear()
        monkeypatch.setenv("AUDITTRACE_MEMORY_MODE", "tools")
        yield
        config_mod.get_settings.cache_clear()

    @pytest.fixture
    def _tools_mode(self, monkeypatch):
        yield from self._flip_to_tools_mode(monkeypatch)

    def test_tools_mode_timeout_returns_504_and_persists_failed_row(
        self, client, _tools_mode
    ):
        fake = _FakeAsyncClient(
            post_exc=httpx.ReadTimeout("tool loop iter 0 timed out")
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "analytical prompt"}],
                    "project": "AuditTrace",
                },
            )

        assert response.status_code == 504
        row = _latest_interaction()
        assert row is not None
        assert row.status == "failed"
        assert row.failure_class == "proxy_timeout"
        assert row.error_detail
        assert row.duration_ms is not None

    def test_tools_mode_generic_exception_triggers_global_envelope(
        self, app, _tools_mode
    ):
        """Unexpected exception: tools-mode handler persists a failed row
        then re-raises, and the FastAPI global handler returns the
        ADR-033 3-audience error envelope.

        Uses a dedicated ``TestClient(raise_server_exceptions=False)`` so
        the handler's 500 response is observable — the default TestClient
        re-raises server-side exceptions for debugging, which would
        prevent asserting on the error envelope body.
        """
        from fastapi.testclient import TestClient

        fake = _FakeAsyncClient(post_exc=RuntimeError("surprise"))
        with TestClient(app, raise_server_exceptions=False) as client:
            with _patch_tool_loop_client(fake), _patch_async_client(fake):
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "qwen3.5-35b",
                        "messages": [{"role": "user", "content": "anything"}],
                        "project": "AuditTrace",
                    },
                )

        assert response.status_code == 500
        body = response.json()
        assert "error" in body
        err = body["error"]
        # OpenAI-compatible core (strict superset):
        assert err["message"]
        assert err["type"] == "api_error"
        assert err["param"] is None
        assert err["code"] == "internal_error"
        # AuditTrace extensions:
        assert err["status"] == 500
        assert "user_facing_message" in err
        assert "operator_hint" in err
        assert "trace_id" in err  # may be None in test env, but key present

        row = _latest_interaction()
        assert row is not None
        assert row.status == "failed"
        assert row.failure_class == "internal_error"


class TestSuccessCaseDurationPersisted:
    """Success-path rows also carry ``duration_ms`` now; regression guard
    against accidentally removing the parameter in future edits."""

    def test_non_streaming_success_populates_duration_ms(self, client):
        fake = _FakeAsyncClient(post_json=_ok_chat_response("Hi"))
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
        row = _latest_interaction()
        assert row is not None
        assert row.status == "success"
        assert row.failure_class is None
        assert row.duration_ms is not None
        assert row.duration_ms >= 0

    def test_streaming_success_populates_duration_ms(self, client):
        stream_lines = [
            'data: {"choices":[{"delta":{"content":"hi"}}]}',
            "",
            'data: {"choices":[{"finish_reason":"stop","delta":{}}],'
            '"model":"qwen3.5",'
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
        row = _latest_interaction()
        assert row is not None
        assert row.status == "success"
        assert row.failure_class is None
        assert row.duration_ms is not None

    def test_tools_mode_success_populates_duration_ms(self, client, monkeypatch):
        from audittrace import config as config_mod
        from tests.test_chat_proxy import _SequencedClient

        config_mod.get_settings.cache_clear()
        monkeypatch.setenv("AUDITTRACE_MEMORY_MODE", "tools")
        try:
            fake = _SequencedClient([_tools_mode_response_text("done")])
            with _patch_tool_loop_client(fake), _patch_async_client(fake):
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "qwen3.5-35b",
                        "messages": [{"role": "user", "content": "hi"}],
                        "project": "AuditTrace",
                    },
                )
            assert response.status_code == 200
            row = _latest_interaction()
            assert row is not None
            assert row.status == "success"
            assert row.duration_ms is not None
        finally:
            config_mod.get_settings.cache_clear()
