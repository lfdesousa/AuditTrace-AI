"""Memory service layer for 4-tier memory architecture."""

import logging
import uuid
from abc import ABC, abstractmethod
from typing import Any

from audittrace.db.factory import ChromaDBClient
from audittrace.logging_config import log_call

logger = logging.getLogger(__name__)


class MemoryService(ABC):
    """Abstract memory service for 4-tier memory architecture."""

    @abstractmethod
    async def store(
        self,
        project: str,
        source: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store a memory chunk."""

    @abstractmethod
    async def retrieve(
        self,
        query: str,
        project: str | None = None,
        limit: int = 10,
        k: int = 10,
    ) -> list[dict[str, Any]]:
        """Retrieve relevant memories for a query."""

    @abstractmethod
    async def count(self, project: str | None = None) -> int:
        """Count total memories."""


class ChromaMemoryService(MemoryService):
    """ChromaDB implementation of memory service."""

    def __init__(self, client: ChromaDBClient, collection_name: str = "audittrace"):
        self.client = client
        self.collection_name = collection_name
        # Lazy-awaited cache — the async Chroma client's
        # get_or_create_collection is a coroutine, so it can't be resolved in
        # __init__. First use awaits it once and memoises (PYTHON-ENGINEERING §2).
        self._collection: Any | None = None

    @log_call(logger=logger)
    async def _ensure_collection(self) -> Any:
        if self._collection is None:
            self._collection = await self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    @log_call(logger=logger)
    async def store(
        self,
        project: str,
        source: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        mem_id = str(uuid.uuid4())
        doc_metadata = {
            "project": project,
            "source": source,
            "type": "memory",
            **(metadata or {}),
        }
        collection = await self._ensure_collection()
        await collection.add(
            ids=[mem_id],
            documents=[content],
            metadatas=[doc_metadata],
        )
        return mem_id

    @log_call(logger=logger)
    async def retrieve(
        self,
        query: str,
        project: str | None = None,
        limit: int = 10,
        k: int = 10,
    ) -> list[dict[str, Any]]:
        collection = await self._ensure_collection()
        results = await collection.query(
            query_texts=[query],
            n_results=min(limit, k),
            where={"project": project} if project else None,
            include=["documents", "metadatas"],
        )
        memories = []
        for i in range(len(results["ids"][0])):
            memories.append(
                {
                    "id": results["ids"][0][i],
                    "content": results["documents"][0][i],
                    "metadata": results["metadatas"][0][i],
                    "distance": (
                        results["distances"][0][i] if "distances" in results else None
                    ),
                }
            )
        return memories

    @log_call(logger=logger)
    async def count(self, project: str | None = None) -> int:
        collection = await self._ensure_collection()
        if project:
            return int(await collection.count(where={"project": project}))
        return int(await collection.count())


class MockMemoryService(MemoryService):
    """Mock memory service for unit testing."""

    def __init__(self) -> None:
        self.memories: list[dict[str, Any]] = []
        self.call_count: int = 0

    @log_call(logger=logger)
    async def store(
        self,
        project: str,
        source: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        mem_id = str(uuid.uuid4())
        self.memories.append(
            {
                "id": mem_id,
                "project": project,
                "source": source,
                "content": content,
                "metadata": metadata or {},
            }
        )
        self.call_count += 1
        return mem_id

    @log_call(logger=logger)
    async def retrieve(
        self,
        query: str,
        project: str | None = None,
        limit: int = 10,
        k: int = 10,
    ) -> list[dict[str, Any]]:
        filtered = [
            m for m in self.memories if project is None or m["project"] == project
        ][:limit]
        return [
            {
                "id": m["id"],
                "content": m["content"],
                "metadata": m["metadata"],
                "distance": 0.0,
            }
            for m in filtered
        ]

    @log_call(logger=logger)
    async def count(self, project: str | None = None) -> int:
        if project:
            return len([m for m in self.memories if m["project"] == project])
        return len(self.memories)

    def reset(self) -> None:
        self.memories.clear()
        self.call_count = 0
