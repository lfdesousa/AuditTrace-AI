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

ADR-062 Phase B (WU-B2): the layer is now **two-tier** — same shape as
:mod:`audittrace.services.episodic`. A caller's own skills live under the
**private** tier (``{private_bucket}/{jwt.sub}/procedural/``); the
pre-existing ``memory-shared`` content is the **corpus** tier (Layer 5,
ADR-062 §1 — shared-read, deliberately unfiltered). Every method derives
``user_id`` from ``user_context.user_id`` (the validated JWT ``sub``, never
a request parameter — ADR-027 §1):

* ``write`` / ``delete`` target the **private** tier only.
* ``read`` tries the private key first, falls back to the corpus key, and
  tags ``metadata["tier"]`` with whichever tier answered.
* ``load`` / ``search`` / ``as_context`` return **private ∪ corpus**, each
  row/Document stamped ``tier`` (``"private"`` | ``"corpus"``); a private
  object shadows a same-named corpus object.
"""

import asyncio
import io
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
    layer_list_cache_key_private,
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
        """Load all SKILL documents visible to the caller (private ∪ corpus)."""

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
        characters (``..``, ``/``) are rejected. Tries the caller's private
        tier first, then falls back to the corpus.
        """

    @abstractmethod
    async def write(
        self, user_context: UserContext, file: str, content: str
    ) -> Document:
        """Create or replace a SKILL document in the caller's **private**
        tier. Returns the persisted ``Document``. Same filename validation
        as ``read``. Caller's private cache entry invalidated on success."""

    @abstractmethod
    async def delete(self, user_context: UserContext, file: str) -> bool:
        """Hard-delete the object from the caller's **private** tier.
        Returns ``True`` if deleted, ``False`` if it didn't exist. Caller's
        private cache entry invalidated on success. Never deletes corpus
        (Layer 5) content — see ADR-062 §6."""

    @abstractmethod
    def invalidate_cache(self) -> None:
        """Drop the shared CORPUS listing cache so the next ``load()``
        re-fetches Layer 5 from the backing store. Private-tier caches
        self-invalidate inside ``write``/``delete``."""


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
    """S3/MinIO-backed procedural service — dual-tier (ADR-062 Phase B).

    Corpus (Layer 5) content lives in ``bucket`` under the ``procedural/``
    prefix — the pre-Phase-B ``memory-shared`` content, shared-read by
    design. Private (per-user) content lives in ``private_bucket`` under
    ``{user_id}/procedural/`` — never visible to another caller.

    Enumeration includes **every** ``.md`` object under a prefix via the
    shared :func:`~audittrace.services.layer_listing.list_layer_objects`
    helper — the same set ``read()`` fetches by key and ``/memory/index``
    embeds (backlog #15, Defect A).

    Each tier's listing is cached independently through the shared
    :class:`~audittrace.services.layer_cache.LayerCacheStore` (Redis in
    production): the corpus key is fleet-shared
    (:func:`~audittrace.services.layer_cache.layer_list_cache_key`); the
    private key is additionally keyed by ``user_id``
    (:func:`~audittrace.services.layer_cache.layer_list_cache_key_private`)
    — the security boundary that stops the fail-open cache from leaking one
    caller's private listing to another. A TTL self-heals any missed
    invalidation. The ``read()`` path bypasses the cache and does a direct
    ``get_object``.
    """

    _LAYER = "procedural"

    def __init__(
        self,
        minio_client: object,
        bucket: str,
        private_bucket: str,
        prefix: str = "procedural/",
        *,
        cache: LayerCacheStore | None = None,
        cache_ttl_seconds: int = _DEFAULT_CACHE_TTL_SECONDS,
    ):
        self._client = minio_client  # minio.Minio instance
        self._bucket = bucket
        self._private_bucket = private_bucket
        self._prefix = prefix
        self._cache_store: LayerCacheStore = (
            cache if cache is not None else InMemoryLayerCacheStore()
        )
        self._cache_ttl = cache_ttl_seconds
        self._cache_key = layer_list_cache_key(self._LAYER)

    def _read_tier_rows(
        self, bucket: str, prefix: str, tier: str
    ) -> list[dict[str, Any]]:
        """Enumerate + read every ``.md`` object under ``bucket``/``prefix``
        as serialisable, cacheable rows tagged with ``tier``.

        Never raises — a backend failure logs and yields what was read so far.
        """
        rows: list[dict[str, Any]] = []
        try:
            client: Any = self._client
            for obj in list_layer_objects(client, bucket, prefix):
                filename = obj["filename"]
                if not is_listable_md_object(filename):
                    continue
                with client.get_object(bucket, obj["key"]) as response:
                    content = response.read().decode("utf-8")
                rows.append(
                    {
                        "file": filename,
                        "content": content,
                        "skill": _skill_name_from_filename(filename),
                        "last_modified_ms": obj["last_modified_ms"],
                        "tier": tier,
                    }
                )
        except Exception as exc:
            logger.warning("S3ProceduralService load failed (tier=%s): %s", tier, exc)
        return rows

    def _corpus_rows(self) -> list[dict[str, Any]]:
        """Shared-cache read-through for the corpus tier. Fail-open."""
        cached = self._cache_store.get(self._cache_key)
        if cached is not None:
            return cached
        rows = self._read_tier_rows(self._bucket, self._prefix, "corpus")
        self._cache_store.set(self._cache_key, rows, self._cache_ttl)
        return rows

    def _private_rows(self, user_id: str) -> list[dict[str, Any]]:
        """User-keyed cache read-through for the private tier. Fail-open."""
        key = layer_list_cache_key_private(self._LAYER, user_id)
        cached = self._cache_store.get(key)
        if cached is not None:
            return cached
        rows = self._read_tier_rows(
            self._private_bucket, f"{user_id}/{self._prefix}", "private"
        )
        self._cache_store.set(key, rows, self._cache_ttl)
        return rows

    def _merged_rows(self, user_id: str) -> list[dict[str, Any]]:
        """Private ∪ corpus, deduped by ``file`` — private shadows corpus.

        Fetches the private tier FIRST, corpus tier SECOND (the corpus
        ``list_objects`` call is therefore always the *last* one a test
        double observes via ``call_args``), but the merge always lets
        private rows win a filename collision regardless of fetch order.
        """
        private_rows = self._private_rows(user_id)
        corpus_rows = self._corpus_rows()
        merged: dict[str, dict[str, Any]] = {row["file"]: row for row in corpus_rows}
        merged.update({row["file"]: row for row in private_rows})
        return list(merged.values())

    @staticmethod
    def _row_to_document(row: dict[str, Any]) -> Document:
        return Document(
            page_content=row["content"],
            metadata={
                "source": "procedural",
                "file": row["file"],
                "skill": row["skill"],
                "last_modified_ms": row.get("last_modified_ms"),
                "tier": row.get("tier", "corpus"),
            },
        )

    @log_call(logger=logger)
    async def load(self, user_context: UserContext) -> list[Document]:
        user_id = user_context.user_id
        # minio SDK is sync-only → offload the blocking S3 listing/get to a
        # worker thread so the event loop stays free (PYTHON-ENGINEERING §3).
        rows = await asyncio.to_thread(self._merged_rows, user_id)
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
        user_id = user_context.user_id
        if not _validate_filename(file):
            return None
        private_key = f"{user_id}/{self._prefix}{file}"
        corpus_key = f"{self._prefix}{file}"

        def _fetch() -> tuple[str, str] | None:
            client: Any = self._client
            try:
                with client.get_object(self._private_bucket, private_key) as response:
                    return str(response.read().decode("utf-8")), "private"
            except ObjectNotFoundError:
                pass
            except Exception as exc:
                logger.warning(
                    "S3ProceduralService.read(%r) private-tier lookup failed: %s",
                    file,
                    exc,
                )
            try:
                with client.get_object(self._bucket, corpus_key) as response:
                    return str(response.read().decode("utf-8")), "corpus"
            except ObjectNotFoundError:
                return None
            except Exception as exc:
                # Network / auth / transient — preserved dual-arm behaviour
                # so a backend hiccup does NOT surface as a 500 to the caller.
                logger.warning("S3ProceduralService.read(%r) failed: %s", file, exc)
                return None

        result = await asyncio.to_thread(_fetch)
        if result is None:
            return None
        content, tier = result
        return Document(
            page_content=content,
            metadata={
                "source": "procedural",
                "file": file,
                "skill": _skill_name_from_filename(file),
                "tier": tier,
            },
        )

    @log_call(logger=logger)
    async def write(
        self, user_context: UserContext, file: str, content: str
    ) -> Document:
        user_id = user_context.user_id
        if not _validate_filename(file):
            raise ValueError(f"invalid filename: {file!r}")

        key = f"{user_id}/{self._prefix}{file}"
        body = content.encode("utf-8")

        def _put() -> None:
            client: Any = self._client
            try:
                with io.BytesIO(body) as buf:
                    client.put_object(self._private_bucket, key, buf, length=len(body))
            except Exception as exc:
                raise RuntimeError(
                    f"S3ProceduralService.write({file!r}) failed: {exc}"
                ) from exc

        await asyncio.to_thread(_put)
        self._invalidate_private(user_id)
        return Document(
            page_content=content,
            metadata={
                "source": "procedural",
                "file": file,
                "skill": _skill_name_from_filename(file),
                "tier": "private",
            },
        )

    @log_call(logger=logger)
    async def delete(self, user_context: UserContext, file: str) -> bool:
        user_id = user_context.user_id
        if not _validate_filename(file):
            return False
        key = f"{user_id}/{self._prefix}{file}"

        def _delete() -> bool:
            client: Any = self._client
            try:
                client.stat_object(self._private_bucket, key)
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
                client.remove_object(self._private_bucket, key)
            except Exception as exc:
                raise RuntimeError(
                    f"S3ProceduralService.delete({file!r}) failed: {exc}"
                ) from exc
            return True

        deleted = await asyncio.to_thread(_delete)
        if deleted:
            self._invalidate_private(user_id)
        return deleted

    def _invalidate_private(self, user_id: str) -> None:
        """Drop the caller's private listing cache entry (fleet-wide)."""
        self._cache_store.invalidate(layer_list_cache_key_private(self._LAYER, user_id))

    @log_call(logger=logger)
    def invalidate_cache(self) -> None:
        # Shared corpus store → one invalidation is seen by every replica
        # (backlog #15, Defect B). TTL is the safety net for a missed call.
        self._cache_store.invalidate(self._cache_key)


