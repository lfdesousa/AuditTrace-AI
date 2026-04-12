"""Tests for PostgresConversationalService — ADR-020.

Mirrors test_conversational_service.py structure but uses SQLAlchemy ORM
via InMemoryPostgresFactory. No real PostgreSQL required.

Phase 2 (DESIGN §15): every service method takes ``user_context`` as the
first positional argument. Seed rows carry the sentinel ``user_id`` so
they're visible through the per-user filter; a dedicated test proves
cross-user isolation at the SQL layer.
"""

import json
from dataclasses import replace

import pytest
from sqlalchemy.orm import Session

from sovereign_memory.db.models import SessionRecord
from sovereign_memory.db.postgres import InMemoryPostgresFactory
from sovereign_memory.identity import SENTINEL_SUBJECT
from sovereign_memory.services.conversational import (
    ConversationalService,
    PostgresConversationalService,
)


@pytest.fixture
def pg_factory():
    """Fresh in-memory factory with tables created."""
    return InMemoryPostgresFactory()


@pytest.fixture
def pg_session(pg_factory) -> Session:
    """Session for seeding test data directly."""
    session = pg_factory.get_session_factory()()
    yield session
    session.close()


@pytest.fixture
def seeded_factory(pg_factory, pg_session):
    """Factory with pre-seeded session data. All rows carry the sentinel
    user_id so the default ``user_context`` fixture (sentinel-backed) can
    see them via the Phase 2 per-user filter."""
    sessions = [
        SessionRecord(
            id="20260331_213317",
            project="AuditTrace",
            date="2026-03-31T21:33:17",
            summary="KV cache compression enabled — q4_0 reduces memory 75%",
            key_points=json.dumps(["ADR-009 accepted", "Generation speed +21%"]),
            model="Qwen3.5-35B-A3B",
            user_id=SENTINEL_SUBJECT,
        ),
        SessionRecord(
            id="20260409_230504",
            project="AuditTrace",
            date="2026-04-09T23:05:04",
            summary="Phase 0 complete: sovereign-memory-server with Factory pattern",
            key_points=json.dumps(["DI container", "90%+ test coverage"]),
            model="Qwen3.5-35B-A3B",
            user_id=SENTINEL_SUBJECT,
        ),
        SessionRecord(
            id="20260410_150000",
            project="OtherProject",
            date="2026-04-10T15:00:00",
            summary="Unrelated project session",
            key_points=json.dumps([]),
            model="Qwen3.5-35B-A3B",
            user_id=SENTINEL_SUBJECT,
        ),
    ]
    for s in sessions:
        pg_session.add(s)
    pg_session.commit()
    return pg_factory


@pytest.fixture
def service(seeded_factory) -> PostgresConversationalService:
    """Service backed by seeded in-memory database."""
    return PostgresConversationalService(
        session_factory=seeded_factory.get_session_factory(),
    )


@pytest.fixture
def empty_service(pg_factory) -> PostgresConversationalService:
    """Service backed by empty in-memory database."""
    return PostgresConversationalService(
        session_factory=pg_factory.get_session_factory(),
    )


# ── PostgresConversationalService tests ───────────────────────────────────────


