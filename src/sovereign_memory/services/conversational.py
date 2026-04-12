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

from sqlalchemy.orm import Session, sessionmaker

from sovereign_memory.db.models import SessionRecord
from sovereign_memory.identity import UserContext
from sovereign_memory.logging_config import log_call

logger = logging.getLogger(__name__)


class ConversationalService(ABC):
    """Abstract conversational memory service — session history."""

    @abstractmethod
    def load_sessions(
        self, user_context: UserContext, project: str, n: int = 5
    ) -> list[dict]:
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
    ) -> list[dict]:
        """Load the N most recent sessions for a project **for this user**."""
        session = self._session_factory()
        try:
            rows = (
                session.query(SessionRecord)
                .filter(SessionRecord.project == project)
                .filter(SessionRecord.user_id == user_context.user_id)
                .order_by(SessionRecord.date.desc())
                .limit(n)
                .all()
            )
            return [
                {
                    "id": row.id,
                    "date": row.date,
                    "summary": row.summary,
                    "key_points": json.loads(row.key_points or "[]"),
                }
                for row in rows
            ]
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
            lines.append(f"\n**Session {date}:** {s['summary'][:200]}")
            if s["key_points"]:
                for kp in s["key_points"]:
                    lines.append(f"- {kp}")
        return "\n".join(lines)


class MockConversationalService(ConversationalService):
    """Mock conversational service for unit testing.

    Honours the Phase 2 per-user contract so tests that stub this layer
    can't accidentally leak rows across users.
    """

    def __init__(self):
        self._sessions: list[dict] = []

    @log_call(logger=logger)
    def load_sessions(
        self, user_context: UserContext, project: str, n: int = 5
    ) -> list[dict]:
        filtered = [
            s
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

    def reset(self):
        """Clear all sessions."""
        self._sessions.clear()
