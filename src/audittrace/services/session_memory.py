"""Session memory service — the ``session`` layer, WU-1 of the
Sovereign-Attach EPIC.

Ephemeral, per-user, Postgres-backed memory layer: the enforced
least-privilege wall the ratified *ephemeral-default* decision requires.
Distinct from episodic/procedural (S3-backed, dual-tier per ADR-062
Phase B) — session content has **no corpus tier** and **no promote path**
in this WU (promotion to a durable layer is WU-4, out of scope here).
This module is write-path + isolation ONLY, per the ratified spec
(2026-09-03-SPEC-wu1-session-layer-narrow-ingest-scope.md).

**WU-5 (same-turn recall, 2026-09-06-SPEC-wu5-same-turn-session-recall.md)
adds :meth:`SessionMemoryService.list_own`** — a recency-ordered LIST of
the caller's own recent uploads (D-a: not a vector search over session
rows; the layer stays ephemeral scratch, never indexed into ChromaDB —
GC is WU-6's job and untouched by this addition).

**WU-6 (2026-09-06-SPEC-wu6-session-gc-live-e2e-release.md, Part A) adds
:meth:`SessionMemoryService.gc_expired`** — the janitor's bounded hard-
DELETE sweep. Unlike every other method on this interface, ``gc_expired``
does NOT take a ``UserContext``: it is a SYSTEM-WIDE sweep across every
user's rows (the janitor, not a user request), so it is deliberately
shaped to be unreachable from any user-facing route — no route in
``routes/`` ever calls it, only ``services/session_gc_janitor.py`` does.
A-b decision: session content has no soft-delete/tombstone concept (it is
ephemeral by ratified design), so GC is a genuine hard DELETE, never a
flag flip. GC of a session ORIGINAL never touches a WU-4-**promoted**
durable copy — the promote path already COPIES content into a completely
different table (``MemoryItem``/ChromaDB row via ``EpisodicService``/
``ChromaSemanticService``), so ``gc_expired`` — which only ever queries
``SessionMemoryItem`` — structurally cannot reach it.

Every method takes ``user_context: UserContext`` and filters explicitly
by ``user_context.user_id`` at the SERVICE layer — the same "Phase 2"
pattern ``PostgresConversationalService`` already uses for the sessions/
interactions tables. This is required, not decorative: Postgres RLS
(migration 022) is a no-op on SQLite, so the unit-test suite would
silently miss a dropped ``WHERE user_id = ...`` filter without this
belt-and-suspenders duplication (feedback_unit_tests_miss_rls).
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from langchain_core.documents import Document
from opentelemetry import metrics
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.db.models import SessionMemoryItem
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.services.semantic import MAX_RECALL_WINDOW

logger = logging.getLogger(__name__)

# ── WU-6 (Part A) telemetry ─────────────────────────────────────────────
# Mirrors the module-level OTel-meter idiom in
# ``services/write_telemetry.py``/``services/recall_telemetry.py`` —
# ``metrics.get_meter(...)`` returns a no-op meter until a real
# ``MeterProvider`` is configured, so this stays a safe no-op when
# ``AUDITTRACE_OTLP_ENDPOINT`` is unset (laptop-default off).
#
# Emitted from INSIDE ``gc_expired`` (not from the janitor's caller, the
# usual ``write_telemetry`` division of labour) because the real
# oldest/newest ``created_at_ms`` bounds only exist here — the victim rows
# are already read (and ordered) to build the bounded DELETE, so this is
# the one place that can log real bounds with ZERO extra DB round-trips.
# The janitor's own loop only ever sees the plain ``int`` count the
# abstract contract promises (spec §2.3 A-D1); this side-channel does not
# change that return-type contract.
_gc_meter = metrics.get_meter("audittrace.session_gc")
_session_gc_deleted_total = _gc_meter.create_counter(
    name="audittrace_session_gc_deleted_total",
    description=(
        "Session-memory rows hard-deleted by the WU-6 retention GC sweep. "
        "No labels, no PII."
    ),
)


def _emit_session_gc_telemetry(
    *, count: int, oldest_created_at_ms: int, newest_created_at_ms: int
) -> None:
    """Counter increment + one structured INFO log line for a non-empty
    GC delete batch. NO content, NO filename — only the row count and the
    ``created_at_ms`` bounds of the batch (spec §2.3 A-D3). Both call
    sites (Postgres + Mock ``gc_expired``) already return early on an
    empty victim set BEFORE calling this — an empty tick never reaches
    here, so this function has no zero-count branch to guard (mirrors
    ``write_telemetry.emit_chunks_indexed``'s zero-chunk-skip rationale,
    just enforced one level up the call chain instead of inside here)."""
    _session_gc_deleted_total.add(count)
    logger.info(
        "session_gc.deleted",
        extra={
            "count": count,
            "oldest_created_at_ms": oldest_created_at_ms,
            "newest_created_at_ms": newest_created_at_ms,
        },
    )


def _validate_session_filename(filename: str) -> bool:
    """Reject empty / path-traversal filenames.

    Unlike episodic/procedural (``.md``-only — ADR-018/ADR-062), session
    uploads are not restricted to a single extension: a chat composer
    attachment can be any short plain-text note, snippet, or log excerpt
    — the layer is ephemeral scratch space, not a curated document
    store. Path-traversal characters are still rejected; there is no
    directory concept to traverse into, but a filename is echoed back
    verbatim in the API response and audit trail, so it must not carry
    control characters an operator's tooling could misinterpret.
    """
    if not isinstance(filename, str) or not filename:
        return False
    if ".." in filename or "/" in filename or "\\" in filename:
        return False
    return True


def _document_from(
    *, row_id: str, user_id: str, filename: str, content: str, created_at_ms: int
) -> Document:
    """Build the ``Document`` shape every service method returns."""
    return Document(
        page_content=content,
        metadata={
            "id": row_id,
            "filename": filename,
            "layer": "session",
            "tier": "private",
            "user_id": user_id,
            "created_at_ms": created_at_ms,
        },
    )


class SessionMemoryService(ABC):
    """Abstract ephemeral session-memory service.

    Write-path + isolation (WU-1) + recency-ordered listing (WU-5,
    :meth:`list_own`) + the janitor's bounded GC sweep (WU-6,
    :meth:`gc_expired`) — no search/promote here; promote is WU-4
    (``routes/memory_promote.py``).
    """

    @abstractmethod
    async def write(
        self, user_context: UserContext, filename: str, content: str
    ) -> Document:
        """Create a session-tier document in the caller's PRIVATE,
        EPHEMERAL layer.

        Returns the persisted ``Document``. Raises ``ValueError`` for an
        invalid filename and ``RuntimeError`` for a backend write
        failure — same contract shape as
        :meth:`~audittrace.services.episodic.EpisodicService.write`, so
        route-layer error handling (``_write_layer_private``) can treat
        every layer uniformly.
        """

    @abstractmethod
    async def read_own(
        self, user_context: UserContext, filename: str
    ) -> Document | None:
        """Return the caller's OWN most recent session upload matching
        *filename*, or ``None`` if they have none.

        MUST NEVER return another user's row — this is the isolation
        guard the RLS acceptance test (spec deliverable 3 / acceptance
        (d)) exercises: writing as user A and reading as user B must
        yield ``None``.
        """

    @abstractmethod
    async def list_own(
        self, user_context: UserContext, *, limit: int, offset: int = 0
    ) -> list[Document]:
        """Return a WINDOW of the caller's OWN session uploads,
        recency-ordered (``created_at_ms`` descending) — WU-5 same-turn
        recall (``2026-09-06-SPEC-wu5-same-turn-session-recall.md``).

        Returns the raw window — up to ``min(offset + limit + 1,
        MAX_RECALL_WINDOW)`` rows, most-recent-first, filtered by
        ``user_id == user_context.user_id`` (the isolation guard the
        recall non-vacuity test neuters: drop that filter and another
        user's uploads leak into the window). The **caller** (the
        ``recall_attachments`` tool handler) slices ``[offset:offset +
        limit]`` for the page and derives ``total``/``has_more`` from
        ``len(window)`` — the same "+1 probe" division of labour
        ``recall_recent_sessions`` already uses over
        ``ConversationalService.load_sessions`` (see
        ``tools/memory_handlers.py``), so a caller paging past a full
        window still gets a correct ``has_more`` signal instead of the
        false-negative a plain ``LIMIT limit`` would produce.

        MUST NEVER include another user's row — the non-vacuity guard
        this method exists to satisfy (spec §6.1): neuter the explicit
        ``user_id`` filter and a cross-user isolation test goes RED.
        """

    @abstractmethod
    async def gc_expired(self, *, older_than_ms: int, limit: int) -> int:
        """SYSTEM sweep — bounded hard-DELETE of every row across every
        user whose ``created_at_ms < older_than_ms``. Returns the count
        actually deleted (``<= limit``).

        Deliberately takes NO ``user_context`` — this is the janitor's
        cross-user sweep, never a per-user request, so it must never be
        wired to a user-facing route (spec §2.3 A-D1). Callers (only
        :class:`~audittrace.services.session_gc_janitor.SessionGCJanitor`)
        loop calling this with the same ``older_than_ms`` until the
        returned count is below ``limit``, so a single call is free to
        stop at the batch boundary rather than draining every eligible
        row in one pass.

        Non-vacuity guards this method exists to satisfy (spec §2.5):
        (1) the ``created_at_ms < older_than_ms`` cutoff — neuter it to
        delete unconditionally and a fresh (non-expired) row goes missing;
        (2) the ``limit`` bound — neuter it to delete unboundedly and a
        batch-cap test goes RED.
        """


class PostgresSessionMemoryService(SessionMemoryService):
    """PostgreSQL-backed session-memory service."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    @log_call(logger=logger)
    async def write(
        self, user_context: UserContext, filename: str, content: str
    ) -> Document:
        if not _validate_session_filename(filename):
            raise ValueError(f"invalid filename: {filename!r}")
        row_id = str(uuid.uuid4())
        size_bytes = len(content.encode("utf-8"))
        created_at_ms = int(time.time() * 1000)
        async with self._session_factory() as session:
            try:
                session.add(
                    SessionMemoryItem(
                        id=row_id,
                        user_id=user_context.user_id,
                        filename=filename,
                        content=content,
                        size_bytes=size_bytes,
                        created_at_ms=created_at_ms,
                    )
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                raise RuntimeError(
                    f"PostgresSessionMemoryService.write({filename!r}) failed: {exc}"
                ) from exc
        return _document_from(
            row_id=row_id,
            user_id=user_context.user_id,
            filename=filename,
            content=content,
            created_at_ms=created_at_ms,
        )

    @log_call(logger=logger)
    async def read_own(
        self, user_context: UserContext, filename: str
    ) -> Document | None:
        # #364 discipline: extract plain values INSIDE the `async with`
        # block — an ORM instance read after its session has closed is a
        # detached-instance footgun (tests/test_session_scope_discipline.py
        # enforces this mechanically across the codebase).
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(SessionMemoryItem)
                        # Explicit user_id filter (Phase-2 pattern) —
                        # this is the guard the isolation test neuters
                        # to prove non-vacuity: drop this .filter() and
                        # user B's read starts returning user A's row.
                        .filter(SessionMemoryItem.user_id == user_context.user_id)
                        .filter(SessionMemoryItem.filename == filename)
                        .order_by(SessionMemoryItem.created_at_ms.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                return None
            row_id, row_user_id, row_filename, row_content, row_created_at_ms = (
                row.id,
                row.user_id,
                row.filename,
                row.content,
                row.created_at_ms,
            )
        return _document_from(
            row_id=row_id,
            user_id=row_user_id,
            filename=row_filename,
            content=row_content,
            created_at_ms=row_created_at_ms,
        )

    @log_call(logger=logger)
    async def list_own(
        self, user_context: UserContext, *, limit: int, offset: int = 0
    ) -> list[Document]:
        window = min(offset + limit + 1, MAX_RECALL_WINDOW)
        # #364 discipline: extract plain values INSIDE the `async with`
        # block (see read_own above) — build the tuple list here, turn
        # each into a Document only AFTER the session has closed.
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(SessionMemoryItem)
                        # Explicit user_id filter (Phase-2 pattern) — the
                        # isolation guard the recall non-vacuity test
                        # neuters: drop this .filter() and another
                        # user's uploads leak into the window.
                        .filter(SessionMemoryItem.user_id == user_context.user_id)
                        .order_by(SessionMemoryItem.created_at_ms.desc())
                        .limit(window)
                    )
                )
                .scalars()
                .all()
            )
            plain_rows = [
                (row.id, row.user_id, row.filename, row.content, row.created_at_ms)
                for row in rows
            ]
        return [
            _document_from(
                row_id=row_id,
                user_id=row_user_id,
                filename=row_filename,
                content=row_content,
                created_at_ms=row_created_at_ms,
            )
            for row_id, row_user_id, row_filename, row_content, row_created_at_ms in plain_rows
        ]

    @log_call(logger=logger)
    async def gc_expired(self, *, older_than_ms: int, limit: int) -> int:
        # Postgres has no portable ``DELETE ... LIMIT`` (unlike MySQL);
        # the standard bounded-delete idiom is SELECT the victim rows
        # (ordered oldest-first for a deterministic sweep order) THEN
        # DELETE WHERE id IN (...). This is the guard the non-vacuity
        # test neuters two ways: drop the ``created_at_ms < older_than_ms``
        # filter (a fresh row goes missing) or drop the ``.limit(limit)``
        # call (an unbounded delete on a single tick).
        async with self._session_factory() as session:
            victims = (
                await session.execute(
                    select(SessionMemoryItem.id, SessionMemoryItem.created_at_ms)
                    .where(SessionMemoryItem.created_at_ms < older_than_ms)
                    .order_by(SessionMemoryItem.created_at_ms.asc())
                    .limit(limit)
                )
            ).all()
            if not victims:
                return 0
            victim_ids = [row.id for row in victims]
            await session.execute(
                delete(SessionMemoryItem).where(SessionMemoryItem.id.in_(victim_ids))
            )
            await session.commit()
        _emit_session_gc_telemetry(
            count=len(victims),
            oldest_created_at_ms=victims[0].created_at_ms,
            newest_created_at_ms=victims[-1].created_at_ms,
        )
        return len(victims)


