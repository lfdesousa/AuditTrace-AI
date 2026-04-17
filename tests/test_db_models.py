"""Tests for SQLAlchemy ORM models — ADR-020 + ADR-026 §15.

Validates SessionRecord, InteractionRecord, ToolCall declarative models:
columns, types, constraints, CRUD, JSON round-trip for key_points,
indexes. The user_id columns are plain VARCHAR(36) Keycloak ``sub``
strings — no FK to a local users table (Keycloak owns identity).
"""

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from audittrace.db.models import (
    Base,
    InteractionRecord,
    SessionRecord,
    ToolCall,
)


@pytest.fixture
def engine():
    """In-memory SQLite engine for SQLAlchemy ORM tests."""
    eng = create_engine("sqlite://", echo=False)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def db_session(engine) -> Session:
    """Fresh SQLAlchemy session, rolled back after each test."""
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    yield session
    session.rollback()
    session.close()


class TestSessionRecordSchema:
    def test_table_name(self):
        assert SessionRecord.__tablename__ == "sessions"

    def test_columns_exist(self, engine):
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("sessions")}
        # user_id (Phase 0 multi-user identity) and summarized_at
        # (ADR-030 Part 2 background summariser) are later additions.
        assert columns == {
            "id",
            "project",
            "date",
            "summary",
            "key_points",
            "model",
            "user_id",
            "summarized_at",
        }

    def test_primary_key_is_id(self, engine):
        inspector = inspect(engine)
        pk = inspector.get_pk_constraint("sessions")
        assert pk["constrained_columns"] == ["id"]

    def test_project_column_indexed(self, engine):
        inspector = inspect(engine)
        indexes = inspector.get_indexes("sessions")
        indexed_columns = {col for idx in indexes for col in idx["column_names"]}
        assert "project" in indexed_columns


class TestSessionRecordCRUD:
    def test_create_and_read(self, db_session: Session):
        record = SessionRecord(
            id="20260410_120000",
            project="AuditTrace",
            date="2026-04-10T12:00:00",
            summary="Test session",
            key_points=json.dumps(["point1", "point2"]),
            model="audittrace",
        )
        db_session.add(record)
        db_session.flush()

        loaded = db_session.get(SessionRecord, "20260410_120000")
        assert loaded is not None
        assert loaded.project == "AuditTrace"
        assert loaded.summary == "Test session"

    def test_json_key_points_round_trip(self, db_session: Session):
        points = ["ADR-020 accepted", "PostgreSQL factory ready"]
        record = SessionRecord(
            id="20260410_130000",
            project="AuditTrace",
            date="2026-04-10T13:00:00",
            summary="JSON test",
            key_points=json.dumps(points),
            model="audittrace",
        )
        db_session.add(record)
        db_session.flush()

        loaded = db_session.get(SessionRecord, "20260410_130000")
        assert json.loads(loaded.key_points) == points

    def test_query_by_project(self, db_session: Session):
        for i, project in enumerate(["ProjA", "ProjA", "ProjB"]):
            db_session.add(
                SessionRecord(
                    id=f"sess_{i}",
                    project=project,
                    date=f"2026-04-10T{10 + i}:00:00",
                    summary=f"Summary {i}",
                    key_points="[]",
                    model="audittrace",
                )
            )
        db_session.flush()

        results = (
            db_session.query(SessionRecord)
            .filter(SessionRecord.project == "ProjA")
            .all()
        )
        assert len(results) == 2

    def test_duplicate_id_raises(self, db_session: Session):
        record1 = SessionRecord(
            id="dup_id",
            project="P",
            date="d",
            summary="s",
            key_points="[]",
            model="m",
        )
        record2 = SessionRecord(
            id="dup_id",
            project="P2",
            date="d2",
            summary="s2",
            key_points="[]",
            model="m2",
        )
        db_session.add(record1)
        db_session.commit()
        db_session.add(record2)
        with pytest.raises(Exception):  # IntegrityError from DB constraint
            db_session.commit()
        db_session.rollback()


