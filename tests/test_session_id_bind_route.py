"""SESSION-ID-BIND (v1.31.0 WP-6), INTEGRATION layer: the real app through
``create_app()`` + ``TestClient`` answering ``POST /v1/chat/completions`` (memory
mode ``inject`` and ``tools``, ``stream`` false and true) and ``POST /mcp``
``tools/call``, with the PRODUCTION structured-logging bootstrap capturing the
stdout lines. The unit layer is ``test_session_id_bind.py``; the two layers are
not mixed.

This is the flipped AC3b of LOG-TRACE-ID (ADDENDUM-1 C2: "no route binds
``session_id`` today"): the same question, now answered the other way, through
the real routes.

Mocks declared: the llama-server upstream is the chat suite's httpx fakes
(``tests.test_chat_proxy._SequencedClient`` / ``_SequencedStreamClient``,
patched onto ``routes.chat.httpx.AsyncClient`` and the tool loop's); the
embedding / vector stores are the in-memory test container's; auth is the
test container's sentinel identity (``AUTH_REQUIRED`` off). Nothing in the
binder, the route, the Filter or the logging bootstrap is patched. No clock,
no network, no wall-clock assertion.

Neuters (each run ALONE, RED recorded in the build record):
  N-6.1a remove the bind in ``chat_completions`` (``request_scope.enter_context``)
  N-6.1b remove the stream re-bind (``stream_with_bound_session``) at the
         tools-mode ``StreamingResponse`` -> the tools/stream cell only
  N-6.1c ... at the inject-mode ``StreamingResponse`` -> the inject/stream cell only
  N-6.2  remove the bind in ``mcp._handle_tools_call``
  N-6.3r remove the reset (``bound_session_id`` without ``reset_session_id``)
"""

from __future__ import annotations

import contextlib
import importlib
import io
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from audittrace import config as config_mod
from audittrace import tools as tools_mod
from audittrace.db.models import InteractionRecord
from audittrace.dependencies import get_postgres_factory
from audittrace.services.console_store._context import current_session_id
from audittrace.tools import reset_registry_for_tests
from tests._log_trace_id_support import bootstrap, isolated_logging_state, log_lines
from tests.test_chat_proxy import (
    _ok_chat_response,
    _patch_async_client,
    _patch_tool_loop_client,
    _SequencedClient,
    _SequencedStreamClient,
    _sse_content_lines,
    _tools_mode_response_text,
)

SESSION_HEADER = "session-bind-route-0001"
OTHER_SESSION = "session-bind-route-0002"
PERSISTED = "interaction persisted"  # emitted from the handler / the stream body


@pytest.fixture(autouse=True)
def _fresh_registry_with_real_handlers() -> Iterator[None]:
    """The MCP cells need the REAL registered memory tools (same set-up as
    ``test_mcp_routes.py``). Unlike that file this one RESTORES the registry it
    found on teardown: leaving it empty broke the tools-mode chat tests that run
    after this file in a shared session (measured: 8 failures in
    ``test_chat_proxy.py`` when this file ran first)."""
    saved_registry = dict(tools_mod.MEMORY_TOOL_REGISTRY)
    saved_names = dict(tools_mod._BY_VISIBLE_NAME)
    reset_registry_for_tests()
    import audittrace.tools.memory_handlers as handlers_mod

    importlib.reload(handlers_mod)
    yield
    reset_registry_for_tests()
    tools_mod.MEMORY_TOOL_REGISTRY.update(saved_registry)
    tools_mod._BY_VISIBLE_NAME.update(saved_names)


@pytest.fixture
def captured(app: Any) -> Iterator[tuple[Any, io.StringIO]]:
    """The real app + a buffer holding the production JSON lines."""
    with isolated_logging_state():
        yield app, bootstrap()  # AFTER create_app(): it reconfigures logging


def _flip_memory_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    config_mod.get_settings.cache_clear()
    monkeypatch.setenv("AUDITTRACE_MEMORY_MODE", mode)


def _chat(
    app: Any,
    *,
    mode: str,
    stream: bool,
    headers: dict[str, str] | None = None,
    content: str = "hi",
) -> Any:
    """One ``POST /v1/chat/completions`` through the real stack."""
    if stream:
        fake: Any = _SequencedStreamClient(
            [_sse_content_lines(["a", "b"])], post_responses=[_ok_chat_response()]
        )
    else:
        fake = _SequencedClient([_tools_mode_response_text("x")])
        if mode == "inject":
            fake = _SequencedClient([_ok_chat_response()])
    with (
        contextlib.closing(TestClient(app, raise_server_exceptions=False)) as client,
        _patch_async_client(fake),
        _patch_tool_loop_client(fake),
    ):
        response = client.post(
            "/v1/chat/completions",
            headers=headers or {},
            json={
                "model": "qwen3.5-35b",
                "messages": [{"role": "user", "content": content}],
                "stream": stream,
            },
        )
        _ = response.content  # drain so a streaming generator finalises
    assert response.status_code == 200
    return response


