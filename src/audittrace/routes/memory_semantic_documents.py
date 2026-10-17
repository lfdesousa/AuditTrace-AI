"""Document-level grouping for ``GET /memory/semantic`` (WU-1, closes D15).

Lives in a SIBLING module to ``routes/memory.py`` rather than inline in
that file, mirroring the established ``routes/memory_promote.py`` /
``routes/memory_md_manifest.py`` split precedent (``routes/memory.py`` is
already >3000 LOC — PYTHON-ENGINEERING skill §11, "module LOC > 2000 ->
stop adding, new work goes in a sibling module").

**The bug this closes (SPEC-2026-09-17-memory-retrieval-and-learning-loop-
telemetry, WU-1).** ``list_semantic`` returns one row per ChromaDB CHUNK,
not one row per DOCUMENT — a multi-chunk file (e.g. a build record chunked
by ``_chunk_text`` at ``CHUNK_SIZE=1500``) surfaces as several rows sharing
the same ``title``. Before this fix, ``limit=N`` therefore meant "the first
N chunks", not "the first N documents": a caller asking for the 25 most
recent decisions could get as few as 4 *distinct* documents back (measured
live, 2026-09-17) because several of the 25 chunk-rows belonged to the same
handful of large records — turning recall into a recency echo chamber that
starved older, still-relevant lessons.

**The fix.** ``title`` is the document identity (every chunk of one folded
``.md`` file gets the SAME ``title`` — the source filename, stamped by
``_flush_md_manifest``/``_merge_semantic_with_chroma``: see
``routes/memory.py::_index_md_objects``'s docstring). Group chunk-level
items by ``title`` (falling back to the chunk's own identity when ``title``
is falsy — a direct ``POST /memory/semantic`` write with no caller-supplied
title is its own single-chunk document, so grouping it with every OTHER
untitled row would UNDER-count, not over-count, which is the wrong
direction for a recall fix). ``limit``/``offset`` then page the resulting
document groups, not the underlying chunks.

**Kept, not removed** (additive-only, per the WU-1 spec's frozen
invariants): the raw chunk-level view is still reachable via
``?granularity=chunk`` — ``scripts/curator/runner.py``'s intake walk
depends on one row per physical ChromaDB id (it reads each row's content
individually via ``GET /memory/semantic/{collection}/{document_id}``), so
the default could not silently change shape out from under it; the
Curator's call site was updated to request ``granularity=chunk`` explicitly
(see that module's ``list_semantic_collection``) rather than relying on
whatever the default happened to be.

**Other known consumers of the new default (WU-1 fix-round-3 F14, fix-round-4
F23 — enumerated by grepping the repo for callers of the route, not by
reasoning about it).**
``scripts/deploy/memory.py::recall_deploy_lessons`` and
``scripts/release/memory.py::recall_release_lessons`` DELIBERATELY inherit
the document-grouped default (see both functions' docstrings). Real
consumers found reading ``GET /memory/semantic`` with no ``granularity``
param at all, so they too inherit this default: ``bff/memory_proxy.py``
(transparent forward — see that module's docstring); the LibreChat fork's
``api/server/services/AuditTraceMemory/index.js::getAllUserMemories``
(the human-facing Souvenirs panel); and — fix-round-4, F23 — the
**canonical interactive human path**, Bruno's
``bruno/audittrace/memory/semantic/01-list.bru`` and
``01b-list-paged.bru`` (both plain ``GET {{baseUrl}}/memory/semantic``,
no ``granularity`` query param). ``docs/guides/memory-backoffice.md``'s
endpoint matrix has been corrected to say so (previously documented the
route as plain "List", silent on the default's shape). None of these is a
concern (the representative row's ``key`` still addresses a real,
authorized chunk — see :func:`group_semantic_chunks_by_document`'s
docstring on which chunk is chosen), but a multi-chunk document now shows
a whole-document ``size_bytes`` against a single-chunk ``key`` in that
panel — tracked as a follow-up in the private backlog, not fixed by this
WU (out of this repo's diff).
"""

from __future__ import annotations

from typing import Any


