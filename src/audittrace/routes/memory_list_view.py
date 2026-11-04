"""List-view helpers for the ``/memory`` LIST routes (GP4-CI, 2026-11-02).

Split out of ``routes/memory.py`` (PYTHON-ENGINEERING section 11: that module
is over the 2000-LOC "stop adding" line).  Everything here is pure CPU and
I/O-free: no awaits, no sessions, no globals, so the route can run it via
``asyncio.to_thread`` and a large manifest cannot hold the event loop.
Identity is always an explicit argument (``user``/``caller``), never read
from a ContextVar inside the worker.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from audittrace.identity import UserContext
from audittrace.routes.memory_semantic_documents import (
    dedupe_semantic_chunks,
    group_semantic_chunks_by_document,
)
from audittrace.services.memory_manifest import ManifestEntry
from audittrace.services.pagination import sort_and_paginate as _core_sort_and_paginate


def entries_to_dicts(entries: list[ManifestEntry]) -> list[dict[str, Any]]:
    """``[e.to_dict() ...]`` as a pure-CPU unit for ``asyncio.to_thread``."""
    return [e.to_dict() for e in entries]


def entries_with_prefix(
    entries: list[ManifestEntry], prefix: str
) -> list[ManifestEntry]:
    """Collection-prefix filter, a pure-CPU unit for ``asyncio.to_thread``."""
    return [e for e in entries if e.key.startswith(prefix)]


def entry_keys(entries: list[ManifestEntry]) -> set[str]:
    """The dedup key set, a pure-CPU unit for ``asyncio.to_thread``."""
    return {e.key for e in entries}


_SORT_FIELD_MS = {"created_at": "created_at_ms", "modified_at": "modified_at_ms"}


def sort_and_paginate(
    items: list[dict[str, Any]],
    *,
    sort: str,
    order: str,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(page, total)`` — a total-order-safe sorted, paginated slice.

    ``total`` is the full pre-limit count so a client can page. Sorting is
    total-order safe: timestamp/size keys coalesce ``None`` → ``0`` and the
    ``key`` sort coalesces to ``""``, so no comparison ever touches ``None``
    (backlog #15, R5). Ties break on the object ``key`` ascending regardless
    of ``order`` (stable secondary sort) so paging is deterministic.

    The sort/slice mechanic itself is the shared
    :func:`audittrace.services.pagination.sort_and_paginate` core (backlog
    #15 R2b / #375) — only the field-name-to-key-function mapping below is
    specific to this endpoint's manifest-row shape.
    """

    def _int_key(field: str) -> Callable[[dict[str, Any]], Any]:
        def key(item: dict[str, Any]) -> Any:
            value = item.get(field)
            return value if isinstance(value, int) else 0

        return key

    def _str_key(item: dict[str, Any]) -> Any:
        return str(item.get("key") or "")

    primary: Callable[[dict[str, Any]], Any]
    if sort == "key":
        primary = _str_key
    elif sort == "size":
        primary = _int_key("size_bytes")
    else:
        primary = _int_key(_SORT_FIELD_MS.get(sort, "created_at_ms"))

    return _core_sort_and_paginate(
        items,
        key_fn=primary,
        tiebreak_fn=_str_key,
        reverse=(order != "asc"),
        limit=limit,
        offset=offset,
    )


def semantic_list_view(
    items: list[dict[str, Any]],
    user: UserContext,
    *,
    granularity: str,
    sort: str,
    order: str,
    limit: int,
    offset: int,
    corpus_gate: Callable[[list[dict[str, Any]], UserContext], list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], int, int, int]:
    """The pure-CPU tail of ``list_semantic`` (corpus gate, dedupe, group,
    sort, paginate) returning ``(page, total, total_documents, total_chunks)``.

    ``corpus_gate`` is the route's ADR-062 read gate, injected so this module
    does not import ``routes.memory`` (circular)."""
    items = corpus_gate(items, user)
    chunk_items = dedupe_semantic_chunks(items)
    total_chunks = len(chunk_items)
    if granularity == "chunk":
        view_items = chunk_items
        total_documents = len(group_semantic_chunks_by_document(chunk_items))
    else:
        view_items = group_semantic_chunks_by_document(chunk_items)
        total_documents = len(view_items)
    page, total = sort_and_paginate(
        view_items, sort=sort, order=order, limit=limit, offset=offset
    )
    return page, total, total_documents, total_chunks
