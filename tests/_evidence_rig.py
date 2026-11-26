"""CONSOLE-AUDIT-EVIDENCE WU-A -- shared rig of the evidence tests (not a test module).

The two contract fixtures are the bytes frozen by the spec chain (A6 section 1); their
sha256 is asserted by ``test_evidence_fixture_pins.py`` before any test reads them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from audittrace.db.models import InteractionRecord as InteractionRow
from audittrace.db.models import ToolCall as ToolCallRow
from audittrace.dependencies import get_postgres_factory

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_V2 = FIXTURES / "console_evidence_v2.json"
FIXTURE_V3 = FIXTURES / "console_evidence_v3.json"
FIXTURE_V3_IMAGE = FIXTURES / "console_evidence_v3_image.json"
AUDIT_SCOPE = "audittrace:audit"


def load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_bytes())
    return loaded


def key_paths(node: Any, prefix: str = "") -> set[str]:
    """Every key path of a JSON document; list elements collapse to ``[]``."""
    paths: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{prefix}.{key}" if prefix else key
            paths.add(here)
            paths |= key_paths(value, here)
    elif isinstance(node, list):
        for item in node:
            paths |= key_paths(item, f"{prefix}[]")
    return paths


@contextmanager
def identity(sub: str, scope: str = AUDIT_SCOPE) -> Iterator[None]:
    """The REAL ``validate_jwt`` + ``require_user`` cold path for ``sub``."""
    with (
        patch("audittrace.auth.get_settings") as mock_settings,
        patch("audittrace.auth._get_jwks_keys") as mock_jwks,
        patch("audittrace.auth._decode_jwt_with_allowed_issuers") as mock_decode,
    ):
        mock_settings.return_value = MagicMock(auth_enabled=True, auth_required=True)
        mock_jwks.return_value = ["fake-key"]
        mock_decode.return_value = {"sub": sub, "scope": scope}
        yield


def bearer(sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer token-for-{sub}"}


def get(client: Any, path: str, sub: str, scope: str = AUDIT_SCOPE) -> Any:
    with identity(sub, scope):
        return client.get(path, headers=bearer(sub))


def row_from_fixture(fixture: dict[str, Any], **over: Any) -> InteractionRow:
    """The stored row a fixture describes (hash, ids and all)."""
    served = fixture["interaction"]
    media = served.get("input_media")  # fixture v2 predates the key
    fields: dict[str, Any] = {
        "id": served["id"],
        "project": served["project"],
        "source": served["source"],
        "question": served["question"],
        "answer": served["answer"],
        "prompt_tokens": served["prompt_tokens"],
        "completion_tokens": served["completion_tokens"],
        "timestamp": served["timestamp"],
        "session_id": served["session_id"],
        "model": served["model"],
        "user_id": served["user_id"],
        "status": served["status"],
        "failure_class": served["failure_class"],
        "error_detail": served["error_detail"],
        "duration_ms": served["duration_ms"],
        "trace_id": served["trace_id"],
        "event_class": served["event_class"],
        "created_at": datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC),
        "content_hash": fixture["integrity"]["content_hash"],
        "client_message_id": served["client_message_id"],
        "input_media": (
            None
            if media is None
            else json.dumps(media, sort_keys=True, separators=(",", ":"))
        ),
    }
    fields.update(over)
    return InteractionRow(**fields)


def tool_call_from_fixture(
    fixture: dict[str, Any], interaction_id: int, user_id: str
) -> ToolCallRow:
    tc = fixture["tool_calls"][0]
    return ToolCallRow(
        id=tc["id"],
        interaction_id=interaction_id,
        user_id=user_id,
        agent_type="opencode",
        tool_name=tc["tool_name"],
        args=tc["args"],
        result_summary="2 hits",
        error=tc["error"],
        started_at=datetime(2026, 1, 1),
        duration_ms=tc["duration_ms"],
        granted_scope=tc["granted_scope"],
        recall_matches=tc["recall_matches"],
        recall_matches_truncated=tc["recall_matches_truncated"],
    )


async def seed(*rows: Any) -> None:
    session_factory = get_postgres_factory().get_session_factory()
    async with session_factory() as db:
        for row in rows:
            db.add(row)
        await db.commit()


async def all_rows() -> list[InteractionRow]:
    from sqlalchemy import select

    session_factory = get_postgres_factory().get_session_factory()
    async with session_factory() as db:
        return list((await db.execute(select(InteractionRow))).scalars().all())
