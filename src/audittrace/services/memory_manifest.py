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

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from sqlalchemy import Row, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.db.models import MemoryItem
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.services.memory_keys import (
    ConcurrentManifestWriteError as ConcurrentManifestWriteError,
)
from audittrace.services.memory_keys import (
    CrossOwnerWriteError as CrossOwnerWriteError,
)
from audittrace.services.memory_keys import (
    InvalidPrivateSubjectError as InvalidPrivateSubjectError,
)
from audittrace.services.memory_keys import (
    LogicalKeyTooLongError as LogicalKeyTooLongError,
)
from audittrace.services.memory_keys import (
    ManifestAuthorizationError as ManifestAuthorizationError,
)
from audittrace.services.memory_keys import (
    Resolution,
    RowFacts,
    logical_key,
    physical_key,
    resolve_read,
    resolve_write,
    tier_conflict_message,
)

logger = logging.getLogger(__name__)


def _facts(row: MemoryItem | None) -> RowFacts | None:
    """The resolver's backend-neutral view of an ORM row."""
    if row is None:
        return None
    return RowFacts(
        key=row.key,
        tier=row.tier,
        created_by_user_id=row.created_by_user_id,
        deleted_at_ms=row.deleted_at_ms,
    )


def _entry_facts(entry: ManifestEntry | None) -> RowFacts | None:
    """The resolver's view of a ``get()`` result (``authorize_write`` reads
    through the public ``get`` so any manifest double keeps working)."""
    if entry is None:
        return None
    return RowFacts(
        key=entry.storage_key or entry.key,
        tier=entry.tier,
        created_by_user_id=entry.created_by_user_id,
        deleted_at_ms=entry.deleted_at_ms,
    )


def _is_unique_violation(exc: IntegrityError) -> bool:
    """``True`` iff *exc* is a ``UNIQUE`` violation (Postgres SQLSTATE
    ``23505``; SQLite's ``UNIQUE constraint failed`` text). A CHECK or NOT
    NULL violation is NOT one: it stays a hard error (fail-closed)."""
    orig = exc.orig
    state = (
        getattr(orig, "sqlstate", None)
        or getattr(orig, "pgcode", None)
        or getattr(getattr(orig, "__cause__", None), "sqlstate", None)
    )
    if state is not None:
        return str(state) == "23505"
    return "UNIQUE constraint failed" in str(orig)


async def _commit_or_conflict(session: AsyncSession, layer: str, key: str) -> None:
    """Commit; turn losing the ``UNIQUE (layer, key)`` race into the
    retryable :class:`ConcurrentManifestWriteError` (LC-9 WP1 advisory A2:
    two simultaneous copy-forwards of one owner's legacy row used to
    surface as an IntegrityError -> 500). Any other IntegrityError (the 034
    CHECK included) propagates unchanged."""
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if _is_unique_violation(exc):
            raise ConcurrentManifestWriteError(
                f"concurrent write to layer={layer!r} key={key!r}; retry"
            ) from exc
        raise


def _now_ms() -> int:
    """Current Unix epoch in milliseconds UTC. Matches `Date.now()` in JS."""
    return int(time.time() * 1000)


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


def _pdf_private_row_foreign(
    existing_tier: str, existing_owner: str, caller_user_id: str
) -> bool:
    """``True`` iff a PDF-metadata refresh would touch ANOTHER user's
    **private**-tier row (LC-9 WP1 review advisory A1). Neither the shared-
    write authorization nor an admin/curator role overrides it (ADR-062
    curators are not private-data admins, operator row O-3): before this
    guard a foreign legacy bare private row was refused only by the
    Postgres CHECK (034), never by the service."""
    return existing_tier == "private" and existing_owner != caller_user_id


