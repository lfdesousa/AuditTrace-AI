"""Console-tool-favorites service — the Tool-Favorites domain of the
MongoDB-elimination EPIC, the FIRST domain migrated onto
``services/console_store`` (WU-A reference domain).

The AuditTrace-side, RLS-isolated store backing LibreChat's
``ToolFavorite`` record (the fork's Mongo ``ToolFavorite`` collection,
``packages/data-schemas/src/schema/favorite.ts``), per the ratified spec
(2026-09-13-SPEC-mongo-repl-wu-tool-favorites-store.md). The public
contract — :class:`ConsoleToolFavoritesService` and the two constructors
``PostgresConsoleToolFavoritesService(session_factory=...)`` /
``MockConsoleToolFavoritesService()`` — is unchanged; ``routes/
console_tool_favorites.py``, ``dependencies.py`` and the pre-existing test
suites are untouched, which IS the behaviour-equivalence proof.

**What this module declares now, and what it no longer implements.**
:class:`ToolFavoritesDomain` names the ORM model, the client key
``(item_type, item_id)``, the writable columns ``tenant_id`` /
``metadata_json``, the oldest-first order, the ``MAX_TOOL_FAVORITES`` cap
(as the ``cap()`` hook — the worked example of ADDENDUM A §2) and the
per-row defaults. Everything that was previously
hand-written per domain — the ``user_sub`` filter on every query AND on
the cap-COUNT aggregate, the D13 tombstone lookup + un-tombstone, the
``trace_id`` stamp, serialize-inside-the-session, refusing a body-supplied
``user_sub`` — is the BASE's, once. This module holds no session factory,
no ``select()``, no ``func.count()``: it cannot write an unscoped query
because it has nothing to write one with.

**Own-favorites-only v1.** Every row is owned by exactly one ``user_sub``.

**No cursor pagination on ``list_tool_favorites`` (spec-faithful).** The
fork's ``getToolFavorites`` returns the caller's ENTIRE list oldest-first,
bounded by the cap, so this service asks the base for one page of
``MAX_TOOL_FAVORITES`` rows. A next-cursor can only appear if more than
``MAX_TOOL_FAVORITES`` ACTIVE rows exist for one user (a cap-invariant
breach) — logged as a WARNING, never silently truncated.

**``MAX_TOOL_FAVORITES = 100`` per user — enforced server-side.** Adding a
101st ACTIVE favorite raises :class:`ToolFavoritesCapExceededError` (the
route maps it to 409). Re-affirming an active pair, or updating its
``tenant_id``/``metadata``, never counts against the cap — only a genuine
transition into "active" (brand-new row, or un-tombstoning) is capped.
The COUNT behind that decision is the base's user-scoped aggregate
(``lesson-aggregate-queries-must-be-user-scoped-20260913``).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.db.models import ConsoleToolFavorite
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.services.console_store import (
    ConsoleDomain,
    ConsoleStoreBase,
    ConsoleStoreCapExceededError,
    MockConsoleStore,
    PostgresConsoleStore,
)

logger = logging.getLogger(__name__)

# Mirrors the fork's methods/favorite.ts::MAX_TOOL_FAVORITES exactly —
# the business rule this domain enforces server-side (the fork's own
# enforcement is a Mongo countDocuments() read-then-write check; ours
# is the base's Postgres-backed equivalent, same "soft UX cap, may
# transiently overshoot by one under concurrency" acknowledgement).
MAX_TOOL_FAVORITES = 100

# The closed ``item_type`` vocabulary has ONE source of truth:
# ``audittrace.models._TOOL_FAVORITE_ITEM_TYPE`` (422 at the route
# boundary). No duplicate tuple here (2026-09-13 review F2).


class ToolFavoritesCapExceededError(ConsoleStoreCapExceededError):
    """Raised by :meth:`ConsoleToolFavoritesService.add_tool_favorite`
    when the caller already owns ``MAX_TOOL_FAVORITES`` ACTIVE favorites
    and the requested ``(item_type, item_id)`` pair is not already one of
    them. Route layer maps this to HTTP 409."""

    def __init__(self) -> None:
        super().__init__(
            "console_tool_favorites",
            MAX_TOOL_FAVORITES,
            f"maximum of {MAX_TOOL_FAVORITES} tool favorites reached",
        )


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

        MUST NEVER include another user's favorite — the base's
        ``user_sub`` predicate is the guard the two-sub tests neuter.
        """

    @abstractmethod
    async def remove_tool_favorite(
        self, user_context: UserContext, item_type: str, item_id: str
    ) -> bool:
        """Soft-delete the caller's OWN favorite. Returns ``True`` if a
        (previously non-deleted) favorite was found and removed,
        ``False`` otherwise (not found/not owned/already removed —
        idempotent no-op)."""


