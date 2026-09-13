"""Console-tool-favorites service — the Tool-Favorites domain of the
MongoDB-elimination EPIC.

The AuditTrace-side, RLS-isolated store backing LibreChat's
``ToolFavorite`` record (the fork's Mongo ``ToolFavorite`` collection,
``packages/data-schemas/src/schema/favorite.ts``), per the ratified
spec (2026-09-13-SPEC-mongo-repl-wu-tool-favorites-store.md). Mirrors
``services/console_conversation_tags.py``'s shape and discipline
EXACTLY (the spec's instruction): an ABC + a Postgres-backed
implementation with an explicit ``user_sub`` filter on every query (the
Phase-2 pattern) + a Mock for fast unit tests.

**Own-favorites-only v1.** Every row is owned by exactly one
``user_sub`` — there is no shared/global favorite concept, same
discipline as every other console-* domain's own-scoped v1.

**No cursor pagination (a deliberate, spec-faithful deviation).** The
grounding methods (``getToolFavorites``) return the caller's ENTIRE
favorites list, sorted oldest-first, with no pagination parameter — and
``MAX_TOOL_FAVORITES`` bounds that list at 100 rows, so a single
unpaginated response is always small. ``list_tool_favorites`` mirrors
that shape exactly rather than inventing a cursor contract the fork
does not have.

**``MAX_TOOL_FAVORITES = 100`` per user — enforced server-side.**
Mirrors the fork's ``methods/favorite.ts`` business rule
(``capError``/``MAX_FAVORITES_EXCEEDED``): adding a 101st ACTIVE
favorite raises :class:`ToolFavoritesCapExceededError`. Re-favoriting
an existing (non-deleted) pair, or updating an already-active row's
``tenant_id``/``metadata``, is idempotent and never counts against the
cap — only a genuine transition into "active" (brand-new row, or
un-tombstoning a soft-deleted row) is capped.

**AVOID the D13 soft-delete quirk.** The existence lookup for an ACTIVE
row filters ``deleted_at_ms IS NULL`` explicitly; when a caller
re-favorites a pair whose only prior row is soft-deleted, that SAME row
is un-tombstoned (``deleted_at_ms`` cleared) rather than either leaving
it deleted under an apparently-successful upsert, or attempting a
second INSERT that would violate the unique constraint.

**COMPLETENESS (the WU-2 lesson).** Every method below (other than the
abstract-interface constructor) takes a ``UserContext`` and filters
explicitly by ``user_context.user_id`` at the SERVICE layer — Postgres
RLS (migration 030) is a no-op on SQLite, so this belt-and-suspenders
duplication is required, not decorative
(feedback_unit_tests_miss_rls). ``user_sub`` is ALWAYS taken from the
``UserContext`` the route resolved from the token — a caller-supplied
``user_sub`` in a request body is never read by any method here
(feedback_never_trust_caller_metadata_for_security_fields); the route
layer enforces this by construction (it never passes a body field
named ``user_sub``/``user_id`` into these methods).
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.db.models import ConsoleToolFavorite
from audittrace.identity import UserContext
from audittrace.logging_config import log_call

logger = logging.getLogger(__name__)

# Mirrors the fork's methods/favorite.ts::MAX_TOOL_FAVORITES exactly —
# the business rule this domain enforces server-side (the fork's own
# enforcement is a Mongo countDocuments() read-then-write check; ours
# is the Postgres-backed equivalent, same "soft UX cap, may transiently
# overshoot by one under concurrency" acknowledgement).
MAX_TOOL_FAVORITES = 100

# The closed ``item_type`` vocabulary (the fork's
# types/favorite.ts::FAVORITE_ITEM_TYPES Mongoose enum) has ONE source of
# truth: ``audittrace.models._TOOL_FAVORITE_ITEM_TYPE`` — the Pydantic
# ``Literal`` that rejects an unknown item_type with 422 at the route
# boundary BEFORE any service method runs. This module deliberately does
# NOT carry a duplicate tuple (a second copy was dead code that could
# silently drift from the enforced one — 2026-09-13 review F2).


class ToolFavoritesCapExceededError(Exception):
    """Raised by :meth:`ConsoleToolFavoritesService.add_tool_favorite`
    when the caller already owns ``MAX_TOOL_FAVORITES`` ACTIVE
    favorites and the requested ``(item_type, item_id)`` pair is not
    already one of them (a transition into "active" that would exceed
    the cap). Route layer maps this to HTTP 409."""


def _now_ms() -> int:
    return int(time.time() * 1000)


class ConsoleToolFavoritesService(ABC):
    """Abstract console-tool-favorites store — the sovereign replacement
    for LibreChat's Mongo ``ToolFavorite`` collection."""

    @abstractmethod
    async def add_tool_favorite(
        self,
        user_context: UserContext,
        item_type: str,
        item_id: str,
        *,
        tenant_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create (or idempotently re-affirm) the caller's OWN favorite
        identified by ``(user_sub, item_type, item_id)``. Raises
        :class:`ToolFavoritesCapExceededError` if the caller already
        owns ``MAX_TOOL_FAVORITES`` ACTIVE favorites and this pair is
        not already one of them. Returns the persisted row as a plain
        dict."""

    @abstractmethod
    async def list_tool_favorites(
        self, user_context: UserContext
    ) -> list[dict[str, Any]]:
        """Return ALL of the caller's OWN (non-deleted) favorites,
        oldest-first (mirrors the fork's ``getToolFavorites`` sort). No
        pagination — bounded by ``MAX_TOOL_FAVORITES``.

        MUST NEVER include another user's favorite — the isolation
        guard the non-vacuity test neuters: drop the explicit
        ``user_sub`` filter and another user's rows leak into the list.
        """

    @abstractmethod
    async def remove_tool_favorite(
        self, user_context: UserContext, item_type: str, item_id: str
    ) -> bool:
        """Soft-delete the caller's OWN favorite. Returns ``True`` if a
        (previously non-deleted) favorite was found and removed,
        ``False`` otherwise (not found/not owned/already removed —
        idempotent no-op)."""


def _tool_favorite_to_dict(row: ConsoleToolFavorite) -> dict[str, Any]:
    return {
        "item_type": row.item_type,
        "item_id": row.item_id,
        "user_sub": row.user_sub,
        "tenant_id": row.tenant_id,
        "created_at_ms": row.created_at_ms,
        "updated_at_ms": row.updated_at_ms,
        "deleted_at_ms": row.deleted_at_ms,
        "metadata": row.metadata_json,
    }


class PostgresConsoleToolFavoritesService(ConsoleToolFavoritesService):
    """PostgreSQL-backed console-tool-favorites service."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    @log_call(logger=logger)
    async def add_tool_favorite(
        self,
        user_context: UserContext,
        item_type: str,
        item_id: str,
        *,
        tenant_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        async with self._session_factory() as session:
            try:
                active = (
                    await session.execute(
                        select(ConsoleToolFavorite)
                        # The isolation guard this add's non-vacuity
                        # test neuters — drop this filter and a
                        # hostile caller could resurrect/update
                        # ANOTHER user's row by guessing their
                        # item_type/item_id.
                        .filter(ConsoleToolFavorite.user_sub == user_context.user_id)
                        .filter(ConsoleToolFavorite.item_type == item_type)
                        .filter(ConsoleToolFavorite.item_id == item_id)
                        # D13 avoidance: an ACTIVE-row lookup MUST
                        # filter deleted_at_ms IS NULL — never treat a
                        # soft-deleted row as "already active".
                        .filter(ConsoleToolFavorite.deleted_at_ms.is_(None))
                    )
                ).scalar_one_or_none()

                if active is not None:
                    # Idempotent re-affirm of an already-active
                    # favorite — never counts against the cap.
                    if tenant_id is not None:
                        active.tenant_id = tenant_id
                    if metadata is not None:
                        active.metadata_json = metadata
                    active.updated_at_ms = now
                    await session.commit()
                    return _tool_favorite_to_dict(active)

                # Not currently active (either no row at all, or a
                # soft-deleted tombstone) — this is a genuine
                # transition into "active", so the cap applies.
                active_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(ConsoleToolFavorite)
                        .filter(ConsoleToolFavorite.user_sub == user_context.user_id)
                        .filter(ConsoleToolFavorite.deleted_at_ms.is_(None))
                    )
                ).scalar_one()
                if active_count >= MAX_TOOL_FAVORITES:
                    raise ToolFavoritesCapExceededError(
                        f"maximum of {MAX_TOOL_FAVORITES} tool favorites reached"
                    )

                # D13 avoidance: look for a soft-deleted tombstone
                # under the SAME unique key (user_sub, item_type,
                # item_id) — the unique constraint forbids a second
                # row, so re-favoriting MUST un-tombstone this row,
                # never attempt a fresh INSERT.
                tombstoned = (
                    await session.execute(
                        select(ConsoleToolFavorite)
                        .filter(ConsoleToolFavorite.user_sub == user_context.user_id)
                        .filter(ConsoleToolFavorite.item_type == item_type)
                        .filter(ConsoleToolFavorite.item_id == item_id)
                    )
                ).scalar_one_or_none()

                if tombstoned is not None:
                    tombstoned.deleted_at_ms = None
                    if tenant_id is not None:
                        tombstoned.tenant_id = tenant_id
                    if metadata is not None:
                        tombstoned.metadata_json = metadata
                    tombstoned.updated_at_ms = now
                    row = tombstoned
                else:
                    row = ConsoleToolFavorite(
                        id=str(uuid.uuid4()),
                        user_sub=user_context.user_id,
                        item_type=item_type,
                        item_id=item_id,
                        tenant_id=tenant_id,
                        created_at_ms=now,
                        updated_at_ms=now,
                        metadata_json=metadata or {},
                    )
                    session.add(row)
                await session.commit()
                result = _tool_favorite_to_dict(row)
            except ToolFavoritesCapExceededError:
                await session.rollback()
                raise
            except Exception as exc:
                await session.rollback()
                raise RuntimeError(
                    f"add_tool_favorite({item_type!r}, {item_id!r}) failed: {exc}"
                ) from exc
        return result

    @log_call(logger=logger)
    async def list_tool_favorites(
        self, user_context: UserContext
    ) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            stmt = (
                select(ConsoleToolFavorite)
                # Isolation guard — the non-vacuity test neuters this
                # exact filter.
                .filter(ConsoleToolFavorite.user_sub == user_context.user_id)
                .filter(ConsoleToolFavorite.deleted_at_ms.is_(None))
                .order_by(
                    ConsoleToolFavorite.created_at_ms.asc(),
                    ConsoleToolFavorite.id.asc(),
                )
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_tool_favorite_to_dict(r) for r in rows]

    @log_call(logger=logger)
    async def remove_tool_favorite(
        self, user_context: UserContext, item_type: str, item_id: str
    ) -> bool:
        now = _now_ms()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsoleToolFavorite)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsoleToolFavorite.user_sub == user_context.user_id)
                    .filter(ConsoleToolFavorite.item_type == item_type)
                    .filter(ConsoleToolFavorite.item_id == item_id)
                    .filter(ConsoleToolFavorite.deleted_at_ms.is_(None))
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.deleted_at_ms = now
            row.updated_at_ms = now
            await session.commit()
        return True


