"""Episodic memory service — Layer 1 of the 4-layer memory architecture (ADR-018).

Loads Architecture Decision Records (ADR-*.md) from object storage and provides
query-driven retrieval based on keyword matching against content.

Storage is **always S3-backed** (MinIO) — there is no filesystem implementation.
Tests use ``MockEpisodicService``. See ``feedback_storage_always_s3`` for the
durable rule and ``dependencies.py`` for the startup-time enforcement.

DESIGN §15 Phase 2: every method takes ``user_context: UserContext`` as the
first positional argument. ADRs are shared content (not per-user), so the
parameter is plumbing here — it exists for uniform service shape and future
audit/scope checks in Phase 3.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any

from langchain_core.documents import Document

from audittrace.identity import UserContext
from audittrace.logging_config import log_call

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

    @abstractmethod
    def read(self, user_context: UserContext, file: str) -> Document | None:
        """Fetch a single ADR by exact filename. Returns ``None`` if not found.

        ``file`` must be a leaf filename like ``ADR-025.md``. Path-traversal
        characters (``..``, ``/``) are rejected by every backend; semantically
        ADRs are flat objects keyed by filename, not a directory tree.
        """


def _validate_filename(file: str) -> bool:
    """Reject empty, path-traversal, and non-``.md`` filenames."""
    if not isinstance(file, str) or not file:
        return False
    if ".." in file or "/" in file or "\\" in file:
        return False
    if not file.endswith(".md"):
        return False
    return True


def _title_from_content(content: str, fallback: str) -> str:
    """Parse the first ``# `` heading; fall back to a stem-style default."""
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


class S3EpisodicService(EpisodicService):
    """S3/MinIO-backed episodic service reading ADR-*.md from object storage.

    Reads from the ``memory-shared`` bucket under the ``episodic/`` prefix.
    ADRs are shared content — ``user_context`` is required (authenticated)
    but not used for path scoping (ADR-027 §2).

    Documents are cached in memory on first ``load()`` since ADRs are static,
    small (~24 files), and read-heavy. Cache is per-process lifetime. The
    ``read()`` path bypasses the cache and does a direct ``get_object`` to
    keep point-fetch latency O(1) regardless of corpus size.
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
            client: Any = self._client
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
                docs.append(
                    Document(
                        page_content=content,
                        metadata={
                            "source": "episodic",
                            "file": filename,
                            "title": _title_from_content(content, filename[:-3]),
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

    @log_call(logger=logger)
    def read(self, user_context: UserContext, file: str) -> Document | None:
        del user_context  # shared content — not per-user scoped
        if not _validate_filename(file):
            return None
        client: Any = self._client
        key = f"{self._prefix}{file}"
        try:
            response = client.get_object(self._bucket, key)
        except Exception as exc:  # MinIO raises S3Error on missing/etc
            code = getattr(exc, "code", "")
            if code == "NoSuchKey":
                return None
            logger.warning("S3EpisodicService.read(%r) failed: %s", file, exc)
            return None
        try:
            content = response.read().decode("utf-8")
        finally:
            response.close()
            response.release_conn()
        return Document(
            page_content=content,
            metadata={
                "source": "episodic",
                "file": file,
                "title": _title_from_content(content, file[:-3]),
            },
        )


class MockEpisodicService(EpisodicService):
    """Mock episodic service for unit testing."""

    def __init__(self) -> None:
        self._documents: list[Document] = []

    @log_call(logger=logger)
    def add_document(
        self, content: str, title: str = "Mock ADR", file: str = "ADR-mock.md"
    ) -> None:
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

    @log_call(logger=logger)
    def read(self, user_context: UserContext, file: str) -> Document | None:
        del user_context  # plumbing only
        if not _validate_filename(file):
            return None
        for d in self._documents:
            if d.metadata.get("file") == file:
                return d
        return None

    def reset(self) -> None:
        """Clear all documents."""
        self._documents.clear()
