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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.documents import Document

from audittrace.db.factory import ChromaDBClient
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.services.embedder import embed_via_nomic
from audittrace.services.pagination import sort_and_paginate

logger = logging.getLogger(__name__)


class _ManifestTombstoneCheck(Protocol):
    """Structural type for the one manifest capability recall needs.

    Deliberately NOT importing ``MemoryManifestService`` — this module
    should not gain a hard dependency on the manifest module just to
    honour ADR-062 §6's soft-delete tombstone (WU-A5). Both
    ``MemoryManifestService`` and ``MockMemoryManifestService`` satisfy
    this protocol structurally.
    """

    async def get_deleted_keys(self, layer: str, keys: Iterable[str]) -> set[str]: ...


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
        *,
        tier: str = "private",
    ) -> None:
        """Insert or replace a single document in ``collection`` keyed by
        ``document_id``. The collection's embedding function (configured
        when the collection was created) handles vectorisation. The
        item's ``user_id`` field in ``metadata`` is set from
        ``user_context`` if not already provided so the existing per-user
        ``where`` filter in ``search()`` continues to apply.

        ADR-062 Phase B (D1): ``tier`` selects the physical write target —
        ``"private"`` (default) writes to the caller's per-user collection
        stamped ``tier="private"``; ``"corpus"`` writes to the shared
        collection stamped ``tier="corpus"`` (read unfiltered by every
        caller). The corpus-write SCOPE GATE
        (``memory:corpus:<collection>:write``) is enforced at the route
        layer in WU-B5 — this parameter only wires the write TARGET so
        WU-B5 has something to gate; it does not itself authorize a
        promote.

        ``metadata["tier"]`` and ``metadata["user_id"]`` are ALWAYS
        overwritten from ``tier``/``user_context`` (WU-B5 review fix,
        2026-08-04) — never honoured from caller-supplied ``metadata``,
        even if the caller is an operator. Both are TOKEN-DERIVED /
        route-authorized values (ADR-027 §1); a caller-supplied override
        used to be silently accepted (``dict.setdefault``), which let a
        writer holding only ``memory:semantic:write`` smuggle
        ``metadata={"tier": "corpus"}`` past the entire WU-B5 promote
        gate (the gate only ever inspected the top-level body/query
        signal) and forge another user's ``user_id`` for attribution."""

    @abstractmethod
    async def delete_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> bool:
        """Hard-delete a document from a ChromaDB collection. Returns
        ``True`` if the document existed and was removed, ``False`` if
        it didn't exist OR the caller is not authorized to delete it
        (ADR-062 Phase B §3 — no existence disclosure, see
        ``ChromaSemanticService.delete_document``)."""

    @abstractmethod
    async def get_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> Document | None:
        """Fetch a single document by ID. Returns ``None`` if the
        document doesn't exist in the collection OR the caller is not
        authorized to read it (ADR-062 Phase B §3 — a cross-user read of
        a private-tier document returns the SAME ``None`` as a genuine
        miss; no existence disclosure)."""