def _persisted_line(buf: io.StringIO) -> dict[str, Any]:
    found = [ln for ln in log_lines(buf) if PERSISTED in ln["message"]]
    assert len(found) == 1, buf.getvalue()  # positive control: the line exists
    return found[0]


_CELLS = [
    pytest.param("inject", False, id="inject-nonstream"),
    pytest.param("inject", True, id="inject-stream"),
    pytest.param("tools", False, id="tools-nonstream"),
    pytest.param("tools", True, id="tools-stream"),
]


# ---------------------------------------------------------------------------
# AC-6.1 (the flipped AC3b): a request line carries session_id, stream x2
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("mode", "stream"), _CELLS)
def test_ac_6_1_a_chat_request_line_carries_the_x_session_id(
    captured: tuple[Any, io.StringIO],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    stream: bool,
) -> None:
    """AC-6.1. A line emitted by the real chat route (non-stream: from the
    handler; ``stream: true``: from INSIDE the stream body, after the handler
    returned) carries ``session_id`` == the request's ``X-Session-Id``.

    Neuters: N-6.1a -> RED on all four cells; N-6.1b -> RED on tools-stream
    only; N-6.1c -> RED on inject-stream only.
    """
    app, buf = captured
    _flip_memory_mode(monkeypatch, mode)
    try:
        _chat(app, mode=mode, stream=stream, headers={"X-Session-Id": SESSION_HEADER})
    finally:
        config_mod.get_settings.cache_clear()
    assert _persisted_line(buf)["session_id"] == SESSION_HEADER


@pytest.mark.parametrize(("mode", "stream"), _CELLS)
async def test_ac_6_1_without_the_header_the_line_carries_the_persisted_session_id(
    captured: tuple[Any, io.StringIO],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    stream: bool,
) -> None:
    """AC-6.1 reconstruction half. With no ``X-Session-Id`` the route computes
    one; the log line's ``session_id`` is that same value as persisted in
    ``interactions.session_id`` (log line <-> audit row, one id).

    Neuter: N-6.1a -> RED.
    """
    app, buf = captured
    _flip_memory_mode(monkeypatch, mode)
    try:
        _chat(app, mode=mode, stream=stream)
    finally:
        config_mod.get_settings.cache_clear()
    line = _persisted_line(buf)
    pg = get_postgres_factory()
    async with pg.get_session_factory()() as db:
        rows = (await db.execute(select(InteractionRecord))).scalars().all()
    assert len(rows) == 1
    assert rows[0].session_id
    assert line["session_id"] == rows[0].session_id


# ---------------------------------------------------------------------------
# AC-6.2: the MCP tools/call line carries it too
# ---------------------------------------------------------------------------
def test_ac_6_2_an_mcp_tool_call_line_carries_the_x_session_id(
    captured: tuple[Any, io.StringIO],
) -> None:
    """AC-6.2. ``POST /mcp`` ``tools/call`` with ``X-Session-Id``: the line the
    dispatch / audit write emits carries that session id.

    Neuter: N-6.2 -> RED.
    """
    app, buf = captured
    with contextlib.closing(TestClient(app, raise_server_exceptions=False)) as client:
        response = client.post(
            "/mcp",
            headers={"X-Session-Id": SESSION_HEADER},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "recall_decisions",
                    "arguments": {"query": "kv cache"},
                },
            },
        )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    assert _persisted_line(buf)["session_id"] == SESSION_HEADER


# ---------------------------------------------------------------------------
# AC-6.3 at the route layer: successive requests never share an id
# ---------------------------------------------------------------------------
def test_ac_6_3_successive_requests_carry_their_own_session_id_only(
    captured: tuple[Any, io.StringIO],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two requests one after the other: each ``persisted`` line carries its OWN
    ``X-Session-Id``, and the test's own context sees nothing bound afterwards.

    The route layer through ``TestClient`` runs on a copied context, so this is
    NOT the AC-6.3 proof (``test_session_id_bind.py`` is: bind -> reset ->
    unset, unit-level, ADDENDUM-1 C14); it pins that no per-process state
    carries one request's id into the next.

    Neuter: make the binder ignore the new value (keep the first) -> RED.
    """
    app, buf = captured
    _flip_memory_mode(monkeypatch, "inject")
    try:
        _chat(
            app,
            mode="inject",
            stream=False,
            headers={"X-Session-Id": SESSION_HEADER},
            content="first",
        )
        _chat(
            app,
            mode="inject",
            stream=True,
            headers={"X-Session-Id": OTHER_SESSION},
            content="second",
        )
    finally:
        config_mod.get_settings.cache_clear()
    persisted = [ln for ln in log_lines(buf) if PERSISTED in ln["message"]]
    assert [ln["session_id"] for ln in persisted] == [SESSION_HEADER, OTHER_SESSION]
    assert current_session_id() is None
