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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.documents import Document

from audittrace.db.factory import ChromaDBClient
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.services.embedder import embed_via_nomic
from audittrace.services.pagination import sort_and_paginate

logger = logging.getLogger(__name__)

# ── Pagination (backlog #15 residual, #375 / RECALL-PAGINATION-20260803) ──
#
# Hard ceiling on how many ranked candidates a single recall call will ever
# retrieve or report on, regardless of ``offset``. Protects ChromaDB query
# cost and keeps ``total`` a bounded, honest number ("true hit count up to
# MAX_RECALL_WINDOW") instead of an unbounded full-corpus count. Luis
# 2026-08-03, RESOLVED design choice (b).
MAX_RECALL_WINDOW = 500

# Closed sort/order vocabulary (R2b). ``relevance`` mirrors today's
# behaviour (ChromaDB distance ascending — closest match first);
# ``recency``/``id`` turn recall into a pageable ENUMERATION over the
# collection (ignoring the query's relevance ranking) — the mechanism that
# guarantees a caller can eventually reach any item by paging, which is the
# whole point of this change (a stuck reviewer had no way to page past a
# relevance cut-off). Each sort has its own natural default ``order`` so a
# caller who only sets ``sort`` gets the least-surprising direction.
_RECALL_VALID_SORTS = frozenset({"relevance", "recency", "id"})
_RECALL_VALID_ORDERS = frozenset({"asc", "desc"})
_RECALL_DEFAULT_ORDER: dict[str, str] = {
    "relevance": "asc",  # closest match first (today's behaviour)
    "recency": "desc",  # newest first
    "id": "asc",
}


@dataclass(frozen=True)
class SearchPage:
    """A paginated, sorted window of semantic search results.

    ``total`` is the true candidate count discovered while building this
    page, bounded by :data:`MAX_RECALL_WINDOW` — never the length of
    ``matches`` (the historical bug: the tool result's ``total`` used to lie
    by reporting ``len(matches)``, so a caller had no way to tell whether
    more existed). ``has_more`` is computed once, here, so every caller gets
    the same ``offset + limit < total`` formula instead of re-deriving it
    (and possibly getting it wrong) at each call site.
    """

    matches: list[Document]
    total: int
    limit: int
    offset: int
    sort: str
    order: str
    has_more: bool


def _distance_key(doc: Document) -> float:
    """Sort key for ``sort="relevance"`` — the raw ChromaDB distance, lower
    is closer. A distance-less / degraded match (``None``) coalesces to
    ``+inf`` so it always sorts as the WORST match rather than crashing the
    comparison or accidentally sorting first."""
    distance = doc.metadata.get("distance")
    return float(distance) if isinstance(distance, int | float) else float("inf")


def _recency_key(doc: Document) -> int:
    """Sort key for ``sort="recency"`` — the chunk's ingestion timestamp
    (``ingestion_ts_ms``, stamped by the PDF indexing pipeline;
    ``.md``-sourced chunks do not carry one yet). Coalesces missing/non-int
    values to ``0`` (total-order safe, R2b) so undated chunks sort as
    OLDEST rather than raising or floating to an arbitrary position."""
    ts = doc.metadata.get("ingestion_ts_ms")
    return ts if isinstance(ts, int) else 0


def _durable_sort_id(doc: Document) -> str:
    """Sort key for ``sort="id"`` and the stable tiebreak for every sort.

    Mirrors ``tools.memory_handlers._recall_identity_fields``'s fallback
    chain (chunk_id, else file → source → title → chunk_id → collection) so
    the tiebreak/enumeration order lines up with the ``id`` the LLM actually
    sees in the shaped tool result. Deliberately NOT imported from that
    module (this package must not depend on ``tools/``, which depends on
    this one) — keep the two in sync if the fallback chain ever changes.
    """
    m = doc.metadata
    source_ref = (
        m.get("file")
        or m.get("source")
        or m.get("title")
        or m.get("chunk_id")
        or m.get("collection")
        or ""
    )
    chunk_id = m.get("chunk_id")
    return str(chunk_id if chunk_id else source_ref)


def _recall_key_fn(sort: str) -> Callable[[Document], Any]:
    if sort == "recency":
        return _recency_key
    if sort == "id":
        return _durable_sort_id
    return _distance_key


