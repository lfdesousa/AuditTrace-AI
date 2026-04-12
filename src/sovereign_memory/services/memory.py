"""Memory service layer for 4-tier memory architecture."""

import logging
import uuid
from abc import ABC, abstractmethod
from typing import Any

from sovereign_memory.db.factory import ChromaDBClient
from sovereign_memory.logging_config import log_call

logger = logging.getLogger(__name__)


class MemoryService(ABC):
    """Abstract memory service for 4-tier memory architecture."""

    @abstractmethod
    def store(
        self,
        project: str,
        source: str,
        content: str,
        metadata: dict | None = None,
    ) -> str:
        """Store a memory chunk."""

    @abstractmethod
    def retrieve(
        self,
        query: str,
        project: str | None = None,
        limit: int = 10,
        k: int = 10,
    ) -> list[dict[str, Any]]:
        """Retrieve relevant memories for a query."""

    @abstractmethod
    def count(self, project: str | None = None) -> int:
        """Count total memories."""


class ChromaMemoryService(MemoryService):
    """ChromaDB implementation of memory service."""

    def __init__(
        self, client: ChromaDBClient, collection_name: str = "sovereign_memory"
    ):
        self.client = client
        self.collection_name = collection_name
        self._collection = self._ensure_collection()

    @log_call(logger=logger)
    def _ensure_collection(self):
        return self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    @log_call(logger=logger)
    def store(
        self,
        project: str,
        source: str,
        content: str,
        metadata: dict | None = None,
    ) -> str:
        mem_id = str(uuid.uuid4())
        doc_metadata = {
            "project": project,
            "source": source,
            "type": "memory",
            **(metadata or {}),
        }
        self._collection.add(
            ids=[mem_id],
            documents=[content],
            metadatas=[doc_metadata],
        )
        return mem_id

    @log_call(logger=logger)
    def retrieve(
        self,
        query: str,
        project: str | None = None,
        limit: int = 10,
        k: int = 10,
    ) -> list[dict[str, Any]]:
        results = self._collection.query(
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
                    "distance": results["distances"][0][i]
                    if "distances" in results
                    else None,
                }
            )
        return memories

    @log_call(logger=logger)
    def count(self, project: str | None = None) -> int:
        if project:
            return self._collection.count(where={"project": project})
        return self._collection.count()


class MockMemoryService(MemoryService):
    """Mock memory service for unit testing."""

    def __init__(self):
        self.memories: list[dict] = []
        self.call_count: int = 0

    @log_call(logger=logger)
    def store(
        self,
        project: str,
        source: str,
        content: str,
        metadata: dict | None = None,
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
    def retrieve(
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
    def count(self, project: str | None = None) -> int:
        if project:
            return len([m for m in self.memories if m["project"] == project])
        return len(self.memories)

    def reset(self):
        self.memories.clear()
        self.call_count = 0
