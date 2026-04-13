"""Episodic memory service — Layer 1 of the 4-layer memory architecture (ADR-018).

Loads Architecture Decision Records (ADR-*.md) from the filesystem and provides
query-driven retrieval based on keyword matching against content.

DESIGN §15 Phase 2: every method takes ``user_context: UserContext`` as the
first positional argument. Episodic data is filesystem-backed and shared
(ADRs are not per-user), so the parameter is pure plumbing here — it exists
for uniform service shape and future audit/scope checks in Phase 3.
"""

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from langchain_core.documents import Document

from sovereign_memory.identity import UserContext
from sovereign_memory.logging_config import log_call

logger = logging.getLogger(__name__)


class EpisodicService(ABC):
    """Abstract episodic memory service — ADR-based decision records."""

    @abstractmethod
    def load(self, user_context: UserContext) -> list[Document]:
        """Load all ADR documents."""

    @abstractmethod
    def search(self, user_context: UserContext, query: str) -> list[Document]:
        """Search ADRs by query relevance. No arbitrary caps."""

    @abstractmethod
    def as_context(self, user_context: UserContext, query: str) -> str:
        """Return matched ADRs formatted as context string."""


class FileEpisodicService(EpisodicService):
    """Filesystem-based episodic service reading ADR-*.md files."""

    def __init__(self, adr_dir: Path | str):
        self._adr_dir = Path(adr_dir)

    @log_call(logger=logger)
    def load(self, user_context: UserContext) -> list[Document]:
        """Load all ADR-*.md files as LangChain Documents."""
        del user_context  # plumbing only — ADRs are shared, not per-user
        docs: list[Document] = []
        if not self._adr_dir.exists():
            return docs
        for f in sorted(self._adr_dir.glob("ADR-*.md")):
            content = f.read_text(encoding="utf-8")
            title = f.stem
            for line in content.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            docs.append(
                Document(
                    page_content=content,
                    metadata={"source": "episodic", "file": f.name, "title": title},
                )
            )
        return docs

    @log_call(logger=logger)
    def search(self, user_context: UserContext, query: str) -> list[Document]:
        """Filter ADRs by keyword relevance. No arbitrary caps on results."""
        adrs = self.load(user_context)
        query_lower = query.lower()
        keywords = [kw for kw in query_lower.split() if len(kw) > 3]
        if not keywords:
            return []
        return [
            adr
            for adr in adrs
            if any(kw in adr.page_content.lower() for kw in keywords)
        ]

    @log_call(logger=logger)
    def as_context(self, user_context: UserContext, query: str) -> str:
        """Return matched ADRs formatted as a context section."""
        matched = self.search(user_context, query)
        if not matched:
            return ""
        lines = ["## Architecture Decisions"]
        for adr in matched:
            lines.append(f"\n### {adr.metadata['title']}\n{adr.page_content[:400]}")
        return "\n".join(lines)


class S3EpisodicService(EpisodicService):
    """S3/MinIO-backed episodic service reading ADR-*.md from object storage.

    Reads from the ``memory-shared`` bucket under the ``episodic/`` prefix.
    ADRs are shared content — ``user_context`` is required (authenticated)
    but not used for path scoping (ADR-027 §2).

    Documents are cached in memory on first load since ADRs are static,
    small (~14 files), and read-heavy. Cache is per-process lifetime.
    """

    def __init__(self, minio_client: object, bucket: str, prefix: str = "episodic/"):
        self._client = minio_client  # minio.Minio instance
        self._bucket = bucket
        self._prefix = prefix
        self._cache: list[Document] | None = None

    def _load_from_s3(self) -> list[Document]:
        """Download all ADR-*.md objects from MinIO and parse as Documents."""
        if self._cache is not None:
            return self._cache
        docs: list[Document] = []
        try:
            from minio import Minio

            client: Minio = self._client  # type: ignore[assignment]
            objects = client.list_objects(self._bucket, prefix=self._prefix)
            for obj in objects:
                name = obj.object_name or ""
                filename = name.rsplit("/", 1)[-1] if "/" in name else name
                if not filename.startswith("ADR-") or not filename.endswith(".md"):
                    continue
                response = client.get_object(self._bucket, name)
                try:
                    content = response.read().decode("utf-8")
                finally:
                    response.close()
                    response.release_conn()
                title = filename.replace(".md", "")
                for line in content.splitlines():
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
                docs.append(
                    Document(
                        page_content=content,
                        metadata={
                            "source": "episodic",
                            "file": filename,
                            "title": title,
                        },
                    )
                )
        except Exception as exc:
            logger.warning("S3EpisodicService load failed: %s", exc)
        self._cache = docs
        return docs

    @log_call(logger=logger)
    def load(self, user_context: UserContext) -> list[Document]:
        del user_context  # shared content — not per-user scoped
        return list(self._load_from_s3())

    @log_call(logger=logger)
    def search(self, user_context: UserContext, query: str) -> list[Document]:
        adrs = self.load(user_context)
        query_lower = query.lower()
        keywords = [kw for kw in query_lower.split() if len(kw) > 3]
        if not keywords:
            return []
        return [
            adr
            for adr in adrs
            if any(kw in adr.page_content.lower() for kw in keywords)
        ]

    @log_call(logger=logger)
    def as_context(self, user_context: UserContext, query: str) -> str:
        matched = self.search(user_context, query)
        if not matched:
            return ""
        lines = ["## Architecture Decisions"]
        for adr in matched:
            lines.append(f"\n### {adr.metadata['title']}\n{adr.page_content[:400]}")
        return "\n".join(lines)


class MockEpisodicService(EpisodicService):
    """Mock episodic service for unit testing."""

    def __init__(self):
        self._documents: list[Document] = []

    @log_call(logger=logger)
    def add_document(
        self, content: str, title: str = "Mock ADR", file: str = "ADR-mock.md"
    ):
        """Add a document for testing."""
        self._documents.append(
            Document(
                page_content=content,
                metadata={"source": "episodic", "file": file, "title": title},
            )
        )

    @log_call(logger=logger)
    def load(self, user_context: UserContext) -> list[Document]:
        del user_context  # plumbing only
        return list(self._documents)

    @log_call(logger=logger)
    def search(self, user_context: UserContext, query: str) -> list[Document]:
        del user_context  # plumbing only
        query_lower = query.lower()
        keywords = [kw for kw in query_lower.split() if len(kw) > 3]
        if not keywords:
            return []
        return [
            d
            for d in self._documents
            if any(kw in d.page_content.lower() for kw in keywords)
        ]

    @log_call(logger=logger)
    def as_context(self, user_context: UserContext, query: str) -> str:
        matched = self.search(user_context, query)
        if not matched:
            return ""
        lines = ["## Architecture Decisions"]
        for d in matched:
            lines.append(f"\n### {d.metadata['title']}\n{d.page_content[:400]}")
        return "\n".join(lines)

    def reset(self):
        """Clear all documents."""
        self._documents.clear()
