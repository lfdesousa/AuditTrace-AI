"""Semantic memory service — Layer 4 of the 4-layer memory architecture (ADR-018).

Wraps ChromaDB for vector-similarity search across multiple collections.
Uses the existing ChromaDBClient protocol from db/factory.py.

DESIGN §15 Phase 2: every method takes ``user_context: UserContext`` as the
first positional argument. ``ChromaSemanticService.search`` applies a
``where={"user_id": user_context.user_id}`` filter when the caller is NOT
admin — a preview of the Phase 4 ChromaDB scoped wrapper. Admins see
every row, which keeps the sentinel-backed test fixtures visible.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any

from langchain_core.documents import Document

from audittrace.db.factory import ChromaDBClient
from audittrace.identity import UserContext
from audittrace.logging_config import log_call

logger = logging.getLogger(__name__)


class SemanticService(ABC):
    """Abstract semantic memory service — vector search."""

    @abstractmethod
    def search(
        self,
        user_context: UserContext,
        query: str,
        k: int = 4,
        collections: list[str] | None = None,
    ) -> list[Document]:
        """Search for relevant documents across collections."""

    @abstractmethod
    def available_collections(self) -> list[str]:
        """List available ChromaDB collections."""

    @abstractmethod
    def upsert(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert or replace a single document in ``collection`` keyed by
        ``document_id``. The collection's embedding function (configured
        when the collection was created) handles vectorisation. The
        item's ``user_id`` field in ``metadata`` is set from
        ``user_context`` if not already provided so the existing per-user
        ``where`` filter in ``search()`` continues to apply."""

    @abstractmethod
    def delete_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> bool:
        """Hard-delete a document from a ChromaDB collection. Returns
        ``True`` if the document existed and was removed, ``False`` if
        it didn't exist."""

    @abstractmethod
    def get_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> Document | None:
        """Fetch a single document by ID. Returns ``None`` if the
        document doesn't exist in the collection."""