@dataclass
class _MockToolFavorite:
    item_type: str
    item_id: str
    user_sub: str
    tenant_id: str | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0
    deleted_at_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_type": self.item_type,
            "item_id": self.item_id,
            "user_sub": self.user_sub,
            "tenant_id": self.tenant_id,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "deleted_at_ms": self.deleted_at_ms,
            "metadata": self.metadata,
        }


class MockConsoleToolFavoritesService(ConsoleToolFavoritesService):
    """In-process mock for unit tests that don't wire a Postgres factory
    (mirrors ``MockConsoleConversationTagsService``'s shape)."""

    def __init__(self) -> None:
        self._favorites: list[_MockToolFavorite] = []

    def reset(self) -> None:
        self._favorites.clear()

    def _find_active(
        self, user_sub: str, item_type: str, item_id: str
    ) -> _MockToolFavorite | None:
        for f in self._favorites:
            if (
                f.user_sub == user_sub
                and f.item_type == item_type
                and f.item_id == item_id
                and f.deleted_at_ms is None
            ):
                return f
        return None

    def _find_any(
        self, user_sub: str, item_type: str, item_id: str
    ) -> _MockToolFavorite | None:
        for f in self._favorites:
            if (
                f.user_sub == user_sub
                and f.item_type == item_type
                and f.item_id == item_id
            ):
                return f
        return None

    @log_call(logger=logger)
    async def add_tool_favorite(
        self,
        user_context: UserContext,
        item_type: str,
        item_id: str,
        *,
        tenant_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        # Isolation guard — non-vacuity test neuters this filter (via
        # _find_active/_find_any).
        active = self._find_active(user_context.user_id, item_type, item_id)
        if active is not None:
            if tenant_id is not None:
                active.tenant_id = tenant_id
            if metadata is not None:
                active.metadata = metadata
            active.updated_at_ms = now
            return active.to_dict()

        active_count = sum(
            1
            for f in self._favorites
            if f.user_sub == user_context.user_id and f.deleted_at_ms is None
        )
        if active_count >= MAX_TOOL_FAVORITES:
            raise ToolFavoritesCapExceededError(
                f"maximum of {MAX_TOOL_FAVORITES} tool favorites reached"
            )

        tombstoned = self._find_any(user_context.user_id, item_type, item_id)
        if tombstoned is not None:
            tombstoned.deleted_at_ms = None
            if tenant_id is not None:
                tombstoned.tenant_id = tenant_id
            if metadata is not None:
                tombstoned.metadata = metadata
            tombstoned.updated_at_ms = now
            return tombstoned.to_dict()

        row = _MockToolFavorite(
            item_type=item_type,
            item_id=item_id,
            user_sub=user_context.user_id,
            tenant_id=tenant_id,
            created_at_ms=now,
            updated_at_ms=now,
            metadata=metadata or {},
        )
        self._favorites.append(row)
        return row.to_dict()

    @log_call(logger=logger)
    async def list_tool_favorites(
        self, user_context: UserContext
    ) -> list[dict[str, Any]]:
        # Isolation guard — non-vacuity test neuters this filter.
        mine = [
            f
            for f in self._favorites
            if f.user_sub == user_context.user_id and f.deleted_at_ms is None
        ]
        mine.sort(key=lambda f: (f.created_at_ms, f.item_type, f.item_id))
        return [f.to_dict() for f in mine]

    @log_call(logger=logger)
    async def remove_tool_favorite(
        self, user_context: UserContext, item_type: str, item_id: str
    ) -> bool:
        row = self._find_active(user_context.user_id, item_type, item_id)
        if row is None:
            return False
        now = _now_ms()
        row.deleted_at_ms = now
        row.updated_at_ms = now
        return True