class ChromaSemanticService(SemanticService):
    """ChromaDB-based semantic memory service."""

    def __init__(
        self,
        client: ChromaDBClient,
        default_collections: list[str] | None = None,
        *,
        embed_url: str = "",
        embed_model: str = "nomic-embed-text",
        manifest: _ManifestTombstoneCheck | None = None,
    ):
        self._client = client
        self._default_collections = default_collections or ["audittrace"]
        # ADR-047 — vectors are always computed on the dedicated nomic
        # server; collections are opened with embedding_function=None so
        # ChromaDB never embeds client-side.
        self._embed_url = embed_url
        self._embed_model = embed_model
        # ADR-062 §6 (WU-A5) — optional so every existing call site /
        # test double that constructs this class with just ``client=``
        # keeps working unchanged; production wiring
        # (``dependencies.py``) passes the real manifest so recall
        # honours the soft-delete tombstone. ``None`` here means "this
        # service instance cannot filter soft-deletes", not "nothing is
        # deleted" — see ``_filter_soft_deleted``.
        self._manifest = manifest

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

    @staticmethod
    def _physical_corpus(name: str) -> str:
        """Physical ChromaDB collection name for the CORPUS tier of
        ``name`` (ADR-062 Phase B, D1). A genuinely SEPARATE collection —
        never merged into ``_physical`` via ``$or``/``$in`` — because the
        in-repo mock only supports flat-equality ``where``
        (``db/factory.py:183-269``); two independent flat-equality queries
        (one ``where``-scoped, one unfiltered) are mock-safe where a
        single ``$or`` query would not be. Separate collections also give
        PHYSICAL isolation: a bug in the ``where`` filter can never leak a
        private row through a corpus read, because the private row simply
        does not live in this collection. Keeps the same ``_v2`` (nomic)
        generation suffix as ``_physical`` so a corpus write never
        collides with a legacy 384-dim collection."""
        return f"{name}_corpus_v2"

    @staticmethod
    def _tier_authorized(meta: dict[str, Any], user_context: UserContext) -> bool:
        """ADR-062 Phase B §3 ownership predicate — shared by
        ``get_document`` and ``delete_document`` (the two formerly-
        unfiltered reads/writes-by-id). A non-admin caller is authorized
        for a document iff they own it (``metadata["user_id"] ==
        caller``) OR the document is corpus-tier
        (``metadata["tier"] == "corpus"``, deliberately unfiltered — D1).
        Admins are always authorized, mirroring ``search()``/
        ``search_page()``, which never filter for admin callers."""
        if user_context.is_admin:
            return True
        return (
            meta.get("user_id") == user_context.user_id or meta.get("tier") == "corpus"
        )

    async def _query_tier(
        self,
        physical_name: str,
        query_embedding: list[float],
        n_results: int,
        where: dict[str, Any] | None,
        col_name: str,
        tier: str,
    ) -> list[Document]:
        """Query ONE physical ChromaDB collection — either the private or
        the corpus physical name for ``col_name`` — and tag every result
        with ``metadata["tier"]`` (ADR-062 Phase B, D1). Isolated
        exception handling per physical collection: a corpus-collection
        outage must not take the private-tier results down with it, and
        vice versa (mirrors the pre-Phase-B per-collection isolation)."""
        try:
            collection = await self._client.get_or_create_collection(
                name=physical_name, embedding_function=None
            )
            count = await collection.count()
            if count == 0:
                return []
            query_kwargs: dict[str, Any] = {
                "query_embeddings": [query_embedding],
                "n_results": min(n_results, count),
                # ``distances`` rides along so each match records HOW
                # strongly it matched (#372 D2). ``ids`` is always returned
                # by ChromaDB and IS the stable ``document_id`` supplied at
                # upsert.
                "include": ["documents", "metadatas", "distances"],
            }
            if where is not None:
                query_kwargs["where"] = where
            results = await collection.query(**query_kwargs)
        except Exception as e:
            logger.warning(
                "Semantic search failed on physical collection %s (tier=%s): %s",
                physical_name,
                tier,
                e,
            )
            return []
        docs = self._shape_query_results(results, col_name)
        for doc in docs:
            doc.metadata["tier"] = tier
        return docs

    @log_call(logger=logger)
    async def search(
        self,
        user_context: UserContext,
        query: str,
        k: int = 4,
        collections: list[str] | None = None,
    ) -> list[Document]:
        """Search across ChromaDB collections. No arbitrary caps.

        ADR-062 Phase B (D1): each requested (logical) collection resolves
        to TWO physical ChromaDB collections — a per-user PRIVATE one
        (``_physical``, ``where={"user_id": ...}`` for non-admin, #383)
        and a shared CORPUS one (``_physical_corpus``, always unfiltered).
        Both are queried and the results merged, each tagged
        ``metadata["tier"]`` (``"private"``/``"corpus"``). Two separate
        flat-equality queries — never ``$or``/``$in`` (the in-repo mock
        only supports flat-equality ``where``, ``db/factory.py``).

        Non-admin callers get the private-tier ``where={"user_id": ...}``
        filter (#383, unchanged since Phase 2/4). Admins (including the
        bypass-mode sentinel) see every private-tier row too — this
        predates Phase B. The corpus-tier query has NO filter for anyone —
        that is the deliberate shared-read design (D1), not a gap.
        """
        target_collections = collections or self._default_collections
        all_docs: list[Document] = []

        where: dict[str, Any] | None = None
        if not user_context.is_admin:
            where = {"user_id": user_context.user_id}

        # Vectorise the query once on the nomic server (shared across both
        # tiers and every target collection); an embed failure degrades the
        # whole call to "no hits" rather than raising, same posture as a
        # per-collection Chroma failure.
        try:
            query_embedding = await self._embed_one(query)
        except Exception as e:
            logger.warning("Semantic search embed failed: %s", e)
            return []

        for col_name in target_collections:
            all_docs.extend(
                await self._query_tier(
                    self._physical(col_name),
                    query_embedding,
                    k,
                    where,
                    col_name,
                    "private",
                )
            )
            all_docs.extend(
                await self._query_tier(
                    self._physical_corpus(col_name),
                    query_embedding,
                    k,
                    None,
                    col_name,
                    "corpus",
                )
            )

        return await self._filter_soft_deleted(all_docs)

    async def _filter_soft_deleted(self, docs: list[Document]) -> list[Document]:
        """Drop candidates whose manifest row is soft-deleted (ADR-062 §6).

        The Postgres manifest is authoritative for corpus soft-delete
        (WU-A5); ChromaDB itself has no delete state for a
        ``/memory/semantic``-CRUD-created document, so without this check
        a soft-deleted item would keep surfacing via recall even though
        the backoffice list already hides it (CS-7 / #374 — soft-delete
        and the underlying Chroma vector were previously independent).

        No-ops (returns ``docs`` unfiltered) when this service was
        constructed without a manifest (test doubles, legacy wiring) OR
        when the manifest lookup itself fails — fail-open on the
        FILTERING CAPABILITY (a manifest outage must not take recall
        down with it), never a silent "nothing is deleted" assumption
        once the manifest IS reachable and says otherwise.
        """
        if self._manifest is None or not docs:
            return docs
        keys = {
            f"{doc.metadata.get('collection')}/{doc.metadata.get('chunk_id')}"
            for doc in docs
        }
        try:
            deleted = await self._manifest.get_deleted_keys("semantic", keys)
        except Exception as exc:
            logger.warning(
                "soft-delete tombstone check failed — candidates NOT "
                "filtered this call: %s",
                exc,
            )
            return docs
        if not deleted:
            return docs
        return [
            doc
            for doc in docs
            if f"{doc.metadata.get('collection')}/{doc.metadata.get('chunk_id')}"
            not in deleted
        ]

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

    async def _get_tier(
        self,
        physical_name: str,
        where: dict[str, Any] | None,
        col_name: str,
        tier: str,
    ) -> list[Document]:
        """Non-ranked ``.get()`` fetch of ONE physical collection (private
        OR corpus) for the enumeration sort paths (recency/id), tagged
        with ``metadata["tier"]`` (ADR-062 Phase B, D1)."""
        try:
            collection = await self._client.get_or_create_collection(
                name=physical_name, embedding_function=None
            )
            get_kwargs: dict[str, Any] = {
                "limit": MAX_RECALL_WINDOW,
                "include": ["documents", "metadatas"],
            }
            if where is not None:
                get_kwargs["where"] = where
            results = await collection.get(**get_kwargs)
        except Exception as e:
            logger.warning(
                "search_page enumeration failed on physical collection %s "
                "(tier=%s): %s",
                physical_name,
                tier,
                e,
            )
            return []
        docs = self._shape_get_results(results, col_name)
        for doc in docs:
            doc.metadata["tier"] = tier
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
          contain the true global top-W merged pool. ADR-062 Phase B (D1)
          extends this WITHOUT changing the proof: each logical collection
          now resolves to two PHYSICAL collections (private + corpus,
          ``_physical``/``_physical_corpus``), each queried independently
          at its own top-W — the proof only relies on "every physical
          collection queried contributes its own top-W", which does not
          care whether a physical collection is the private or the corpus
          half of a logical name.

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
        ``search()``, applied ONLY to the private physical collection; the
        corpus physical collection is always queried unfiltered and every
        result — either tier — is tagged ``metadata["tier"]``
        (ADR-062 Phase B, D1). A call with no ``offset``/``sort``/``order``
        returns the same relevance top-k as today (R2 backward-compat):
        with ``offset=0``, the probe window is
        ``min(k + 1, MAX_RECALL_WINDOW)`` and the returned page is still
        exactly the top ``k`` by distance.
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
            # Vectorise once, shared across both tiers and every target
            # collection — an embed failure degrades the whole call to an
            # empty pool rather than raising (same posture as search()).
            try:
                query_embedding: list[float] | None = await self._embed_one(query)
            except Exception as e:
                logger.warning("search_page (relevance) embed failed: %s", e)
                query_embedding = None
            if query_embedding is not None:
                for col_name in target_collections:
                    pool.extend(
                        await self._query_tier(
                            self._physical(col_name),
                            query_embedding,
                            probe_window,
                            where,
                            col_name,
                            "private",
                        )
                    )
                    pool.extend(
                        await self._query_tier(
                            self._physical_corpus(col_name),
                            query_embedding,
                            probe_window,
                            None,
                            col_name,
                            "corpus",
                        )
                    )
        else:
            for col_name in target_collections:
                pool.extend(
                    await self._get_tier(
                        self._physical(col_name), where, col_name, "private"
                    )
                )
                pool.extend(
                    await self._get_tier(
                        self._physical_corpus(col_name), None, col_name, "corpus"
                    )
                )

        # ADR-062 §6 (WU-A5) — drop soft-deleted candidates before paging.
        # Filtering here (rather than post-page) means a soft-deleted item
        # can never occupy a page slot; the documented "honest lower
        # bound when saturated" property of total/has_more above is
        # preserved (filtering only ever shrinks the pool, never invents
        # extra unseen candidates), so the "never lies" guarantee holds —
        # a caller may need one extra page fetch in the rare case a probe
        # window was saturated with tombstoned candidates, but total is
        # never overstated.
        pool = await self._filter_soft_deleted(pool)

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
        *,
        tier: str = "private",
    ) -> None:
        # Stamp user_id + tier into the metadata UNCONDITIONALLY (WU-B5
        # review fix, 2026-08-04) — both are TOKEN-DERIVED / route-
        # authorized values (ADR-027 §1), never caller-supplied. This
        # used to be `dict.setdefault`, which silently HONOURED a
        # caller-provided override — a writer holding only
        # `memory:semantic:write` could smuggle
        # `metadata={"tier": "corpus"}` past the entire WU-B5 promote
        # gate (the gate only ever inspected the top-level body/query
        # `tier`/`?promote=` signal, never nested `metadata`), and the
        # forged tag was then trusted by `_tier_authorized` on every
        # subsequent read — an unauthorized-promote + attribution-
        # forgery bypass. Direct assignment makes both fields
        # true-by-construction: no caller input can ever reach the
        # stored value.
        meta = dict(metadata or {})
        meta["user_id"] = user_context.user_id
        if tier == "corpus":
            # ADR-062 Phase B (D1): the corpus write TARGET. The
            # promote SCOPE-GATE (memory:corpus:<collection>:write) is
            # enforced by the route layer in WU-B5, not here.
            physical_name = self._physical_corpus(collection)
            meta["tier"] = "corpus"
        else:
            physical_name = self._physical(collection)
            meta["tier"] = "private"
        col = await self._client.get_or_create_collection(
            name=physical_name, embedding_function=None
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

    async def _get_from_physical(
        self, physical_name: str, logical_collection: str, document_id: str
    ) -> Document | None:
        """Fetch a document by id from ONE physical ChromaDB collection,
        with NO ownership check — the caller applies
        ``_tier_authorized``."""
        col = await self._client.get_or_create_collection(
            name=physical_name, embedding_function=None
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
            metadata={**meta, "collection": logical_collection},
        )

    @log_call(logger=logger)
    async def delete_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> bool:
        """Hard-delete a document by id.

        ADR-062 Phase B (§3 hole 1) mirrors ``get_document``'s ownership
        check — this method previously ``del user_context`` unconditionally.
        Checks the private physical collection first, then the corpus one;
        returns ``False`` (not a distinct "forbidden" signal) both when
        the id genuinely doesn't exist AND when it exists but the caller
        isn't authorized — the same no-existence-disclosure posture as
        ``get_document``. The route layer additionally requires
        ``audittrace:admin`` for ``?hard=true`` (``delete_semantic``), so
        in practice only admins reach this today; the service-level check
        is defense in depth for any future caller."""
        for physical_name in (
            self._physical(collection),
            self._physical_corpus(collection),
        ):
            col = await self._client.get_or_create_collection(
                name=physical_name, embedding_function=None
            )
            # ChromaDB's `delete` is silently idempotent (deleting a non-
            # existent ID does not raise). To return a faithful boolean we
            # check existence (and ownership) first.
            existing = await col.get(
                ids=[document_id], include=["documents", "metadatas"]
            )
            if not existing.get("ids"):
                continue
            meta = (existing.get("metadatas") or [{}])[0] or {}
            if not self._tier_authorized(meta, user_context):
                return False
            await col.delete(ids=[document_id])
            return True
        return False

    @log_call(logger=logger)
    async def get_document(
        self,
        user_context: UserContext,
        collection: str,
        document_id: str,
    ) -> Document | None:
        """Fetch a single document by id.

        ADR-062 Phase B (§3 hole 1) closes the formerly-unfiltered read:
        before this change ANY caller with ``memory:semantic:read`` could
        read ANY document by id regardless of ownership (``del
        user_context`` at the top of this method). Now a document is
        returned only if the caller is admin, the document is
        corpus-tier, or the caller owns it (``_tier_authorized``). A
        cross-user attempt returns ``None`` — the SAME signal as a
        genuine miss, so the route layer's 404 never discloses that the
        document exists under someone else's identity."""
        doc = await self._get_from_physical(
            self._physical(collection), collection, document_id
        )
        if doc is None:
            doc = await self._get_from_physical(
                self._physical_corpus(collection), collection, document_id
            )
        if doc is None:
            return None
        if not self._tier_authorized(doc.metadata, user_context):
            return None
        return doc


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
        *,
        tier: str = "private",
    ) -> None:
        del user_context
        await self._inner.upsert(
            self._bound_user, collection, document_id, text, metadata, tier=tier
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
        *,
        tier: str = "private",
    ) -> None:
        # mock: no scoping. This double deliberately does NOT enforce the
        # ADR-062 Phase B ownership predicate — real isolation is proven
        # against ChromaSemanticService (see its docstrings). `tier` is
        # still recorded so route-layer code that reads it back (e.g. a
        # future backoffice tier column) sees a consistent value.
        del user_context
        meta = dict(metadata or {})
        meta.setdefault("collection", collection)
        meta.setdefault("document_id", document_id)
        meta.setdefault("tier", tier)
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
