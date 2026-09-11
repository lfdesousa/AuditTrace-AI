"""Console-chat-projects service — the Chat-Projects domain of the
MongoDB-elimination EPIC.

The AuditTrace-side, RLS-isolated store backing LibreChat's first-class
chat-projects (the fork's Mongo ``ChatProject`` collection), per the
ratified spec
(2026-09-11-SPEC-mongo-repl-wu-chatprojects-store.md). Mirrors
``services/console_conversations.py``'s (and ``services/
console_presets.py``'s) shape and discipline EXACTLY (the spec's
instruction): an ABC + a Postgres-backed implementation with an
explicit ``user_sub`` filter on every query (the Phase-2 pattern) + a
Mock for fast unit tests.

**Model shape.** A chat-project is a plain name+description+metadata
record — unlike ``console_presets``' single loosely-typed ``data`` blob,
LibreChat's ``ChatProject`` schema has a small, stable field set
(name/description), so both are promoted to first-class columns (see
``db/models.py::ConsoleChatProject`` for the full design note, including
how ``chat_project_id`` relates to the nullable ``chat_project_id``
column WU-1 already reserved on ``console_conversations``).

**COMPLETENESS (the WU-2 lesson).** Every method below (other than the
abstract-interface constructor) takes a ``UserContext`` and filters
explicitly by ``user_context.user_id`` at the SERVICE layer — Postgres
RLS (migration 026) is a no-op on SQLite, so this belt-and-suspenders
duplication is required, not decorative
(feedback_unit_tests_miss_rls). ``user_sub`` is ALWAYS taken from the
``UserContext`` the route resolved from the token — a caller-supplied
``user_sub`` in a request body is never read by any method here
(feedback_never_trust_caller_metadata_for_security_fields); the route
layer enforces this by construction (it never passes a body field
named ``user_sub``/``user_id`` into these methods).
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.db.models import ConsoleChatProject
from audittrace.identity import UserContext
from audittrace.logging_config import log_call

logger = logging.getLogger(__name__)

# List pages default to 25 rows, capped at 200 — same rationale as
# console_conversations'/console_presets' DEFAULT_LIST_LIMIT/MAX_LIST_LIMIT.
DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 200


def _now_ms() -> int:
    return int(time.time() * 1000)


def _encode_cursor(*, updated_at_ms: int, chat_project_id: str) -> str:
    """Opaque pagination cursor: base64 of
    ``updated_at_ms:chat_project_id``.

    The tie-break on ``chat_project_id`` makes ordering deterministic
    when two chat-projects share the same millisecond ``updated_at_ms``
    (append-only writes issued back-to-back with no I/O between them
    can tie). Callers must treat the cursor as opaque — the encoding is
    an implementation detail, not an API contract.
    """
    raw = f"{updated_at_ms}:{chat_project_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[int, str]:
    """Inverse of :func:`_encode_cursor`. Raises ``ValueError`` for a
    malformed cursor — callers map this to a 400, never a silent
    "start from the beginning" (which would look like an empty result
    to a caller who supplied a garbled cursor)."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid cursor: {cursor!r}") from exc
    updated_at_ms_str, _, chat_project_id = raw.partition(":")
    if not chat_project_id:
        raise ValueError(f"invalid cursor: {cursor!r}")
    try:
        updated_at_ms = int(updated_at_ms_str)
    except ValueError as exc:
        raise ValueError(f"invalid cursor: {cursor!r}") from exc
    return updated_at_ms, chat_project_id


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIST_LIMIT))


