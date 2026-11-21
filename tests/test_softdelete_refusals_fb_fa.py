"""#472 live findings F-b and F-a (operator rulings, 2026-11-21).

F-b: a refused re-fold (the 409 of an admin, the 422 of everyone else) wrote a
``write`` audit row with ``status="success"``. The row now says ``failed`` (the
audit schema's existing status / failure_class vocabulary: no column, no
migration), and says nothing else about why, so the owner who reads it back
learns no more than the 422 body gave.

F-a: a refused read of a soft-deleted document answered the never-existed 404
and left no log line carrying the trace. It now leaves exactly one structured
line (trace_id, user_id, a neutral code) and the 404 body is unchanged.

Every assertion goes through the real routes of ``create_app()``; statuses are
asserted exactly. Same harness as ``tests/test_softdelete_direct_read_a2.py``.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from audittrace.db.models import InteractionRecord
from audittrace.dependencies import get_postgres_factory
from audittrace.services.memory_audit import MEMORY_AUDIT_EVENT_CLASS
from tests import test_softdelete_direct_read_a1 as a1
from tests._log_trace_id_support import (
    bootstrap,
    in_span,
    isolated_logging_state,
    log_lines,
)
from tests.memory_truncation_support import (
    USER,
    distinct_body,
    memory_client,
    read_whole,
)
from tests.memory_truncation_support_a1 import (
    STEP,
    as_admin,
    fold_before_the_stamp,
    manifest,
)
from tests.test_softdelete_direct_read_a1 import (
    CANARY,
    SOURCES,
    _delete_source,
    _fold_source,
)
from tests.test_softdelete_direct_read_a2 import ADMIN_REFOLD_409, _refold, _stored

tool_cache = a1.tool_cache
_real_read_tools = a1._real_read_tools

TRACE_SHAPE = re.compile(r"^[0-9a-f]{32}$")
REFUSED = "memory.read refused"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


@pytest.fixture
def logs() -> Iterator[io.StringIO]:
    """The production handler + filter, stdout bound to a buffer, installed
    AFTER ``create_app()`` (which configures logging itself)."""
    with isolated_logging_state():
        with memory_client() as c:
            a1_client.append(c)
            yield bootstrap()
            a1_client.pop()


a1_client: list[TestClient] = []


def _index_write_rows() -> list[tuple[str, str | None, dict[str, Any]]]:
    """``(status, failure_class, detail)`` of every single-file index write row."""

    async def read() -> list[tuple[str, str | None, dict[str, Any]]]:
        factory = get_postgres_factory().get_session_factory()
        async with factory() as db:
            rows = await db.execute(
                select(
                    InteractionRecord.status,
                    InteractionRecord.failure_class,
                    InteractionRecord.error_detail,
                )
                .where(InteractionRecord.event_class == MEMORY_AUDIT_EVENT_CLASS)
                .order_by(InteractionRecord.id)
            )
            out = [(s, f, json.loads(d)) for (s, f, d) in rows.all()]
        return [r for r in out if r[2].get("mode") == "single_file"]

    return asyncio.run(read())


class TestARefusedRefoldIsNeverRecordedAsSuccess:
    """F-b. rows 35553/35554 on the v1.34.0-rc1 deploy: status success, 0 chunks."""

    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_owner_refold_of_a_deleted_source_is_a_422_with_a_failed_row(
        self, client: TestClient, layer: str
    ) -> None:
        name, collection, _tool, _detail = SOURCES[layer]
        _fold_source(client, layer, collection)
        stored = _stored(client, layer)
        _delete_source(client, layer)
        before = len(_index_write_rows())

        back = _refold(client, layer, name, collection, stored)

        assert back.status_code == 422
        (row,) = _index_write_rows()[before:]
        assert row[:2] == ("failed", "memory_access_failed")
        assert row[2]["total_chunks"] == 0

    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_the_refusal_row_is_the_row_of_a_key_that_never_existed(
        self, client: TestClient, layer: str
    ) -> None:
        """No-leak: the owner reads their own audit rows; the row of the refused
        re-fold must not tell a deleted source from one that never existed."""
        name, collection, _tool, _detail = SOURCES[layer]
        ghost = "z" * (len(name) - 3) + ".md"
        _fold_source(client, layer, collection)
        stored = _stored(client, layer)
        _delete_source(client, layer)
        before = len(_index_write_rows())

        _refold(client, layer, name, collection, stored)
        _refold(client, layer, ghost, collection, None)

        mine, theirs = _index_write_rows()[before:]
        assert mine[:2] == theirs[:2] == ("failed", "memory_access_failed")
        assert {**theirs[2], "key": mine[2]["key"]} == mine[2]

    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_the_admin_409_is_a_failed_row_too(
        self, client: TestClient, layer: str
    ) -> None:
        name, collection, _tool, _detail = SOURCES[layer]
        _fold_source(client, layer, collection)
        stored = _stored(client, layer)
        _delete_source(client, layer)
        before = len(_index_write_rows())

        with as_admin(client):
            back = _refold(client, layer, name, collection, stored)

        assert back.status_code == 409 and back.json() == ADMIN_REFOLD_409
        (row,) = _index_write_rows()[before:]
        assert row[:2] == ("failed", "memory_access_failed")

    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_control_a_live_refold_is_still_a_success_row(
        self, client: TestClient, layer: str
    ) -> None:
        name, collection, _tool, _detail = SOURCES[layer]
        _fold_source(client, layer, collection)
        stored = _stored(client, layer)
        before = len(_index_write_rows())

        again = _refold(client, layer, name, collection, stored)

        assert again.status_code == 200, again.text
        (row,) = _index_write_rows()[before:]
        assert row[0] == "success" and row[1] is None
        assert row[2]["total_chunks"] > 0


def _reads(client: TestClient, layer: str) -> dict[str, Any]:
    """Every route that answers the refused-read 404, for the deleted source of
    *layer* and the fold the source left behind."""
    name, collection, _tool, _detail = SOURCES[layer]
    chunk0 = a1.m._doc_id(collection, name, 0)
    return {
        "source": lambda: client.get(f"/memory/{layer}/{name}"),
        "chunk": lambda: client.get(f"/memory/semantic/{collection}/{chunk0}"),
        "whole": lambda: read_whole(client, collection, name),
    }


def _refused_lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [ln for ln in log_lines(buf) if REFUSED in ln["message"]]


class TestARefusedReadLeavesOneTracedLogLine:
    """F-a. The 404 stays byte-equal to never-existed; the server-side line
    carries trace_id, user_id and a neutral code, and nothing about why."""

    @pytest.mark.parametrize("route", ["source", "chunk", "whole"])
    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_exactly_one_line_with_trace_and_user(
        self, logs: io.StringIO, layer: str, route: str
    ) -> None:
        client = a1_client[-1]
        _name, collection, _tool, _detail = SOURCES[layer]
        _fold_source(client, layer, collection)
        _delete_source(client, layer)
        before = len(_refused_lines(logs))

        with in_span():
            back = _reads(client, layer)[route]()

        assert back.status_code == 404
        (line,) = _refused_lines(logs)[before:]
        assert TRACE_SHAPE.match(line["trace_id"])
        assert line["user_id"] == USER
        assert line["code"] == "not_found"
        assert line["level"] == "INFO"
        # nothing in the line says the document existed or was deleted
        text = json.dumps(line).lower()
        for word in ("delet", "tombstone", CANARY, "soft"):
            assert word not in text

    def test_a_whole_read_refused_on_a_gathered_chunk_logs_one_line_too(
        self, logs: io.StringIO
    ) -> None:
        """The second refusal site: an UNSTAMPED fold whose chunk 0 row was
        tombstoned before the build. The entry chunk names no tombstone; the
        whole read finds it among the gathered chunks."""
        client = a1_client[-1]
        text = CANARY + "\n" + distinct_body(4 * STEP)
        count = fold_before_the_stamp("decisions", "old.md", text)
        ids = [a1.m._doc_id("decisions", "old.md", i) for i in range(count)]
        key = a1.m._semantic_key("decisions", ids[0])
        asyncio.run(
            manifest().record_create(
                layer="semantic", key=key, title="old.md", size_bytes=1, user_id=USER
            )
        )
        asyncio.run(manifest().record_delete("semantic", key, USER))
        before = len(_refused_lines(logs))

        with in_span():
            whole = client.get(
                f"/memory/semantic/decisions/{ids[1]}",
                params={"whole_document": "true"},
            )

        assert whole.status_code == 404
        (line,) = _refused_lines(logs)[before:]
        assert TRACE_SHAPE.match(line["trace_id"]) and line["user_id"] == USER

    @pytest.mark.parametrize("route", ["source", "chunk", "whole"])
    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_the_404_body_is_unchanged(
        self, client: TestClient, layer: str, route: str
    ) -> None:
        _name, collection, _tool, _detail = SOURCES[layer]
        _fold_source(client, layer, collection)
        _delete_source(client, layer)
        never = (
            client.get(f"/memory/{layer}/never-written.md")
            if route == "source"
            else client.get(f"/memory/semantic/{collection}/0000000000000000")
        )

        a1._same_reply(_reads(client, layer)[route](), never)

    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_no_line_for_a_key_that_never_existed_or_for_the_admin(
        self, logs: io.StringIO, layer: str
    ) -> None:
        client = a1_client[-1]
        name, collection, _tool, _detail = SOURCES[layer]
        _fold_source(client, layer, collection)
        _delete_source(client, layer)

        with in_span():
            assert client.get(f"/memory/{layer}/never-written.md").status_code == 404
            with as_admin(client):
                assert client.get(f"/memory/{layer}/{name}").status_code == 200

        assert _refused_lines(logs) == []
