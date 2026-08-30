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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.db.models import MemoryItem
from audittrace.identity import UserContext
from audittrace.logging_config import log_call

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    """Current Unix epoch in milliseconds UTC. Matches `Date.now()` in JS."""
    return int(time.time() * 1000)


class ManifestAuthorizationError(PermissionError):
    """Raised by ``record_create``/``record_update`` when a caller writes
    over an EXISTING **corpus**-tier manifest row without the shared-write
    authorization that row requires (SPEC security-memory-manifest-tier-
    authz, 2026-08-30 — the M3-WU-D2-2 reviewer's cross-user corpus-hijack/
    hide finding).

    ``record_create``/``record_update`` are the shared manifest choke for
    every writer (the REST ``/memory/{episodic,procedural,semantic}``
    routes, the ``.md`` fold path via ``memory_md_manifest._flush_md_manifest``,
    and the ``mcp_write`` tools) — raising HERE, not only at the route
    layer, means no caller can reach an unauthorized corpus overwrite by a
    path that forgets to duplicate the route-level scope check. Routes
    MUST map this to HTTP 403 (never let it fall through to a generic 500)
    — see ``routes/memory.py``'s create/update handlers.
    """


def _tier_write_unauthorized(existing_tier: str, caller_can_write_shared: bool) -> bool:
    """``True`` iff writing over a row currently at *existing_tier*
    requires shared-write authorization the caller does NOT hold.

    Only the **corpus** tier is "shared" in the ADR-062 model — a
    **private**-tier row's owner-agnostic overwrite semantics (the D2/D4
    per-key-collision follow-up, out of THIS WU's scope — see
    ``_manifest_visible``'s docstring) are unaffected by this guard. Named
    as its own predicate so ``record_create``/``record_update`` (and their
    Mock twins) apply the EXACT same rule, rather than four independently-
    maintained ``if`` conditions that could drift apart under a future
    edit.
    """
    return existing_tier == "corpus" and not caller_can_write_shared


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
    # ── ADR-062 Phase B (WU-B4, migration 018) — per-user tiering ────
    # Closed-set ``"corpus"`` | ``"private"``. Defaults ``"corpus"`` to
    # match the DB column's ``server_default`` (D2) and to keep every
    # pre-existing direct-construction call site (tests, the PDF
    # manifest writer) reading the same value it always implicitly
    # meant before this column existed.
    tier: str = "corpus"

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
            tier=row.tier,
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
            "tier": self.tier,
        }


_VALID_LAYERS = frozenset({"episodic", "procedural", "semantic"})


def _validate_layer(layer: str) -> None:
    if layer not in _VALID_LAYERS:
        raise ValueError(
            f"Invalid memory layer {layer!r}; expected one of {sorted(_VALID_LAYERS)}"
        )


# ── SPEC #374 (WU-1) — read-path index-status query ──────────────────────
# Caps the best-effort named-match list (sub-decision #5, ratified
# 2026-08-16): a recall tool result is not the place for an unbounded list.
_MAX_MATCHED_UNINDEXED = 5


@dataclass(frozen=True, slots=True)
class MatchedUnindexed:
    """One accessible un-indexed/dead-lettered ``memory_items`` row whose
    ``key``/``title`` matched a query-derived token (SPEC #374, sub-decision
    #5). ``state`` is derived, never stored: ``"dead_lettered"`` when
    ``index_failed_at_ms IS NOT NULL``, else ``"pending"``."""

    key: str
    state: str
    index_failure_code: str | None


