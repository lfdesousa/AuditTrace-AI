"""Episodic memory service — Layer 1 of the 4-layer memory architecture (ADR-018).

Loads markdown decision records (``.md`` — ADRs, decisions, session docs)
from object storage and provides query-driven retrieval based on keyword
matching against content.

Storage is **always S3-backed** (MinIO) — there is no filesystem implementation.
Tests use ``MockEpisodicService``. See ``feedback_storage_always_s3`` for the
durable rule and ``dependencies.py`` for the startup-time enforcement.

Backlog #15: enumeration flows through the shared
:func:`audittrace.services.layer_listing.list_layer_objects` helper and
includes **every** ``.md`` object under the layer prefix (not just
``ADR-*.md``), so ``read()`` by key, the ``/memory/index`` walk, and the
list all see the same set. The per-process listing cache was replaced by a
shared :class:`~audittrace.services.layer_cache.LayerCacheStore` so a write
invalidation is coherent across all replicas.

DESIGN §15 Phase 2: every method takes ``user_context: UserContext`` as the
first positional argument. Decision records are shared content (not
per-user), so the parameter is plumbing here — it exists for uniform service
shape and future audit/scope checks in Phase 3.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

from audittrace_object_storage import ObjectNotFoundError
from langchain_core.documents import Document

from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.services.layer_cache import (
    InMemoryLayerCacheStore,
    LayerCacheStore,
    layer_list_cache_key,
)
from audittrace.services.layer_listing import (
    is_listable_md_object,
    list_layer_objects,
)

logger = logging.getLogger(__name__)

# Default TTL for the shared listing cache when a service is constructed
# without an explicit value (direct construction in tests). Production wires
# ``settings.memory_cache_ttl`` through ``dependencies.py``.
_DEFAULT_CACHE_TTL_SECONDS = 3600


class EpisodicService(ABC):
    """Abstract episodic memory service — ADR-based decision records."""

    @abstractmethod
    async def load(self, user_context: UserContext) -> list[Document]:
        """Load all ADR documents."""

    @abstractmethod
    async def search(self, user_context: UserContext, query: str) -> list[Document]:
        """Search ADRs by query relevance. No arbitrary caps."""

    @abstractmethod
    async def as_context(self, user_context: UserContext, query: str) -> str:
        """Return matched ADRs formatted as context string."""

    @abstractmethod
    async def read(self, user_context: UserContext, file: str) -> Document | None:
        """Fetch a single ADR by exact filename. Returns ``None`` if not found.

        ``file`` must be a leaf filename like ``ADR-025.md``. Path-traversal
        characters (``..``, ``/``) are rejected by every backend; semantically
        ADRs are flat objects keyed by filename, not a directory tree.
        """

    @abstractmethod
    async def write(
        self, user_context: UserContext, file: str, content: str
    ) -> Document:
        """Create or replace an ADR. Returns the persisted ``Document``.

        Validates the filename (same rules as ``read``: ``.md`` extension,
        no path traversal). Implementations that maintain an in-memory
        cache MUST call ``invalidate_cache()`` so subsequent ``load()``
        / ``search()`` see the new content. Raises ``ValueError`` for an
        invalid filename and ``RuntimeError`` for a backend write failure.
        """

    @abstractmethod
    async def delete(self, user_context: UserContext, file: str) -> bool:
        """Hard-delete the underlying object from storage.

        Returns ``True`` if the object was deleted, ``False`` if it
        didn't exist. Implementations MUST invalidate the cache on
        success so the deleted ADR doesn't keep appearing in
        ``load()`` / ``search()`` results until the next pod restart.
        """

    @abstractmethod
    def invalidate_cache(self) -> None:
        """Drop any in-memory cache so the next ``load()`` re-fetches
        from the backing store. Safe to call multiple times. Required
        after any write/delete so changes propagate without a pod
        restart (Phase A's S3 services kept a per-process cache that
        only refreshed on cold start)."""


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
    """S3/MinIO-backed episodic service reading ``.md`` records from object storage.

    Reads from the ``memory-shared`` bucket under the ``episodic/`` prefix.
    Decision records are shared content — ``user_context`` is required
    (authenticated) but not used for path scoping (ADR-027 §2).

    Enumeration includes **every** ``.md`` object under the prefix (ADRs,
    decisions, session docs) via the shared
    :func:`~audittrace.services.layer_listing.list_layer_objects` helper —
    the same set ``read()`` can fetch by key and ``/memory/index`` embeds
    (backlog #15, Defect A).

    The listing is cached through a shared
    :class:`~audittrace.services.layer_cache.LayerCacheStore`. In production
    a Redis-backed store is injected so a single ``invalidate_cache()`` is
    coherent across all replicas (backlog #15, Defect B); the default
    in-memory store keeps per-process caching for direct construction. A TTL
    self-heals any missed invalidation. The ``read()`` path bypasses the
    cache and does a direct ``get_object`` to keep point-fetch latency O(1)
    regardless of corpus size.
    """

    _LAYER = "episodic"

    def __init__(
        self,
        minio_client: object,
        bucket: str,
        prefix: str = "episodic/",
        *,
        cache: LayerCacheStore | None = None,
        cache_ttl_seconds: int = _DEFAULT_CACHE_TTL_SECONDS,
    ):
        self._client = minio_client  # minio.Minio instance
        self._bucket = bucket
        self._prefix = prefix
        # None → per-instance in-memory store (preserves the old per-process
        # caching for direct construction). Production injects the shared
        # Redis-backed store so invalidations are fleet-wide (backlog #15).
        self._cache_store: LayerCacheStore = (
            cache if cache is not None else InMemoryLayerCacheStore()
        )
        self._cache_ttl = cache_ttl_seconds
        self._cache_key = layer_list_cache_key(self._LAYER)

    def _read_layer_rows(self) -> list[dict[str, Any]]:
        """Enumerate + read every ``.md`` object as serialisable, cacheable rows.

        Never raises — a backend failure logs and yields what was read so far
        (fail-open), matching the historical ``load()`` contract.
        """
        rows: list[dict[str, Any]] = []
        try:
            client: Any = self._client
            for obj in list_layer_objects(client, self._bucket, self._prefix):
                filename = obj["filename"]
                if not is_listable_md_object(filename):
                    continue
                with client.get_object(self._bucket, obj["key"]) as response:
                    content = response.read().decode("utf-8")
                rows.append(
                    {
                        "file": filename,
                        "content": content,
                        "title": _title_from_content(content, filename[:-3]),
                        "last_modified_ms": obj["last_modified_ms"],
                    }
                )
        except Exception as exc:
            logger.warning("S3EpisodicService load failed: %s", exc)
        return rows

    def _layer_rows(self) -> list[dict[str, Any]]:
        """Shared-cache read-through. Fail-open: any cache miss/error → fresh S3."""
        cached = self._cache_store.get(self._cache_key)
        if cached is not None:
            return cached
        rows = self._read_layer_rows()
        self._cache_store.set(self._cache_key, rows, self._cache_ttl)
        return rows

    @staticmethod
    def _row_to_document(row: dict[str, Any]) -> Document:
        return Document(
            page_content=row["content"],
            metadata={
                "source": "episodic",
                "file": row["file"],
                "title": row["title"],
                "last_modified_ms": row.get("last_modified_ms"),
            },
        )

    @log_call(logger=logger)
    async def load(self, user_context: UserContext) -> list[Document]:
        del user_context  # shared content — not per-user scoped
        # minio SDK is sync-only → offload the blocking S3 listing/get to a
        # worker thread so the event loop stays free (PYTHON-ENGINEERING §3).
        rows = await asyncio.to_thread(self._layer_rows)
        return [self._row_to_document(row) for row in rows]

    @log_call(logger=logger)
    async def search(self, user_context: UserContext, query: str) -> list[Document]:
        adrs = await self.load(user_context)
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
    async def as_context(self, user_context: UserContext, query: str) -> str:
        matched = await self.search(user_context, query)
        if not matched:
            return ""
        lines = ["## Architecture Decisions"]
        for adr in matched:
            lines.append(f"\n### {adr.metadata['title']}\n{adr.page_content[:400]}")
        return "\n".join(lines)

    @log_call(logger=logger)
    async def read(self, user_context: UserContext, file: str) -> Document | None:
        del user_context  # shared content — not per-user scoped
        if not _validate_filename(file):
            return None
        key = f"{self._prefix}{file}"

        def _fetch() -> str | None:
            client: Any = self._client
            try:
                with client.get_object(self._bucket, key) as response:
                    return str(response.read().decode("utf-8"))
            except ObjectNotFoundError:
                return None
            except Exception as exc:
                # Network / auth / transient — preserved dual-arm behaviour
                # so a backend hiccup does NOT surface as a 500 to the caller.
                logger.warning("S3EpisodicService.read(%r) failed: %s", file, exc)
                return None

        content = await asyncio.to_thread(_fetch)
        if content is None:
            return None
        return Document(
            page_content=content,
            metadata={
                "source": "episodic",
                "file": file,
                "title": _title_from_content(content, file[:-3]),
            },
        )

    @log_call(logger=logger)
    async def write(
        self, user_context: UserContext, file: str, content: str
    ) -> Document:
        del user_context  # shared content — not per-user scoped
        if not _validate_filename(file):
            raise ValueError(f"invalid filename: {file!r}")
        import io

        key = f"{self._prefix}{file}"
        body = content.encode("utf-8")

        def _put() -> None:
            client: Any = self._client
            try:
                with io.BytesIO(body) as buf:
                    client.put_object(self._bucket, key, buf, length=len(body))
            except Exception as exc:
                raise RuntimeError(
                    f"S3EpisodicService.write({file!r}) failed: {exc}"
                ) from exc

        await asyncio.to_thread(_put)
        self.invalidate_cache()
        return Document(
            page_content=content,
            metadata={
                "source": "episodic",
                "file": file,
                "title": _title_from_content(content, file[:-3]),
            },
        )

    @log_call(logger=logger)
    async def delete(self, user_context: UserContext, file: str) -> bool:
        del user_context  # shared content — not per-user scoped
        if not _validate_filename(file):
            return False
        key = f"{self._prefix}{file}"

        def _delete() -> bool:
            client: Any = self._client
            # Existence check first so "didn't exist" → False (idempotent),
            # not "boom". MinIO `remove_object` happily no-ops on missing,
            # but we want the explicit signal back to the caller.
            try:
                client.stat_object(self._bucket, key)
            except ObjectNotFoundError:
                return False
            except Exception as exc:
                # On any other stat failure, attempt the delete anyway; if
                # it succeeds we return True, else propagate.
                logger.warning(
                    "S3EpisodicService.delete(%r): stat failed (%s) — "
                    "attempting remove regardless",
                    file,
                    exc,
                )
            try:
                client.remove_object(self._bucket, key)
            except Exception as exc:
                raise RuntimeError(
                    f"S3EpisodicService.delete({file!r}) failed: {exc}"
                ) from exc
            return True

        deleted = await asyncio.to_thread(_delete)
        if deleted:
            self.invalidate_cache()
        return deleted

    @log_call(logger=logger)
    def invalidate_cache(self) -> None:
        # Shared store → one invalidation is seen by every replica
        # (backlog #15, Defect B). TTL is the safety net for a missed call.
        self._cache_store.invalidate(self._cache_key)


class MockEpisodicService(EpisodicService):
    """Mock episodic service for unit testing."""

    def __init__(self) -> None:
        self._documents: list[Document] = []

    @log_call(logger=logger)
    def add_document(
        self,
        content: str,
        title: str = "Mock ADR",
        file: str = "ADR-mock.md",
        last_modified_ms: int | None = None,
    ) -> None:
        """Add a document for testing.

        ``last_modified_ms`` mirrors the real S3 ``last_modified`` timestamp
        the discovered-entry merge reads (backlog #15, R4); ``None`` keeps the
        pre-R4 "no timestamp" shape.
        """
        self._documents.append(
            Document(
                page_content=content,
                metadata={
                    "source": "episodic",
                    "file": file,
                    "title": title,
                    "last_modified_ms": last_modified_ms,
                },
            )
        )

    @log_call(logger=logger)
    async def load(self, user_context: UserContext) -> list[Document]:
        del user_context  # plumbing only
        return list(self._documents)

    @log_call(logger=logger)
    async def search(self, user_context: UserContext, query: str) -> list[Document]:
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
    async def as_context(self, user_context: UserContext, query: str) -> str:
        matched = await self.search(user_context, query)
        if not matched:
            return ""
        lines = ["## Architecture Decisions"]
        for d in matched:
            lines.append(f"\n### {d.metadata['title']}\n{d.page_content[:400]}")
        return "\n".join(lines)

    @log_call(logger=logger)
    async def read(self, user_context: UserContext, file: str) -> Document | None:
        del user_context  # plumbing only
        if not _validate_filename(file):
            return None
        for d in self._documents:
            if d.metadata.get("file") == file:
                return d
        return None

    @log_call(logger=logger)
    async def write(
        self, user_context: UserContext, file: str, content: str
    ) -> Document:
        del user_context  # plumbing only
        if not _validate_filename(file):
            raise ValueError(f"invalid filename: {file!r}")
        # Replace if exists, else append.
        for i, d in enumerate(self._documents):
            if d.metadata.get("file") == file:
                self._documents[i] = Document(
                    page_content=content,
                    metadata={
                        "source": "episodic",
                        "file": file,
                        "title": _title_from_content(content, file[:-3]),
                    },
                )
                return self._documents[i]
        new_doc = Document(
            page_content=content,
            metadata={
                "source": "episodic",
                "file": file,
                "title": _title_from_content(content, file[:-3]),
            },
        )
        self._documents.append(new_doc)
        return new_doc

    @log_call(logger=logger)
    async def delete(self, user_context: UserContext, file: str) -> bool:
        del user_context  # plumbing only
        if not _validate_filename(file):
            return False
        for i, d in enumerate(self._documents):
            if d.metadata.get("file") == file:
                self._documents.pop(i)
                return True
        return False

    @log_call(logger=logger)
    def invalidate_cache(self) -> None:
        # MockEpisodicService doesn't maintain a cache (data is the
        # source of truth), so this is a no-op. Implemented to satisfy
        # the abstract method.
        pass

    def reset(self) -> None:
        """Clear all documents."""
        self._documents.clear()
