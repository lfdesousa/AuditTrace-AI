"""Memory-layer manifest service — Postgres-backed audit trail for
operator-managed CRUD across episodic / procedural / semantic layers.

Migration 009 introduces ``memory_items``. This service owns the
read/write surface for that table. Consumed by the
``/memory/<layer>`` REST endpoints. Supports per-key uniqueness across
the lifetime of the key (a delete-then-recreate reuses the same row,
so audit history accumulates on one row rather than fragmenting).

Timestamp contract: **Unix epoch milliseconds UTC** for every
timestamp column (``created_at_ms`` / ``modified_at_ms`` /
``deleted_at_ms``). Rationale + history in the migration's docstring
+ ``project_session_20260503``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.db.models import MemoryItem
from audittrace.logging_config import log_call

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    """Current Unix epoch in milliseconds UTC. Matches `Date.now()` in JS."""
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """Plain-data view of a ``MemoryItem`` row, serialisable to JSON.

    Frozen so route handlers can safely return it without callers
    mutating shared state. Slots for memory + access speed in
    list-heavy endpoints.
    """

    id: str
    layer: str
    key: str
    title: str | None
    size_bytes: int | None
    created_at_ms: int
    modified_at_ms: int
    created_by_user_id: str
    modified_by_user_id: str
    deleted_at_ms: int | None
    deleted_by_user_id: str | None
    # ── Tier-B PDF manifest fields (ADR-050 #22) ─────────────────────
    # All Optional — non-PDF rows + PDFs that pre-date migration 010
    # leave them unset. Counts default to 0 to disambiguate "no
    # attachment in this doc" from "this doc was indexed before tier-B
    # shipped" — the count fields read NULL only on the latter.
    page_count: int | None = None
    signature_status: str | None = None
    ocr_coverage_pct: float | None = None
    attachment_count: int | None = None
    form_field_count: int | None = None
    extraction_warnings: list[dict[str, Any]] | None = None
    document_sha256: str | None = None
    # ── Tier-C PDF document metadata (ADR-056 #10) ───────────────────
    # Populated from pymupdf ``doc.metadata`` during /memory/index. All
    # Optional — non-PDF rows + PDFs pre-tier-C leave them None.
    pdf_title: str | None = None
    pdf_author: str | None = None
    pdf_creator: str | None = None
    pdf_creation_date: datetime | None = None
    # ADR-056 #14 PDF/A + #13 LTV
    pdfa_part: str | None = None
    pdfa_conformance: str | None = None
    ltv_data: dict[str, Any] | None = None

    @classmethod
    def from_row(cls, row: MemoryItem) -> ManifestEntry:
        return cls(
            id=row.id,
            layer=row.layer,
            key=row.key,
            title=row.title,
            size_bytes=row.size_bytes,
            created_at_ms=row.created_at_ms,
            modified_at_ms=row.modified_at_ms,
            created_by_user_id=row.created_by_user_id,
            modified_by_user_id=row.modified_by_user_id,
            deleted_at_ms=row.deleted_at_ms,
            deleted_by_user_id=row.deleted_by_user_id,
            page_count=row.page_count,
            signature_status=row.signature_status,
            ocr_coverage_pct=row.ocr_coverage_pct,
            attachment_count=row.attachment_count,
            form_field_count=row.form_field_count,
            extraction_warnings=row.extraction_warnings,
            document_sha256=row.document_sha256,
            pdf_title=row.pdf_title,
            pdf_author=row.pdf_author,
            pdf_creator=row.pdf_creator,
            pdf_creation_date=row.pdf_creation_date,
            pdfa_part=row.pdfa_part,
            pdfa_conformance=row.pdfa_conformance,
            ltv_data=row.ltv_data,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly serialisation — direct dict of the dataclass fields."""
        return {
            "id": self.id,
            "layer": self.layer,
            "key": self.key,
            "title": self.title,
            "size_bytes": self.size_bytes,
            "created_at_ms": self.created_at_ms,
            "modified_at_ms": self.modified_at_ms,
            "created_by_user_id": self.created_by_user_id,
            "modified_by_user_id": self.modified_by_user_id,
            "deleted_at_ms": self.deleted_at_ms,
            "deleted_by_user_id": self.deleted_by_user_id,
            "page_count": self.page_count,
            "signature_status": self.signature_status,
            "ocr_coverage_pct": self.ocr_coverage_pct,
            "attachment_count": self.attachment_count,
            "form_field_count": self.form_field_count,
            "extraction_warnings": self.extraction_warnings,
            "document_sha256": self.document_sha256,
            "pdf_title": self.pdf_title,
            "pdf_author": self.pdf_author,
            "pdf_creator": self.pdf_creator,
            "pdf_creation_date": (
                self.pdf_creation_date.isoformat()
                if self.pdf_creation_date is not None
                else None
            ),
            "pdfa_part": self.pdfa_part,
            "pdfa_conformance": self.pdfa_conformance,
            "ltv_data": self.ltv_data,
        }


