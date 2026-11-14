"""AUDIT-ROW-MISSING-NONOBJECT-USAGE, ADDENDUM-1 (gate r1, S3): the WARNING and the row join.

After the fix the audit row of a turn whose upstream ``usage`` is not a JSON object is
the row an absent ``usage`` gets: the durable record no longer tells the two apart. What
is left of the incident is one WARNING. It is a witness of THAT turn only if its log line
carries the identity of the request that produced the row. This file pins that join:

    the WARNING's production JSON line ``trace_id`` == ``interactions.trace_id`` of the
    one row the turn added (and ``session_id`` likewise).

INTEGRATION layer, the pattern of ``tests/test_session_id_bind_route.py``: the real app
through ``create_app()`` + ``TestClient``, the PRODUCTION structured-logging bootstrap
(``setup_logging`` then ``init_telemetry``) with the handler's stdout bound to a buffer.

Mocks declared: the llama-server upstream is the chat suite's httpx fake
(``tests.test_chat_proxy._FakeAsyncClient``); stores and identity are the in-memory test
container's. Nothing in the route, the Filter or the logging bootstrap is patched. No
clock, no network, no wall-clock assertion.

Neuters (each run ALONE, RED recorded in the build record):
  N-J1 the WARNING is emitted outside the request's context
       (``contextvars.Context().run(logger.warning, ...)``): the line has no trace_id
  N-J2 the guard logs no WARNING (the spec's ``no_warning``)
  N-J3 no guard at all (the parent commit): no WARNING line, no row
  N-J4 ``_persist_interaction`` stores another trace id than the active span's
Not a neuter of THIS file: a ``TraceContextFilter`` that stops stamping ``trace_id``
never reaches these tests; the suite's (and the pod's) startup log-correlation
self-test raises first (``telemetry._run_startup_log_correlation_self_test``).
"""

from __future__ import annotations

import contextlib
import io
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from audittrace import config as config_mod
from audittrace.db.models import InteractionRecord
from audittrace.dependencies import get_postgres_factory
from tests._log_trace_id_support import bootstrap, isolated_logging_state, log_lines
from tests.test_chat_proxy import (
    _FakeAsyncClient,
    _patch_async_client,
    _tools_mode_response_text,
)

WARNING_HEAD = "upstream usage is not a JSON object"
PERSISTED = "interaction persisted"
NON_OBJECT_USAGE = "oops"
WELL_FORMED_USAGE = {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}
TRACE_SHAPE = re.compile(r"^[0-9a-f]{32}$")
REQUEST = {
    "model": "audittrace-default",
    "messages": [{"role": "user", "content": "What governs memory_mode?"}],
    "project": "AuditTrace",
    "stream": False,
}


@pytest.fixture
def captured(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Any, io.StringIO]]:
    """The real app in ``memory_mode=inject`` + a buffer holding the production
    JSON lines. The settings cache is cleared on every exit path."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(isolated_logging_state())
        stack.callback(config_mod.get_settings.cache_clear)
        config_mod.get_settings.cache_clear()
        monkeypatch.setenv("AUDITTRACE_MEMORY_MODE", "inject")
        yield app, bootstrap()  # AFTER create_app(): it reconfigures logging


def _turn(app: Any, usage: Any) -> None:
    """One served non-stream turn through the real stack."""
    body = _tools_mode_response_text("done")
    body["usage"] = usage
    fake = _FakeAsyncClient(post_json=body)
    with (
        contextlib.closing(TestClient(app, raise_server_exceptions=False)) as client,
        _patch_async_client(fake),
    ):
        response = client.post("/v1/chat/completions", json=REQUEST)
    assert len(fake.post_calls) == 1  # control: the upstream was called once
    assert response.status_code == 200


async def _rows() -> list[InteractionRecord]:
    pg = get_postgres_factory()
    async with pg.get_session_factory()() as db:
        return list((await db.execute(select(InteractionRecord))).scalars().all())


def _lines(buf: io.StringIO, needle: str) -> list[dict[str, Any]]:
    return [ln for ln in log_lines(buf) if needle in ln["message"]]


async def test_the_warning_line_and_the_audit_row_carry_one_trace_id(
    captured: tuple[Any, io.StringIO],
) -> None:
    """The join. Neuters: N-J1, N-J2, N-J3, N-J4 -> RED."""
    app, buf = captured

    _turn(app, NON_OBJECT_USAGE)

    warnings = _lines(buf, WARNING_HEAD)
    assert len(warnings) == 1, buf.getvalue()
    line = warnings[0]
    assert line["level"] == "WARNING"
    assert line["logger"] == "audittrace.routes.chat"
    rows = await _rows()
    assert len(rows) == 1
    # B-1: a lost request context must fail as an assertion, not a KeyError
    assert "trace_id" in line, line
    assert TRACE_SHAPE.match(line["trace_id"])
    assert line["trace_id"] == rows[0].trace_id
    assert rows[0].session_id
    assert line["session_id"] == rows[0].session_id
    # the witness names the type, never the value the upstream sent
    assert NON_OBJECT_USAGE not in buf.getvalue()


async def test_a_well_formed_turn_has_a_joined_line_and_no_warning(
    captured: tuple[Any, io.StringIO],
) -> None:
    """Control (GREEN before and after the fix): the join is read from the real
    Filter on a normal turn, and a normal turn does not warn."""
    app, buf = captured

    _turn(app, WELL_FORMED_USAGE)

    assert _lines(buf, WARNING_HEAD) == []
    persisted = _lines(buf, PERSISTED)
    assert len(persisted) == 1, buf.getvalue()  # positive control: the line exists
    rows = await _rows()
    assert len(rows) == 1
    assert persisted[0]["trace_id"] == rows[0].trace_id