# A manifest-tracked chunk's ``key`` is logical-collection-prefixed
# (``decisions/<hash>``); a raw ChromaDB-discovered chunk's ``key`` is
# PHYSICAL-collection-prefixed (``decisions_v2/<hash>`` —
# ``_merge_semantic_with_chroma`` builds it from the physical collection
# name it happened to scan). Both prefixes wrap the SAME deterministic
# ``_doc_id`` hash, so the hash (the part after the last ``/``) is the only
# reliable per-chunk identity when a chunk could plausibly be listed via
# EITHER path. See ``routes/memory.py::_doc_id`` for the hash formula.
def _chunk_identity(item: dict[str, Any]) -> str:
    """The stable per-chunk identity: the trailing hash of ``key``."""
    key = str(item.get("key") or "")
    return key.rsplit("/", 1)[-1] if key else key


def dedupe_semantic_chunks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse chunk rows that are the SAME physical ChromaDB id reached
    via two different discovery paths (manifest-tracked prefix vs raw
    ChromaDB-discovered prefix — see the module docstring) down to one row.

    A manifest-tracked row (``discovered`` falsy) wins over a
    ChromaDB-discovered row (``discovered`` truthy) for the same identity,
    since the manifest copy carries real authorship/timestamps; ties (both
    discovered, or both tracked — should not happen, but no assertion is
    made either way) keep the FIRST occurrence, which preserves the
    caller's existing ordering assumptions.
    """
    kept: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        identity = _chunk_identity(item)
        if not identity:
            # No key at all — cannot dedupe safely; keep every such row as
            # its own bucket using its object id so it neither vanishes
            # nor collides with a genuine chunk.
            identity = f"__no-key__{id(item)}"
        if identity not in kept:
            kept[identity] = item
            order.append(identity)
            continue
        existing = kept[identity]
        if existing.get("discovered") and not item.get("discovered"):
            kept[identity] = item
    return [kept[i] for i in order]


def _document_identity(item: dict[str, Any]) -> str:
    """The grouping key for ``group_semantic_chunks_by_document`` — the
    document's ``title`` when present (multiple chunks of one folded file
    share it), else the chunk's own identity (an untitled row is its own
    document, never merged with another untitled row — see module
    docstring for why under-counting is the fail-closed direction here)."""
    title = item.get("title")
    if isinstance(title, str) and title:
        return f"title:{title}"
    return f"chunk:{_chunk_identity(item)}"


def _coalesce_ms(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def group_semantic_chunks_by_document(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group deduplicated chunk-level items into one row per DOCUMENT.

    Each returned dict is the group's REPRESENTATIVE item (earliest
    ``created_at_ms``, ties broken by ``key`` ascending — a stable,
    deterministic choice; it is NOT guaranteed to be chunk 0 / the
    document's front matter — use ``GET
    /memory/semantic/{collection}/{document_id}?whole_document=true`` for
    that, which reconstructs the document from its deterministic chunk-id
    sequence rather than from whichever chunk happened to be selected
    here) with two aggregated fields added:

    * ``chunk_count`` — how many chunks compose the document.
    * ``size_bytes`` — the SUM of every chunk's ``size_bytes`` (the
      representative's own field would under-report a multi-chunk
      document's real size).

    ``created_at_ms``/``modified_at_ms`` are also aggregated across the
    group (min/max respectively) so sorting a document list by recency
    reflects the document's full lifetime, not just its representative
    chunk's.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in items:
        identity = _document_identity(item)
        if identity not in groups:
            groups[identity] = []
            order.append(identity)
        groups[identity].append(item)

    documents: list[dict[str, Any]] = []
    for identity in order:
        chunks = groups[identity]

        def _tiebreak(entry: dict[str, Any]) -> tuple[int, str]:
            created = _coalesce_ms(entry.get("created_at_ms"))
            return (
                created if created is not None else 0,
                str(entry.get("key") or ""),
            )

        representative = min(chunks, key=_tiebreak)
        created_values = [
            v
            for v in (_coalesce_ms(c.get("created_at_ms")) for c in chunks)
            if v is not None
        ]
        modified_values = [
            v
            for v in (_coalesce_ms(c.get("modified_at_ms")) for c in chunks)
            if v is not None
        ]
        size_values = [
            v for v in (c.get("size_bytes") for c in chunks) if isinstance(v, int)
        ]
        document = dict(representative)
        document["chunk_count"] = len(chunks)
        document["size_bytes"] = (
            sum(size_values) if size_values else representative.get("size_bytes")
        )
        if created_values:
            document["created_at_ms"] = min(created_values)
        if modified_values:
            document["modified_at_ms"] = max(modified_values)
        documents.append(document)
    return documents
