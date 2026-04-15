"""Conversational memory service — Layer 3 of the 4-layer memory architecture.

ADR-018: 4-layer memory port.
ADR-020: PostgreSQL replaces SQLite — no more file-based databases.

Stores and retrieves session history from PostgreSQL via SQLAlchemy ORM.
Each session has a project, date, summary, and key decision points.
Provides continuity across conversations.

DESIGN §15 Phase 2: every method takes ``user_context: UserContext`` as
the first positional argument. ``load_sessions`` filters by
``user_context.user_id`` unconditionally and ``save_session`` persists
it on the ``SessionRecord`` row. Conversations are inherently personal
so there is **no admin bypass** at this layer — admins querying their
own sessions is the correct semantics.
"""

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session, sessionmaker

from sovereign_memory.db.models import InteractionRecord, SessionRecord
from sovereign_memory.identity import UserContext
from sovereign_memory.logging_config import log_call

logger = logging.getLogger(__name__)


class ConversationalService(ABC):
    """Abstract conversational memory service — session history."""

    @abstractmethod
    def load_sessions(
        self, user_context: UserContext, project: str, n: int = 5
    ) -> list[dict[str, Any]]:
        """Load the N most recent sessions for a project."""

    @abstractmethod
    def save_session(
        self,
        user_context: UserContext,
        project: str,
        summary: str,
        key_points: list[str] | None = None,
    ) -> str:
        """Save a session summary. Returns session ID."""

    @abstractmethod
    def as_context(self, user_context: UserContext, project: str) -> str:
        """Return recent sessions formatted as context string."""