# ────────── Audit + identity-bearing tables (DESIGN §15) ────────────────────
# Identity is delegated to Keycloak; ``user_id`` columns are plain
# VARCHAR(36) Keycloak ``sub`` strings with no FK to a local users table.

# A fixed Keycloak-shaped sub used by every test that needs an attribution.
_TEST_SUB = "00000000-0000-0000-0000-000000000042"


class TestToolCallSchema:
    def test_table_name(self):
        assert ToolCall.__tablename__ == "tool_calls"

    def test_columns_exist(self, engine):
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("tool_calls")}
        assert columns == {
            "id",
            "interaction_id",
            "user_id",
            "agent_type",
            "tool_name",
            "args",
            "result_summary",
            "error",
            "started_at",
            "duration_ms",
            "granted_scope",
        }


class TestToolCallCRUD:
    def test_create_and_read(self, db_session: Session):
        # ``user_id`` is a Keycloak sub string — no FK to a local users table
        interaction = InteractionRecord(
            project="AuditTrace",
            source="opencode",
            question="loc count?",
            answer="12345",
            prompt_tokens=10,
            completion_tokens=5,
            timestamp="2026-04-11T10:00:00",
            user_id=_TEST_SUB,
        )
        db_session.add(interaction)
        db_session.flush()

        call = ToolCall(
            interaction_id=interaction.id,
            user_id=_TEST_SUB,
            agent_type="opencode",
            tool_name="recall_decisions",
            args='{"query": "kv cache"}',
            result_summary="2 ADRs returned",
            started_at=datetime.utcnow(),
            duration_ms=42,
            granted_scope="memory:read-decisions",
        )
        db_session.add(call)
        db_session.flush()

        loaded = db_session.get(ToolCall, call.id)
        assert loaded is not None
        assert loaded.tool_name == "recall_decisions"
        assert loaded.granted_scope == "memory:read-decisions"
        assert loaded.error is None
        assert loaded.user_id == _TEST_SUB


class TestInteractionRecordUserId:
    """``interactions.user_id`` holds a Keycloak sub string (no FK)."""

    def test_user_id_column_exists(self, engine):
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("interactions")}
        assert "user_id" in columns

    def test_no_foreign_key_to_users(self, engine):
        inspector = inspect(engine)
        fks = inspector.get_foreign_keys("interactions")
        for fk in fks:
            assert fk["referred_table"] != "users"

    def test_persist_with_user_id(self, db_session: Session):
        interaction = InteractionRecord(
            project="AuditTrace",
            source="opencode",
            question="hi",
            answer="hello",
            prompt_tokens=5,
            completion_tokens=2,
            timestamp="2026-04-11T10:00:00",
            user_id=_TEST_SUB,
        )
        db_session.add(interaction)
        db_session.flush()
        loaded = db_session.get(InteractionRecord, interaction.id)
        assert loaded.user_id == _TEST_SUB


class TestSessionRecordUserId:
    """``sessions.user_id`` holds a Keycloak sub string (no FK)."""

    def test_user_id_column_exists(self, engine):
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("sessions")}
        assert "user_id" in columns

    def test_no_foreign_key_to_users(self, engine):
        inspector = inspect(engine)
        fks = inspector.get_foreign_keys("sessions")
        for fk in fks:
            assert fk["referred_table"] != "users"

    def test_persist_with_user_id(self, db_session: Session):
        sess = SessionRecord(
            id="sess-with-user",
            project="AuditTrace",
            date="2026-04-11T10:00:00",
            summary="A summary",
            key_points="[]",
            model="m",
            user_id=_TEST_SUB,
        )
        db_session.add(sess)
        db_session.flush()
        loaded = db_session.get(SessionRecord, "sess-with-user")
        assert loaded.user_id == _TEST_SUB