@dataclass(frozen=True, slots=True)
class IndexStatusSummary:
    """Tenancy-scoped read-path answer to "how incomplete is the corpus
    the caller can see, and does any of it name what they just asked
    about?" (SPEC #374 / #374-recall-not-indexed-signal). ``pending`` +
    ``dead_lettered`` are corpus-wide counts (sub-decision #4); ``matched``
    is the bounded, best-effort name match (sub-decision #5, capped at
    :data:`_MAX_MATCHED_UNINDEXED`)."""

    pending: int
    dead_lettered: int
    matched: list[MatchedUnindexed]


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
        tier: str = "private",
        *,
        caller_can_write_shared: bool = False,
    ) -> ManifestEntry:
        """Insert a new manifest row, OR un-soft-delete + bump
        timestamps if a row with the same (layer, key) already exists
        (e.g. an operator deletes ADR-007 then recreates with the same
        key).

        ``tier`` (ADR-062 Phase B, WU-B4/WU-B5) defaults ``"private"`` —
        D2's "new writes default private" policy lives here as much as
        at the route layer, so any future call site that forgets to
        pass ``tier`` explicitly still lands on the fail-closed
        (least-shared) side rather than silently defaulting to
        ``"corpus"``. Recreating an existing row (revive-after-delete or
        overwrite) also re-stamps ``tier`` to the caller's requested
        value — same as ``title``/``size_bytes`` on that branch — but
        ONLY when authorized (see ``caller_can_write_shared`` below).

        ``caller_can_write_shared`` (SPEC security-memory-manifest-tier-
        authz, 2026-08-30) — ``True`` iff the caller holds
        ``memory:corpus:<layer>:write`` (or ``audittrace:admin``/admin)
        for this write, computed by the route from the token's scopes.
        Defaults ``False`` — FAIL-CLOSED, so a caller of this SERVICE
        method (not just the route) that forgets to pass it lands on the
        least-privileged side, same discipline as the ``tier`` default.

        Fixes the cross-user corpus-hijack/hide vulnerability the M3-WU-
        D2-2 reviewer surfaced: before this guard, an EXISTING row's
        lookup was ``(layer, key)`` only — no tier/ownership check — so
        a caller holding just the base ``memory:<layer>:write`` scope
        could POST a create at a shared **corpus**-tier item's key and
        silently demote it to their own private tier + re-own it,
        hiding it from everyone else. Now: if the EXISTING row is
        corpus-tier and the caller is not authorized for shared writes,
        this raises :class:`ManifestAuthorizationError` — no tier
        change, no title/content overwrite, no re-authorship. An
        authorized caller (or a write landing on an existing PRIVATE
        row) keeps working exactly as before; an unauthorized write
        that reaches an existing PRIVATE row still updates
        title/size/``modified_by_user_id`` (unaffected — private-tier
        cross-owner collisions are a separate, already-flagged follow-
        up, see ``routes/memory.py::_manifest_visible``'s docstring) but
        its ``tier`` field is left untouched rather than blindly set to
        whatever the caller requested, so an unauthorized promote-to-
        corpus can never silently land via this defense-in-depth path
        either. ``created_by_user_id`` is never reassigned (unchanged
        behavior — always was owner-preserving on this branch).

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
                    tier=tier,
                )
                session.add(row)
            else:
                if _tier_write_unauthorized(existing.tier, caller_can_write_shared):
                    raise ManifestAuthorizationError(
                        f"caller lacks shared-write authorization to overwrite "
                        f"existing corpus-tier manifest row layer={layer!r} "
                        f"key={key!r}"
                    )
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
                if caller_can_write_shared:
                    existing.tier = tier
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
        *,
        caller_can_write_shared: bool = False,
    ) -> ManifestEntry:
        """Bump ``modified_at_ms`` + ``modified_by_user_id`` on the row.

        ``title`` is updated only if non-None (PUT semantics — empty
        string clears it). Raises ``LookupError`` if no row exists for
        ``(layer, key)``. Raises ``RuntimeError`` if the row is
        soft-deleted (caller should ``record_create`` to revive
        rather than update a deleted row).

        ``caller_can_write_shared`` (SPEC security-memory-manifest-tier-
        authz, 2026-08-30) — same fail-closed (default ``False``)
        shared-write authorization ``record_create`` takes. Raises
        :class:`ManifestAuthorizationError` — BEFORE any field is
        touched — when the row is corpus-tier and the caller lacks it:
        this closes the same vulnerability class as ``record_create``'s
        guard for the update path, where an unauthorized caller could
        previously overwrite a shared corpus row's ``title`` and
        re-stamp ``modified_by_user_id`` to themselves with no
        ownership/tier check at all. ``record_update`` never touches
        ``tier`` (unchanged — it never did), so authorized callers see
        no behavior change.
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
            if _tier_write_unauthorized(row.tier, caller_can_write_shared):
                raise ManifestAuthorizationError(
                    f"caller lacks shared-write authorization to overwrite "
                    f"existing corpus-tier manifest row layer={layer!r} "
                    f"key={key!r}"
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
        self,
        layer: str,
        *,
        include_deleted: bool = False,
        caller: UserContext | None = None,
    ) -> list[ManifestEntry]:
        """Return manifest entries for ``layer``, ordered by
        ``modified_at_ms DESC`` (most recently touched first).

        ``include_deleted=False`` (default) hides soft-deleted rows —
        right answer for the standard LIST endpoint. Audit-scope
        callers can request ``include_deleted=True``.

        ``caller`` (ADR-062 Phase B, WU-B4) is the owner-or-corpus
        isolation predicate: when supplied and the caller is not an
        admin, a row is only returned if ``row.created_by_user_id ==
        caller.user_id`` OR ``row.tier == "corpus"`` — the same shape as
        ``ChromaSemanticService._tier_authorized`` and the WU-B3
        ``_merge_semantic_with_chroma`` discovery guard. ``caller=None``
        (the default) is UNFILTERED — preserves the pre-WU-B4 behaviour
        for internal/admin callers (e.g. the dedup-lookup passes in
        ``_merge_layer_items_with_s3``/``_merge_semantic_with_chroma``
        pass the real caller explicitly; anything that still omits
        ``caller`` gets the full operator-global view, same as before
        this column existed)."""
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
            entries = [ManifestEntry.from_row(r) for r in rows]
            if caller is not None and not caller.is_admin:
                entries = [
                    e
                    for e in entries
                    if e.created_by_user_id == caller.user_id or e.tier == "corpus"
                ]
            return entries

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

    @log_call(logger=logger)
    async def index_status_summary(
        self,
        user_context: UserContext,
        tokens: Sequence[str],
        *,
        skip_counts_if_no_match: bool = False,
    ) -> IndexStatusSummary:
        """Tenancy-scoped read-path index-status query (SPEC #374 WU-1).

        Answers "for this caller, how many accessible docs are un-indexed
        / dead-lettered, and which of them match these tokens?" — the
        capability the read path (recall) has never had; only
        ``IndexJanitor._scan_orphans`` read ``indexed_at_ms`` /
        ``index_failed_at_ms`` before this, as an unscoped batch poll.

        ``tokens`` are pre-extracted, lower-cased doc-like tokens (see
        ``tools.memory_handlers._doc_tokens``, sub-decision #5) — this
        method owns the SQL query construction (the ``%token%`` ILIKE
        wrapping) so the token-extraction RULE and the query MECHANICS
        stay in their own single-responsibility homes, per the module
        CHANGELOG precedent elsewhere in this codebase.

        Tenancy (sub-decision #4, the load-bearing correctness property):
        ``memory_items`` carries **no Postgres RLS policy** — the model's
        own docstring is explicit that the manifest is operator-global,
        not RLS'd, and access is gated in the application layer. So this
        method reuses the SAME owner-or-corpus predicate ``list_for_layer
        (caller=...)`` already applies (ADR-062 Phase B, WU-B4): a
        non-admin caller only ever counts/matches a row where
        ``created_by_user_id == user_context.user_id`` OR ``tier ==
        "corpus"``; admins are unfiltered. The predicate is derived from
        the AUTHENTICATED ``user_context`` (token-derived), never from
        caller-supplied request data, per
        ``feedback_never_trust_caller_metadata_for_security_fields``.

        Two bounded queries, both scoped ``deleted_at_ms IS NULL``:
          - counts: ``pending`` (``indexed_at_ms IS NULL AND
            index_failed_at_ms IS NULL``) and ``dead_lettered``
            (``index_failed_at_ms IS NOT NULL``);
          - match (only when ``tokens`` is non-empty): un-indexed OR
            dead-lettered rows whose ``key`` or ``title`` ILIKE-matches
            any token, capped at :data:`_MAX_MATCHED_UNINDEXED`, ordered
            by ``key`` for determinism (sub-decision #5: "deterministic +
            testable").

        Deviation from the spec's illustrative predicate: the spec names
        ``key ILIKE ANY(tokens) OR source ILIKE ANY(tokens))`` — but
        ``MemoryItem`` has no ``source`` column (that field lives on the
        ChromaDB vector-store metadata the *other* three recall tools
        read, not this Postgres manifest table). ``title`` is this
        table's equivalent human-readable name field, so the match
        predicate uses ``key``/``title`` instead — least-surprising
        reading of an otherwise-nonexistent column reference.
        """
        async with self._session_factory() as session:
            filters: list[Any] = [MemoryItem.deleted_at_ms.is_(None)]
            if not user_context.is_admin:
                filters.append(
                    or_(
                        MemoryItem.tier == "corpus",
                        MemoryItem.created_by_user_id == user_context.user_id,
                    )
                )

            # Match query FIRST. The read-path hot caller (``_maybe_corpus_status``,
            # now on EVERY recall since the reshape dropped the ``page.total == 0``
            # gate) only surfaces ``corpus_status`` when a doc matched, and discards
            # the summary otherwise. So with ``skip_counts_if_no_match`` set and no
            # match we short-circuit BEFORE the two COUNT round-trips (the caller
            # never reads the counts in that case). General callers keep honest counts.
            matched: list[MatchedUnindexed] = []
            if tokens:
                token_clauses = [
                    or_(
                        MemoryItem.key.ilike(f"%{token}%"),
                        MemoryItem.title.ilike(f"%{token}%"),
                    )
                    for token in tokens
                ]
                rows = (
                    (
                        await session.execute(
                            select(MemoryItem)
                            .where(
                                *filters,
                                or_(
                                    MemoryItem.indexed_at_ms.is_(None),
                                    MemoryItem.index_failed_at_ms.is_not(None),
                                ),
                                or_(*token_clauses),
                            )
                            .order_by(MemoryItem.key)
                            .limit(_MAX_MATCHED_UNINDEXED)
                        )
                    )
                    .scalars()
                    .all()
                )
                matched = [
                    MatchedUnindexed(
                        key=row.key,
                        state=(
                            "dead_lettered"
                            if row.index_failed_at_ms is not None
                            else "pending"
                        ),
                        index_failure_code=row.index_failure_code,
                    )
                    for row in rows
                ]

            if skip_counts_if_no_match and not matched:
                # counts uncomputed (0) — the sole hot caller discards on empty match
                return IndexStatusSummary(pending=0, dead_lettered=0, matched=[])

            pending_count = (
                await session.execute(
                    select(func.count())
                    .select_from(MemoryItem)
                    .where(
                        *filters,
                        MemoryItem.indexed_at_ms.is_(None),
                        MemoryItem.index_failed_at_ms.is_(None),
                    )
                )
            ).scalar_one()
            dead_lettered_count = (
                await session.execute(
                    select(func.count())
                    .select_from(MemoryItem)
                    .where(*filters, MemoryItem.index_failed_at_ms.is_not(None))
                )
            ).scalar_one()

            return IndexStatusSummary(
                pending=pending_count,
                dead_lettered=dead_lettered_count,
                matched=matched,
            )

    @log_call(logger=logger)
    async def get_deleted_keys(self, layer: str, keys: Iterable[str]) -> set[str]:
        """Return the subset of ``keys`` that are soft-deleted for ``layer``.

        ADR-062 §6 (WU-A5): the manifest soft-delete is authoritative for
        corpus content — ``ChromaSemanticService.search``/``search_page``
        consult this to keep a soft-deleted ``/memory/semantic`` item out
        of recall, mirroring what ``list_for_layer(include_deleted=False)``
        already enforces for the backoffice list (closes CS-7 / #374: soft-
        delete and the Chroma vector were previously independent). Empty
        ``keys`` short-circuits without touching the DB.
        """
        _validate_layer(layer)
        key_list = list(keys)
        if not key_list:
            return set()
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(MemoryItem.key).where(
                        MemoryItem.layer == layer,
                        MemoryItem.key.in_(key_list),
                        MemoryItem.deleted_at_ms.is_not(None),
                    )
                )
            ).scalars()
            return set(rows.all())


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
            tier=row.get("tier", "corpus"),
        )

    async def record_create(
        self,
        layer: str,
        key: str,
        title: str | None,
        size_bytes: int | None,
        user_id: str,
        tier: str = "private",
        *,
        caller_can_write_shared: bool = False,
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
                "tier": tier,
            }
            self._rows[(layer, key)] = existing
        else:
            if _tier_write_unauthorized(
                existing.get("tier", "corpus"), caller_can_write_shared
            ):
                raise ManifestAuthorizationError(
                    f"caller lacks shared-write authorization to overwrite "
                    f"existing corpus-tier manifest row layer={layer!r} "
                    f"key={key!r}"
                )
            existing["title"] = title
            existing["size_bytes"] = size_bytes
            existing["modified_at_ms"] = now
            existing["modified_by_user_id"] = user_id
            if caller_can_write_shared:
                existing["tier"] = tier
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
        *,
        caller_can_write_shared: bool = False,
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
        if _tier_write_unauthorized(
            existing.get("tier", "corpus"), caller_can_write_shared
        ):
            raise ManifestAuthorizationError(
                f"caller lacks shared-write authorization to overwrite "
                f"existing corpus-tier manifest row layer={layer!r} "
                f"key={key!r}"
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
        self,
        layer: str,
        *,
        include_deleted: bool = False,
        caller: UserContext | None = None,
    ) -> list[ManifestEntry]:
        _validate_layer(layer)
        rows = [row for (row_layer, _), row in self._rows.items() if row_layer == layer]
        if not include_deleted:
            rows = [r for r in rows if r.get("deleted_at_ms") is None]
        if caller is not None and not caller.is_admin:
            rows = [
                r
                for r in rows
                if r.get("created_by_user_id") == caller.user_id
                or r.get("tier", "corpus") == "corpus"
            ]
        rows.sort(key=lambda r: r["modified_at_ms"], reverse=True)
        return [self._to_entry(r) for r in rows]

    async def get(self, layer: str, key: str) -> ManifestEntry | None:
        _validate_layer(layer)
        existing = self._rows.get((layer, key))
        return self._to_entry(existing) if existing is not None else None

    def set_index_state(
        self,
        layer: str,
        key: str,
        *,
        indexed_at_ms: int | None = None,
        index_failed_at_ms: int | None = None,
        index_failure_code: str | None = None,
    ) -> None:
        """Test-only seam (SPEC #374 WU-4). The mock's ``_rows`` dict has
        no schema, so ``record_create``/``upsert_pdf_metadata`` never
        populate the migration-019/020 index-outbox / dead-letter columns
        — tests that need a row in a specific index-status state call
        this directly after creating the row via ``record_create``."""
        row = self._rows.get((layer, key))
        if row is None:
            raise LookupError(f"no manifest row for layer={layer!r} key={key!r}")
        row["indexed_at_ms"] = indexed_at_ms
        row["index_failed_at_ms"] = index_failed_at_ms
        row["index_failure_code"] = index_failure_code

    async def index_status_summary(
        self,
        user_context: UserContext,
        tokens: Sequence[str],
        *,
        skip_counts_if_no_match: bool = False,
    ) -> IndexStatusSummary:
        """Mirrors ``MemoryManifestService.index_status_summary`` — same
        owner-or-corpus tenancy predicate, same corpus-wide (no
        per-layer filter) scope, same ``_MAX_MATCHED_UNINDEXED`` cap, same
        ``skip_counts_if_no_match`` short-circuit."""
        rows = [row for row in self._rows.values() if row.get("deleted_at_ms") is None]
        if not user_context.is_admin:
            rows = [
                r
                for r in rows
                if r.get("created_by_user_id") == user_context.user_id
                or r.get("tier", "corpus") == "corpus"
            ]

        matched: list[MatchedUnindexed] = []
        if tokens:
            lowered_tokens = [t.lower() for t in tokens]
            candidates = sorted(
                (
                    r
                    for r in rows
                    if r.get("indexed_at_ms") is None
                    or r.get("index_failed_at_ms") is not None
                ),
                key=lambda r: r["key"],
            )
            for r in candidates:
                haystacks = (r["key"].lower(), (r.get("title") or "").lower())
                if any(token in h for token in lowered_tokens for h in haystacks):
                    matched.append(
                        MatchedUnindexed(
                            key=r["key"],
                            state=(
                                "dead_lettered"
                                if r.get("index_failed_at_ms") is not None
                                else "pending"
                            ),
                            index_failure_code=r.get("index_failure_code"),
                        )
                    )
                if len(matched) >= _MAX_MATCHED_UNINDEXED:
                    break

        if skip_counts_if_no_match and not matched:
            return IndexStatusSummary(pending=0, dead_lettered=0, matched=[])

        pending = sum(
            1
            for r in rows
            if r.get("indexed_at_ms") is None and r.get("index_failed_at_ms") is None
        )
        dead_lettered = sum(1 for r in rows if r.get("index_failed_at_ms") is not None)

        return IndexStatusSummary(
            pending=pending, dead_lettered=dead_lettered, matched=matched
        )

    async def get_deleted_keys(self, layer: str, keys: Iterable[str]) -> set[str]:
        _validate_layer(layer)
        key_set = set(keys)
        if not key_set:
            return set()
        return {
            row_key
            for (row_layer, row_key), row in self._rows.items()
            if row_layer == layer
            and row_key in key_set
            and row.get("deleted_at_ms") is not None
        }

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