class ToolFavoritesDomain(ConsoleDomain[dict[str, Any]]):
    """What the Tool-Favorites domain declares — and nothing else."""

    name = "console_tool_favorites"
    model = ConsoleToolFavorite
    key_columns = ("item_type", "item_id")
    value_columns = ("tenant_id", "metadata_json")
    # Oldest-first (the fork's getToolFavorites sort), with the client
    # key as a deterministic tie-break inside one millisecond.
    order_by = (
        ("created_at_ms", "asc"),
        ("item_type", "asc"),
        ("item_id", "asc"),
    )
    default_list_limit = MAX_TOOL_FAVORITES
    max_list_limit = MAX_TOOL_FAVORITES

    def cap(self) -> int | None:
        return MAX_TOOL_FAVORITES

    def defaults(self, key: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"tenant_id": None, "metadata_json": {}}

    # No custom merge(): the base's default ("the patch wins for the columns
    # it names") is exactly "only the fields the caller supplied change",
    # because add_tool_favorite() puts a column into the patch ONLY when the
    # caller passed it — a re-add without tenant_id/metadata keeps both.

    def to_item(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "item_type": row["item_type"],
            "item_id": row["item_id"],
            "user_sub": row["user_sub"],
            "tenant_id": row["tenant_id"],
            "created_at_ms": row["created_at_ms"],
            "updated_at_ms": row["updated_at_ms"],
            "deleted_at_ms": row["deleted_at_ms"],
            "metadata": row["metadata_json"],
        }


class _StoreBackedToolFavoritesService(ConsoleToolFavoritesService):
    """The domain-specific verbs, expressed over the base's guarded API."""

    def __init__(self, store: ConsoleStoreBase[dict[str, Any]]) -> None:
        self._store = store

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
        key = {"item_type": item_type, "item_id": item_id}
        values: dict[str, Any] = {}
        if tenant_id is not None:
            values["tenant_id"] = tenant_id
        if metadata is not None:
            values["metadata_json"] = metadata
        try:
            return await self._store.upsert(user_context, key, values)
        except ConsoleStoreCapExceededError as exc:
            raise ToolFavoritesCapExceededError() from exc
        except RuntimeError as exc:
            raise RuntimeError(
                f"add_tool_favorite({item_type!r}, {item_id!r}) failed: {exc}"
            ) from exc

    @log_call(logger=logger)
    async def list_tool_favorites(
        self, user_context: UserContext
    ) -> list[dict[str, Any]]:
        items, next_cursor = await self._store.list(
            user_context, limit=MAX_TOOL_FAVORITES
        )
        if next_cursor is not None:
            logger.warning(
                "console_tool_favorites: more than %d active rows for one user "
                "(cap invariant breached); list truncated",
                MAX_TOOL_FAVORITES,
            )
        return items

    @log_call(logger=logger)
    async def remove_tool_favorite(
        self, user_context: UserContext, item_type: str, item_id: str
    ) -> bool:
        return await self._store.delete(
            user_context, {"item_type": item_type, "item_id": item_id}
        )


class PostgresConsoleToolFavoritesService(_StoreBackedToolFavoritesService):
    """PostgreSQL-backed console-tool-favorites service."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(PostgresConsoleStore(ToolFavoritesDomain(), session_factory))


class MockConsoleToolFavoritesService(_StoreBackedToolFavoritesService):
    """In-process mock for unit tests that don't wire a Postgres factory."""

    def __init__(self) -> None:
        self._mock_store: MockConsoleStore[dict[str, Any]] = MockConsoleStore(
            ToolFavoritesDomain()
        )
        super().__init__(self._mock_store)

    def reset(self) -> None:
        self._mock_store.reset()
