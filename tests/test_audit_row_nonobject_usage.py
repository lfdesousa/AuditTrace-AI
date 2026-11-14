"""AUDIT-ROW-MISSING-NONOBJECT-USAGE: a served inject non-stream turn leaves exactly one audit row.

The defect (EU AI Act Art 12 completeness): when the upstream's 200 body carries a ``usage``
that is not a JSON object, the client gets HTTP 200 with the answer and the ``interactions``
table gets nothing. The fix reads such a ``usage`` as absent FOR THE ROW ONLY: the row is the
one an absent ``usage`` gets today, the bytes sent to the client do not change, and one
WARNING names the TYPE of what the upstream sent (never its value).

Through the real route (``POST /v1/chat/completions``) with a stubbed upstream. Rows are
COUNTED (ids before / after), so "exactly one" is observed and a second row cannot hide
behind "the latest row looks right".

Scope, on purpose: the non-object ``usage`` of the inject non-stream success path. A
malformed VALUE inside an object ``usage`` (negative, string, bool) is not touched here
(AUDIT-USAGE-SANITISATION), nor is the tools non-stream arm.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from audittrace.db.models import InteractionRecord
from audittrace.dependencies import get_postgres_factory
from audittrace.routes import chat as chat_mod
from tests.test_chat_proxy import (
    _FakeAsyncClient,
    _patch_async_client,
    _tools_mode_response_text,
)
from tests.test_chat_unsupported_content_error import (  # noqa: F401 (fixture)
    inject_mode,
)

pytestmark = pytest.mark.usefixtures("inject_mode")

_ABSENT: Any = object()
ANSWER = "done"
WELL_FORMED = {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}
REQUEST = {
    "model": "audittrace-default",
    "messages": [{"role": "user", "content": "What governs memory_mode?"}],
    "project": "AuditTrace",
    "stream": False,
}

# JSON has six value types. ``object`` is the well-formed one; the read in the route
# (``body.get("usage") or {}``) splits the other five by truthiness. One value per
# (type, truthiness) pair that exists: the two tables below are that whole set.
NON_OBJECT_USAGE: dict[str, Any] = {
    "string": "oops",
    "array": ["a"],
    "int": 7,
    "true": True,
    "float": 1.5,
}
ABSENT_LIKE_USAGE: dict[str, Any] = {
    "absent": _ABSENT,
    "null": None,
    "empty_object": {},
    "empty_string": "",
    "empty_array": [],
    "zero": 0,
    "false": False,
}

# Columns that differ between two rows by construction. Every OTHER column of the
# table, including one added later, must be equal between the row under test and the
# reference row: an identity over the mapper's columns, not a list of expected fields.
PER_ROW_COLUMNS = frozenset(
    {"id", "timestamp", "created_at", "duration_ms", "trace_id", "content_hash"}
)


def _upstream_body(usage: Any) -> dict[str, Any]:
    body = _tools_mode_response_text(ANSWER)
    if usage is _ABSENT:
        body.pop("usage")
    else:
        body["usage"] = usage
    return body


async def _interaction_rows() -> list[InteractionRecord]:
    pg = get_postgres_factory()
    async with pg.get_session_factory()() as db:
        result = await db.execute(
            select(InteractionRecord).order_by(InteractionRecord.id)
        )
        return list(result.scalars().all())


def _comparable(row: InteractionRecord) -> dict[str, Any]:
    return {
        name: getattr(row, name)
        for name in InteractionRecord.__table__.columns.keys()
        if name not in PER_ROW_COLUMNS
    }


@contextlib.contextmanager
def _route_warnings() -> Iterator[list[logging.LogRecord]]:
    """Every record at WARNING or above that ``audittrace.routes.chat`` logs."""
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collect(level=logging.WARNING)
    chat_mod.logger.addHandler(handler)
    with contextlib.ExitStack() as stack:
        stack.callback(chat_mod.logger.removeHandler, handler)
        yield records


async def _turn(
    client: TestClient, usage: Any
) -> tuple[list[InteractionRecord], list[logging.LogRecord]]:
    """One non-stream turn against a stub upstream.

    Returns the rows it ADDED and what the route logged at WARNING or above. Asserts
    the wire on every call: HTTP 200 and a body that is, byte for byte, the upstream's
    body in the route's compact JSON rendering. The expected bytes are rendered BEFORE
    the request and the stub gets its own copy, so a route that rewrites the body it
    was handed cannot move the reference with it.
    """
    body = _upstream_body(usage)
    expected = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    before = {row.id for row in await _interaction_rows()}
    fake = _FakeAsyncClient(post_json=copy.deepcopy(body))
    with _patch_async_client(fake), _route_warnings() as logged:
        response = client.post("/v1/chat/completions", json=REQUEST)
    assert len(fake.post_calls) == 1  # control: the upstream was called once
    assert response.status_code == 200
    assert response.content == expected
    added = [row for row in await _interaction_rows() if row.id not in before]
    return added, logged


def _assert_absent_usage_row(row: InteractionRecord) -> None:
    """The exact row an absent ``usage`` gets today."""
    assert row.status == "success"
    assert (row.prompt_tokens, row.completion_tokens) == (0, 0)
    assert type(row.prompt_tokens) is int and type(row.completion_tokens) is int
    assert row.failure_class is None
    assert row.error_detail is None
    assert row.answer == ANSWER
    assert row.question == REQUEST["messages"][0]["content"]
    assert row.model == REQUEST["model"]
    assert row.project == REQUEST["project"]
    assert row.content_hash  # ADR-058: the row carries its tamper-evidence hash
    assert row.trace_id  # Art 12: the row is tied to the request's trace


@pytest.mark.parametrize("shape", sorted(NON_OBJECT_USAGE))
async def test_non_object_usage_turn_leaves_exactly_one_row(
    client: TestClient, shape: str
) -> None:
    reference, _ = await _turn(client, _ABSENT)
    assert len(reference) == 1  # control: the reference turn itself left one row

    added, _ = await _turn(client, NON_OBJECT_USAGE[shape])

    assert len(added) == 1, f"served turn left {len(added)} audit rows, want 1"
    _assert_absent_usage_row(added[0])
    assert _comparable(added[0]) == _comparable(reference[0])


@pytest.mark.parametrize("shape", sorted(NON_OBJECT_USAGE))
async def test_non_object_usage_is_logged_by_type_never_by_value(
    client: TestClient, shape: str
) -> None:
    value = NON_OBJECT_USAGE[shape]

    _, logged = await _turn(client, value)

    assert [(r.levelno, r.getMessage()) for r in logged] == [
        (
            logging.WARNING,
            "upstream usage is not a JSON object "
            f"(type={type(value).__name__}); stored as absent",
        )
    ]


@pytest.mark.parametrize("shape", sorted(ABSENT_LIKE_USAGE))
async def test_absent_like_usage_turn_leaves_exactly_one_row(
    client: TestClient, shape: str
) -> None:
    """Control (GREEN before and after): what the fix must make the cells above equal to."""
    added, logged = await _turn(client, ABSENT_LIKE_USAGE[shape])

    assert len(added) == 1
    _assert_absent_usage_row(added[0])
    assert logged == []  # an absent usage is the normal case: no warning


async def test_well_formed_usage_is_stored_as_sent(client: TestClient) -> None:
    """Control: the fix reads a NON-object as absent, and nothing else."""
    added, logged = await _turn(client, WELL_FORMED)

    assert len(added) == 1
    assert logged == []
    assert added[0].status == "success"
    assert (added[0].prompt_tokens, added[0].completion_tokens) == (
        WELL_FORMED["prompt_tokens"],
        WELL_FORMED["completion_tokens"],
    )
    assert added[0].answer == ANSWER


async def test_failure_after_the_row_write_adds_no_second_row(
    client: TestClient,
) -> None:
    """Control for "exactly one": a fix that writes a fallback row whenever the
    capture block raises would write a second row here."""

    async def langfuse_push_fails(**_kwargs: Any) -> None:
        raise RuntimeError("langfuse push failed")

    with patch.object(chat_mod, "_record_langfuse_output", langfuse_push_fails):
        added, _ = await _turn(client, WELL_FORMED)

    assert len(added) == 1
    assert (added[0].prompt_tokens, added[0].completion_tokens) == (
        WELL_FORMED["prompt_tokens"],
        WELL_FORMED["completion_tokens"],
    )