class ConsoleChatProjectsService(ABC):
    """Abstract console-chat-projects store — the sovereign replacement
    for LibreChat's Mongo ``ChatProject`` collection."""

    @abstractmethod
    async def upsert_chat_project(
        self,
        user_context: UserContext,
        chat_project_id: str,
        *,
        name: str,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update the caller's chat-project identified by
        ``(user_sub, chat_project_id)``. Idempotent: calling twice with
        the same ``chat_project_id`` updates the existing row (bumping
        ``updated_at_ms``) rather than creating a duplicate — the
        unique constraint on ``(user_sub, chat_project_id)`` is what
        this method upserts against. Returns the persisted row as a
        plain dict."""

    @abstractmethod
    async def list_chat_projects(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return a page of the caller's OWN (non-deleted) chat-projects,
        newest-first by ``updated_at_ms``, plus the opaque cursor for
        the next page (``None`` when there is no further page).

        MUST NEVER include another user's chat-project — the isolation
        guard the non-vacuity test neuters: drop the explicit
        ``user_sub`` filter and another user's rows leak into the page.
        """

    @abstractmethod
    async def get_chat_project(
        self, user_context: UserContext, chat_project_id: str
    ) -> dict[str, Any] | None:
        """Return the caller's OWN chat-project, or ``None`` if it
        doesn't exist, is soft-deleted, or belongs to another user (the
        three cases are indistinguishable from the caller's point of
        view — 404, never a 403 that would leak existence)."""

    @abstractmethod
    async def delete_chat_project(
        self, user_context: UserContext, chat_project_id: str
    ) -> bool:
        """Soft-delete the caller's OWN chat-project. Returns ``True``
        if a (previously non-deleted) chat-project was found and
        deleted, ``False`` otherwise (not found/not owned/already
        deleted — idempotent no-op)."""


def _chat_project_to_dict(row: ConsoleChatProject) -> dict[str, Any]:
    return {
        "chat_project_id": row.chat_project_id,
        "user_sub": row.user_sub,
        "name": row.name,
        "description": row.description,
        "created_at_ms": row.created_at_ms,
        "updated_at_ms": row.updated_at_ms,
        "deleted_at_ms": row.deleted_at_ms,
        "metadata": row.metadata_json,
    }


class PostgresConsoleChatProjectsService(ConsoleChatProjectsService):
    """PostgreSQL-backed console-chat-projects service."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    @log_call(logger=logger)
    async def upsert_chat_project(
        self,
        user_context: UserContext,
        chat_project_id: str,
        *,
        name: str,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        async with self._session_factory() as session:
            try:
                existing = (
                    await session.execute(
                        select(ConsoleChatProject)
                        # The isolation guard this upsert's non-vacuity
                        # test neuters — drop this filter and a hostile
                        # caller could update ANOTHER user's row by
                        # guessing their chat_project_id.
                        .filter(ConsoleChatProject.user_sub == user_context.user_id)
                        .filter(ConsoleChatProject.chat_project_id == chat_project_id)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    row = ConsoleChatProject(
                        id=str(uuid.uuid4()),
                        chat_project_id=chat_project_id,
                        user_sub=user_context.user_id,
                        name=name,
                        description=description or "",
                        created_at_ms=now,
                        updated_at_ms=now,
                        metadata_json=metadata or {},
                    )
                    session.add(row)
                else:
                    row = existing
                    row.name = name
                    if description is not None:
                        row.description = description
                    if metadata is not None:
                        row.metadata_json = metadata
                    row.updated_at_ms = now
                await session.commit()
                result = _chat_project_to_dict(row)
            except Exception as exc:
                await session.rollback()
                raise RuntimeError(
                    f"upsert_chat_project({chat_project_id!r}) failed: {exc}"
                ) from exc
        return result

    @log_call(logger=logger)
    async def list_chat_projects(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        effective_limit = _clamp_limit(limit)
        async with self._session_factory() as session:
            stmt = (
                select(ConsoleChatProject)
                # Isolation guard — the non-vacuity test neuters this
                # exact filter.
                .filter(ConsoleChatProject.user_sub == user_context.user_id)
                .filter(ConsoleChatProject.deleted_at_ms.is_(None))
            )
            if cursor is not None:
                cursor_updated_at_ms, cursor_chat_project_id = _decode_cursor(cursor)
                stmt = stmt.filter(
                    (ConsoleChatProject.updated_at_ms < cursor_updated_at_ms)
                    | (
                        (ConsoleChatProject.updated_at_ms == cursor_updated_at_ms)
                        & (ConsoleChatProject.chat_project_id < cursor_chat_project_id)
                    )
                )
            stmt = stmt.order_by(
                ConsoleChatProject.updated_at_ms.desc(),
                ConsoleChatProject.chat_project_id.desc(),
            ).limit(effective_limit + 1)
            rows = (await session.execute(stmt)).scalars().all()
            plain = [_chat_project_to_dict(r) for r in rows]

        has_more = len(plain) > effective_limit
        page = plain[:effective_limit]
        next_cursor = (
            _encode_cursor(
                updated_at_ms=page[-1]["updated_at_ms"],
                chat_project_id=page[-1]["chat_project_id"],
            )
            if has_more and page
            else None
        )
        return page, next_cursor

    @log_call(logger=logger)
    async def get_chat_project(
        self, user_context: UserContext, chat_project_id: str
    ) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsoleChatProject)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsoleChatProject.user_sub == user_context.user_id)
                    .filter(ConsoleChatProject.chat_project_id == chat_project_id)
                    .filter(ConsoleChatProject.deleted_at_ms.is_(None))
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return _chat_project_to_dict(row)

    @log_call(logger=logger)
    async def delete_chat_project(
        self, user_context: UserContext, chat_project_id: str
    ) -> bool:
        now = _now_ms()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsoleChatProject)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsoleChatProject.user_sub == user_context.user_id)
                    .filter(ConsoleChatProject.chat_project_id == chat_project_id)
                    .filter(ConsoleChatProject.deleted_at_ms.is_(None))
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.deleted_at_ms = now
            row.updated_at_ms = now
            await session.commit()
        return True


@dataclass
class _MockChatProject:
    chat_project_id: str
    user_sub: str
    name: str
    description: str
    created_at_ms: int
    updated_at_ms: int
    deleted_at_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_project_id": self.chat_project_id,
            "user_sub": self.user_sub,
            "name": self.name,
            "description": self.description,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "deleted_at_ms": self.deleted_at_ms,
            "metadata": self.metadata,
        }


class MockConsoleChatProjectsService(ConsoleChatProjectsService):
    """In-process mock for unit tests that don't wire a Postgres factory
    (mirrors ``MockConsolePresetsService``'s shape)."""

    def __init__(self) -> None:
        self._chat_projects: list[_MockChatProject] = []

    def reset(self) -> None:
        self._chat_projects.clear()

    def _find(self, user_sub: str, chat_project_id: str) -> _MockChatProject | None:
        for p in self._chat_projects:
            if (
                p.user_sub == user_sub
                and p.chat_project_id == chat_project_id
                and p.deleted_at_ms is None
            ):
                return p
        return None

    @log_call(logger=logger)
    async def upsert_chat_project(
        self,
        user_context: UserContext,
        chat_project_id: str,
        *,
        name: str,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        existing = self._find(user_context.user_id, chat_project_id)
        if existing is None:
            row = _MockChatProject(
                chat_project_id=chat_project_id,
                user_sub=user_context.user_id,
                name=name,
                description=description or "",
                created_at_ms=now,
                updated_at_ms=now,
                metadata=metadata or {},
            )
            self._chat_projects.append(row)
        else:
            row = existing
            row.name = name
            if description is not None:
                row.description = description
            if metadata is not None:
                row.metadata = metadata
            row.updated_at_ms = now
        return row.to_dict()

    @log_call(logger=logger)
    async def list_chat_projects(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        effective_limit = _clamp_limit(limit)
        # Isolation guard — non-vacuity test neuters this filter.
        mine = [
            p
            for p in self._chat_projects
            if p.user_sub == user_context.user_id and p.deleted_at_ms is None
        ]
        mine.sort(key=lambda p: (p.updated_at_ms, p.chat_project_id), reverse=True)
        if cursor is not None:
            cursor_updated_at_ms, cursor_chat_project_id = _decode_cursor(cursor)
            mine = [
                p
                for p in mine
                if (p.updated_at_ms, p.chat_project_id)
                < (cursor_updated_at_ms, cursor_chat_project_id)
            ]
        page = mine[: effective_limit + 1]
        has_more = len(page) > effective_limit
        page = page[:effective_limit]
        next_cursor = (
            _encode_cursor(
                updated_at_ms=page[-1].updated_at_ms,
                chat_project_id=page[-1].chat_project_id,
            )
            if has_more and page
            else None
        )
        return [p.to_dict() for p in page], next_cursor

    @log_call(logger=logger)
    async def get_chat_project(
        self, user_context: UserContext, chat_project_id: str
    ) -> dict[str, Any] | None:
        row = self._find(user_context.user_id, chat_project_id)
        return row.to_dict() if row is not None else None

    @log_call(logger=logger)
    async def delete_chat_project(
        self, user_context: UserContext, chat_project_id: str
    ) -> bool:
        row = self._find(user_context.user_id, chat_project_id)
        if row is None:
            return False
        now = _now_ms()
        row.deleted_at_ms = now
        row.updated_at_ms = now
        return True