class SemanticService(ABC):
    """Abstract semantic memory service — vector search."""

    @abstractmethod
    async def search(
        self,
        user_context: UserContext,
        query: str,
        k: int = 4,
        collections: list[str] | None = None,
    ) -> list[Document]:
        """Search for relevant documents across collections."""

    async def search_page(
        self,
        user_context: UserContext,
        query: str,
        k: int = 4,
        collections: list[str] | None = None,
        *,
        offset: int = 0,
        sort: str = "relevance",
        order: str | None = None,
    ) -> SearchPage:
        """Paginated, sorted window over the same candidates ``search()``
        draws from (backlog #15 residual, #375 / RECALL-PAGINATION-20260803).

        ``search()`` keeps its original contract byte-for-byte (existing
        callers — ``context_builder.py``, every pre-existing test double —
        are unaffected); this method is additive. ``search()``'s own
        ``k``/``collections`` params are unchanged, only ``offset`` /
        ``sort`` / ``order`` are new, keyword-only, defaulted so a call with
        none of them is behaviourally identical to today's top-k.

        This is a CONCRETE (non-abstract) default: it fetches a bounded
        window via ``self.search()`` and applies the shared
        :func:`audittrace.services.pagination.sort_and_paginate` core. It
        exists so every ``SemanticService`` implementation — including test
        doubles that only implement ``search()`` — satisfies the pagination
        contract without hand-rolling it. It has one honest limitation:
        because ``search()`` returns a flat list with no "is this window
        exhausted" signal, ``total``/``has_more`` here can UNDER-report when
        the window is saturated (ambiguous: window exactly full could mean
        "exactly that many exist" or "more exist beyond the window") —
        acceptable for a mock/wrapper fallback. ``ChromaSemanticService``
        overrides this with an efficient, unambiguous, ChromaDB-native
        implementation (see there for why); production traffic always goes
        through that override, never this fallback.
        """
        if offset < 0:
            offset = 0
        if k < 1:
            k = 1
        sort = sort if sort in _RECALL_VALID_SORTS else "relevance"
        effective_order = (
            order if order in _RECALL_VALID_ORDERS else _RECALL_DEFAULT_ORDER[sort]
        )
        window = min(offset + k, MAX_RECALL_WINDOW)
        docs = await self.search(user_context, query, k=window, collections=collections)
        page, total = sort_and_paginate(
            docs,
            key_fn=_recall_key_fn(sort),
            tiebreak_fn=_durable_sort_id,
            reverse=(effective_order != "asc"),
            limit=k,
            offset=offset,
        )
        return SearchPage(
            matches=page,
            total=total,
            limit=k,
            offset=offset,
            sort=sort,
            order=effective_order,
            has_more=offset + k < total,
        )

    @abstractmethod
    async def available_collections(self) -> list[str]:
        """List available ChromaDB collections."""

    @abstractmethod
    async def upsert(
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
    async def delete_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> bool:
        """Hard-delete a document from a ChromaDB collection. Returns
        ``True`` if the document existed and was removed, ``False`` if
        it didn't exist."""

    @abstractmethod
    async def get_document(
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
        *,
        embed_url: str = "",
        embed_model: str = "nomic-embed-text",
    ):
        self._client = client
        self._default_collections = default_collections or ["audittrace"]
        # ADR-047 — vectors are always computed on the dedicated nomic
        # server; collections are opened with embedding_function=None so
        # ChromaDB never embeds client-side.
        self._embed_url = embed_url
        self._embed_model = embed_model

    async def _embed_one(self, text: str) -> list[float]:
        """Vectorise a single string on the nomic server."""
        vectors = await embed_via_nomic(
            [text], embed_url=self._embed_url, model=self._embed_model
        )
        return vectors[0]

    @staticmethod
    def _physical(name: str) -> str:
        """Physical ChromaDB collection name for the nomic (768-dim)
        generation. The ``_v2`` suffix keeps these disjoint from the legacy
        in-process (384-dim) collections, so the cutover is non-destructive
        and re-indexing is incremental (ADR-047)."""
        return f"{name}_v2"

    @log_call(logger=logger)
    async def search(
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
                collection = await self._client.get_or_create_collection(
                    name=self._physical(col_name), embedding_function=None
                )
                count = await collection.count()
                if count == 0:
                    continue
                # Vectorise the query on the nomic server; an embed failure
                # raises EmbeddingServerError, which the surrounding except
                # degrades to "no hits for this collection" (same posture as
                # a Chroma failure).
                query_kwargs: dict[str, Any] = {
                    "query_embeddings": [await self._embed_one(query)],
                    "n_results": min(k, count),
                    # ``distances`` rides along so each match records HOW
                    # strongly it matched (#372 D2). ``ids`` is always returned
                    # by ChromaDB and IS the stable ``document_id`` supplied at
                    # upsert — we now thread it through instead of using it only
                    # as a loop counter, so a ``tool_calls`` row names the exact
                    # passage that shaped an answer, not just its text (#372 D1).
                    "include": ["documents", "metadatas", "distances"],
                }
                if where is not None:
                    query_kwargs["where"] = where
                results = await collection.query(**query_kwargs)
                ids_row = results["ids"][0]
                # ``distances`` may be absent (older client / include dropped)
                # or None — guard so a distance-less response never raises and
                # ``distance`` degrades to ``None`` for that match.
                distances = results.get("distances")
                dist_row = distances[0] if distances else None
                for i in range(len(ids_row)):
                    doc_content = results["documents"][0][i]
                    doc_metadata = (
                        results["metadatas"][0][i] if results.get("metadatas") else {}
                    )
                    distance = (
                        dist_row[i]
                        if dist_row is not None and i < len(dist_row)
                        else None
                    )
                    all_docs.append(
                        Document(
                            page_content=doc_content,
                            metadata={
                                **doc_metadata,
                                "collection": col_name,
                                # #372 D1 — the ChromaDB id is the durable
                                # document_id from upsert; recording it lets a
                                # reconstruction query fetch this exact passage.
                                "chunk_id": ids_row[i],
                                # #372 D2 — the RAW ChromaDB distance (lower =
                                # closer). Recorded honestly named, not converted
                                # to a similarity score.
                                "distance": distance,
                            },
                        )
                    )
            except Exception as e:
                logger.warning(
                    "Semantic search failed on collection %s: %s", col_name, e
                )

        return all_docs

    @staticmethod
    def _shape_query_results(results: dict[str, Any], col_name: str) -> list[Document]:
        """Shape a ``collection.query()`` response into ``Document`` objects
        (the #372 D1/D2 chunk_id + distance threading), shared between
        ``search()``'s inline loop body and ``search_page()``'s relevance
        path so the shaping logic has one home even though the two callers'
        surrounding loops differ enough not to share the loop itself."""
        docs: list[Document] = []
        ids_row = results["ids"][0]
        distances = results.get("distances")
        dist_row = distances[0] if distances else None
        for i in range(len(ids_row)):
            doc_content = results["documents"][0][i]
            doc_metadata = (
                results["metadatas"][0][i] if results.get("metadatas") else {}
            )
            distance = (
                dist_row[i] if dist_row is not None and i < len(dist_row) else None
            )
            docs.append(
                Document(
                    page_content=doc_content,
                    metadata={
                        **doc_metadata,
                        "collection": col_name,
                        "chunk_id": ids_row[i],
                        "distance": distance,
                    },
                )
            )
        return docs

    @staticmethod
    def _shape_get_results(results: dict[str, Any], col_name: str) -> list[Document]:
        """Shape a ``collection.get()`` response (non-ranked listing, no
        ``distances``) into ``Document`` objects for the enumeration sort
        paths (recency/id) in ``search_page()``."""
        docs: list[Document] = []
        ids_row = results.get("ids") or []
        documents_row = results.get("documents") or []
        metadatas_row = results.get("metadatas") or []
        for i in range(len(ids_row)):
            doc_content = documents_row[i] if i < len(documents_row) else ""
            doc_metadata = metadatas_row[i] if i < len(metadatas_row) else {}
            docs.append(
                Document(
                    page_content=doc_content,
                    metadata={
                        **doc_metadata,
                        "collection": col_name,
                        "chunk_id": ids_row[i],
                        "distance": None,
                    },
                )
            )
        return docs

    @log_call(logger=logger)
    async def search_page(
        self,
        user_context: UserContext,
        query: str,
        k: int = 4,
        collections: list[str] | None = None,
        *,
        offset: int = 0,
        sort: str = "relevance",
        order: str | None = None,
    ) -> SearchPage:
        """The real, efficient, ChromaDB-native paginated search (R1/R2/R2b,
        #375 / RECALL-PAGINATION-20260803). Overrides the ABC's generic
        ``search()``-based fallback with two retrieval strategies chosen by
        ``sort``/``order``:

        * ``sort="relevance"`` with ``order in (None, "asc")`` — today's
          default path. ChromaDB's ``query()`` already ranks nearest-first,
          so a BOUNDED window is enough: per collection, fetch
          ``n_results = min(offset + k + 1, MAX_RECALL_WINDOW, count)`` —
          one MORE than the page needs (the "+1 probe" — not in the
          RATIFIED spec's literal formula, but a necessary refinement: it
          is what makes ``total``/``has_more`` internally consistent with
          the spec's own ``has_more = offset + k < total`` formula; without
          it, a fully-saturated window would report ``total == offset + k``
          and ``has_more`` would wrongly read ``False`` even though more
          candidates exist — reintroducing the exact "no way to page
          forward" defect this change fixes. Flagged as a resolved
          deviation, not silently reinterpreted).

          PROOF this window is safe to merge across multiple target
          collections: if a document ranks in the GLOBAL top-W (W =
          offset+k+1) across every target collection combined, at most W-1
          OTHER documents (from any collection) can have a strictly lower
          distance — so at most W-1 documents from its OWN collection can
          outrank it locally, meaning it ranks at position <= W within that
          collection's native (distance-ascending) order too. Fetching each
          collection's own top-W and merging is therefore guaranteed to
          contain the true global top-W merged pool.

          ``total = len(merged pool)``: exact when a collection returned
          FEWER than requested (that collection is provably exhausted); an
          honest, verified LOWER BOUND when every collection was saturated
          (returned == requested — "at least this many exist"). Paired with
          ``has_more = offset + k < total`` this is never a lie either way:
          a saturated collection alone contributes >= offset+k+1 candidates
          to the pool, so ``total`` is then always strictly > offset+k and
          ``has_more`` correctly comes out ``True``.

        * Any other ``sort``/``order`` (``recency``, ``id``, or
          ``relevance``+``desc``) — ChromaDB's vector index has no way to
          rank by anything but embedding distance, so a distance-bounded
          window cannot be trusted to contain the true top-W by recency/id
          (a recent-but-semantically-distant document could sit entirely
          outside the relevance window). These sorts turn recall into a
          pageable ENUMERATION (R2b) — exactly the mechanism a caller stuck
          below a relevance cut-off needs — so this path uses ChromaDB's
          non-ranked, ``where``-filtered ``get(limit=MAX_RECALL_WINDOW)``
          per collection instead of ``query()``, then applies the shared
          ``sort_and_paginate`` core locally. Documented limitation: when
          the true where-filtered candidate count exceeds
          MAX_RECALL_WINDOW, ``get()`` has no ORDER BY to guarantee which
          candidates land inside that window — bounded by the same hard cap
          as the relevance path, just without ChromaDB doing the ranking.

        Non-admin callers get the same per-user ``where`` filter as
        ``search()``. A call with no ``offset``/``sort``/``order`` returns
        the same relevance top-k as today (R2 backward-compat): with
        ``offset=0``, the probe window is ``min(k + 1, MAX_RECALL_WINDOW)``
        and the returned page is still exactly the top ``k`` by distance.
        """
        if offset < 0:
            offset = 0
        if k < 1:
            k = 1
        sort = sort if sort in _RECALL_VALID_SORTS else "relevance"
        effective_order = (
            order if order in _RECALL_VALID_ORDERS else _RECALL_DEFAULT_ORDER[sort]
        )

        target_collections = collections or self._default_collections
        where: dict[str, Any] | None = None
        if not user_context.is_admin:
            where = {"user_id": user_context.user_id}

        efficient = sort == "relevance" and effective_order == "asc"
        pool: list[Document] = []

        if efficient:
            probe_window = min(offset + k + 1, MAX_RECALL_WINDOW)
            query_embedding: list[float] | None = None
            for col_name in target_collections:
                try:
                    collection = await self._client.get_or_create_collection(
                        name=self._physical(col_name), embedding_function=None
                    )
                    count = await collection.count()
                    if count == 0:
                        continue
                    if query_embedding is None:
                        query_embedding = await self._embed_one(query)
                    query_kwargs: dict[str, Any] = {
                        "query_embeddings": [query_embedding],
                        "n_results": min(probe_window, count),
                        "include": ["documents", "metadatas", "distances"],
                    }
                    if where is not None:
                        query_kwargs["where"] = where
                    results = await collection.query(**query_kwargs)
                    pool.extend(self._shape_query_results(results, col_name))
                except Exception as e:
                    logger.warning(
                        "search_page (relevance) failed on collection %s: %s",
                        col_name,
                        e,
                    )
        else:
            for col_name in target_collections:
                try:
                    collection = await self._client.get_or_create_collection(
                        name=self._physical(col_name), embedding_function=None
                    )
                    get_kwargs: dict[str, Any] = {
                        "limit": MAX_RECALL_WINDOW,
                        "include": ["documents", "metadatas"],
                    }
                    if where is not None:
                        get_kwargs["where"] = where
                    results = await collection.get(**get_kwargs)
                    pool.extend(self._shape_get_results(results, col_name))
                except Exception as e:
                    logger.warning(
                        "search_page (%s) failed on collection %s: %s",
                        sort,
                        col_name,
                        e,
                    )

        page, total = sort_and_paginate(
            pool,
            key_fn=_recall_key_fn(sort),
            tiebreak_fn=_durable_sort_id,
            reverse=(effective_order != "asc"),
            limit=k,
            offset=offset,
        )
        return SearchPage(
            matches=page,
            total=total,
            limit=k,
            offset=offset,
            sort=sort,
            order=effective_order,
            has_more=offset + k < total,
        )

    @log_call(logger=logger)
    async def available_collections(self) -> list[str]:
        """List all collections in ChromaDB."""
        try:
            return [c.name for c in await self._client.list_collections()]  # type: ignore[attr-defined]
        except Exception:
            return []

    @log_call(logger=logger)
    async def upsert(
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
        col = await self._client.get_or_create_collection(
            name=self._physical(collection), embedding_function=None
        )
        # ChromaDB's `upsert` is exactly what we want — insert or replace.
        # The vector is computed on the nomic server and supplied explicitly;
        # an embed failure propagates so a write never silently drops the doc.
        await col.upsert(
            ids=[document_id],
            documents=[text],
            embeddings=[await self._embed_one(text)],
            metadatas=[meta],
        )

    @log_call(logger=logger)
    async def delete_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> bool:
        del user_context  # operator-side write; not user-scoped
        col = await self._client.get_or_create_collection(
            name=self._physical(collection), embedding_function=None
        )
        # ChromaDB's `delete` is silently idempotent (deleting a non-
        # existent ID does not raise). To return a faithful boolean we
        # check existence first.
        existing = await col.get(ids=[document_id], include=["documents"])
        if not existing.get("ids"):
            return False
        await col.delete(ids=[document_id])
        return True

    @log_call(logger=logger)
    async def get_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> Document | None:
        del user_context  # operator-side read; admin scope gates the route
        col = await self._client.get_or_create_collection(
            name=self._physical(collection), embedding_function=None
        )
        result = await col.get(ids=[document_id], include=["documents", "metadatas"])
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
    async def search(
        self,
        user_context: UserContext,
        query: str,
        k: int = 4,
        collections: list[str] | None = None,
    ) -> list[Document]:
        # Ignore the per-call user_context in favour of the bound one.
        # This is deliberate — see class docstring.
        del user_context
        return await self._inner.search(self._bound_user, query, k, collections)

    @log_call(logger=logger)
    async def search_page(
        self,
        user_context: UserContext,
        query: str,
        k: int = 4,
        collections: list[str] | None = None,
        *,
        offset: int = 0,
        sort: str = "relevance",
        order: str | None = None,
    ) -> SearchPage:
        # Same override rationale as `search()` — bound identity wins, and
        # delegating to the inner service's OWN `search_page` (rather than
        # falling through to the ABC default via `self.search`) means a
        # ``ChromaSemanticService`` inner gets its efficient, unambiguous
        # implementation in production (dependencies.py wires exactly this
        # wrapper around a `ChromaSemanticService`).
        del user_context
        return await self._inner.search_page(
            self._bound_user,
            query,
            k,
            collections,
            offset=offset,
            sort=sort,
            order=order,
        )

    @log_call(logger=logger)
    async def available_collections(self) -> list[str]:
        return await self._inner.available_collections()

    @log_call(logger=logger)
    async def upsert(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        del user_context
        await self._inner.upsert(
            self._bound_user, collection, document_id, text, metadata
        )

    @log_call(logger=logger)
    async def delete_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> bool:
        del user_context
        return await self._inner.delete_document(
            self._bound_user, collection, document_id
        )

    @log_call(logger=logger)
    async def get_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> Document | None:
        del user_context
        return await self._inner.get_document(self._bound_user, collection, document_id)


class MockSemanticService(SemanticService):
    """Mock semantic service for unit testing."""

    def __init__(self) -> None:
        self._docs: dict[str, list[Document]] = {}

    @log_call(logger=logger)
    async def add_document(
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
    async def search(
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
    async def available_collections(self) -> list[str]:
        return list(self._docs.keys())

    @log_call(logger=logger)
    async def upsert(
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
    async def delete_document(
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
    async def get_document(
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