class MockProceduralService(ProceduralService):
    """Mock procedural service for unit testing — dual-tier (ADR-062 Phase B).

    Corpus documents (``tier="corpus"``, the default — matches pre-Phase-B
    behaviour for existing callers of :meth:`add_document`) are visible to
    every caller. Private documents are keyed by the owning ``user_id`` and
    are visible only when ``user_context.user_id`` matches.
    """

    def __init__(self) -> None:
        self._corpus: list[Document] = []
        self._private: dict[str, list[Document]] = {}

    @log_call(logger=logger)
    def add_document(
        self,
        content: str,
        skill: str = "MockSkill",
        file: str = "SKILL-mock.md",
        last_modified_ms: int | None = None,
        *,
        tier: str = "corpus",
        user_id: str | None = None,
    ) -> None:
        """Add a document for testing.

        ``last_modified_ms`` mirrors the real S3 ``last_modified`` timestamp
        the discovered-entry merge reads (backlog #15, R4); ``None`` keeps the
        pre-R4 "no timestamp" shape. ``tier="private"`` requires ``user_id``
        (ADR-062 Phase B) and stores the document only for that caller.
        """
        doc = Document(
            page_content=content,
            metadata={
                "source": "procedural",
                "file": file,
                "skill": skill,
                "last_modified_ms": last_modified_ms,
                "tier": tier,
            },
        )
        if tier == "private":
            if not user_id:
                raise ValueError("tier='private' requires a user_id")
            self._private.setdefault(user_id, []).append(doc)
        else:
            self._corpus.append(doc)

    def _merged(self, user_id: str) -> list[Document]:
        merged: dict[str, Document] = {d.metadata["file"]: d for d in self._corpus}
        merged.update({d.metadata["file"]: d for d in self._private.get(user_id, [])})
        return list(merged.values())

    @log_call(logger=logger)
    async def load(self, user_context: UserContext) -> list[Document]:
        return self._merged(user_context.user_id)

    @log_call(logger=logger)
    async def search(self, user_context: UserContext, query: str) -> list[Document]:
        docs = await self.load(user_context)
        query_lower = query.lower()
        keywords = [kw for kw in query_lower.split() if len(kw) > 3]
        if not keywords:
            return []
        return [
            d
            for d in docs
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
        if not _validate_filename(file):
            return None
        user_id = user_context.user_id
        for d in self._private.get(user_id, []):
            if d.metadata.get("file") == file:
                return d
        for d in self._corpus:
            if d.metadata.get("file") == file:
                return d
        return None

    @log_call(logger=logger)
    async def write(
        self, user_context: UserContext, file: str, content: str
    ) -> Document:
        if not _validate_filename(file):
            raise ValueError(f"invalid filename: {file!r}")
        bucket = self._private.setdefault(user_context.user_id, [])
        new_doc = Document(
            page_content=content,
            metadata={
                "source": "procedural",
                "file": file,
                "skill": _skill_name_from_filename(file),
                "tier": "private",
            },
        )
        for i, d in enumerate(bucket):
            if d.metadata.get("file") == file:
                bucket[i] = new_doc
                return new_doc
        bucket.append(new_doc)
        return new_doc

    @log_call(logger=logger)
    async def delete(self, user_context: UserContext, file: str) -> bool:
        if not _validate_filename(file):
            return False
        bucket = self._private.get(user_context.user_id, [])
        for i, d in enumerate(bucket):
            if d.metadata.get("file") == file:
                bucket.pop(i)
                return True
        return False

    @log_call(logger=logger)
    def invalidate_cache(self) -> None:
        # No cache to invalidate; mock is the source of truth.
        pass

    def reset(self) -> None:
        """Clear all documents (both tiers)."""
        self._corpus.clear()
        self._private.clear()


class UserScopedProceduralService(ProceduralService):
    """Request-scoped wrapper that binds a ``UserContext`` at construction
    time and overrides any ``user_context`` passed at call time.

    ADR-062 Phase B (WU-B1) — mirrors
    :class:`audittrace.services.semantic.UserScopedSemanticService`. The
    private-tier prefix (``{user_id}/procedural/``) is derived from
    ``user_context.user_id`` inside the wrapped service on every call; this
    wrapper makes the identity that seeds that derivation **true by
    construction** — even if upstream code accidentally threads a different
    (or admin) ``UserContext`` into a shared ``DefaultContextBuilder``, the
    wrapper substitutes the identity bound at ``get_context_builder()``
    request-dependency resolution time instead.
    """

    def __init__(self, inner: ProceduralService, user_context: UserContext):
        self._inner = inner
        self._bound_user = user_context

    @log_call(logger=logger)
    async def load(self, user_context: UserContext) -> list[Document]:
        del user_context  # bound identity wins — see class docstring
        return await self._inner.load(self._bound_user)

    @log_call(logger=logger)
    async def search(self, user_context: UserContext, query: str) -> list[Document]:
        del user_context
        return await self._inner.search(self._bound_user, query)

    @log_call(logger=logger)
    async def as_context(self, user_context: UserContext, query: str) -> str:
        del user_context
        return await self._inner.as_context(self._bound_user, query)

    @log_call(logger=logger)
    async def read(self, user_context: UserContext, file: str) -> Document | None:
        del user_context
        return await self._inner.read(self._bound_user, file)

    @log_call(logger=logger)
    async def write(
        self, user_context: UserContext, file: str, content: str
    ) -> Document:
        del user_context
        return await self._inner.write(self._bound_user, file, content)

    @log_call(logger=logger)
    async def delete(self, user_context: UserContext, file: str) -> bool:
        del user_context
        return await self._inner.delete(self._bound_user, file)

    @log_call(logger=logger)
    def invalidate_cache(self) -> None:
        self._inner.invalidate_cache()
