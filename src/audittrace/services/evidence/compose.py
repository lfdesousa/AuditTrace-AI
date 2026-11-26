"""Assemble the ``audittrace.evidence.v1`` response of the evidence routes."""

from __future__ import annotations

import json
from typing import Any

from audittrace.config import get_settings
from audittrace.services.evidence.integrity_view import integrity_block
from audittrace.services.evidence.visibility import answer_visibility, sources_block

CONTRACT = "audittrace.evidence.v1"


def _input_media(row: Any) -> dict[str, Any] | None:
    """The stored canonical JSON, returned parsed: type, size, sha256 per image part,
    never the bytes (the column never holds them)."""
    raw = getattr(row, "input_media", None)
    parsed: dict[str, Any] | None = json.loads(raw) if raw else None
    return parsed


def interaction_view(row: Any) -> dict[str, Any]:
    """The 20 keys of the contract's ``interaction`` object (``content_hash`` lives
    under ``integrity``)."""
    return {
        "id": row.id,
        "trace_id": row.trace_id,
        "session_id": row.session_id,
        "client_message_id": row.client_message_id,
        "source": row.source,
        "project": row.project,
        "model": row.model,
        "timestamp": row.timestamp,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "duration_ms": row.duration_ms,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "status": row.status,
        "failure_class": row.failure_class,
        "error_detail": row.error_detail,
        "event_class": row.event_class,
        "user_id": row.user_id,
        "question": row.question,
        "answer": row.answer,
        "input_media": _input_media(row),
    }


def tool_call_view(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "tool_name": row.tool_name,
        "granted_scope": row.granted_scope,
        "args": row.args,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "duration_ms": row.duration_ms,
        "error": row.error,
        "recall_matches": row.recall_matches,
        "recall_matches_truncated": row.recall_matches_truncated,
    }


def evidence_document(
    *,
    row: Any,
    tool_call_rows: list[Any],
    caller_id: str,
    lookup_by: str,
    lookup_value: str,
    candidates: int,
) -> dict[str, Any]:
    """The document for one interaction row the caller owns.

    ``user_id`` appears once, in ``interaction``, and is the caller's own sub: no other
    user's id is read into this document.
    """
    tool_calls = [tool_call_view(tc) for tc in tool_call_rows]
    return {
        "contract": CONTRACT,
        "lookup": {"by": lookup_by, "value": lookup_value, "candidates": candidates},
        "interaction": interaction_view(row),
        "integrity": integrity_block(row),
        "tool_calls": tool_calls,
        "sources": sources_block(tool_calls),
        "visibility": {
            "answer": answer_visibility(
                owner_is_caller=row.user_id == caller_id,
                readers_raw=get_settings().evidence_service_readers,
            )
        },
    }
