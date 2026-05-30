"""Async PostgreSQL factory pattern for dependency injection — ADR-020.

Async-only data layer (asyncpg in production, aiosqlite in tests) so the
FastAPI event loop is never blocked on DB I/O under load. Consumers always
use the session via an ``async with`` context manager (PYTHON-ENGINEERING §1
— deterministic cleanup, no try/finally, no leaked connections). All public
entry points emit observability events via @log_call.
"""

import logging
from abc import ABC, abstractmethod

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from audittrace.db.models import Base
from audittrace.logging_config import log_call

logger = logging.getLogger(__name__)


class PostgresFactory(ABC):
    """Abstract factory for async PostgreSQL engine/session creation."""

    @abstractmethod
    def get_engine(self) -> AsyncEngine:
        """Return the async SQLAlchemy engine."""

    @abstractmethod
    def get_session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Return an async_sessionmaker bound to the engine.

        The returned factory is used as ``async with factory() as session:``
        — the AsyncSession is an async context manager that closes the
        connection deterministically on every exit path.
        """

    async def create_schema(self) -> None:
        """Create ORM tables. No-op for the URL factory (Alembic owns the
        prod schema); the in-memory test factories build the schema here.
        Awaited once at app/test startup."""
        return None


class URLPostgresFactory(PostgresFactory):
    """Production factory — async engine via URL with connection pooling."""

    def __init__(self, url: str, pool_size: int = 5):
        self._url = url
        self._pool_size = pool_size
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @log_call(logger=logger)
    def get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                self._url,
                pool_size=self._pool_size,
                pool_pre_ping=True,
            )
            # Statement-level OTel spans. The async engine wraps a sync
            # Engine at .sync_engine — that's what the instrumentor hooks.
            try:
                from opentelemetry.instrumentation.sqlalchemy import (
                    SQLAlchemyInstrumentor,
                )

                SQLAlchemyInstrumentor().instrument(engine=self._engine.sync_engine)
            except Exception as exc:  # pragma: no cover - optional dep
                logger.warning("SQLAlchemy engine instrumentation failed: %s", exc)
        return self._engine

    @log_call(logger=logger)
    def get_session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            # expire_on_commit=False: attributes stay usable after commit
            # without an extra (async) refresh round-trip.
            self._session_factory = async_sessionmaker(
                bind=self.get_engine(), expire_on_commit=False
            )
        return self._session_factory


class InMemoryPostgresFactory(PostgresFactory):
    """Test-only factory — aiosqlite in-memory engine running the ORM.

    ``StaticPool`` so every connection shares the one in-memory database
    (otherwise SQLite ``:memory:`` gives a fresh empty DB per connection and
    the tables vanish). The schema is built by ``await create_schema()`` at
    startup since ``create_all`` must run through the async engine.
    """

    def __init__(self) -> None:
        self._engine: AsyncEngine = create_async_engine(
            "sqlite+aiosqlite://",
            echo=False,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine, expire_on_commit=False
        )

    async def create_schema(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @log_call(logger=logger)
    def get_engine(self) -> AsyncEngine:
        return self._engine

    @log_call(logger=logger)
    def get_session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory


class MockPostgresFactory(PostgresFactory):
    """Mock factory for unit testing — tracks calls, aiosqlite in-memory."""

    def __init__(self) -> None:
        self.call_count: int = 0
        self._engine: AsyncEngine = create_async_engine(
            "sqlite+aiosqlite://",
            echo=False,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine, expire_on_commit=False
        )

    async def create_schema(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @log_call(logger=logger)
    def get_engine(self) -> AsyncEngine:
        self.call_count += 1
        return self._engine

    @log_call(logger=logger)
    def get_session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    def reset(self) -> None:
        self.call_count = 0
