"""SQLAlchemy ORM models for sovereign-memory-server.

ADR-020: PostgreSQL (production) and SQLite-in-memory (tests) via the
same declarative models.

ADR-026 §15 (2026-04-11): identity is delegated
to Keycloak. There is NO local users table — see §15.1 for the
mental model. ``user_id`` columns on ``interactions``, ``sessions``,
and ``tool_calls`` are plain VARCHAR(36) Keycloak ``sub`` claims with
no foreign-key constraint to a local users table (because no such
table exists).

The Phase 0 ``users`` / ``user_roles`` / ``pat_tokens`` tables were
dropped via Alembic migration 004 in the same refactor.

All schema changes are managed via Alembic migrations.
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid_str() -> str:
    """UUID4 as a 36-character string. Cross-database default."""
    import uuid

    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class SessionRecord(Base):
    """Conversational memory session — Layer 3 of the 4-layer architecture."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project: Mapped[str] = mapped_column(String, index=True)
    date: Mapped[str] = mapped_column(String)
    summary: Mapped[str] = mapped_column(Text)
    key_points: Mapped[str] = mapped_column(Text)  # JSON-encoded list
    model: Mapped[str] = mapped_column(String)
    # Keycloak ``sub`` claim — no FK because Keycloak owns the identity
    # store. Nullable until Phase 5 flips it after backfill + isolation
    # tests.
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)


class InteractionRecord(Base):
    """Audit trail — every question/answer pair with token counts."""

    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(String, index=True)
    source: Mapped[str] = mapped_column(String, default="unknown")
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    timestamp: Mapped[str] = mapped_column(String, index=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    # Keycloak ``sub`` claim — see SessionRecord.user_id docstring.
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)


class ToolCall(Base):
    """Audit row for a single memory tool invocation by an LLM.

    One row per tool call. Multiple rows per ``interactions`` row when
    the LLM calls more than one tool in a single chat completion. The
    combination of ``user_id`` + ``granted_scope`` answers "who was
    allowed to call what" under audit.

    ``user_id`` is a Keycloak ``sub`` claim (no FK to a local users
    table — see DESIGN §15). ``interaction_id`` keeps its FK to
    ``interactions`` because that table is owned by sovereign-memory-
    server.
    """

    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    interaction_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("interactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    args: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    granted_scope: Mapped[str] = mapped_column(String(255), nullable=False)