class ChromaSemanticService(SemanticService):
    """ChromaDB-based semantic memory service."""

    def __init__(
        self,
        client: ChromaDBClient,
        default_collections: list[str] | None = None,
    ):
        self._client = client
        self._default_collections = default_collections or ["audittrace"]

    @log_call(logger=logger)
    def search(
        self,
        user_context: UserContext,
        query: str,
        k: int = 4,
        collections: list[str] | None = None,
    ) -> list[Document]:
        """Search across ChromaDB collections. No arbitrary caps.

        Non-admin callers get a ``where={"user_id": ...}`` filter so they
        only see rows they own. Admins (including the bypass-mode sentinel)
        see every row in the collection — no filter applied. This is a
        Phase 4 preview; the authoritative ChromaDB scoped wrapper lands
        with the RLS + cross-user isolation test work.
        """
        target_collections = collections or self._default_collections
        all_docs: list[Document] = []

        where: dict[str, Any] | None = None
        if not user_context.is_admin:
            where = {"user_id": user_context.user_id}

        for col_name in target_collections:
            try:
                collection = self._client.get_or_create_collection(name=col_name)
                count = collection.count()
                if count == 0:
                    continue
                query_kwargs: dict[str, Any] = {
                    "query_texts": [query],
                    "n_results": min(k, count),
                    "include": ["documents", "metadatas"],
                }
                if where is not None:
                    query_kwargs["where"] = where
                results = collection.query(**query_kwargs)
                for i in range(len(results["ids"][0])):
                    doc_content = results["documents"][0][i]  # type: ignore[index]
                    doc_metadata = (
                        results["metadatas"][0][i] if results.get("metadatas") else {}  # type: ignore[index]
                    )
                    all_docs.append(
                        Document(
                            page_content=doc_content,
                            metadata={**doc_metadata, "collection": col_name},
                        )
                    )
            except Exception as e:
                logger.warning(
                    "Semantic search failed on collection %s: %s", col_name, e
                )

        return all_docs

    @log_call(logger=logger)
    def available_collections(self) -> list[str]:
        """List all collections in ChromaDB."""
        try:
            return [c.name for c in self._client.list_collections()]  # type: ignore[attr-defined]
        except Exception:
            return []

    @log_call(logger=logger)
    def upsert(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        # Stamp user_id into the metadata so the per-user `where` filter
        # in `search()` keeps working for the new doc. Operator overrides
        # are honoured if the caller provided their own user_id.
        meta = dict(metadata or {})
        meta.setdefault("user_id", user_context.user_id)
        col = self._client.get_or_create_collection(name=collection)
        # ChromaDB's `upsert` is exactly what we want — insert or replace.
        col.upsert(ids=[document_id], documents=[text], metadatas=[meta])

    @log_call(logger=logger)
    def delete_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> bool:
        del user_context  # operator-side write; not user-scoped
        col = self._client.get_or_create_collection(name=collection)
        # ChromaDB's `delete` is silently idempotent (deleting a non-
        # existent ID does not raise). To return a faithful boolean we
        # check existence first.
        existing = col.get(ids=[document_id], include=["documents"])
        if not existing.get("ids"):
            return False
        col.delete(ids=[document_id])
        return True

    @log_call(logger=logger)
    def get_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> Document | None:
        del user_context  # operator-side read; admin scope gates the route
        col = self._client.get_or_create_collection(name=collection)
        result = col.get(ids=[document_id], include=["documents", "metadatas"])
        if not result.get("ids"):
            return None
        documents = result.get("documents") or []
        if not documents:
            return None
        meta = (result.get("metadatas") or [{}])[0] or {}
        return Document(
            page_content=documents[0],
            metadata={**meta, "collection": collection},
        )


class UserScopedSemanticService(SemanticService):
    """Request-scoped wrapper that binds a ``UserContext`` at construction
    time and overrides any ``user_context`` passed at call time.

    DESIGN §16 Phase 4: complements the Postgres RLS policies from
    migration 005. ChromaDB has no native RLS equivalent, so this
    wrapper is how we enforce the per-user ``where`` filter at the
    infrastructure seam.

    The wrapper makes the isolation property **true by construction**:
    even if upstream code accidentally passes an admin context to a
    non-admin user's request handler, the wrapper uses the bound
    identity — the one the request's ``require_user`` dependency
    resolved — instead of the per-call argument. A future service-
    code bug cannot leak data across users.

    If the binding itself carries an admin ``UserContext`` (e.g. the
    sentinel bypass or a real admin JWT), the wrapper delegates with
    admin semantics and the filter is bypassed. Authority is frozen
    at construction time, not trustable per call.
    """

    def __init__(self, inner: SemanticService, user_context: UserContext):
        self._inner = inner
        self._bound_user = user_context

    @log_call(logger=logger)
    def search(
        self,
        user_context: UserContext,
        query: str,
        k: int = 4,
        collections: list[str] | None = None,
    ) -> list[Document]:
        # Ignore the per-call user_context in favour of the bound one.
        # This is deliberate — see class docstring.
        del user_context
        return self._inner.search(self._bound_user, query, k, collections)

    @log_call(logger=logger)
    def available_collections(self) -> list[str]:
        return self._inner.available_collections()

    @log_call(logger=logger)
    def upsert(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        del user_context
        self._inner.upsert(self._bound_user, collection, document_id, text, metadata)

    @log_call(logger=logger)
    def delete_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> bool:
        del user_context
        return self._inner.delete_document(self._bound_user, collection, document_id)

    @log_call(logger=logger)
    def get_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> Document | None:
        del user_context
        return self._inner.get_document(self._bound_user, collection, document_id)


class MockSemanticService(SemanticService):
    """Mock semantic service for unit testing."""

    def __init__(self) -> None:
        self._docs: dict[str, list[Document]] = {}

    @log_call(logger=logger)
    def add_document(
        self, content: str, source: str = "mock", collection: str = "default"
    ) -> None:
        """Add a document to a collection for testing."""
        if collection not in self._docs:
            self._docs[collection] = []
        self._docs[collection].append(
            Document(
                page_content=content,
                metadata={"source": source, "collection": collection},
            )
        )

    @log_call(logger=logger)
    def search(
        self,
        user_context: UserContext,
        query: str,
        k: int = 4,
        collections: list[str] | None = None,
    ) -> list[Document]:
        del user_context  # mock: no scoping — admin-like behaviour
        query_lower = query.lower()
        keywords = [kw for kw in query_lower.split() if len(kw) > 3]
        results: list[Document] = []
        target = collections or list(self._docs.keys())
        for col in target:
            for doc in self._docs.get(col, []):
                if not keywords or any(
                    kw in doc.page_content.lower() for kw in keywords
                ):
                    results.append(doc)
        return results[:k]

    @log_call(logger=logger)
    def available_collections(self) -> list[str]:
        return list(self._docs.keys())

    @log_call(logger=logger)
    def upsert(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        del user_context  # mock: no scoping
        meta = dict(metadata or {})
        meta.setdefault("collection", collection)
        meta.setdefault("document_id", document_id)
        if collection not in self._docs:
            self._docs[collection] = []
        # Replace if same document_id, else append.
        for i, d in enumerate(self._docs[collection]):
            if d.metadata.get("document_id") == document_id:
                self._docs[collection][i] = Document(page_content=text, metadata=meta)
                return
        self._docs[collection].append(Document(page_content=text, metadata=meta))

    @log_call(logger=logger)
    def delete_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> bool:
        del user_context  # mock: no scoping
        for i, d in enumerate(self._docs.get(collection, [])):
            if d.metadata.get("document_id") == document_id:
                self._docs[collection].pop(i)
                return True
        return False

    @log_call(logger=logger)
    def get_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> Document | None:
        del user_context  # mock: no scoping
        for d in self._docs.get(collection, []):
            if d.metadata.get("document_id") == document_id:
                return d
        return None

    def reset(self) -> None:
        """Clear all documents."""
        self._docs.clear()