class TestPostgresConversationalService:
    def test_implements_abc(self, service):
        assert isinstance(service, ConversationalService)

    def test_load_sessions_for_project(self, service, user_context):
        sessions = service.load_sessions(user_context, "AuditTrace")
        assert len(sessions) == 2

    def test_load_sessions_filters_by_project(self, service, user_context):
        sessions = service.load_sessions(user_context, "OtherProject")
        assert len(sessions) == 1

    def test_load_sessions_empty_project(self, service, user_context):
        sessions = service.load_sessions(user_context, "NonExistent")
        assert sessions == []

    def test_load_sessions_respects_limit(self, service, user_context):
        sessions = service.load_sessions(user_context, "AuditTrace", n=1)
        assert len(sessions) == 1

    def test_load_sessions_ordered_by_date_desc(self, service, user_context):
        sessions = service.load_sessions(user_context, "AuditTrace")
        dates = [s["date"] for s in sessions]
        assert dates == sorted(dates, reverse=True)

    def test_load_sessions_content(self, service, user_context):
        sessions = service.load_sessions(user_context, "AuditTrace")
        summaries = [s["summary"] for s in sessions]
        assert any("KV cache" in s for s in summaries)
        assert any("Phase 0" in s for s in summaries)

    def test_load_sessions_includes_key_points(self, service, user_context):
        sessions = service.load_sessions(user_context, "AuditTrace")
        for s in sessions:
            assert "key_points" in s
            assert isinstance(s["key_points"], list)

    def test_save_session_creates_record(self, empty_service, user_context):
        session_id = empty_service.save_session(
            user_context,
            "AuditTrace",
            "Test save",
            ["point1"],
        )
        assert session_id is not None
        sessions = empty_service.load_sessions(user_context, "AuditTrace")
        assert len(sessions) == 1
        assert sessions[0]["summary"] == "Test save"

    def test_save_session_persists_user_id(self, empty_service, user_context):
        """Phase 2 write-side contract: save_session persists
        ``user_context.user_id`` on the SessionRecord row."""
        empty_service.save_session(user_context, "P", "Summary", ["k1"])
        # Read back via raw query to see the column value, not the dict slice.
        from sqlalchemy.orm import Session as _Session

        sess: _Session = empty_service._session_factory()
        try:
            row = sess.query(SessionRecord).filter(SessionRecord.project == "P").one()
            assert row.user_id == user_context.user_id
        finally:
            sess.close()

    def test_save_session_persists(self, service, user_context):
        service.save_session(user_context, "AuditTrace", "New session", ["k1", "k2"])
        sessions = service.load_sessions(user_context, "AuditTrace")
        assert len(sessions) == 3  # 2 existing + 1 new

    def test_save_session_key_points_default(self, empty_service, user_context):
        empty_service.save_session(user_context, "P", "Summary")
        sessions = empty_service.load_sessions(user_context, "P")
        assert sessions[0]["key_points"] == []

    def test_as_context_returns_formatted_string(self, service, user_context):
        ctx = service.as_context(user_context, "AuditTrace")
        assert "Recent Sessions" in ctx
        assert "KV cache" in ctx or "Phase 0" in ctx

    def test_as_context_empty_for_missing_project(self, service, user_context):
        ctx = service.as_context(user_context, "NonExistent")
        assert ctx == ""

    def test_as_context_includes_key_points(self, service, user_context):
        ctx = service.as_context(user_context, "AuditTrace")
        assert "ADR-009" in ctx or "DI container" in ctx

    def test_cross_user_isolation(self, empty_service, user_context):
        """Phase 2 isolation contract: user B cannot read user A's sessions
        even for the same project. No admin bypass at this layer."""
        alice = replace(user_context, user_id="user-alice", is_admin=False)
        bob = replace(user_context, user_id="user-bob", is_admin=False)
        empty_service.save_session(alice, "SharedProject", "Alice summary", [])
        empty_service.save_session(bob, "SharedProject", "Bob summary", [])

        alice_sessions = empty_service.load_sessions(alice, "SharedProject")
        bob_sessions = empty_service.load_sessions(bob, "SharedProject")
        assert len(alice_sessions) == 1
        assert alice_sessions[0]["summary"] == "Alice summary"
        assert len(bob_sessions) == 1
        assert bob_sessions[0]["summary"] == "Bob summary"

    def test_session_isolation(self, pg_factory, user_context):
        """Each service instance sees its own committed data."""
        svc1 = PostgresConversationalService(
            session_factory=pg_factory.get_session_factory(),
        )
        svc2 = PostgresConversationalService(
            session_factory=pg_factory.get_session_factory(),
        )
        svc1.save_session(user_context, "Isolated", "From svc1", ["p1"])
        # svc2 should see svc1's committed data (same engine)
        sessions = svc2.load_sessions(user_context, "Isolated")
        assert len(sessions) == 1
