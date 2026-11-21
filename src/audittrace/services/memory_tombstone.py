"""The tombstone rules of a soft-deleted memory document, in one place.

A soft delete writes ``deleted_at_ms`` on the manifest row; this module is what
every reader and every delete asks about it (SPEC SOFTDELETE-DIRECT-READ).

* **Direct reads** (``GET /memory/...``, the ``read_decision`` / ``read_skill``
  tools): a read is refused, with the reply of a key that never existed, when
  the row it resolves is a tombstone and the caller cannot read tombstones
  (:func:`is_hidden`, :func:`any_hidden`).
* **The document, not the row.** A folded document is a sequence of chunk rows.
  A delete of one chunk tombstones the whole sequence
  (:func:`tombstone_semantic_document`) and a delete of a source tombstones the
  folds it wrote (:func:`tombstone_fold_copies`); a read of any chunk of a
  document with a tombstoned row is refused (:func:`any_hidden` over
  :func:`stamped_chunk_ids`).
* **``/context``** drops tombstoned documents for every caller
  (:func:`without_tombstoned_files`, :func:`without_tombstoned_chunks`).
* **A tombstoned source is not folded again**
  (:func:`source_is_tombstoned`).

Fail closed: no function here wraps a manifest lookup in ``suppress`` or a broad
``except``. A lookup that raises reaches the caller, which answers 5xx, an error
result, or (``/context``) drops the layer; it never serves the content.

Functions only; ``services/`` does not import ``routes/``.
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.documents import Document

from audittrace.identity import UserContext
from audittrace.services.memory_chunk_ids import (
    CHUNK_READ_WINDOW,
    document_chunk_ids,
    read_stamp,
)
from audittrace.services.memory_keys import semantic_logical_key
from audittrace.services.memory_manifest import ManifestEntry, MemoryManifestService
from audittrace.services.semantic import SemanticService, tombstone_candidates
from audittrace.services.semantic_document_incomplete_error import (
    SemanticDocumentIncompleteError,
)

# The collections ``routes.memory._index_md_objects`` folds each source layer
# into (its two call sites in ``index_memory``: episodic -> decisions and
# semantic, procedural -> skills and semantic). The PDF folds
# (``_index_pdf_objects``) are NOT here on purpose: no route can soft-delete a
# PDF source or a PDF chunk, so there is no delete to cascade from
# (SPEC ADDENDUM-2 B19; ``test_softdelete_direct_read_a2`` pins the premise).
FOLD_TARGETS: dict[str, tuple[str, ...]] = {
    "episodic": ("decisions", "semantic"),
    "procedural": ("skills", "semantic"),
}


def reads_tombstones(user: UserContext) -> bool:
    """May this caller read a tombstone? Token-derived scopes only (B1)."""
    return user.is_admin or "audittrace:admin" in user.scopes


def is_hidden(entry: ManifestEntry | None, user: UserContext) -> bool:
    """A tombstone the caller may not read."""
    if entry is None or entry.deleted_at_ms is None:
        return False
    return not reads_tombstones(user)


async def any_hidden(
    manifest: MemoryManifestService,
    layer: str,
    logical_keys: Sequence[str],
    user: UserContext,
) -> bool:
    """Is ANY of *logical_keys* a tombstone the caller may not read?

    ONE manifest round trip however many keys; raises what the manifest raises.
    """
    entries = await manifest.resolve_read_many(layer, logical_keys, user)
    return any(is_hidden(entry, user) for entry in entries.values())


def _title_of(doc: Document) -> str | None:
    title = doc.metadata.get("title") or doc.metadata.get("source")
    return title if isinstance(title, str) and title else None


def stamped_chunk_ids(collection: str, document_id: str, doc: Document) -> list[str]:
    """The chunk ids the fold's stamp names for the document *doc* belongs to.

    No I/O. ``[document_id]`` for a row that is untitled, unstamped, carries a
    malformed stamp, or whose stamped sequence does not contain it.
    """
    title = _title_of(doc)
    if title is None:
        return [document_id]
    try:
        stamp = read_stamp(doc.metadata)
    except SemanticDocumentIncompleteError:
        return [document_id]
    if stamp is None:
        return [document_id]
    ids = document_chunk_ids(collection, title, 0, stamp[0])
    return ids if document_id in ids else [document_id]


async def document_chunk_id_set(
    service: SemanticService,
    user: UserContext,
    collection: str,
    document_id: str,
    doc: Document | None,
) -> list[str]:
    """The ids a whole-document read of *doc*'s document addresses.

    Stamped: ``0..chunk_total-1``. Unstamped with a title: the sequence up to the
    first missing chunk. Otherwise the row itself.
    """
    title = _title_of(doc) if doc is not None else None
    if doc is None or title is None:
        return [document_id]
    try:
        stamp = read_stamp(doc.metadata)
    except SemanticDocumentIncompleteError:
        return [document_id]
    if stamp is not None:
        return stamped_chunk_ids(collection, document_id, doc)
    ids: list[str] = []
    while True:
        window = document_chunk_ids(
            collection, title, len(ids), len(ids) + CHUNK_READ_WINDOW
        )
        fetched = await service.get_documents(user, collection, window)
        live = [cid for cid, found in zip(window, fetched, strict=True) if found]
        ids.extend(live)
        if len(live) < len(window):
            break
    return ids if document_id in ids else [document_id]


def _same_tier_and_owner(
    rows: dict[str, ManifestEntry | None], anchor: ManifestEntry
) -> list[str]:
    """The keys of *rows* that exist at the anchor row's tier and owner."""
    return [
        key
        for key, entry in rows.items()
        if entry is not None
        and entry.tier == anchor.tier
        and entry.created_by_user_id == anchor.created_by_user_id
    ]


