"""The index-outbox row predicates, in one place.

A ``memory_items`` row is **pending index** when the scan pipeline promoted it
(``scan_status == "scanned_clean"``) and the index worker has neither indexed it
nor given up on it, and it is not soft-deleted. It is **dead-lettered** when the
worker gave up (``index_failed_at_ms`` set) and it is not soft-deleted.

Three readers share these: ``/health`` (``unindexed_count``,
``dead_lettered_index_count``), ``IndexJanitor._scan_orphans`` (which adds its
grace window) and ``MemoryManifestService.index_status_summary`` (which adds
tenancy). A row that never entered the scan pipeline has ``scan_status IS NULL``
and ``indexed_at_ms IS NULL`` forever; it is not pending.

Each rule has a SQL form and a plain-value form (for the in-memory mock);
``tests/test_index_pending_predicate.py`` pins the two forms equal on every state.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import ColumnElement

from audittrace.db.models import MemoryItem

ELIGIBLE_SCAN_STATUS: Final = "scanned_clean"


def pending_index_clauses() -> tuple[ColumnElement[bool], ...]:
    """WHERE clauses selecting rows that are pending index."""
    return (
        MemoryItem.scan_status == ELIGIBLE_SCAN_STATUS,
        MemoryItem.indexed_at_ms.is_(None),
        MemoryItem.index_failed_at_ms.is_(None),
        MemoryItem.deleted_at_ms.is_(None),
    )


def dead_lettered_index_clauses() -> tuple[ColumnElement[bool], ...]:
    """WHERE clauses selecting rows the index worker gave up on."""
    return (
        MemoryItem.index_failed_at_ms.is_not(None),
        MemoryItem.deleted_at_ms.is_(None),
    )


def is_pending_index(
    *,
    scan_status: str | None,
    indexed_at_ms: int | None,
    index_failed_at_ms: int | None,
    deleted_at_ms: int | None,
) -> bool:
    """Plain-value twin of :func:`pending_index_clauses`."""
    return (
        scan_status == ELIGIBLE_SCAN_STATUS
        and indexed_at_ms is None
        and index_failed_at_ms is None
        and deleted_at_ms is None
    )


def is_dead_lettered_index(
    *, index_failed_at_ms: int | None, deleted_at_ms: int | None
) -> bool:
    """Plain-value twin of :func:`dead_lettered_index_clauses`."""
    return index_failed_at_ms is not None and deleted_at_ms is None
