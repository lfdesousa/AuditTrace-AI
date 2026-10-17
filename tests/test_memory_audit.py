"""Unit tests for ``services/memory_audit.py`` (ADR-062 §5 / WU-A4).

"Reading the recorder is recorded": every ``/memory/*`` read/list/write/
delete emits a first-class, owner-scoped ``event_class="memory_access"``
audit row, reconstructable via ``GET /interactions``. These tests exercise
the shared emit helper directly; ``tests/test_memory_routes.py`` proves the
route-level wiring (the falsifiable "GET /memory/episodic produces a
matching audit row" gate) and the read=fail-open / write=fail-closed
discipline.
"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi import BackgroundTasks

from audittrace.services.memory_audit import (
    MEMORY_AUDIT_EVENT_CLASS,
    MEMORY_AUDIT_PROJECT,
    MEMORY_AUDIT_SOURCE,
    emit_memory_audit_event,
    emit_memory_audit_event_background,
    schedule_read_audit,
)


class TestBuildRowNullOwnerGuard:
    """ADR-058 invariant — never persist an audit row with a NULL owner."""

    async def test_missing_user_id_raises(self, client, user_context) -> None:
        empty_user = replace(user_context, user_id="")
        with pytest.raises(ValueError, match="refusing to persist"):
            await emit_memory_audit_event(
                user=empty_user, op="read", layer="episodic", key="x.md"
            )


class TestEmitMemoryAuditEvent:
    async def test_persists_row_queryable_via_interactions(
        self, client, user_context
    ) -> None:
        await emit_memory_audit_event(
            user=user_context,
            op="read",
            layer="episodic",
            key="ADR-x.md",
        )
        r = client.get(
            "/interactions", params={"event_class": MEMORY_AUDIT_EVENT_CLASS}
        )
        assert r.status_code == 200
        rows = r.json()["interactions"]
        matches = [
            row
            for row in rows
            if row["question"] == "op=read layer=episodic key=ADR-x.md"
        ]
        assert len(matches) == 1
        row = matches[0]
        assert row["project"] == MEMORY_AUDIT_PROJECT
        assert row["source"] == MEMORY_AUDIT_SOURCE
        assert row["event_class"] == MEMORY_AUDIT_EVENT_CLASS
        assert row["status"] == "success"
        assert row["user_id"] == user_context.user_id
        detail = json.loads(row["error_detail"])
        assert detail == {
            "op": "read",
            "layer": "episodic",
            "collection": None,
            "key": "ADR-x.md",
            "tier": "corpus",
        }

    async def test_collection_and_detail_extra_are_recorded(
        self, client, user_context
    ) -> None:
        await emit_memory_audit_event(
            user=user_context,
            op="delete",
            layer="semantic",
            collection="decisions",
            key="doc-1",
            detail_extra={"hard": True},
        )
        r = client.get(
            "/interactions", params={"event_class": MEMORY_AUDIT_EVENT_CLASS}
        )
        rows = r.json()["interactions"]
        row = next(row for row in rows if row["question"].startswith("op=delete"))
        detail = json.loads(row["error_detail"])
        assert detail["collection"] == "decisions"
        assert detail["hard"] is True

    async def test_outcome_failed_sets_failure_class(
        self, client, user_context
    ) -> None:
        await emit_memory_audit_event(
            user=user_context,
            op="write",
            layer="procedural",
            key="fails.md",
            outcome="failed",
        )
        r = client.get(
            "/interactions",
            params={"event_class": MEMORY_AUDIT_EVENT_CLASS, "status": "failed"},
        )
        rows = r.json()["interactions"]
        row = next(row for row in rows if row["question"].startswith("op=write"))
        assert row["failure_class"] == "memory_access_failed"

    async def test_trace_id_passthrough(self, client, user_context) -> None:
        await emit_memory_audit_event(
            user=user_context,
            op="read",
            layer="episodic",
            key="traced.md",
            trace_id="a" * 32,
        )
        r = client.get(
            "/interactions", params={"event_class": MEMORY_AUDIT_EVENT_CLASS}
        )
        row = next(
            row
            for row in r.json()["interactions"]
            if row["question"] == "op=read layer=episodic key=traced.md"
        )
        assert row["trace_id"] == "a" * 32


class TestEmitMemoryAuditEventBackground:
    """The fail-open wrapper used for the READ path."""

    async def test_success_persists_like_the_foreground_call(
        self, client, user_context
    ) -> None:
        await emit_memory_audit_event_background(
            user=user_context, op="list", layer="procedural"
        )
        r = client.get(
            "/interactions", params={"event_class": MEMORY_AUDIT_EVENT_CLASS}
        )
        rows = r.json()["interactions"]
        assert any(row["question"] == "op=list layer=procedural key=-" for row in rows)

    async def test_failure_is_swallowed_and_logged(
        self, client, user_context, monkeypatch
    ) -> None:
        """A read must never fail because its audit-store write failed —
        the exception is caught here, not propagated.

        Asserts via a patched module logger rather than ``caplog``: some
        other test in the full suite calls ``setup_logging`` (which does
        ``root.handlers.clear()``), detaching pytest's LogCaptureHandler —
        the same order-fragility documented in
        ``test_scan_verdict_consumer.py``. Patching the module logger
        directly is hermetic and survives test ordering.
        """

        async def _boom(**_kwargs: object) -> None:
            raise RuntimeError("audit store unavailable")

        monkeypatch.setattr(
            "audittrace.services.memory_audit.emit_memory_audit_event", _boom
        )
        with patch("audittrace.services.memory_audit.logger") as mock_logger:
            # Must not raise.
            await emit_memory_audit_event_background(
                user=user_context, op="read", layer="episodic", key="x.md"
            )
        assert mock_logger.exception.call_count == 1
        message = mock_logger.exception.call_args[0][0]
        assert "memory_audit.read_emit_failed" in message
        assert "NOT reconstructable" in message


class TestScheduleReadAudit:
    async def test_schedules_the_background_wrapper(self, user_context) -> None:
        bg = BackgroundTasks()
        schedule_read_audit(
            bg, user=user_context, op="read", layer="episodic", key="k.md"
        )
        assert len(bg.tasks) == 1
        task = bg.tasks[0]
        assert task.func is emit_memory_audit_event_background
        assert task.kwargs["op"] == "read"
        assert task.kwargs["layer"] == "episodic"
        assert task.kwargs["key"] == "k.md"

    async def test_runs_and_persists_when_invoked(self, client, user_context) -> None:
        bg = BackgroundTasks()
        schedule_read_audit(
            bg, user=user_context, op="read", layer="semantic", collection="decisions"
        )
        await bg()
        r = client.get(
            "/interactions", params={"event_class": MEMORY_AUDIT_EVENT_CLASS}
        )
        rows = r.json()["interactions"]
        assert any(row["question"] == "op=read layer=semantic key=-" for row in rows)