class PostgresConversationalService(ConversationalService):
    """PostgreSQL-backed conversational memory service via SQLAlchemy ORM."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    @log_call(logger=logger)
    def load_sessions(
        self, user_context: UserContext, project: str, n: int = 5
    ) -> list[dict[str, Any]]:
        """Load the N most recent sessions — hybrid real + synthetic (ADR-030 Part 1).

        Returns up to ``n`` rows ordered by recency. Real rows come from
        the ``sessions`` table (written by the background summariser).
        Synthetic rows come from distinct ``interactions.session_id``
        values not yet summarised; each carries a draft summary built
        from the session's first question and last answer. Synthetic
        rows are flagged with ``synthetic=True`` so callers can label
        them for the LLM as "draft, not yet finalised".

        The contract assumes ``SessionRecord.id == InteractionRecord.session_id``
        for sessions produced by the summariser — that's how the two
        tables join without a side table.
        """
        session = self._session_factory()
        try:
            # Step 1 — real summaries, already ordered and limited.
            real_rows = (
                session.query(SessionRecord)
                .filter(SessionRecord.project == project)
                .filter(SessionRecord.user_id == user_context.user_id)
                .order_by(SessionRecord.date.desc())
                .limit(n)
                .all()
            )
            real_ids: set[str] = {row.id for row in real_rows}
            real: list[dict[str, Any]] = [
                {
                    "id": row.id,
                    "date": row.date,
                    "summary": row.summary,
                    "key_points": json.loads(row.key_points or "[]"),
                    "synthetic": False,
                }
                for row in real_rows
            ]

            # Step 2 — eligible session_ids from interactions (not yet
            # summarised). Ordered by most recent interaction first, at
            # most ``n`` — never need more since we truncate after merge.
            eligible_q = (
                session.query(
                    InteractionRecord.session_id,
                    func.max(InteractionRecord.timestamp).label("last_ts"),
                )
                .filter(InteractionRecord.project == project)
                .filter(InteractionRecord.user_id == user_context.user_id)
                .filter(InteractionRecord.session_id.isnot(None))
                .group_by(InteractionRecord.session_id)
            )
            if real_ids:
                eligible_q = eligible_q.filter(
                    InteractionRecord.session_id.notin_(real_ids)
                )
            eligible = (
                eligible_q.order_by(func.max(InteractionRecord.timestamp).desc())
                .limit(n)
                .all()
            )

            # Step 3 — pull the anchor interactions (first Q, last A) for
            # each eligible session in a single query; group in Python.
            synthetic: list[dict[str, Any]] = []
            if eligible:
                eligible_ids = [row.session_id for row in eligible]
                anchor_rows = (
                    session.query(InteractionRecord)
                    .filter(InteractionRecord.project == project)
                    .filter(InteractionRecord.user_id == user_context.user_id)
                    .filter(InteractionRecord.session_id.in_(eligible_ids))
                    .order_by(InteractionRecord.session_id, InteractionRecord.timestamp)
                    .all()
                )
                by_session: dict[str, list[InteractionRecord]] = {}
                for row in anchor_rows:
                    # session_id is nullable in the schema but the
                    # eligibility query filters nulls out, so only
                    # non-null ids reach here.
                    if row.session_id is None:
                        continue
                    by_session.setdefault(row.session_id, []).append(row)
                for sid, last_ts in ((e.session_id, e.last_ts) for e in eligible):
                    turns = by_session.get(sid) or []
                    if not turns:
                        continue
                    first_q = (turns[0].question or "")[:200]
                    last_a = (turns[-1].answer or "")[:400]
                    summary = f"Q: {first_q}\n\nA: {last_a}"
                    synthetic.append(
                        {
                            "id": sid,
                            "date": last_ts,
                            "summary": summary,
                            "key_points": [],
                            "synthetic": True,
                        }
                    )

            # Step 4 — merge and truncate. ISO-format strings compare
            # chronologically, so ``date desc`` is correct for both shapes.
            combined = real + synthetic
            combined.sort(key=lambda d: d.get("date") or "", reverse=True)
            return combined[:n]
        finally:
            session.close()

    @log_call(logger=logger)
    def save_session(
        self,
        user_context: UserContext,
        project: str,
        summary: str,
        key_points: list[str] | None = None,
    ) -> str:
        """Save a session summary to PostgreSQL. Returns session ID.

        The row is stamped with ``user_context.user_id`` so subsequent
        ``load_sessions`` calls for the same user see it.
        """
        session = self._session_factory()
        try:
            # Microsecond precision keeps row ids unique within a process
            # when two saves (e.g. two users in a test) land in the same second.
            session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            record = SessionRecord(
                id=session_id,
                project=project,
                date=datetime.now().isoformat(),
                summary=summary,
                key_points=json.dumps(key_points or []),
                model="sovereign-memory",
                user_id=user_context.user_id,
            )
            session.add(record)
            session.commit()
            return session_id
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @log_call(logger=logger)
    def as_context(self, user_context: UserContext, project: str) -> str:
        """Return recent sessions formatted as a context section."""
        sessions = self.load_sessions(user_context, project, n=3)
        if not sessions:
            return ""
        lines = ["## Recent Sessions"]
        for s in sessions:
            date = s["date"][:10] if s["date"] else "unknown"
            label = "Session (draft)" if s.get("synthetic") else "Session"
            lines.append(f"\n**{label} {date}:** {s['summary'][:200]}")
            if s["key_points"]:
                for kp in s["key_points"]:
                    lines.append(f"- {kp}")
        return "\n".join(lines)


class MockConversationalService(ConversationalService):
    """Mock conversational service for unit testing.

    Honours the Phase 2 per-user contract so tests that stub this layer
    can't accidentally leak rows across users.
    """

    def __init__(self) -> None:
        self._sessions: list[dict[str, Any]] = []

    @log_call(logger=logger)
    def load_sessions(
        self, user_context: UserContext, project: str, n: int = 5
    ) -> list[dict[str, Any]]:
        filtered = [
            {**s, "synthetic": s.get("synthetic", False)}
            for s in self._sessions
            if s["project"] == project and s["user_id"] == user_context.user_id
        ]
        return filtered[-n:]

    @log_call(logger=logger)
    def save_session(
        self,
        user_context: UserContext,
        project: str,
        summary: str,
        key_points: list[str] | None = None,
    ) -> str:
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._sessions.append(
            {
                "id": session_id,
                "project": project,
                "date": datetime.now().isoformat(),
                "summary": summary,
                "key_points": key_points or [],
                "user_id": user_context.user_id,
            }
        )
        return session_id

    @log_call(logger=logger)
    def as_context(self, user_context: UserContext, project: str) -> str:
        sessions = self.load_sessions(user_context, project, n=3)
        if not sessions:
            return ""
        lines = ["## Recent Sessions"]
        for s in sessions:
            lines.append(f"\n**Session:** {s['summary'][:200]}")
        return "\n".join(lines)

    def reset(self) -> None:
        """Clear all sessions."""
        self._sessions.clear()
