"""Procedural memory service — Layer 2 of the 4-layer memory architecture (ADR-018).

Loads ``.md`` skill files from object storage and provides query-driven
retrieval based on keyword matching against skill names and content.

Storage is **always S3-backed** (MinIO) — there is no filesystem implementation.
Tests use ``MockProceduralService``. See ``feedback_storage_always_s3`` for the
durable rule.

Backlog #15: enumeration flows through the shared
:func:`audittrace.services.layer_listing.list_layer_objects` helper and
includes **every** ``.md`` object under the layer prefix (not just
``SKILL-*.md``); the per-process listing cache was replaced by a shared
:class:`~audittrace.services.layer_cache.LayerCacheStore` so a write
invalidation is coherent across all replicas.

DESIGN §15 Phase 2: every method takes ``user_context: UserContext`` as the
first positional argument. SKILL files are shared (not per-user), so the
parameter is plumbing here.
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


class ProceduralService(ABC):
    """Abstract procedural memory service — skill-based knowledge."""

    @abstractmethod
    async def load(self, user_context: UserContext) -> list[Document]:
        """Load all SKILL documents."""

    @abstractmethod
    async def search(self, user_context: UserContext, query: str) -> list[Document]:
        """Search skills by query relevance. No arbitrary caps."""

    @abstractmethod
    async def as_context(self, user_context: UserContext, query: str) -> str:
        """Return matched skills formatted as context string."""

    @abstractmethod
    async def read(self, user_context: UserContext, file: str) -> Document | None:
        """Fetch a single SKILL by exact filename. Returns ``None`` if not found.

        ``file`` must be a leaf filename like ``SKILL-IAM.md``. Path-traversal
        characters (``..``, ``/``) are rejected.
        """

    @abstractmethod
    async def write(
        self, user_context: UserContext, file: str, content: str
    ) -> Document:
        """Create or replace a SKILL document. Returns the persisted
        ``Document``. Same filename validation as ``read``. Caches
        invalidated on success."""

    @abstractmethod
    async def delete(self, user_context: UserContext, file: str) -> bool:
        """Hard-delete the underlying object from storage. Returns
        ``True`` if deleted, ``False`` if it didn't exist. Caches
        invalidated on success."""

    @abstractmethod
    def invalidate_cache(self) -> None:
        """Drop any in-memory cache so the next ``load()`` re-fetches
        from the backing store."""


def _validate_filename(file: str) -> bool:
    """Reject empty, path-traversal, and non-``.md`` filenames."""
    if not isinstance(file, str) or not file:
        return False
    if ".." in file or "/" in file or "\\" in file:
        return False
    if not file.endswith(".md"):
        return False
    return True


def _skill_name_from_filename(filename: str) -> str:
    return filename.replace("SKILL-", "").replace(".md", "")


class S3ProceduralService(ProceduralService):
    """S3/MinIO-backed procedural service reading ``.md`` skills from object storage.

    Reads from the ``memory-shared`` bucket under the ``procedural/`` prefix.
    Skills are shared content — ``user_context`` is required (authenticated)
    but not used for path scoping (ADR-027 §2).

    Enumeration includes **every** ``.md`` object under the prefix via the
    shared :func:`~audittrace.services.layer_listing.list_layer_objects`
    helper — the same set ``read()`` fetches by key and ``/memory/index``
    embeds (backlog #15, Defect A). The listing is cached through a shared
    :class:`~audittrace.services.layer_cache.LayerCacheStore` (Redis in
    production) so a single ``invalidate_cache()`` is coherent across all
    replicas (Defect B); a TTL self-heals a missed invalidation. The
    ``read()`` path bypasses the cache and does a direct ``get_object``.
    """

    _LAYER = "procedural"

    def __init__(
        self,
        minio_client: object,
        bucket: str,
        prefix: str = "procedural/",
        *,
        cache: LayerCacheStore | None = None,
        cache_ttl_seconds: int = _DEFAULT_CACHE_TTL_SECONDS,
    ):
        self._client = minio_client  # minio.Minio instance
        self._bucket = bucket
        self._prefix = prefix
        self._cache_store: LayerCacheStore = (
            cache if cache is not None else InMemoryLayerCacheStore()
        )
        self._cache_ttl = cache_ttl_seconds
        self._cache_key = layer_list_cache_key(self._LAYER)

    def _read_layer_rows(self) -> list[dict[str, Any]]:
        """Enumerate + read every ``.md`` object as serialisable, cacheable rows.

        Never raises — a backend failure logs and yields what was read so far.
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
                        "skill": _skill_name_from_filename(filename),
                        "last_modified_ms": obj["last_modified_ms"],
                    }
                )
        except Exception as exc:
            logger.warning("S3ProceduralService load failed: %s", exc)
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
                "source": "procedural",
                "file": row["file"],
                "skill": row["skill"],
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
        skills = await self.load(user_context)
        query_lower = query.lower()
        keywords = [kw for kw in query_lower.split() if len(kw) > 3]
        if not keywords:
            return []
        return [
            s
            for s in skills
            if any(
                kw in s.metadata.get("skill", "").lower()
                or kw in s.page_content.lower()
                for kw in keywords
            )
        ]

    @log_call(logger=logger)
    async def as_context(self, user_context: UserContext, query: str) -> str:
        matched = await self.search(user_context, query)
        if not matched:
            return ""
        lines = ["## Relevant Skills"]
        for s in matched:
            lines.append(f"- **{s.metadata['skill']}** ({s.metadata['file']})")
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
                logger.warning("S3ProceduralService.read(%r) failed: %s", file, exc)
                return None

        content = await asyncio.to_thread(_fetch)
        if content is None:
            return None
        return Document(
            page_content=content,
            metadata={
                "source": "procedural",
                "file": file,
                "skill": _skill_name_from_filename(file),
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
                    f"S3ProceduralService.write({file!r}) failed: {exc}"
                ) from exc

        await asyncio.to_thread(_put)
        self.invalidate_cache()
        return Document(
            page_content=content,
            metadata={
                "source": "procedural",
                "file": file,
                "skill": _skill_name_from_filename(file),
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
            try:
                client.stat_object(self._bucket, key)
            except ObjectNotFoundError:
                return False
            except Exception as exc:
                logger.warning(
                    "S3ProceduralService.delete(%r): stat failed (%s) — "
                    "attempting remove regardless",
                    file,
                    exc,
                )
            try:
                client.remove_object(self._bucket, key)
            except Exception as exc:
                raise RuntimeError(
                    f"S3ProceduralService.delete({file!r}) failed: {exc}"
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


class MockProceduralService(ProceduralService):
    """Mock procedural service for unit testing."""

    def __init__(self) -> None:
        self._documents: list[Document] = []

    @log_call(logger=logger)
    def add_document(
        self,
        content: str,
        skill: str = "MockSkill",
        file: str = "SKILL-mock.md",
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
                    "source": "procedural",
                    "file": file,
                    "skill": skill,
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
            if any(
                kw in d.metadata.get("skill", "").lower()
                or kw in d.page_content.lower()
                for kw in keywords
            )
        ]

    @log_call(logger=logger)
    async def as_context(self, user_context: UserContext, query: str) -> str:
        matched = await self.search(user_context, query)
        if not matched:
            return ""
        lines = ["## Relevant Skills"]
        for s in matched:
            lines.append(f"- **{s.metadata['skill']}** ({s.metadata['file']})")
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
        for i, d in enumerate(self._documents):
            if d.metadata.get("file") == file:
                self._documents[i] = Document(
                    page_content=content,
                    metadata={
                        "source": "procedural",
                        "file": file,
                        "skill": _skill_name_from_filename(file),
                    },
                )
                return self._documents[i]
        new_doc = Document(
            page_content=content,
            metadata={
                "source": "procedural",
                "file": file,
                "skill": _skill_name_from_filename(file),
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
        # No cache to invalidate; mock is the source of truth.
        pass

    def reset(self) -> None:
        """Clear all documents."""
        self._documents.clear()
