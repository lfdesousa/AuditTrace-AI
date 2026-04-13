"""PostgreSQL factory pattern for dependency injection — ADR-020.

Mirrors the ChromaDB factory in db/factory.py. All public entry points
emit observability events via @log_call.
"""

import logging
from abc import ABC, abstractmethod

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sovereign_memory.db.models import Base
from sovereign_memory.logging_config import log_call

logger = logging.getLogger(__name__)


class PostgresFactory(ABC):
    """Abstract factory for PostgreSQL engine/session creation."""

    @abstractmethod
    def get_engine(self) -> Engine:
        """Return the SQLAlchemy engine."""

    @abstractmethod
    def get_session_factory(self) -> sessionmaker[Session]:
        """Return a sessionmaker bound to the engine."""


class URLPostgresFactory(PostgresFactory):
    """Production factory — connects via URL with connection pooling."""

    def __init__(self, url: str, pool_size: int = 5):
        self._url = url
        self._pool_size = pool_size
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None

    @log_call(logger=logger)
    def get_engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(
                self._url,
                pool_size=self._pool_size,
                pool_pre_ping=True,
            )
        return self._engine

    @log_call(logger=logger)
    def get_session_factory(self) -> sessionmaker[Session]:
        if self._session_factory is None:
            self._session_factory = sessionmaker(bind=self.get_engine())
        return self._session_factory


class InMemoryPostgresFactory(PostgresFactory):
    """Test-only factory — SQLite in-memory engine running SQLAlchemy ORM.

    Uses ``StaticPool`` so every connection (including those opened from
    FastAPI's thread-pool workers when sync code runs in async handlers)
    shares the same in-memory database. Without it, SQLite ``:memory:``
    creates a fresh empty DB per thread and our tables vanish.
    """

    def __init__(self) -> None:
        self._engine = create_engine(
            "sqlite://",
            echo=False,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)

    @log_call(logger=logger)
    def get_engine(self) -> Engine:
        return self._engine

    @log_call(logger=logger)
    def get_session_factory(self) -> sessionmaker[Session]:
        return self._session_factory


class MockPostgresFactory(PostgresFactory):
    """Mock factory for unit testing — tracks calls, no real connections."""

    def __init__(self) -> None:
        self.call_count: int = 0
        self._engine = create_engine(
            "sqlite://",
            echo=False,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self._session_factory = sessionmaker(bind=self._engine)

    @log_call(logger=logger)
    def get_engine(self) -> Engine:
        self.call_count += 1
        return self._engine

    @log_call(logger=logger)
    def get_session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    def reset(self) -> None:
        self.call_count = 0