_VALID_LAYERS = frozenset({"episodic", "procedural", "semantic"})


def _validate_layer(layer: str) -> None:
    if layer not in _VALID_LAYERS:
        raise ValueError(
            f"Invalid memory layer {layer!r}; expected one of {sorted(_VALID_LAYERS)}"
        )


class MemoryManifestService:
    """Postgres-backed manifest of operator-managed memory items.

    Mirrors ``PostgresConversationalService`` shape — takes a
    SQLAlchemy ``sessionmaker`` and runs CRUD via short-lived
    sessions. Not user-context-aware: this service is operator-global
    (RLS is not applied to ``memory_items`` because the items
    themselves are global content shared across users).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @log_call(logger=logger)
    async def record_create(
        self,
        layer: str,
        key: str,
        title: str | None,
        size_bytes: int | None,
        user_id: str,
    ) -> ManifestEntry:
        """Insert a new manifest row, OR un-soft-delete + bump
        timestamps if a row with the same (layer, key) already exists
        (e.g. an operator deletes ADR-007 then recreates with the same
        key).

        Returns the resulting entry.
        """
        _validate_layer(layer)
        now = _now_ms()
        async with self._session_factory() as session:
            existing = (
                await session.execute(
                    select(MemoryItem).filter_by(layer=layer, key=key)
                )
            ).scalar_one_or_none()
            if existing is None:
                row = MemoryItem(
                    layer=layer,
                    key=key,
                    title=title,
                    size_bytes=size_bytes,
                    created_at_ms=now,
                    modified_at_ms=now,
                    created_by_user_id=user_id,
                    modified_by_user_id=user_id,
                )
                session.add(row)
            else:
                # Recreating after a soft-delete (or overwriting an
                # existing live entry — caller should usually call
                # `record_update` for the latter; this path is
                # idempotent either way).
                if existing.deleted_at_ms is not None:
                    existing.deleted_at_ms = None
                    existing.deleted_by_user_id = None
                existing.title = title
                existing.size_bytes = size_bytes
                existing.modified_at_ms = now
                existing.modified_by_user_id = user_id
                row = existing
            await session.commit()
            await session.refresh(row)
            return ManifestEntry.from_row(row)

    @log_call(logger=logger)
    async def record_update(
        self,
        layer: str,
        key: str,
        size_bytes: int | None,
        user_id: str,
        title: str | None = None,
    ) -> ManifestEntry:
        """Bump ``modified_at_ms`` + ``modified_by_user_id`` on the row.

        ``title`` is updated only if non-None (PUT semantics — empty
        string clears it). Raises ``LookupError`` if no row exists for
        ``(layer, key)``. Raises ``RuntimeError`` if the row is
        soft-deleted (caller should ``record_create`` to revive
        rather than update a deleted row).
        """
        _validate_layer(layer)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(MemoryItem).filter_by(layer=layer, key=key)
                )
            ).scalar_one_or_none()
            if row is None:
                raise LookupError(f"no manifest row for layer={layer!r} key={key!r}")
            if row.deleted_at_ms is not None:
                raise RuntimeError(
                    f"manifest row for layer={layer!r} key={key!r} is "
                    f"soft-deleted; use record_create to revive"
                )
            if title is not None:
                row.title = title
            row.size_bytes = size_bytes
            row.modified_at_ms = _now_ms()
            row.modified_by_user_id = user_id
            await session.commit()
            await session.refresh(row)
            return ManifestEntry.from_row(row)

    @log_call(logger=logger)
    async def record_delete(self, layer: str, key: str, user_id: str) -> ManifestEntry:
        """Soft-delete: set ``deleted_at_ms`` + ``deleted_by_user_id``.
        Idempotent — calling on an already-deleted row is a no-op
        that returns the existing entry. Raises ``LookupError`` if
        the row does not exist at all.
        """
        _validate_layer(layer)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(MemoryItem).filter_by(layer=layer, key=key)
                )
            ).scalar_one_or_none()
            if row is None:
                raise LookupError(f"no manifest row for layer={layer!r} key={key!r}")
            if row.deleted_at_ms is None:
                row.deleted_at_ms = _now_ms()
                row.deleted_by_user_id = user_id
                await session.commit()
                await session.refresh(row)
            return ManifestEntry.from_row(row)

    @log_call(logger=logger)
    async def list_for_layer(
        self, layer: str, *, include_deleted: bool = False
    ) -> list[ManifestEntry]:
        """Return manifest entries for ``layer``, ordered by
        ``modified_at_ms DESC`` (most recently touched first).

        ``include_deleted=False`` (default) hides soft-deleted rows —
        right answer for the standard LIST endpoint. Audit-scope
        callers can request ``include_deleted=True``.
        """
        _validate_layer(layer)
        async with self._session_factory() as session:
            q = select(MemoryItem).filter_by(layer=layer)
            if not include_deleted:
                q = q.filter(MemoryItem.deleted_at_ms.is_(None))
            rows = (
                (await session.execute(q.order_by(MemoryItem.modified_at_ms.desc())))
                .scalars()
                .all()
            )
            return [ManifestEntry.from_row(r) for r in rows]

    @log_call(logger=logger)
    async def upsert_pdf_metadata(
        self,
        layer: str,
        key: str,
        *,
        user_id: str,
        size_bytes: int | None,
        page_count: int | None,
        signature_status: str | None,
        ocr_coverage_pct: float | None,
        attachment_count: int,
        form_field_count: int,
        extraction_warnings: list[dict[str, Any]],
        document_sha256: str | None,
        pdf_title: str | None = None,
        pdf_author: str | None = None,
        pdf_creator: str | None = None,
        pdf_creation_date: datetime | None = None,
        pdfa_part: str | None = None,
        pdfa_conformance: str | None = None,
        ltv_data: dict[str, Any] | None = None,
    ) -> ManifestEntry:
        """Write tier-B + tier-C PDF manifest fields for ``(layer, key)``.

        Creates a new ``MemoryItem`` row if none exists (since /memory/index
        is itself the first touch for many uploaded PDFs — the per-layer
        CRUD endpoints don't fire on direct /memory/upload). Existing rows
        keep their authorship; only the PDF fields + ``modified_*`` are
        bumped.

        Per ADR-050 #22 + ADR-056 #10: this is the single audit-pivot
        writer. Every successful PDF index call lands one of these per
        file.
        """
        _validate_layer(layer)
        now = _now_ms()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(MemoryItem).filter_by(layer=layer, key=key)
                )
            ).scalar_one_or_none()
            if row is None:
                row = MemoryItem(
                    layer=layer,
                    key=key,
                    title=None,
                    size_bytes=size_bytes,
                    created_at_ms=now,
                    modified_at_ms=now,
                    created_by_user_id=user_id,
                    modified_by_user_id=user_id,
                )
                session.add(row)
            else:
                row.modified_at_ms = now
                row.modified_by_user_id = user_id
                if size_bytes is not None:
                    row.size_bytes = size_bytes
            row.page_count = page_count
            row.signature_status = signature_status
            row.ocr_coverage_pct = ocr_coverage_pct
            row.attachment_count = attachment_count
            row.form_field_count = form_field_count
            row.extraction_warnings = extraction_warnings
            row.document_sha256 = document_sha256
            row.pdf_title = pdf_title
            row.pdf_author = pdf_author
            row.pdf_creator = pdf_creator
            row.pdf_creation_date = pdf_creation_date
            row.pdfa_part = pdfa_part
            row.pdfa_conformance = pdfa_conformance
            row.ltv_data = ltv_data
            await session.commit()
            await session.refresh(row)
            return ManifestEntry.from_row(row)

    @log_call(logger=logger)
    async def get(self, layer: str, key: str) -> ManifestEntry | None:
        """Return a single manifest entry, or ``None`` if no row
        exists. Soft-deleted rows ARE returned (the caller decides
        whether to treat them as missing — the per-layer service
        usually treats them as missing for normal reads, while
        admin/audit paths surface them)."""
        _validate_layer(layer)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(MemoryItem).filter_by(layer=layer, key=key)
                )
            ).scalar_one_or_none()
            return ManifestEntry.from_row(row) if row is not None else None


class MockMemoryManifestService(MemoryManifestService):
    """In-memory variant for unit tests.

    Reuses the parent class signature but delegates to a tiny dict
    instead of a SQLAlchemy session. Keeps the type-checker happy by
    inheriting; overrides every method.
    """

    def __init__(self) -> None:
        # Skip parent __init__; no session_factory needed.
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    def _to_entry(self, row: dict[str, Any]) -> ManifestEntry:
        return ManifestEntry(
            id=row["id"],
            layer=row["layer"],
            key=row["key"],
            title=row.get("title"),
            size_bytes=row.get("size_bytes"),
            created_at_ms=row["created_at_ms"],
            modified_at_ms=row["modified_at_ms"],
            created_by_user_id=row["created_by_user_id"],
            modified_by_user_id=row["modified_by_user_id"],
            deleted_at_ms=row.get("deleted_at_ms"),
            deleted_by_user_id=row.get("deleted_by_user_id"),
            page_count=row.get("page_count"),
            signature_status=row.get("signature_status"),
            ocr_coverage_pct=row.get("ocr_coverage_pct"),
            attachment_count=row.get("attachment_count"),
            form_field_count=row.get("form_field_count"),
            extraction_warnings=row.get("extraction_warnings"),
            document_sha256=row.get("document_sha256"),
            pdf_title=row.get("pdf_title"),
            pdf_author=row.get("pdf_author"),
            pdf_creator=row.get("pdf_creator"),
            pdf_creation_date=row.get("pdf_creation_date"),
            pdfa_part=row.get("pdfa_part"),
            pdfa_conformance=row.get("pdfa_conformance"),
            ltv_data=row.get("ltv_data"),
        )

    async def record_create(
        self,
        layer: str,
        key: str,
        title: str | None,
        size_bytes: int | None,
        user_id: str,
    ) -> ManifestEntry:
        _validate_layer(layer)
        now = _now_ms()
        existing = self._rows.get((layer, key))
        if existing is None:
            import uuid

            existing = {
                "id": str(uuid.uuid4()),
                "layer": layer,
                "key": key,
                "title": title,
                "size_bytes": size_bytes,
                "created_at_ms": now,
                "modified_at_ms": now,
                "created_by_user_id": user_id,
                "modified_by_user_id": user_id,
                "deleted_at_ms": None,
                "deleted_by_user_id": None,
            }
            self._rows[(layer, key)] = existing
        else:
            existing["title"] = title
            existing["size_bytes"] = size_bytes
            existing["modified_at_ms"] = now
            existing["modified_by_user_id"] = user_id
            if existing.get("deleted_at_ms") is not None:
                existing["deleted_at_ms"] = None
                existing["deleted_by_user_id"] = None
        return self._to_entry(existing)

    async def record_update(
        self,
        layer: str,
        key: str,
        size_bytes: int | None,
        user_id: str,
        title: str | None = None,
    ) -> ManifestEntry:
        _validate_layer(layer)
        existing = self._rows.get((layer, key))
        if existing is None:
            raise LookupError(f"no manifest row for layer={layer!r} key={key!r}")
        if existing.get("deleted_at_ms") is not None:
            raise RuntimeError(
                f"manifest row for layer={layer!r} key={key!r} is "
                f"soft-deleted; use record_create to revive"
            )
        if title is not None:
            existing["title"] = title
        existing["size_bytes"] = size_bytes
        existing["modified_at_ms"] = _now_ms()
        existing["modified_by_user_id"] = user_id
        return self._to_entry(existing)

    async def record_delete(self, layer: str, key: str, user_id: str) -> ManifestEntry:
        _validate_layer(layer)
        existing = self._rows.get((layer, key))
        if existing is None:
            raise LookupError(f"no manifest row for layer={layer!r} key={key!r}")
        if existing.get("deleted_at_ms") is None:
            existing["deleted_at_ms"] = _now_ms()
            existing["deleted_by_user_id"] = user_id
        return self._to_entry(existing)

    async def list_for_layer(
        self, layer: str, *, include_deleted: bool = False
    ) -> list[ManifestEntry]:
        _validate_layer(layer)
        rows = [row for (row_layer, _), row in self._rows.items() if row_layer == layer]
        if not include_deleted:
            rows = [r for r in rows if r.get("deleted_at_ms") is None]
        rows.sort(key=lambda r: r["modified_at_ms"], reverse=True)
        return [self._to_entry(r) for r in rows]

    async def get(self, layer: str, key: str) -> ManifestEntry | None:
        _validate_layer(layer)
        existing = self._rows.get((layer, key))
        return self._to_entry(existing) if existing is not None else None

    async def upsert_pdf_metadata(
        self,
        layer: str,
        key: str,
        *,
        user_id: str,
        size_bytes: int | None,
        page_count: int | None,
        signature_status: str | None,
        ocr_coverage_pct: float | None,
        attachment_count: int,
        form_field_count: int,
        extraction_warnings: list[dict[str, Any]],
        document_sha256: str | None,
        pdf_title: str | None = None,
        pdf_author: str | None = None,
        pdf_creator: str | None = None,
        pdf_creation_date: datetime | None = None,
        pdfa_part: str | None = None,
        pdfa_conformance: str | None = None,
        ltv_data: dict[str, Any] | None = None,
    ) -> ManifestEntry:
        _validate_layer(layer)
        now = _now_ms()
        existing = self._rows.get((layer, key))
        if existing is None:
            import uuid

            existing = {
                "id": str(uuid.uuid4()),
                "layer": layer,
                "key": key,
                "title": None,
                "size_bytes": size_bytes,
                "created_at_ms": now,
                "modified_at_ms": now,
                "created_by_user_id": user_id,
                "modified_by_user_id": user_id,
                "deleted_at_ms": None,
                "deleted_by_user_id": None,
            }
            self._rows[(layer, key)] = existing
        else:
            existing["modified_at_ms"] = now
            existing["modified_by_user_id"] = user_id
            if size_bytes is not None:
                existing["size_bytes"] = size_bytes
        existing["page_count"] = page_count
        existing["signature_status"] = signature_status
        existing["ocr_coverage_pct"] = ocr_coverage_pct
        existing["attachment_count"] = attachment_count
        existing["form_field_count"] = form_field_count
        existing["extraction_warnings"] = extraction_warnings
        existing["document_sha256"] = document_sha256
        existing["pdf_title"] = pdf_title
        existing["pdf_author"] = pdf_author
        existing["pdf_creator"] = pdf_creator
        existing["pdf_creation_date"] = pdf_creation_date
        existing["pdfa_part"] = pdfa_part
        existing["pdfa_conformance"] = pdfa_conformance
        existing["ltv_data"] = ltv_data
        return self._to_entry(existing)

    def reset(self) -> None:
        self._rows.clear()