async def _tombstone_sequence(
    service: SemanticService,
    manifest: MemoryManifestService,
    user: UserContext,
    collection: str,
    document_id: str,
    doc: Document | None,
    anchor: ManifestEntry,
    *,
    caller_can_write_shared: bool,
) -> int:
    """Soft-delete every chunk row of the document, in one transaction; returns
    how many rows of it the manifest holds at the anchor's tier and owner."""
    ids = await document_chunk_id_set(service, user, collection, document_id, doc)
    keys = [semantic_logical_key(collection, cid) for cid in ids]
    rows = await manifest.resolve_read_many("semantic", keys, user)
    same = _same_tier_and_owner(rows, anchor)
    await manifest.record_delete_many(
        "semantic",
        same,
        user.user_id,
        caller_can_write_shared=caller_can_write_shared,
    )
    return len(same)


async def tombstone_semantic_document(
    service: SemanticService,
    manifest: MemoryManifestService,
    user: UserContext,
    collection: str,
    document_id: str,
    anchor: ManifestEntry,
    *,
    caller_can_write_shared: bool,
) -> int:
    """B9/B17: the delete of ONE chunk tombstones the whole document it belongs
    to (hard or soft), at the addressed row's tier and owner. Returns the row
    count the delete's audit row reports."""
    doc = await service.get_document(user, collection, document_id)
    return await _tombstone_sequence(
        service,
        manifest,
        user,
        collection,
        document_id,
        doc,
        anchor,
        caller_can_write_shared=caller_can_write_shared,
    )


async def tombstone_fold_copies(
    service: SemanticService,
    manifest: MemoryManifestService,
    user: UserContext,
    layer: str,
    filename: str,
    source: ManifestEntry,
    *,
    caller_can_write_shared: bool,
) -> list[tuple[str, str, int]]:
    """B8: soft-delete every chunk row a fold of ``layer/filename`` wrote.

    Folds are found by identity (chunk 0's id in each fold target), never by
    listing, and are this source's only when the chunk says
    ``category == layer`` and ``source == filename``. Rows are kept only at the
    source row's tier and owner. Returns ``(collection, chunk-0 id, rows)`` per
    fold document tombstoned.
    """
    touched: list[tuple[str, str, int]] = []
    for collection in FOLD_TARGETS[layer]:
        chunk0 = document_chunk_ids(collection, filename, 0, 1)[0]
        doc = await service.get_document(user, collection, chunk0)
        if doc is None:
            continue
        if doc.metadata.get("category") != layer:
            continue
        if doc.metadata.get("source") != filename:
            continue
        rows = await _tombstone_sequence(
            service,
            manifest,
            user,
            collection,
            chunk0,
            doc,
            source,
            caller_can_write_shared=caller_can_write_shared,
        )
        if rows:
            touched.append((collection, chunk0, rows))
    return touched


async def source_is_tombstoned(
    manifest: MemoryManifestService,
    user: UserContext,
    layer: str,
    logical: str,
    tier: str,
) -> bool:
    """B21: is the row a single-file index of ``layer/logical`` folds FROM a
    tombstone? Private: the caller's own row; corpus: the bare row. Raises what
    the manifest raises."""
    if tier == "private":
        entry = await manifest.resolve_read(layer, logical, user)
    else:
        entry = await manifest.get(layer, logical)
    return entry is not None and entry.tier == tier and entry.deleted_at_ms is not None


async def without_tombstoned_files(
    manifest: MemoryManifestService,
    layer: str,
    docs: list[Document],
    user: UserContext,
) -> list[Document]:
    """B14: *docs* minus those whose ``metadata["file"]`` is a tombstone, for
    every caller (recall semantics: an admin's ``/context`` hides them too)."""
    files = [str(doc.metadata.get("file", "")) for doc in docs]
    entries = await manifest.resolve_read_many(layer, [f for f in files if f], user)
    return [
        doc
        for doc, file in zip(docs, files, strict=True)
        if not _is_tombstone(entries.get(file))
    ]


def _is_tombstone(entry: ManifestEntry | None) -> bool:
    return entry is not None and entry.deleted_at_ms is not None


async def without_tombstoned_chunks(
    manifest: MemoryManifestService, docs: list[Document]
) -> list[Document]:
    """B18: semantic search hits minus the tombstoned ones, STRICT.

    The derivation is recall's own (:func:`tombstone_candidates`); unlike
    ``ChromaSemanticService._filter_soft_deleted`` there is no fail-open arm: a
    lookup that raises reaches the caller, which drops the layer.
    """
    if not docs:
        return docs
    candidates = [tombstone_candidates(doc) for doc in docs]
    deleted = await manifest.get_deleted_keys("semantic", set().union(*candidates))
    return [
        doc for doc, keys in zip(docs, candidates, strict=True) if not (keys & deleted)
    ]
