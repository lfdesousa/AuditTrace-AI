"""Tests for PostgreSQL factory pattern — ADR-020.

Validates the ABC + implementation + mock pattern for PostgreSQL connectivity.
Uses SQLite in-memory engine for tests — no real PostgreSQL required.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from sovereign_memory.db.models import Base, SessionRecord
from sovereign_memory.db.postgres import (
    InMemoryPostgresFactory,
    MockPostgresFactory,
    PostgresFactory,
    URLPostgresFactory,
)

# ── ABC tests ──────────────────────────────────────────────────────────────────


class TestPostgresFactoryABC:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            PostgresFactory()  # type: ignore[abstract]

    def test_subclass_must_implement_get_engine(self):
        class Incomplete(PostgresFactory):
            pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_subclass_must_implement_get_session_factory(self):
        class PartialImpl(PostgresFactory):
            def get_engine(self) -> Engine:
                return None  # type: ignore

        with pytest.raises(TypeError):
            PartialImpl()  # type: ignore[abstract]


# ── URLPostgresFactory tests ──────────────────────────────────────────────────


class TestURLPostgresFactory:
    def test_creates_engine_with_url(self):
        factory = URLPostgresFactory("sqlite://")
        engine = factory.get_engine()
        assert isinstance(engine, Engine)
        assert str(engine.url) == "sqlite://"

    def test_engine_is_singleton(self):
        factory = URLPostgresFactory("sqlite://")
        assert factory.get_engine() is factory.get_engine()

    def test_session_factory_returns_sessions(self):
        factory = URLPostgresFactory("sqlite://")
        session_factory = factory.get_session_factory()
        session = session_factory()
        assert isinstance(session, Session)
        session.close()

    def test_pool_pre_ping_enabled(self):
        factory = URLPostgresFactory("sqlite://")
        engine = factory.get_engine()
        assert engine.pool._pre_ping is True


# ── InMemoryPostgresFactory tests ─────────────────────────────────────────────


class TestInMemoryPostgresFactory:
    def test_creates_sqlite_in_memory_engine(self):
        factory = InMemoryPostgresFactory()
        engine = factory.get_engine()
        assert isinstance(engine, Engine)
        assert "sqlite" in str(engine.url)

    def test_creates_tables_on_init(self):
        factory = InMemoryPostgresFactory()
        session_factory = factory.get_session_factory()
        session = session_factory()
        # Tables should already exist — query should not raise
        result = session.execute(text("SELECT count(*) FROM sessions"))
        assert result.scalar() == 0
        session.close()

    def test_crud_works(self):
        factory = InMemoryPostgresFactory()
        session_factory = factory.get_session_factory()
        session = session_factory()
        Base.metadata.create_all(factory.get_engine())
        record = SessionRecord(
            id="test_1",
            project="P",
            date="d",
            summary="s",
            key_points="[]",
            model="m",
        )
        session.add(record)
        session.commit()
        loaded = session.get(SessionRecord, "test_1")
        assert loaded is not None
        assert loaded.project == "P"
        session.close()


# ── MockPostgresFactory tests ─────────────────────────────────────────────────


class TestMockPostgresFactory:
    def test_get_engine_returns_mock(self):
        factory = MockPostgresFactory()
        engine = factory.get_engine()
        assert engine is not None

    def test_get_session_factory_returns_callable(self):
        factory = MockPostgresFactory()
        session_factory = factory.get_session_factory()
        assert callable(session_factory)

    def test_tracks_call_count(self):
        factory = MockPostgresFactory()
        factory.get_engine()
        factory.get_engine()
        assert factory.call_count == 2

    def test_reset_clears_state(self):
        factory = MockPostgresFactory()
        factory.get_engine()
        factory.get_engine()
        factory.reset()
        assert factory.call_count == 0