def _pdf_metadata_write_unauthorized(
    existing_tier: str,
    existing_owner: str,
    caller_user_id: str,
    caller_can_write_shared: bool,
) -> bool:
    """``True`` iff a PDF-metadata refresh over a row at *existing_tier*
    owned by *existing_owner* requires shared-write authorization the
    caller does NOT hold (SPEC security-memory-manifest-tier-authz,
    2026-08-30 — the sibling gap the M3-WU-D2-2 reviewer live-demonstrated
    in ``upsert_pdf_metadata``, missed by the initial ``record_create``/
    ``record_update`` fix).

    Looser than :func:`_tier_write_unauthorized` (which
    ``record_create``/``record_update`` apply unconditionally to any
    corpus-tier row, regardless of ownership): ``upsert_pdf_metadata`` is
    the AUTOMATIC re-index writer — every successful ``/memory/index``
    PDF pass calls it, including routine, DOCUMENTED idempotent re-runs
    on the SAME file (``routes/memory.py::index_memory``'s own docstring:
    "Single-file mode... idempotent upsert of one MinIO object") — and
    EVERY PDF-indexed row defaults to ``tier="corpus"`` server-side
    (``db/models.py``'s ``tier`` column ``server_default``) regardless of
    whether the underlying file is actually private or shared, since this
    writer has no ``tier`` parameter at all. Blocking the row's OWN
    creator from ever re-indexing their own just-uploaded PDF a second
    time would be a functional regression, not a security fix — so a
    caller who already owns the row (``existing_owner == caller_user_id``)
    is exempt even when the row reads ``tier=="corpus"``. A caller who is
    NOT the owner still needs ``caller_can_write_shared`` to touch a
    corpus-tier row — that remains the exact cross-user overwrite this
    fix closes (the reviewer's live exploit: another user reindexing a
    curator's corpus-tier PDF and silently reassigning
    ``modified_by_user_id``/overwriting ``pdf_title``/``signature_status``/
    ``document_sha256``).
    """
    if existing_tier != "corpus" or caller_can_write_shared:
        return False
    return existing_owner != caller_user_id


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
    # ── LC-9 — the STORED key. ``key`` is the LOGICAL key (the owner prefix
    # of a private row is stripped once, here, by ``logical_key``); callers
    # that must address the row itself use ``storage_key``. Not serialised.
    storage_key: str = ""

    @classmethod
    def from_row(cls, row: MemoryItem | Row[Any]) -> ManifestEntry:
        """THE single stored -> logical conversion point (LC-9 ADDENDUM-1 C3,
        reader #1); the Mock twin's ``_to_entry`` applies the same
        :func:`logical_key`. *row* is an ORM ``MemoryItem`` or a plain-column
        ``Row`` from ``list_for_layer`` (``_MANIFEST_COLUMNS``)."""
        return cls(
            id=row.id,
            layer=row.layer,
            key=logical_key(row.key, tier=row.tier, owner=row.created_by_user_id),
            storage_key=row.key,
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


async def authorize_write(
    manifest: MemoryManifestService,
    user: UserContext,
    layer: str,
    key: str,
    *,
    requested_tier: str = "private",
    owner_exempt: bool = False,
) -> None:
    """The PRE-WRITE authorization choke (SPEC security-memory-write-
    authorization-choke, 2026-08-30) — SUPERSEDES the manifest-only fix
    (SPEC security-memory-manifest-tier-authz) that three independent
    reviews proved insufficient.

    **Why this exists.** The manifest-layer guards
    (``_tier_write_unauthorized``/``_pdf_metadata_write_unauthorized``,
    applied inside ``record_create``/``record_update``/``record_delete``/
    ``upsert_pdf_metadata``) authorize the Postgres AUDIT row. On the
    index paths (the ``.md``/PDF fold, ``mcp_write``) the ChromaDB content
    write happens BEFORE that audit-row check, and the flush helpers used
    to swallow the resulting ``ManifestAuthorizationError`` in a broad
    ``except Exception`` (WARNING only) — so an unauthorized caller's
    content landed anyway and the response was a silent 200. The
    third-review reviewer live-demonstrated this: a caller holding only
    base ``memory:episodic:write`` (no corpus scope) named their private
    ``.md`` identically to a curator-seeded corpus document; the ChromaDB
    row was overwritten with the attacker's payload and its
    ``tier``/``user_id`` metadata flipped to the attacker's — hijack AND
    hide, achieved with LESS privilege than the exploits the manifest-only
    fix closed.

    **What this does.** Call this FIRST, before ANY content write
    (ChromaDB upsert, S3 write) OR manifest mutation, at every write entry
    point. It resolves the target's CURRENT state via ``manifest.get()`` —
    a FRESH read, independent of and prior to whatever the eventual
    manifest write's own internal check does — and enforces the SAME
    invariant the four manifest guards enforce, just BEFORE instead of
    AFTER the content lands: a caller may write only (a) a NEW item
    requested at a tier it's authorized for, or (b) an EXISTING item
    whose current tier it's authorized to touch. Raises
    :class:`ManifestAuthorizationError` — routes/callers MUST map that to
    HTTP 403, and MUST NOT catch-and-swallow it on any content-write path
    (see ``memory_md_manifest.py``/``memory_pdf/manifest.py``, which no
    longer do).

    **LC-9 (cross-user private-row isolation).** The target is resolved by
    the shared pure resolver (:func:`~audittrace.services.memory_keys.resolve_write`)
    over the (at most two) rows the write can touch: the caller's owner key
    ``{user}/{key}`` and the bare key. Another user's private row can never
    enter the decision, so a private write is always the caller's own space;
    a corpus write over ANY private row, and a foreign row at the caller's own
    target, raise :class:`CrossOwnerWriteError` (403) HERE, before any
    content write. ``key`` is the LOGICAL key.

    ``requested_tier`` is the tier the WRITE is targeting (``"private"``
    by default; ``"corpus"`` when the caller explicitly promotes, or when
    a fold/index path resolved the destination to the shared prefix) —
    checked against the caller's corpus-write authorization REGARDLESS of
    whether the target is new or existing, so a promote-into-corpus
    attempt on a brand-new key is refused just as reliably as an
    overwrite of an existing corpus row.

    ``owner_exempt`` (default ``False`` — the STRICT rule
    ``record_create``/``record_update``/``record_delete`` already use: an
    existing corpus-tier row requires shared-write authorization
    regardless of who created it) — set ``True`` ONLY for the PDF fold
    path, whose downstream manifest write (``upsert_pdf_metadata``) uses
    the LOOSER ``_pdf_metadata_write_unauthorized`` predicate: every
    PDF-indexed row defaults ``tier="corpus"`` server-side (this writer
    has no ``tier`` parameter at all), and a routine, DOCUMENTED
    idempotent re-index of one's OWN just-uploaded PDF must keep working
    without a corpus scope. When ``True``, an existing row the caller
    already owns (``existing.created_by_user_id == user.user_id``) is
    exempt from the corpus-tier check — mirrors
    ``_pdf_metadata_write_unauthorized`` exactly, so this pre-write choke
    never rejects a write the downstream manifest guard would have
    allowed (only ever the reverse: blocking earlier what the manifest
    guard would also have blocked, before any content lands).

    The corpus-write authorization check itself is
    ``routes.memory._corpus_write_authorized`` — imported LAZILY
    (function-scoped) to avoid a circular import: ``routes.memory``
    already imports ``ManifestAuthorizationError``/``ManifestEntry`` from
    THIS module at module load time, so a module-level import the other
    way round would deadlock the import graph. This mirrors the existing
    precedent in ``routes/memory_pdf/pipeline.py::_index_pdf_objects``,
    which lazily imports shared helpers from ``routes.memory`` for the
    identical reason (see that function's docstring).
    """
    _validate_layer(layer)
    from audittrace.routes.memory import (  # noqa: PLC0415 - lazy, avoids a circular import (see docstring)
        _corpus_write_authorized,
    )

    collection = key.split("/", 1)[0] if layer == "semantic" else None
    caller_can_write_shared = _corpus_write_authorized(user, collection)

    if requested_tier == "corpus" and not caller_can_write_shared:
        raise ManifestAuthorizationError(
            f"caller lacks shared-write authorization to write a corpus-tier "
            f"target layer={layer!r} key={key!r}"
        )

    # LC-9: decide through the shared pure resolver BEFORE any content write.
    # It raises CrossOwnerWriteError (-> 403) for a corpus write over any
    # private row and for a foreign row at the private target. The caller's
    # logical key never reaches another user's private row (their rows live
    # under their own owner prefix), so nothing is written before the 403.
    owner = user.user_id
    target_key = physical_key(owner, key) if requested_tier == "private" else key
    target = (
        _entry_facts(await manifest.get(layer, target_key))
        if target_key != key
        else None
    )
    bare = _entry_facts(await manifest.get(layer, key))
    resolution = resolve_write(
        layer, key, owner, requested_tier, target=target, bare=bare
    )
    existing = resolution.row
    if existing is None or resolution.kind != "corpus":
        return
    if (
        owner_exempt
        and not caller_can_write_shared
        and existing.tier == "corpus"
        and existing.created_by_user_id == user.user_id
    ):
        return
    if _tier_write_unauthorized(existing.tier, caller_can_write_shared):
        raise ManifestAuthorizationError(
            f"caller lacks shared-write authorization to write over an "
            f"existing corpus-tier target layer={layer!r} key={key!r}"
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


# ``ManifestEntry`` fields that are NOT ``memory_items`` columns: LC-9's
# ``storage_key`` is derived by ``from_row`` from the ``key`` column (which
# ``from_row`` turns into the LOGICAL key). Named explicitly so any other
# non-column field still fails loudly at import (``getattr`` below).
_DERIVED_ENTRY_FIELDS = frozenset({"storage_key"})

# The manifest columns ``list_for_layer`` selects: exactly the column-backed
# ``ManifestEntry`` fields, so the query returns plain ``Row`` tuples and no
# session-bound ORM instance (#364: nothing may outlive its session).
_MANIFEST_COLUMNS = tuple(
    getattr(MemoryItem, f.name)
    for f in fields(ManifestEntry)
    if f.name not in _DERIVED_ENTRY_FIELDS
)


def _rows_to_entries(
    rows: Sequence[Any], caller: UserContext | None
) -> list[ManifestEntry]:
    """Materialise ``rows`` into ``ManifestEntry`` and apply the ADR-062
    owner-or-corpus predicate. Pure CPU, no I/O: safe to run in a worker
    thread. ``caller=None`` or an admin caller is UNFILTERED."""
    entries = [ManifestEntry.from_row(r) for r in rows]
    if caller is not None and not caller.is_admin:
        entries = [
            e
            for e in entries
            if e.created_by_user_id == caller.user_id or e.tier == "corpus"
        ]
    return entries


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

    async def _fetch_rows(
        self, session: AsyncSession, layer: str, keys: Iterable[str]
    ) -> dict[str, MemoryItem]:
        """Rows of ``layer`` stored at any of *keys*, by stored key."""
        rows = (
            (
                await session.execute(
                    select(MemoryItem).where(
                        MemoryItem.layer == layer, MemoryItem.key.in_(list(keys))
                    )
                )
            )
            .scalars()
            .all()
        )
        return {r.key: r for r in rows}

    async def _resolve_write_in(
        self,
        session: AsyncSession,
        layer: str,
        logical: str,
        owner: str,
        tier: str,
    ) -> tuple[Resolution, dict[str, MemoryItem]]:
        """Fetch the (at most two) rows a write touches and decide. The
        target key is derived BEFORE the fetch so an invalid sub or an
        over-long key is refused with no query."""
        target_key = physical_key(owner, logical) if tier == "private" else logical
        rows = await self._fetch_rows(session, layer, {target_key, logical})
        res = resolve_write(
            layer,
            logical,
            owner,
            tier,
            target=_facts(rows.get(target_key)) if target_key != logical else None,
            bare=_facts(rows.get(logical)),
        )
        return res, rows

    @log_call(logger=logger)
    async def resolve_read(
        self, layer: str, logical_key: str, user: UserContext
    ) -> ManifestEntry | None:
        """Dual-lookup read (LC-9 R3): the caller's owner key, then the
        caller's OWN legacy bare row, then a corpus row. ``None`` for
        everything else -- another user's private row is indistinguishable
        from an absent one."""
        _validate_layer(layer)
        async with self._session_factory() as session:
            row = await self._read_row(session, layer, logical_key, user.user_id)
            return ManifestEntry.from_row(row) if row is not None else None

    async def _read_row(
        self, session: AsyncSession, layer: str, logical: str, owner: str
    ) -> MemoryItem | None:
        # The owner key is only computed when the sub is a valid prefix: a
        # read never raises on a malformed sub, it just cannot see an owner key.
        keys = {logical}
        with contextlib.suppress(InvalidPrivateSubjectError, LogicalKeyTooLongError):
            keys.add(physical_key(owner, logical))
        rows = await self._fetch_rows(session, layer, keys)
        target = rows.get(f"{owner}/{logical}")
        res = resolve_read(owner, target=_facts(target), bare=_facts(rows.get(logical)))
        return rows.get(res.row.key) if res is not None and res.row else None

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
        """Insert a manifest row for *key* (a LOGICAL key), OR revive/overwrite
        the caller's own row.

        LC-9: a private row is stored at ``{user_id}/{key}`` (``user_id`` MUST
        be the token's ``sub``); a corpus row at the bare key. The resolver
        decides what the write touches: the caller's own row (``own``) is
        updated, the caller's OWN legacy bare row (``legacy_own``) is
        copied forward (new owner-keyed row, ``created_at_ms`` preserved, the
        legacy row tombstoned by its owner, ONE transaction), and a bare
        corpus row is subject to the corpus guard (``caller_can_write_shared``,
        fail-closed ``False``) but is NEVER re-stamped: a private write
        creates its own row beside it. A corpus write over any private row,
        and any other cross-owner shape, raises
        :class:`CrossOwnerWriteError` (the silent tier re-stamp of the
        pre-LC-9 ``existing.tier = tier`` branch is gone).

        ``tier`` defaults ``"private"`` (ADR-062 Phase B D2: new writes
        default to the least-shared side). Returns the entry with the
        LOGICAL key.
        """
        _validate_layer(layer)
        now = _now_ms()
        async with self._session_factory() as session:
            res, rows = await self._resolve_write_in(session, layer, key, user_id, tier)
            existing = rows[res.row.key] if res.row is not None else None
            if res.kind == "corpus" and _tier_write_unauthorized(
                "corpus", caller_can_write_shared
            ):
                raise ManifestAuthorizationError(
                    f"caller lacks shared-write authorization to overwrite "
                    f"existing corpus-tier manifest row layer={layer!r} "
                    f"key={key!r}"
                )
            row: MemoryItem
            if existing is not None and (res.kind == "own" or tier == "corpus"):
                # Recreating after a soft-delete (or overwriting an existing
                # live entry; callers usually use `record_update` for the
                # latter, this path is idempotent either way).
                if existing.deleted_at_ms is not None:
                    existing.deleted_at_ms = None
                    existing.deleted_by_user_id = None
                existing.title = title
                existing.size_bytes = size_bytes
                existing.modified_at_ms = now
                existing.modified_by_user_id = user_id
                row = existing
            else:
                created = now
                if res.kind == "legacy_own" and existing is not None:
                    # copy-forward: keep the audit history continuous, tombstone
                    # the legacy row by its owner, same transaction.
                    created = existing.created_at_ms
                    existing.deleted_at_ms = now
                    existing.deleted_by_user_id = user_id
                row = MemoryItem(
                    layer=layer,
                    key=res.storage_key,
                    title=title,
                    size_bytes=size_bytes,
                    created_at_ms=created,
                    modified_at_ms=now,
                    created_by_user_id=user_id,
                    modified_by_user_id=user_id,
                    tier=tier,
                )
                session.add(row)
            await _commit_or_conflict(session, layer, key)
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

        *key* is a LOGICAL key; the row is found by the dual-lookup order
        (the caller's owner key, the caller's OWN legacy bare row, then a
        corpus row). Another user's private row is never reachable: it is
        ``LookupError``, same as an absent row. An update of the caller's
        own LEGACY row copy-forwards it (see :meth:`record_create`).

        ``title`` is updated only if non-None (PUT semantics). Raises
        ``LookupError`` if no row exists, ``RuntimeError`` if the row is
        soft-deleted (the caller should ``record_create`` to revive), and
        :class:`ManifestAuthorizationError` -- BEFORE any field is touched --
        when the row is corpus-tier and ``caller_can_write_shared`` (SPEC
        security-memory-manifest-tier-authz, fail-closed ``False``) is not
        held. ``tier`` is never touched.
        """
        _validate_layer(layer)
        async with self._session_factory() as session:
            row = await self._read_row(session, layer, key, user_id)
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
            now = _now_ms()
            if row.tier == "private" and row.key == key:
                # The caller's own LEGACY bare row: copy forward.
                new_row = MemoryItem(
                    layer=layer,
                    key=physical_key(user_id, key),
                    title=title if title is not None else row.title,
                    size_bytes=size_bytes,
                    created_at_ms=row.created_at_ms,
                    modified_at_ms=now,
                    created_by_user_id=user_id,
                    modified_by_user_id=user_id,
                    tier="private",
                )
                row.deleted_at_ms = now
                row.deleted_by_user_id = user_id
                session.add(new_row)
                row = new_row
            else:
                if title is not None:
                    row.title = title
                row.size_bytes = size_bytes
                row.modified_at_ms = now
                row.modified_by_user_id = user_id
            await _commit_or_conflict(session, layer, key)
            await session.refresh(row)
            return ManifestEntry.from_row(row)

    @log_call(logger=logger)
    async def record_delete(
        self,
        layer: str,
        key: str,
        user_id: str,
        *,
        caller_can_write_shared: bool = False,
    ) -> ManifestEntry:
        """Soft-delete: set ``deleted_at_ms`` + ``deleted_by_user_id``.
        Idempotent -- calling on an already-deleted row is a no-op
        that returns the existing entry. Raises ``LookupError`` if
        the row does not exist (including: it is another user's private
        row, which the caller can never address).

        *key* is a LOGICAL key, resolved by the dual-lookup order of
        :meth:`record_update`. ``caller_can_write_shared`` (SPEC security-
        memory-manifest-tier-authz) is the same fail-closed corpus guard: it
        raises :class:`ManifestAuthorizationError` BEFORE touching any field
        for a corpus-tier row, and gates the idempotent no-op path too (an
        unauthorized caller must not observe a corpus row's deleted state).
        """
        _validate_layer(layer)
        async with self._session_factory() as session:
            row = await self._read_row(session, layer, key, user_id)
            if row is None:
                raise LookupError(f"no manifest row for layer={layer!r} key={key!r}")
            if _tier_write_unauthorized(row.tier, caller_can_write_shared):
                raise ManifestAuthorizationError(
                    f"caller lacks shared-write authorization to delete "
                    f"existing corpus-tier manifest row layer={layer!r} "
                    f"key={key!r}"
                )
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
            q = select(*_MANIFEST_COLUMNS).where(MemoryItem.layer == layer)
            if not include_deleted:
                q = q.where(MemoryItem.deleted_at_ms.is_(None))
            rows = (
                await session.execute(q.order_by(MemoryItem.modified_at_ms.desc()))
            ).all()
        # GP4-CI (2026-11-02): O(rows) pure-Python materialisation +
        # owner-or-corpus filter, run off the event loop so a large manifest
        # cannot starve /health (PYTHON-ENGINEERING section 3). The hop is
        # AFTER the session closed: the pooled connection is returned before
        # we wait for a default-executor worker. ``rows`` are plain ``Row``
        # tuples of column values (NOT session-bound ORM instances, which the
        # #364 session-scope guard forbids reading after the block), so the
        # worker does no I/O and no lazy load. ``asyncio.to_thread`` copies
        # the ContextVars (identity / trace) into the worker.
        return await asyncio.to_thread(_rows_to_entries, rows, caller)

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
        caller_can_write_shared: bool = False,
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

        ``caller_can_write_shared`` (SPEC security-memory-manifest-tier-
        authz, 2026-08-30) — the SAME fail-closed (default ``False``)
        shared-write authorization ``record_create``/``record_update``
        take. This is the manifest choke for the PDF ingestion pipeline
        (``/memory/index``'s PDF branch, the auto-index outbox worker) —
        it had NO tier/ownership check at all before this fix, so a
        caller holding only base ``memory:<layer>:write`` could reindex
        an EXISTING **corpus**-tier PDF and silently reassign
        ``modified_by_user_id`` + overwrite ``pdf_title``/
        ``signature_status``/``document_sha256``/etc — the same
        cross-owner-overwrite vulnerability class ``record_create``/
        ``record_update`` were fixed for, on a sibling writer this fix
        initially missed. Raises :class:`ManifestAuthorizationError`
        BEFORE touching any field when the EXISTING row is corpus-tier
        and the caller lacks authorization — a legitimate metadata
        refresh (same owner, or the system auto-index worker, which
        always passes ``True``) is unaffected: this method never
        reassigns ``tier`` in the first place (a metadata refresh is not
        a tier/owner operation), so an authorized-or-private-tier caller
        sees no behavior change at all.
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
                if _pdf_private_row_foreign(row.tier, row.created_by_user_id, user_id):
                    raise CrossOwnerWriteError(tier_conflict_message(layer, key))
                if _pdf_metadata_write_unauthorized(
                    row.tier, row.created_by_user_id, user_id, caller_can_write_shared
                ):
                    raise ManifestAuthorizationError(
                        f"caller lacks shared-write authorization to overwrite "
                        f"existing corpus-tier PDF manifest row layer={layer!r} "
                        f"key={key!r}"
                    )
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
                        key=logical_key(
                            row.key, tier=row.tier, owner=row.created_by_user_id
                        ),
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
            key=logical_key(
                row["key"],
                tier=row.get("tier", "corpus"),
                owner=row.get("created_by_user_id"),
            ),
            storage_key=row["key"],
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

    def _mock_facts(self, layer: str, key: str) -> RowFacts | None:
        row = self._rows.get((layer, key))
        if row is None:
            return None
        return RowFacts(
            key=key,
            tier=row.get("tier", "corpus"),
            created_by_user_id=row["created_by_user_id"],
            deleted_at_ms=row.get("deleted_at_ms"),
        )

    def _mock_resolve_write(
        self, layer: str, logical: str, owner: str, tier: str
    ) -> Resolution:
        """Same inputs, same pure decision as the Postgres twin."""
        target_key = physical_key(owner, logical) if tier == "private" else logical
        return resolve_write(
            layer,
            logical,
            owner,
            tier,
            target=self._mock_facts(layer, target_key)
            if target_key != logical
            else None,
            bare=self._mock_facts(layer, logical),
        )

    def _mock_read_row(
        self, layer: str, logical: str, owner: str
    ) -> dict[str, Any] | None:
        res = resolve_read(
            owner,
            target=self._mock_facts(layer, f"{owner}/{logical}"),
            bare=self._mock_facts(layer, logical),
        )
        return self._rows[(layer, res.row.key)] if res is not None and res.row else None

    async def resolve_read(
        self, layer: str, logical_key: str, user: UserContext
    ) -> ManifestEntry | None:
        _validate_layer(layer)
        row = self._mock_read_row(layer, logical_key, user.user_id)
        return self._to_entry(row) if row is not None else None

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
        res = self._mock_resolve_write(layer, key, user_id, tier)
        existing = self._rows[(layer, res.row.key)] if res.row is not None else None
        if res.kind == "corpus" and _tier_write_unauthorized(
            "corpus", caller_can_write_shared
        ):
            raise ManifestAuthorizationError(
                f"caller lacks shared-write authorization to overwrite "
                f"existing corpus-tier manifest row layer={layer!r} "
                f"key={key!r}"
            )
        if existing is not None and (res.kind == "own" or tier == "corpus"):
            existing["title"] = title
            existing["size_bytes"] = size_bytes
            existing["modified_at_ms"] = now
            existing["modified_by_user_id"] = user_id
            if existing.get("deleted_at_ms") is not None:
                existing["deleted_at_ms"] = None
                existing["deleted_by_user_id"] = None
            return self._to_entry(existing)
        import uuid

        created = now
        if res.kind == "legacy_own" and existing is not None:
            created = existing["created_at_ms"]
            existing["deleted_at_ms"] = now
            existing["deleted_by_user_id"] = user_id
        new_row: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "layer": layer,
            "key": res.storage_key,
            "title": title,
            "size_bytes": size_bytes,
            "created_at_ms": created,
            "modified_at_ms": now,
            "created_by_user_id": user_id,
            "modified_by_user_id": user_id,
            "deleted_at_ms": None,
            "deleted_by_user_id": None,
            "tier": tier,
        }
        self._rows[(layer, res.storage_key)] = new_row
        return self._to_entry(new_row)

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
        existing = self._mock_read_row(layer, key, user_id)
        if existing is None:
            raise LookupError(f"no manifest row for layer={layer!r} key={key!r}")
        if existing.get("deleted_at_ms") is not None:
            raise RuntimeError(
                f"manifest row for layer={layer!r} key={key!r} is "
                f"soft-deleted; use record_create to revive"
            )
        tier = existing.get("tier", "corpus")
        if _tier_write_unauthorized(tier, caller_can_write_shared):
            raise ManifestAuthorizationError(
                f"caller lacks shared-write authorization to overwrite "
                f"existing corpus-tier manifest row layer={layer!r} "
                f"key={key!r}"
            )
        now = _now_ms()
        if tier == "private" and existing["key"] == key:
            # The caller's own LEGACY bare row: copy forward.
            import uuid

            new_key = physical_key(user_id, key)
            new_row: dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "layer": layer,
                "key": new_key,
                "title": title if title is not None else existing.get("title"),
                "size_bytes": size_bytes,
                "created_at_ms": existing["created_at_ms"],
                "modified_at_ms": now,
                "created_by_user_id": user_id,
                "modified_by_user_id": user_id,
                "deleted_at_ms": None,
                "deleted_by_user_id": None,
                "tier": "private",
            }
            existing["deleted_at_ms"] = now
            existing["deleted_by_user_id"] = user_id
            self._rows[(layer, new_key)] = new_row
            return self._to_entry(new_row)
        if title is not None:
            existing["title"] = title
        existing["size_bytes"] = size_bytes
        existing["modified_at_ms"] = now
        existing["modified_by_user_id"] = user_id
        return self._to_entry(existing)

    async def record_delete(
        self,
        layer: str,
        key: str,
        user_id: str,
        *,
        caller_can_write_shared: bool = False,
    ) -> ManifestEntry:
        _validate_layer(layer)
        existing = self._mock_read_row(layer, key, user_id)
        if existing is None:
            raise LookupError(f"no manifest row for layer={layer!r} key={key!r}")
        if _tier_write_unauthorized(
            existing.get("tier", "corpus"), caller_can_write_shared
        ):
            raise ManifestAuthorizationError(
                f"caller lacks shared-write authorization to delete "
                f"existing corpus-tier manifest row layer={layer!r} "
                f"key={key!r}"
            )
        if existing.get("deleted_at_ms") is None:
            existing["deleted_at_ms"] = _now_ms()
            existing["deleted_by_user_id"] = user_id
        return self._to_entry(existing)

    @log_call(logger=logger)
    async def list_for_layer(
        self,
        layer: str,
        *,
        include_deleted: bool = False,
        caller: UserContext | None = None,
    ) -> list[ManifestEntry]:
        """SCRUB-EVENT-LOOP-STALL (v1.29.4, R5/F11 — ratified ADDENDUM A1,
        spec fact F11) — carries the IDENTICAL ``@log_call`` decorator as
        ``MemoryManifestService.list_for_layer`` above (G-P5 pins
        ``ast.dump`` equality between the two). Before this fix the mock
        — which the test container wires for every route test
        (``dependencies.py``) — had NO decorator, so no existing route
        test ever exercised the seam that stalled the event loop in
        v1.29.3; only a real-route test through the real decorator can
        catch a regression here."""
        _validate_layer(layer)
        # GP4-CI: same off-loop CPU discipline as the Postgres class.
        return await asyncio.to_thread(
            self._list_entries_sync, layer, include_deleted, caller
        )

    def _list_entries_sync(
        self, layer: str, include_deleted: bool, caller: UserContext | None
    ) -> list[ManifestEntry]:
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
                            key=logical_key(
                                r["key"],
                                tier=r.get("tier", "corpus"),
                                owner=r.get("created_by_user_id"),
                            ),
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
            if _pdf_private_row_foreign(
                existing.get("tier", "corpus"),
                existing.get("created_by_user_id", ""),
                user_id,
            ):
                raise CrossOwnerWriteError(tier_conflict_message(layer, key))
            if _pdf_metadata_write_unauthorized(
                existing.get("tier", "corpus"),
                existing.get("created_by_user_id", ""),
                user_id,
                caller_can_write_shared,
            ):
                raise ManifestAuthorizationError(
                    f"caller lacks shared-write authorization to overwrite "
                    f"existing corpus-tier PDF manifest row layer={layer!r} "
                    f"key={key!r}"
                )
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
