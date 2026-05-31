"""Async PostgreSQL factory pattern for dependency injection — ADR-020.

Async-only data layer (asyncpg in production, aiosqlite in tests) so the
FastAPI event loop is never blocked on DB I/O under load. Consumers always
use the session via an ``async with`` context manager (PYTHON-ENGINEERING §1
— deterministic cleanup, no try/finally, no leaked connections). All public
entry points emit observability events via @log_call.
"""

import atexit
import logging
import os
import tempfile
from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

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
            # pool_size is only valid for a real queue-pooled engine. The
            # sqlite/aiosqlite test URL uses StaticPool, which rejects
            # pool_size — so only pass it for non-sqlite (Postgres) engines.
            kwargs: dict[str, Any] = {"pool_pre_ping": True}
            if not self._url.startswith("sqlite"):
                kwargs["pool_size"] = self._pool_size
            else:
                kwargs["connect_args"] = {"check_same_thread": False}
                kwargs["poolclass"] = StaticPool
            self._engine = create_async_engine(self._url, **kwargs)
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
        # File-backed temp SQLite DB (not ``:memory:``). Rationale: under
        # the async stack the TestClient runs the app lifespan in its own
        # anyio-portal thread + event-loop, while async test bodies run in
        # pytest-asyncio's loop. An ``:memory:`` DB (even shared-cache) is
        # bound to the connection/loop that created it and the tables are
        # invisible to the other loop — manifesting as "no such table".
        # A real file on disk is visible to every thread, connection and
        # event-loop, so writes from the handler loop are seen by the test
        # loop. The temp file is removed at process exit — throwaway.
        self._tmp = tempfile.NamedTemporaryFile(
            prefix="audittrace_test_", suffix=".sqlite", delete=False
        )
        self._tmp.close()
        self._db_path = self._tmp.name
        atexit.register(self._cleanup_file)
        url = f"sqlite+aiosqlite:///{self._db_path}"
        # NullPool (not StaticPool): a file-backed DB lets every event-loop
        # open its own fresh connection to the same file. StaticPool caches
        # ONE connection bound to the loop that created it, so the TestClient
        # lifespan loop and the pytest-asyncio test loop would fight over it.
        self._engine: AsyncEngine = create_async_engine(
            url,
            echo=False,
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine, expire_on_commit=False
        )

        # Classic test setup: build the ORM schema on the file NOW, via a
        # throwaway SYNC engine, so the tables exist before any request or
        # test body queries them — independent of when the async app
        # lifespan happens to call ``create_schema()``. ``create_schema()``
        # below stays available and is idempotent (CREATE ... IF NOT EXISTS).
        from sqlalchemy import create_engine as _create_sync_engine

        _sync_engine = _create_sync_engine(f"sqlite:///{self._db_path}")
        try:
            Base.metadata.create_all(_sync_engine)
        finally:
            _sync_engine.dispose()

    def _cleanup_file(self) -> None:
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

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
