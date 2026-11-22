"""The cascade of a source delete: its folds are tombstoned and audited.

``DELETE /memory/episodic/{name}`` and ``DELETE /memory/procedural/{name}``
soft-delete the source row. The source was also folded into the vector
collections (``decisions`` / ``skills`` / ``semantic``), and a fold's chunk rows
carry their own manifest rows: left alone they stay readable and recallable
after the source is gone. Rule B8 of SPEC SOFTDELETE-DIRECT-READ: the folds are
tombstoned BEFORE the source row, and rule B16: every tombstone is explained by
a ``delete`` audit row. The tombstone logic lives in
:mod:`audittrace.services.memory_tombstone`; this module is the route-side
sequence (it audits, so it sits beside ``memory_write_audit``).
"""

from __future__ import annotations

from typing import Any

from audittrace.identity import UserContext
from audittrace.routes.memory_write_audit import emit_write_audit
from audittrace.services.memory_manifest import MemoryManifestService
from audittrace.services.memory_tombstone import tombstone_fold_copies
from audittrace.services.semantic import SemanticService

# One fold document tombstoned: (collection, chunk-0 id, the ``detail`` of the
# ``delete`` audit row that explains it).
FoldRow = tuple[str, str, dict[str, Any]]


async def tombstone_source_folds(
    service: SemanticService,
    manifest: MemoryManifestService,
    user: UserContext,
    layer: str,
    filename: str,
    *,
    caller_can_write_shared: bool,
) -> list[FoldRow]:
    """Tombstone every fold of the caller's source ``layer/filename``.

    No source row means nothing was folded under it (and the source delete that
    follows answers its own 404). A failure propagates: nothing is deleted.
    Each returned row carries the SOURCE row's tier (B20: ``source.tier``, never
    a request value). The tier rides in each fold's ``detail`` here and
    :func:`audit_source_folds` hands it to ``emit_write_audit`` as the row-level
    ``tier`` (#61 WU-1): ``detail_extra`` never restates a row field.
    """
    source = await manifest.resolve_read(layer, filename, user)
    if source is None:
        return []
    cascaded = await tombstone_fold_copies(
        service,
        manifest,
        user,
        layer,
        filename,
        source,
        caller_can_write_shared=caller_can_write_shared,
    )
    return [
        (
            collection,
            chunk0,
            {
                "hard": False,
                "cascade_from": f"{layer}/{filename}",
                "chunks": rows,
                "tier": source.tier,
            },
        )
        for collection, chunk0, rows in cascaded
    ]


async def audit_source_folds(user: UserContext, cascaded: list[FoldRow]) -> None:
    """One ``delete`` row per fold document tombstoned (B16)."""
    for collection, chunk0, detail in cascaded:
        # Build a new dict: the caller's fold rows are not mutated, and
        # ``emit_write_audit`` refuses a ``detail_extra`` that restates the
        # row-level ``tier``.
        detail_extra = {k: v for k, v in detail.items() if k != "tier"}
        await emit_write_audit(
            user=user,
            op="delete",
            layer="semantic",
            collection=collection,
            key=chunk0,
            tier=detail["tier"],
            detail_extra=detail_extra,
        )