@dataclass(frozen=True)
class _MockRow:
    """Typed row shape for :class:`MockSessionMemoryService` — avoids the
    ``dict[str, object]`` + per-field ``str()``/``int()`` narrowing dance
    a loosely-typed dict would need under mypy strict mode."""

    id: str
    user_id: str
    filename: str
    content: str
    created_at_ms: int


class MockSessionMemoryService(SessionMemoryService):
    """In-process mock for unit tests that don't wire a Postgres factory
    (mirrors ``MockConversationalService``'s shape)."""

    def __init__(self) -> None:
        self._items: list[_MockRow] = []

    @log_call(logger=logger)
    async def write(
        self, user_context: UserContext, filename: str, content: str
    ) -> Document:
        if not _validate_session_filename(filename):
            raise ValueError(f"invalid filename: {filename!r}")
        row_id = str(uuid.uuid4())
        created_at_ms = int(time.time() * 1000)
        self._items.append(
            _MockRow(
                id=row_id,
                user_id=user_context.user_id,
                filename=filename,
                content=content,
                created_at_ms=created_at_ms,
            )
        )
        return _document_from(
            row_id=row_id,
            user_id=user_context.user_id,
            filename=filename,
            content=content,
            created_at_ms=created_at_ms,
        )

    @log_call(logger=logger)
    async def read_own(
        self, user_context: UserContext, filename: str
    ) -> Document | None:
        matches = [
            item
            for item in self._items
            if item.user_id == user_context.user_id and item.filename == filename
        ]
        if not matches:
            return None
        row = max(matches, key=lambda item: item.created_at_ms)
        return _document_from(
            row_id=row.id,
            user_id=row.user_id,
            filename=row.filename,
            content=row.content,
            created_at_ms=row.created_at_ms,
        )

    @log_call(logger=logger)
    async def list_own(
        self, user_context: UserContext, *, limit: int, offset: int = 0
    ) -> list[Document]:
        window = min(offset + limit + 1, MAX_RECALL_WINDOW)
        # Reverse insertion order FIRST, then a stable sort by
        # created_at_ms (descending) — `time.time()` millisecond
        # resolution means two writes issued back-to-back (no I/O
        # between them, unlike the real Postgres path) can tie on
        # created_at_ms; reversing first makes the tie-break
        # deterministically "most-recently-written-first" instead of
        # depending on Python's stable-sort preserving append order.
        mine = [
            item
            for item in reversed(self._items)
            if item.user_id == user_context.user_id
        ]
        mine.sort(key=lambda item: item.created_at_ms, reverse=True)
        return [
            _document_from(
                row_id=row.id,
                user_id=row.user_id,
                filename=row.filename,
                content=row.content,
                created_at_ms=row.created_at_ms,
            )
            for row in mine[:window]
        ]

    @log_call(logger=logger)
    async def gc_expired(self, *, older_than_ms: int, limit: int) -> int:
        # Mirrors the Postgres impl's shape: select victims oldest-first,
        # bounded by ``limit``, then remove exactly those from the store.
        eligible = sorted(
            (item for item in self._items if item.created_at_ms < older_than_ms),
            key=lambda item: item.created_at_ms,
        )
        victims = eligible[:limit]
        if not victims:
            return 0
        victim_ids = {item.id for item in victims}
        self._items = [item for item in self._items if item.id not in victim_ids]
        _emit_session_gc_telemetry(
            count=len(victims),
            oldest_created_at_ms=victims[0].created_at_ms,
            newest_created_at_ms=victims[-1].created_at_ms,
        )
        return len(victims)

    def reset(self) -> None:
        """Clear all session-memory items."""
        self._items.clear()
