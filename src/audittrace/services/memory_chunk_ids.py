"""The chunk identity of a folded document: ids, window and stamp.

One fold (``routes.memory._index_md_objects``) writes a document as chunks
whose ids are a pure function of ``(collection, title, index)`` and stamps each
with :data:`STAMP_TOTAL` and :data:`STAMP_FOLD`. These helpers hold that
formula. They live under ``services/`` because the tombstone rules
(:mod:`audittrace.services.memory_tombstone`) need the same ids as the
whole-document read, and ``services/`` must not import ``routes/``.
"""

from __future__ import annotations

import hashlib
from typing import Any

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
