"""Database factory pattern for dependency injection.

All public entry points emit observability events via @log_call so the
factory layer (a critical trust boundary in the audit story) is visible
in logs, traces, and metrics.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Protocol

import chromadb

from sovereign_memory.logging_config import log_call

logger = logging.getLogger(__name__)


class ChromaDBClient(Protocol):
    """Protocol for ChromaDB client abstraction."""

    def get_or_create_collection(
        self, name: str, **kwargs: Any
    ) -> "chromadb.Collection": ...


class ChromaDBFactory(ABC):
    """Abstract factory for ChromaDB client creation."""

    @abstractmethod
    def get_client(self) -> ChromaDBClient:
        """Create and return a ChromaDB client."""


class MemoryChromaDBFactory(ChromaDBFactory):
    """In-memory ChromaDB (for testing)."""

    @log_call(logger=logger)
    def get_client(self) -> ChromaDBClient:
        return chromadb.Client()  # type: ignore[return-value]


class HTTPChromaDBFactory(ChromaDBFactory):
    """HTTP-based ChromaDB (server mode) with optional token authentication."""

    def __init__(self, url: str = "http://localhost:8000", token: str | None = None):
        self.url = url
        self.token = token

    @log_call(logger=logger)
    def get_client(self) -> ChromaDBClient:
        # Accept both "http://host:port" and "host:port" forms.
        url = self.url
        if "://" in url:
            url = url.split("://", 1)[1]
        host, _, port = url.partition(":")
        kwargs: dict[str, Any] = {"host": host, "port": int(port or 8000)}
        if self.token:
            kwargs["headers"] = {"Authorization": f"Bearer {self.token}"}
        return chromadb.HttpClient(**kwargs)  # type: ignore[return-value]


class MockChromaDBFactory(ChromaDBFactory):
    """Mock ChromaDB factory for unit testing.

    `get_client` returns a fresh `_MockChromaDBClient` each call so tests
    can rely on client identity (two calls → two distinct clients), while
    collections remain shared on the factory so state survives across
    clients within a single test.
    """

    def __init__(self) -> None:
        self.collections: dict[str, MockCollection] = {}
        self.call_count: int = 0

    @log_call(logger=logger)
    def get_client(self) -> "_MockChromaDBClient":
        self.call_count += 1
        return _MockChromaDBClient(self)

    def _get_or_create_collection(self, name: str, **kwargs: Any) -> "MockCollection":
        if name not in self.collections:
            self.collections[name] = MockCollection(name)
        return self.collections[name]

    def reset(self) -> None:
        self.collections.clear()
        self.call_count = 0


class _MockChromaDBClient:
    """Thin client wrapper so each `get_client()` call returns a distinct object."""

    def __init__(self, factory: MockChromaDBFactory):
        self._factory = factory

    @log_call(logger=logger)
    def get_or_create_collection(self, name: str, **kwargs: Any) -> "MockCollection":
        return self._factory._get_or_create_collection(name, **kwargs)


class MockCollection:
    """Mock ChromaDB collection for testing."""

    def __init__(self, name: str):
        self.name = name
        self.data: list[dict[str, Any]] = []

    @log_call(logger=logger)
    def add(
        self,
        ids: list[str] | None = None,
        documents: list[str] | None = None,
        metadatas: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> list[str]:
        ids = ids or []
        documents = documents or []
        metadatas = metadatas or [{} for _ in ids]
        for i, doc_id in enumerate(ids):
            self.data.append(
                {
                    "id": doc_id,
                    "document": documents[i] if i < len(documents) else "",
                    "metadata": metadatas[i] if i < len(metadatas) else {},
                }
            )
        return list(ids)

    @log_call(logger=logger)
    def query(
        self,
        query_texts: list[str] | None = None,
        n_results: int = 5,
        where: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        rows = self.data
        if where:
            rows = [
                r
                for r in rows
                if all(r["metadata"].get(k) == v for k, v in where.items())
            ]
        rows = rows[:n_results]
        return {
            "ids": [[r["id"] for r in rows]],
            "documents": [[r["document"] for r in rows]],
            "metadatas": [[r["metadata"] for r in rows]],
            "distances": [[0.1] * len(rows)],
        }

    @log_call(logger=logger)
    def get(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "ids": [r["id"] for r in self.data],
            "documents": [r["document"] for r in self.data],
            "metadatas": [r["metadata"] for r in self.data],
        }

    @log_call(logger=logger)
    def count(self, where: dict[str, Any] | None = None, **kwargs: Any) -> int:
        if where:
            return sum(
                1
                for r in self.data
                if all(r["metadata"].get(k) == v for k, v in where.items())
            )
        return len(self.data)
