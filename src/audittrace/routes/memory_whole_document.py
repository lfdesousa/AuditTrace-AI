"""Whole-document read of a folded document: whole and verified, or refused.

A fold (``routes.memory._index_md_objects``) stamps every chunk it writes with
:data:`STAMP_TOTAL` (how many chunks the fold wrote) and :data:`STAMP_FOLD`
(sha256 of the text it chunked). A whole-document read returns the text only
when every chunk ``0..total-1`` is readable, carries chunk 0's stamp, and the
reassembled text hashes to it. Anything else is a
:class:`SemanticDocumentIncompleteError`, never a partial ``200``.

Rows folded before the stamp existed are read window by window until the first
miss and reported ``verified=False``: nothing in them says where the document
ends or which fold a chunk belongs to.
"""

from __future__ import annotations

import hashlib
from typing import Any

from audittrace.identity import UserContext
from audittrace.services.semantic_document_incomplete_error import (
    SemanticDocumentIncompleteError,
)

# How many chunk ids one read asks ChromaDB for per round trip. Not a cap on
# the document: the reader keeps going until the stamped total or a miss.
CHUNK_READ_WINDOW = 500
STAMP_TOTAL = "chunk_total"
STAMP_FOLD = "fold_sha256"


def chunk_id(collection: str, source: str, chunk_idx: int) -> str:
    """The deterministic id of chunk *chunk_idx* of *source* in *collection*."""
    raw = f"{collection}:{source}:{chunk_idx}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def document_chunk_ids(collection: str, title: str, start: int, stop: int) -> list[str]:
    """The chunk ids ``[start, stop)`` of one folded document."""
    return [chunk_id(collection, title, i) for i in range(start, stop)]


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fold_stamp(text: str, chunk_count: int) -> dict[str, Any]:
    """The stamp one fold puts on every chunk it writes."""
    return {STAMP_TOTAL: chunk_count, STAMP_FOLD: text_sha256(text)}


def read_stamp(metadata: dict[str, Any]) -> tuple[int, str] | None:
    """``(total, fold)`` of a stamped chunk, ``None`` for an unstamped one.
    Half a stamp, or one of the wrong type, is not a stamp anybody wrote."""
    total, fold = metadata.get(STAMP_TOTAL), metadata.get(STAMP_FOLD)
    if total is None and fold is None:
        return None
    if isinstance(total, int) and not isinstance(total, bool) and total > 0:
        if isinstance(fold, str) and fold:
            return total, fold
    raise SemanticDocumentIncompleteError("semantic document carries a malformed stamp")


def reassemble_chunk_sequence(chunk_docs: list[Any], overlap: int) -> str:
    """De-overlap and join a contiguous chunk sequence back into its text:
    every chunk after the first repeats the last *overlap* characters of the
    one before it (see ``routes.memory._chunk_text``)."""
    parts = [chunk_docs[0].page_content]
    parts.extend(doc.page_content[overlap:] for doc in chunk_docs[1:])
    return "".join(parts)


def _incomplete(readable: int, total: int) -> SemanticDocumentIncompleteError:
    return SemanticDocumentIncompleteError(
        f"semantic document incomplete: {readable} of {total} chunks readable"
    )


async def gather_whole_document(
    service: Any,
    user: UserContext,
    collection: str,
    document_id: str,
    doc: Any,
    *,
    overlap: int,
) -> tuple[list[str], list[Any], bool]:
    """Every chunk of the document *doc* belongs to, in order, and whether the
    result is VERIFIED against the fold's stamp.

    Returns ``(chunk_ids, chunk_docs, verified)``. Raises
    :class:`SemanticDocumentIncompleteError` for a stamped document that cannot
    be returned whole. The single-chunk fallback (``[document_id], [doc]``) is
    for UNSTAMPED rows only: a direct write with no title, or a legacy row
    whose sequence does not reproduce."""
    requested = read_stamp(doc.metadata)
    title = doc.metadata.get("title") or doc.metadata.get("source")
    if not isinstance(title, str) or not title:
        if requested is not None:
            raise _incomplete(0, requested[0])
        return [document_id], [doc], False

    chunk_ids: list[str] = []
    chunk_docs: list[Any] = []
    head: tuple[int, str] | None = None
    complete = False
    while not complete:
        start = len(chunk_ids)
        stop = start + CHUNK_READ_WINDOW
        if head is not None:
            stop = min(stop, head[0])
        window = document_chunk_ids(collection, title, start, stop)
        # One batched read per window (LC-9 WP3: owner ids and the caller's
        # legacy ids together; missing ids come back as None).
        fetched = await service.get_documents(user, collection, window)
        for cid, candidate in zip(window, fetched, strict=True):
            if candidate is None:
                complete = True
                break
            chunk_ids.append(cid)
            chunk_docs.append(candidate)
        if start == 0 and chunk_docs:
            head = read_stamp(chunk_docs[0].metadata)
            if head is not None:
                del chunk_ids[head[0] :], chunk_docs[head[0] :]
        if head is not None and len(chunk_ids) >= head[0]:
            complete = True

    if head is None:
        # Chunk 0 is missing or unstamped. A stamp anywhere else means a fold
        # wrote part of this document: it is not a legacy row.
        stamped = requested is not None or any(
            read_stamp(c.metadata) is not None for c in chunk_docs
        )
        if stamped:
            total = requested[0] if requested is not None else len(chunk_docs)
            raise _incomplete(0, total)
        if not chunk_ids:
            return [document_id], [doc], False
        return chunk_ids, chunk_docs, False

    same_fold = 0
    for candidate in chunk_docs:
        if read_stamp(candidate.metadata) != head:
            break
        same_fold += 1
    if same_fold != head[0]:
        raise _incomplete(same_fold, head[0])
    if text_sha256(reassemble_chunk_sequence(chunk_docs, overlap)) != head[1]:
        raise SemanticDocumentIncompleteError(
            "semantic document does not match the text its fold wrote"
        )
    return chunk_ids, chunk_docs, True
