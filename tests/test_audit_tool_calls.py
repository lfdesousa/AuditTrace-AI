"""Tests for ``GET /interactions/{id}/tool-calls`` (#363 / RF-10).

RF-10, found 2026-07-20: ``tool_calls`` was written on every model tool
invocation but reachable through no API surface, so an auditor holding a
scoped JWT could not ask the system of record how many tools a given
decision invoked. Cross-store corroboration was therefore limited to the
two observability stores (Langfuse, Tempo) — both fed by the same
process, so they could agree with each other but neither could be checked
against Postgres.

**The property under test is "zero versus missing".** A refused tool call
is recorded at dispatch with ``granted_scope=""`` and an ``error``, so an
empty list means the model invoked nothing rather than that the record was
lost. Several tests below exist purely to pin that distinction.

**Isolation is NOT proven here.** These run against the in-memory SQLite
factory, which has no row-level security — see
``feedback_unit_tests_miss_rls``. Cross-user isolation for the exact query
shape this endpoint issues is covered in ``test_rls_isolation.py``
against a real Postgres with the migration-005 policies applied.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from audittrace.db.models import InteractionRecord as InteractionRow
from audittrace.db.models import ToolCall as ToolCallRow
from audittrace.dependencies import get_postgres_factory
from audittrace.routes.audit import _tool_call_to_dict

_UID = "sentinel-user"


async def _seed(interaction_id: int, *tool_calls: ToolCallRow) -> None:
    """Insert one interaction plus its tool-call rows.

    ``tool_calls`` has a NOT NULL FK to ``interactions``, so the parent
    row must exist first — the same ordering the chat path relies on when
    it flushes ``PendingToolCall`` records after the parent interaction.
    """
    pg = get_postgres_factory()
    session_factory = pg.get_session_factory()
    async with session_factory() as db:
        db.add(
            InteractionRow(
                id=interaction_id,
                project="P",
                question="q",
                answer="a",
                user_id=_UID,
                timestamp=datetime.now().isoformat(),
            )
        )
        for tc in tool_calls:
            db.add(tc)
        await db.commit()


def _tool_call(
    interaction_id: int,
    tool_name: str,
    *,
    call_id: str,
    granted_scope: str = "memory:semantic:read",
    error: str | None = None,
    started_at: datetime | None = None,
) -> ToolCallRow:
    return ToolCallRow(
        id=call_id,
        interaction_id=interaction_id,
        user_id=_UID,
        agent_type="opencode",
        tool_name=tool_name,
        args='{"query": "ADR-025"}',
        result_summary=None if error else "3 hits",
        error=error,
        started_at=started_at or datetime(2026, 7, 20, 10, 44, 56),
        duration_ms=42,
        granted_scope=granted_scope,
    )


class TestToolCallSerialisation:
    """Unit coverage for the row -> dict helper."""

    def test_all_columns_surfaced(self) -> None:
        row = _tool_call(1, "recall_semantic", call_id="tc-1")
        d = _tool_call_to_dict(row)
        assert d["id"] == "tc-1"
        assert d["interaction_id"] == 1
        assert d["tool_name"] == "recall_semantic"
        assert d["args"] == '{"query": "ADR-025"}'
        assert d["granted_scope"] == "memory:semantic:read"
        assert d["duration_ms"] == 42

    def test_started_at_normalised_to_iso(self) -> None:
        # started_at is a real DateTime here (unlike interactions.timestamp
        # which is stored as text), so it must not leak a bare object.
        row = _tool_call(1, "t", call_id="tc-1")
        assert _tool_call_to_dict(row)["started_at"] == "2026-07-20T10:44:56"

    def test_started_at_none_survives(self) -> None:
        row = _tool_call(1, "t", call_id="tc-1")
        row.started_at = None  # type: ignore[assignment]
        assert _tool_call_to_dict(row)["started_at"] is None


class TestListToolCalls:
    @pytest.mark.asyncio
    async def test_returns_recorded_calls(self, client) -> None:
        await _seed(
            101,
            _tool_call(101, "recall_semantic", call_id="tc-a"),
            _tool_call(101, "recall_episodic", call_id="tc-b"),
        )
        r = client.get("/interactions/101/tool-calls")
        assert r.status_code == 200
        body = r.json()
        assert body["interaction_id"] == 101
        assert body["total"] == 2
        assert {c["tool_name"] for c in body["tool_calls"]} == {
            "recall_semantic",
            "recall_episodic",
        }

    @pytest.mark.asyncio
    async def test_refusal_is_recorded_not_omitted(self, client) -> None:
        """The RF-10 property: a DENIED call is still a row.

        Without this, an auditor cannot tell "the model was refused" from
        "the model never asked" — which is exactly the ambiguity the
        endpoint exists to remove.
        """
        await _seed(
            102,
            _tool_call(
                102,
                "recall_semantic",
                call_id="tc-denied",
                granted_scope="",
                error="scope denied",
            ),
        )
        body = client.get("/interactions/102/tool-calls").json()
        assert body["total"] == 1
        call = body["tool_calls"][0]
        assert call["granted_scope"] == ""
        assert call["error"] == "scope denied"

    @pytest.mark.asyncio
    async def test_genuine_zero_returns_empty_list(self, client) -> None:
        """An interaction that invoked nothing returns [] — not a 404.

        Paired with the refusal test above, this is what makes zero
        distinguishable from missing.
        """
        await _seed(103)
        body = client.get("/interactions/103/tool-calls").json()
        assert body == {"interaction_id": 103, "tool_calls": [], "total": 0}

    @pytest.mark.asyncio
    async def test_unknown_interaction_is_not_an_existence_oracle(self, client) -> None:
        """Unknown id returns an empty 200, never a 404.

        Distinguishing "does not exist" from "exists but is not yours"
        would let a caller probe for the existence of other users' rows.
        Both cases must look identical from outside.
        """
        r = client.get("/interactions/999999/tool-calls")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_only_the_requested_interactions_calls_are_returned(
        self, client
    ) -> None:
        await _seed(104, _tool_call(104, "recall_semantic", call_id="tc-104"))
        await _seed(105, _tool_call(105, "recall_episodic", call_id="tc-105"))
        body = client.get("/interactions/104/tool-calls").json()
        assert body["total"] == 1
        assert body["tool_calls"][0]["id"] == "tc-104"

    @pytest.mark.asyncio
    async def test_ordered_by_start_time(self, client) -> None:
        await _seed(
            106,
            _tool_call(
                106,
                "second",
                call_id="tc-2",
                started_at=datetime(2026, 7, 20, 10, 0, 2),
            ),
            _tool_call(
                106,
                "first",
                call_id="tc-1",
                started_at=datetime(2026, 7, 20, 10, 0, 1),
            ),
        )
        body = client.get("/interactions/106/tool-calls").json()
        assert [c["tool_name"] for c in body["tool_calls"]] == ["first", "second"]

    def test_audit_store_unavailable_returns_503(self, client, monkeypatch) -> None:
        """A missing PostgresFactory is a 503, not a 500 traceback."""
        import audittrace.routes.audit as audit_mod

        def _boom() -> None:
            raise RuntimeError("not registered")

        monkeypatch.setattr(audit_mod, "get_postgres_factory", _boom)
        r = client.get("/interactions/1/tool-calls")
        assert r.status_code == 503
        assert r.json()["detail"] == "Audit store unavailable"
