"""Console-conversation-tags service — the Conversation-Tags domain of
the MongoDB-elimination EPIC.

The AuditTrace-side, RLS-isolated store backing LibreChat's
``conversationTag`` record (the fork's Mongo ``ConversationTag``
collection), per the ratified spec
(2026-09-13-SPEC-mongo-repl-wu-conversation-tags-store.md). Mirrors
``services/console_chat_projects.py``'s (and ``services/
console_agents.py``'s) shape and discipline EXACTLY (the spec's
instruction): an ABC + a Postgres-backed implementation with an
explicit ``user_sub`` filter on every query (the Phase-2 pattern) + a
Mock for fast unit tests.

**Own-tags-only v1.** Every row is owned by exactly one ``user_sub`` —
there is no shared/global tag concept, same discipline as every other
console-* domain's own-scoped v1.

**``count``/``position`` are caller-maintained.** This service persists
whatever the caller upserts for ``count`` (conversations carrying this
tag) and ``position`` (sort order); it never independently recomputes
``count`` by cross-referencing conversations.

**COMPLETENESS (the WU-2 lesson).** Every method below (other than the
abstract-interface constructor) takes a ``UserContext`` and filters
explicitly by ``user_context.user_id`` at the SERVICE layer — Postgres
RLS (migration 029) is a no-op on SQLite, so this belt-and-suspenders
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

from audittrace.db.models import ConsoleConversationTag
from audittrace.identity import UserContext
from audittrace.logging_config import log_call

logger = logging.getLogger(__name__)

# List pages default to 25 rows, capped at 200 — same rationale as
# console_chat_projects'/console_agents' DEFAULT_LIST_LIMIT/MAX_LIST_LIMIT.
DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 200


def _now_ms() -> int:
    return int(time.time() * 1000)


def _encode_cursor(*, updated_at_ms: int, tag: str) -> str:
    """Opaque pagination cursor: base64 of ``updated_at_ms:tag``.

    The tie-break on ``tag`` makes ordering deterministic when two
    conversation-tags share the same millisecond ``updated_at_ms``
    (append-only writes issued back-to-back with no I/O between them
    can tie). Callers must treat the cursor as opaque — the encoding is
    an implementation detail, not an API contract.
    """
    raw = f"{updated_at_ms}:{tag}"
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
    updated_at_ms_str, _, tag = raw.partition(":")
    if not tag:
        raise ValueError(f"invalid cursor: {cursor!r}")
    try:
        updated_at_ms = int(updated_at_ms_str)
    except ValueError as exc:
        raise ValueError(f"invalid cursor: {cursor!r}") from exc
    return updated_at_ms, tag


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIST_LIMIT))


class ConsoleConversationTagsService(ABC):
    """Abstract console-conversation-tags store — the sovereign
    replacement for LibreChat's Mongo ``ConversationTag`` collection."""

    @abstractmethod
    async def upsert_conversation_tag(
        self,
        user_context: UserContext,
        tag: str,
        *,
        description: str | None = None,
        count: int = 0,
        position: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update the caller's conversation-tag identified by
        ``(user_sub, tag)``. Idempotent: calling twice with the same
        ``tag`` updates the existing row (bumping ``updated_at_ms``)
        rather than creating a duplicate — the unique constraint on
        ``(user_sub, tag)`` is what this method upserts against.
        Returns the persisted row as a plain dict."""

    @abstractmethod
    async def list_conversation_tags(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return a page of the caller's OWN (non-deleted)
        conversation-tags, newest-first by ``updated_at_ms``, plus the
        opaque cursor for the next page (``None`` when there is no
        further page).

        MUST NEVER include another user's conversation-tag — the
        isolation guard the non-vacuity test neuters: drop the explicit
        ``user_sub`` filter and another user's rows leak into the page.
        """

    @abstractmethod
    async def get_conversation_tag(
        self, user_context: UserContext, tag: str
    ) -> dict[str, Any] | None:
        """Return the caller's OWN conversation-tag, or ``None`` if it
        doesn't exist, is soft-deleted, or belongs to another user (the
        three cases are indistinguishable from the caller's point of
        view — 404, never a 403 that would leak existence)."""

    @abstractmethod
    async def delete_conversation_tag(
        self, user_context: UserContext, tag: str
    ) -> bool:
        """Soft-delete the caller's OWN conversation-tag. Returns
        ``True`` if a (previously non-deleted) tag was found and
        deleted, ``False`` otherwise (not found/not owned/already
        deleted — idempotent no-op)."""


def _conversation_tag_to_dict(row: ConsoleConversationTag) -> dict[str, Any]:
    return {
        "tag": row.tag,
        "user_sub": row.user_sub,
        "description": row.description,
        "count": row.count,
        "position": row.position,
        "created_at_ms": row.created_at_ms,
        "updated_at_ms": row.updated_at_ms,
        "deleted_at_ms": row.deleted_at_ms,
        "metadata": row.metadata_json,
    }


class PostgresConsoleConversationTagsService(ConsoleConversationTagsService):
    """PostgreSQL-backed console-conversation-tags service."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    @log_call(logger=logger)
    async def upsert_conversation_tag(
        self,
        user_context: UserContext,
        tag: str,
        *,
        description: str | None = None,
        count: int = 0,
        position: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        async with self._session_factory() as session:
            try:
                existing = (
                    await session.execute(
                        select(ConsoleConversationTag)
                        # The isolation guard this upsert's non-vacuity
                        # test neuters — drop this filter and a hostile
                        # caller could update ANOTHER user's row by
                        # guessing their tag.
                        .filter(ConsoleConversationTag.user_sub == user_context.user_id)
                        .filter(ConsoleConversationTag.tag == tag)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    row = ConsoleConversationTag(
                        id=str(uuid.uuid4()),
                        tag=tag,
                        user_sub=user_context.user_id,
                        description=description or "",
                        count=count,
                        position=position,
                        created_at_ms=now,
                        updated_at_ms=now,
                        metadata_json=metadata or {},
                    )
                    session.add(row)
                else:
                    row = existing
                    if description is not None:
                        row.description = description
                    row.count = count
                    row.position = position
                    if metadata is not None:
                        row.metadata_json = metadata
                    row.updated_at_ms = now
                await session.commit()
                result = _conversation_tag_to_dict(row)
            except Exception as exc:
                await session.rollback()
                raise RuntimeError(
                    f"upsert_conversation_tag({tag!r}) failed: {exc}"
                ) from exc
        return result

    @log_call(logger=logger)
    async def list_conversation_tags(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        effective_limit = _clamp_limit(limit)
        async with self._session_factory() as session:
            stmt = (
                select(ConsoleConversationTag)
                # Isolation guard — the non-vacuity test neuters this
                # exact filter.
                .filter(ConsoleConversationTag.user_sub == user_context.user_id)
                .filter(ConsoleConversationTag.deleted_at_ms.is_(None))
            )
            if cursor is not None:
                cursor_updated_at_ms, cursor_tag = _decode_cursor(cursor)
                stmt = stmt.filter(
                    (ConsoleConversationTag.updated_at_ms < cursor_updated_at_ms)
                    | (
                        (ConsoleConversationTag.updated_at_ms == cursor_updated_at_ms)
                        & (ConsoleConversationTag.tag < cursor_tag)
                    )
                )
            stmt = stmt.order_by(
                ConsoleConversationTag.updated_at_ms.desc(),
                ConsoleConversationTag.tag.desc(),
            ).limit(effective_limit + 1)
            rows = (await session.execute(stmt)).scalars().all()
            plain = [_conversation_tag_to_dict(r) for r in rows]

        has_more = len(plain) > effective_limit
        page = plain[:effective_limit]
        next_cursor = (
            _encode_cursor(
                updated_at_ms=page[-1]["updated_at_ms"],
                tag=page[-1]["tag"],
            )
            if has_more and page
            else None
        )
        return page, next_cursor

    @log_call(logger=logger)
    async def get_conversation_tag(
        self, user_context: UserContext, tag: str
    ) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsoleConversationTag)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsoleConversationTag.user_sub == user_context.user_id)
                    .filter(ConsoleConversationTag.tag == tag)
                    .filter(ConsoleConversationTag.deleted_at_ms.is_(None))
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return _conversation_tag_to_dict(row)

    @log_call(logger=logger)
    async def delete_conversation_tag(
        self, user_context: UserContext, tag: str
    ) -> bool:
        now = _now_ms()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsoleConversationTag)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsoleConversationTag.user_sub == user_context.user_id)
                    .filter(ConsoleConversationTag.tag == tag)
                    .filter(ConsoleConversationTag.deleted_at_ms.is_(None))
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.deleted_at_ms = now
            row.updated_at_ms = now
            await session.commit()
        return True


@dataclass
class _MockConversationTag:
    tag: str
    user_sub: str
    description: str = ""
    count: int = 0
    position: int = 0
    created_at_ms: int = 0
    updated_at_ms: int = 0
    deleted_at_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "user_sub": self.user_sub,
            "description": self.description,
            "count": self.count,
            "position": self.position,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "deleted_at_ms": self.deleted_at_ms,
            "metadata": self.metadata,
        }


class MockConsoleConversationTagsService(ConsoleConversationTagsService):
    """In-process mock for unit tests that don't wire a Postgres factory
    (mirrors ``MockConsoleChatProjectsService``'s shape)."""

    def __init__(self) -> None:
        self._tags: list[_MockConversationTag] = []

    def reset(self) -> None:
        self._tags.clear()

    def _find(self, user_sub: str, tag: str) -> _MockConversationTag | None:
        for t in self._tags:
            if t.user_sub == user_sub and t.tag == tag and t.deleted_at_ms is None:
                return t
        return None

    @log_call(logger=logger)
    async def upsert_conversation_tag(
        self,
        user_context: UserContext,
        tag: str,
        *,
        description: str | None = None,
        count: int = 0,
        position: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        existing = self._find(user_context.user_id, tag)
        if existing is None:
            row = _MockConversationTag(
                tag=tag,
                user_sub=user_context.user_id,
                description=description or "",
                count=count,
                position=position,
                created_at_ms=now,
                updated_at_ms=now,
                metadata=metadata or {},
            )
            self._tags.append(row)
        else:
            row = existing
            if description is not None:
                row.description = description
            row.count = count
            row.position = position
            if metadata is not None:
                row.metadata = metadata
            row.updated_at_ms = now
        return row.to_dict()

    @log_call(logger=logger)
    async def list_conversation_tags(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        effective_limit = _clamp_limit(limit)
        # Isolation guard — non-vacuity test neuters this filter.
        mine = [
            t
            for t in self._tags
            if t.user_sub == user_context.user_id and t.deleted_at_ms is None
        ]
        mine.sort(key=lambda t: (t.updated_at_ms, t.tag), reverse=True)
        if cursor is not None:
            cursor_updated_at_ms, cursor_tag = _decode_cursor(cursor)
            mine = [
                t
                for t in mine
                if (t.updated_at_ms, t.tag) < (cursor_updated_at_ms, cursor_tag)
            ]
        page = mine[: effective_limit + 1]
        has_more = len(page) > effective_limit
        page = page[:effective_limit]
        next_cursor = (
            _encode_cursor(
                updated_at_ms=page[-1].updated_at_ms,
                tag=page[-1].tag,
            )
            if has_more and page
            else None
        )
        return [t.to_dict() for t in page], next_cursor

    @log_call(logger=logger)
    async def get_conversation_tag(
        self, user_context: UserContext, tag: str
    ) -> dict[str, Any] | None:
        row = self._find(user_context.user_id, tag)
        return row.to_dict() if row is not None else None

    @log_call(logger=logger)
    async def delete_conversation_tag(
        self, user_context: UserContext, tag: str
    ) -> bool:
        row = self._find(user_context.user_id, tag)
        if row is None:
            return False
        now = _now_ms()
        row.deleted_at_ms = now
        row.updated_at_ms = now
        return True
