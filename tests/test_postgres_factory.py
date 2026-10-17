"""Tests for PostgreSQL factory pattern — ADR-020.

Validates the ABC + implementation + mock pattern for PostgreSQL connectivity.
Uses SQLite in-memory engine for tests — no real PostgreSQL required.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from audittrace.db.models import SessionRecord
from audittrace.db.postgres import (
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
            def get_engine(self) -> AsyncEngine:
                return None  # type: ignore

        with pytest.raises(TypeError):
            PartialImpl()  # type: ignore[abstract]


# ── URLPostgresFactory tests ──────────────────────────────────────────────────


class TestURLPostgresFactory:
    def test_creates_engine_with_url(self):
        factory = URLPostgresFactory("sqlite+aiosqlite://")
        engine = factory.get_engine()
        assert isinstance(engine, AsyncEngine)
        assert "sqlite" in str(engine.url)

    def test_engine_is_singleton(self):
        factory = URLPostgresFactory("sqlite+aiosqlite://")
        assert factory.get_engine() is factory.get_engine()

    async def test_session_factory_returns_sessions(self):
        factory = URLPostgresFactory("sqlite+aiosqlite://")
        session_factory = factory.get_session_factory()
        async with session_factory() as session:
            assert isinstance(session, AsyncSession)

    def test_pool_pre_ping_enabled(self):
        factory = URLPostgresFactory("sqlite+aiosqlite://")
        engine = factory.get_engine()
        assert engine.pool._pre_ping is True

    def test_postgres_url_gets_configured_pool_size(self):
        """Production (non-sqlite) URLs must build a real queue-pooled engine
        sized from ``pool_size``.

        Why this matters: the sqlite branch swaps in StaticPool (one shared
        connection) because StaticPool rejects ``pool_size``. If the sqlite
        branch ever leaked into the Postgres path, production would run the
        whole FastAPI event loop through a SINGLE serialised connection —
        every concurrent request queued behind the previous one — and the
        ``pool_size`` setting would be silently ignored.

        No connection is opened here: create_async_engine is lazy, so this
        asserts engine configuration without needing a live Postgres.
        """
        factory = URLPostgresFactory(
            "postgresql+asyncpg://user:pw@localhost:5432/audittrace", pool_size=7
        )
        engine = factory.get_engine()
        # size() reports the configured pool_size on a QueuePool — StaticPool
        # has no such notion, so this fails loudly if the branch is inverted.
        assert engine.pool.size() == 7
        assert engine.pool._pre_ping is True

    def test_session_factory_is_cached_across_calls(self):
        """``get_session_factory`` must memoise, not rebuild per call.

        Every request handler resolves the session factory through this
        method. Rebuilding an async_sessionmaker on each call would defeat
        the ``self._session_factory is None`` guard and hand out sessionmakers
        that are no longer identity-comparable — which breaks test overrides
        and any code that caches the factory by identity.
        """
        factory = URLPostgresFactory("sqlite+aiosqlite://")
        first = factory.get_session_factory()
        second = factory.get_session_factory()
        assert first is second
        # And it stays bound to the one singleton engine.
        assert first.kw["bind"] is factory.get_engine()


# ── InMemoryPostgresFactory tests ─────────────────────────────────────────────


class TestInMemoryPostgresFactory:
    def test_creates_sqlite_in_memory_engine(self):
        factory = InMemoryPostgresFactory()
        engine = factory.get_engine()
        assert isinstance(engine, AsyncEngine)
        assert "sqlite" in str(engine.url)

    async def test_creates_tables_on_init(self):
        factory = InMemoryPostgresFactory()
        await factory.create_schema()
        session_factory = factory.get_session_factory()
        async with session_factory() as session:
            # Tables should exist after create_schema() — query must not raise
            result = await session.execute(text("SELECT count(*) FROM sessions"))
            assert result.scalar() == 0

    async def test_crud_works(self):
        factory = InMemoryPostgresFactory()
        await factory.create_schema()
        session_factory = factory.get_session_factory()
        async with session_factory() as session:
            record = SessionRecord(
                id="test_1",
                project="P",
                date="d",
                summary="s",
                key_points="[]",
                model="m",
            )
            session.add(record)
            await session.commit()
            loaded = await session.get(SessionRecord, "test_1")
            assert loaded is not None
            assert loaded.project == "P"


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
