"""Console-conversations service — WU-1 of the MongoDB-elimination EPIC.

The AuditTrace-side, RLS-isolated store backing LibreChat's conversations
+ messages (the fork's Mongo ``Conversation``/``Message`` collections),
per the ratified spec
(2026-09-11-SPEC-mongo-repl-wu1-console-conversations-store.md). Mirrors
``services/session_memory.py``'s shape and discipline: an ABC + a
Postgres-backed implementation with an explicit ``user_sub`` filter on
every query (the Phase-2 pattern) + a Mock for fast unit tests.

Every method (other than the abstract-interface constructor) takes a
``UserContext`` and filters explicitly by ``user_context.user_id`` at the
SERVICE layer — Postgres RLS (migration 023) is a no-op on SQLite, so
this belt-and-suspenders duplication is required, not decorative
(feedback_unit_tests_miss_rls). ``user_sub`` is ALWAYS taken from the
``UserContext`` the route resolved from the token — a caller-supplied
``user_sub`` in a request body is never read by any method here
(feedback_never_trust_caller_metadata_for_security_fields); the route
layer enforces this by construction (it never passes a body field named
``user_sub``/``user_id`` into these methods).
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

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.db.models import ConsoleConversation, ConsoleMessage
from audittrace.identity import UserContext
from audittrace.logging_config import log_call

logger = logging.getLogger(__name__)

# List pages default to 25 rows, capped at 200 — generous enough for a
# sidebar's "load more" pattern without letting a single request drag
# back an unbounded conversation history.
DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 200


def _now_ms() -> int:
    return int(time.time() * 1000)


def _encode_cursor(*, updated_at_ms: int, conversation_id: str) -> str:
    """Opaque pagination cursor: base64 of ``updated_at_ms:conversation_id``.

    The tie-break on ``conversation_id`` makes ordering deterministic
    when two conversations share the same millisecond ``updated_at_ms``
    (append-only writes issued back-to-back with no I/O between them can
    tie). Callers must treat the cursor as opaque — the encoding is an
    implementation detail, not an API contract.
    """
    raw = f"{updated_at_ms}:{conversation_id}"
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
    updated_at_ms_str, _, conversation_id = raw.partition(":")
    if not conversation_id:
        raise ValueError(f"invalid cursor: {cursor!r}")
    try:
        updated_at_ms = int(updated_at_ms_str)
    except ValueError as exc:
        raise ValueError(f"invalid cursor: {cursor!r}") from exc
    return updated_at_ms, conversation_id


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIST_LIMIT))


class ConsoleConversationsService(ABC):
    """Abstract console-conversations store — the message-tree-shaped
    replacement for LibreChat's Mongo convo/message collections."""

    @abstractmethod
    async def upsert_conversation(
        self,
        user_context: UserContext,
        conversation_id: str,
        *,
        title: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
        is_temporary: bool = False,
        agent_id: str | None = None,
        chat_project_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update the caller's conversation identified by
        ``(user_sub, conversation_id)``. Idempotent: calling twice with
        the same ``conversation_id`` updates the existing row (bumping
        ``updated_at_ms``) rather than creating a duplicate — the unique
        constraint on ``(user_sub, conversation_id)`` is what this method
        upserts against. Returns the persisted row as a plain dict."""

    @abstractmethod
    async def list_conversations(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return a page of the caller's OWN (non-deleted) conversations,
        newest-first by ``updated_at_ms``, plus the opaque cursor for the
        next page (``None`` when there is no further page).

        MUST NEVER include another user's conversation — the isolation
        guard the non-vacuity test neuters: drop the explicit
        ``user_sub`` filter and another user's rows leak into the page.
        """

    @abstractmethod
    async def get_conversation(
        self, user_context: UserContext, conversation_id: str
    ) -> dict[str, Any] | None:
        """Return the caller's OWN conversation, or ``None`` if it
        doesn't exist, is soft-deleted, or belongs to another user (the
        three cases are indistinguishable from the caller's point of
        view — 404, never a 403 that would leak existence)."""

    @abstractmethod
    async def update_conversation_title(
        self, user_context: UserContext, conversation_id: str, title: str
    ) -> dict[str, Any] | None:
        """Update the caller's OWN conversation's title. Returns the
        updated row, or ``None`` if not found/not owned/deleted."""

    @abstractmethod
    async def delete_conversation(
        self, user_context: UserContext, conversation_id: str
    ) -> bool:
        """Soft-delete the caller's OWN conversation AND every one of its
        messages. Returns ``True`` if a (previously non-deleted)
        conversation was found and deleted, ``False`` otherwise (not
        found/not owned/already deleted — idempotent no-op)."""

    @abstractmethod
    async def get_messages(
        self, user_context: UserContext, conversation_id: str
    ) -> list[dict[str, Any]]:
        """Return the full message tree for the caller's OWN
        conversation, ordered by ``created_at_ms`` ASC (chronological —
        the shape a client reconstructs the ``parent_message_id`` tree
        from). Empty list if the conversation doesn't exist, is deleted,
        or belongs to another user, or simply has no messages yet —
        callers distinguish "conversation exists" via
        :meth:`get_conversation` first.

        MUST NEVER include another user's messages — the isolation guard
        the non-vacuity test neuters: drop the explicit ``user_sub``
        filter and another user's messages leak into the tree.
        """

    @abstractmethod
    async def upsert_message(
        self,
        user_context: UserContext,
        conversation_id: str,
        message_id: str,
        *,
        sender: str,
        text: str,
        is_created_by_user: bool,
        parent_message_id: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
        token_count: int | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update the caller's message identified by
        ``(user_sub, message_id)``. Idempotent, same shape as
        :meth:`upsert_conversation`. Also bumps the parent conversation's
        ``updated_at_ms`` (so the sidebar's newest-first ordering reflects
        the latest activity) when the conversation exists and is owned by
        the caller; silently a no-op on that bump otherwise (the message
        write itself still succeeds — message upsert does not require the
        conversation to have been created first, mirroring the fork's own
        upsert-on-first-message pattern)."""

    @abstractmethod
    async def edit_message(
        self,
        user_context: UserContext,
        conversation_id: str,
        message_id: str,
        *,
        text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Update the caller's OWN message's text/metadata. Returns the
        updated row, or ``None`` if not found/not owned."""

    @abstractmethod
    async def delete_message(
        self, user_context: UserContext, conversation_id: str, message_id: str
    ) -> bool:
        """Hard-delete the caller's OWN message. Returns ``True`` if a
        message was found and deleted, ``False`` otherwise (not found/not
        owned — idempotent no-op)."""


def _conversation_to_dict(row: ConsoleConversation) -> dict[str, Any]:
    return {
        "conversation_id": row.conversation_id,
        "user_sub": row.user_sub,
        "title": row.title,
        "endpoint": row.endpoint,
        "model": row.model,
        "is_temporary": row.is_temporary,
        "agent_id": row.agent_id,
        "chat_project_id": row.chat_project_id,
        "created_at_ms": row.created_at_ms,
        "updated_at_ms": row.updated_at_ms,
        "deleted_at_ms": row.deleted_at_ms,
        "metadata": row.metadata_json,
    }


def _message_to_dict(row: ConsoleMessage) -> dict[str, Any]:
    return {
        "message_id": row.message_id,
        "conversation_id": row.conversation_id,
        "user_sub": row.user_sub,
        "parent_message_id": row.parent_message_id,
        "sender": row.sender,
        "text": row.text,
        "is_created_by_user": row.is_created_by_user,
        "model": row.model,
        "endpoint": row.endpoint,
        "token_count": row.token_count,
        "error": row.error,
        "created_at_ms": row.created_at_ms,
        "metadata": row.metadata_json,
    }


class PostgresConsoleConversationsService(ConsoleConversationsService):
    """PostgreSQL-backed console-conversations service."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    @log_call(logger=logger)
    async def upsert_conversation(
        self,
        user_context: UserContext,
        conversation_id: str,
        *,
        title: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
        is_temporary: bool = False,
        agent_id: str | None = None,
        chat_project_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        async with self._session_factory() as session:
            try:
                existing = (
                    await session.execute(
                        select(ConsoleConversation)
                        # The isolation guard this upsert's non-vacuity
                        # test neuters — drop this filter and a hostile
                        # caller could update ANOTHER user's row by
                        # guessing their conversation_id.
                        .filter(ConsoleConversation.user_sub == user_context.user_id)
                        .filter(ConsoleConversation.conversation_id == conversation_id)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    row = ConsoleConversation(
                        id=str(uuid.uuid4()),
                        conversation_id=conversation_id,
                        user_sub=user_context.user_id,
                        title=title or "New Chat",
                        endpoint=endpoint,
                        model=model,
                        is_temporary=is_temporary,
                        agent_id=agent_id,
                        chat_project_id=chat_project_id,
                        created_at_ms=now,
                        updated_at_ms=now,
                        metadata_json=metadata or {},
                    )
                    session.add(row)
                else:
                    row = existing
                    if title is not None:
                        row.title = title
                    if endpoint is not None:
                        row.endpoint = endpoint
                    if model is not None:
                        row.model = model
                    row.is_temporary = is_temporary
                    if agent_id is not None:
                        row.agent_id = agent_id
                    if chat_project_id is not None:
                        row.chat_project_id = chat_project_id
                    if metadata is not None:
                        row.metadata_json = metadata
                    row.updated_at_ms = now
                await session.commit()
                result = _conversation_to_dict(row)
            except Exception as exc:
                await session.rollback()
                raise RuntimeError(
                    f"upsert_conversation({conversation_id!r}) failed: {exc}"
                ) from exc
        return result

    @log_call(logger=logger)
    async def list_conversations(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        effective_limit = _clamp_limit(limit)
        async with self._session_factory() as session:
            stmt = (
                select(ConsoleConversation)
                # Isolation guard — the non-vacuity test neuters this
                # exact filter.
                .filter(ConsoleConversation.user_sub == user_context.user_id)
                .filter(ConsoleConversation.deleted_at_ms.is_(None))
            )
            if cursor is not None:
                cursor_updated_at_ms, cursor_conversation_id = _decode_cursor(cursor)
                stmt = stmt.filter(
                    (ConsoleConversation.updated_at_ms < cursor_updated_at_ms)
                    | (
                        (ConsoleConversation.updated_at_ms == cursor_updated_at_ms)
                        & (ConsoleConversation.conversation_id < cursor_conversation_id)
                    )
                )
            stmt = stmt.order_by(
                ConsoleConversation.updated_at_ms.desc(),
                ConsoleConversation.conversation_id.desc(),
            ).limit(effective_limit + 1)
            rows = (await session.execute(stmt)).scalars().all()
            plain = [_conversation_to_dict(r) for r in rows]

        has_more = len(plain) > effective_limit
        page = plain[:effective_limit]
        next_cursor = (
            _encode_cursor(
                updated_at_ms=page[-1]["updated_at_ms"],
                conversation_id=page[-1]["conversation_id"],
            )
            if has_more and page
            else None
        )
        return page, next_cursor

    @log_call(logger=logger)
    async def get_conversation(
        self, user_context: UserContext, conversation_id: str
    ) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsoleConversation)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsoleConversation.user_sub == user_context.user_id)
                    .filter(ConsoleConversation.conversation_id == conversation_id)
                    .filter(ConsoleConversation.deleted_at_ms.is_(None))
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return _conversation_to_dict(row)

    @log_call(logger=logger)
    async def update_conversation_title(
        self, user_context: UserContext, conversation_id: str, title: str
    ) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsoleConversation)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsoleConversation.user_sub == user_context.user_id)
                    .filter(ConsoleConversation.conversation_id == conversation_id)
                    .filter(ConsoleConversation.deleted_at_ms.is_(None))
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.title = title
            row.updated_at_ms = _now_ms()
            await session.commit()
            return _conversation_to_dict(row)

    @log_call(logger=logger)
    async def delete_conversation(
        self, user_context: UserContext, conversation_id: str
    ) -> bool:
        now = _now_ms()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsoleConversation)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsoleConversation.user_sub == user_context.user_id)
                    .filter(ConsoleConversation.conversation_id == conversation_id)
                    .filter(ConsoleConversation.deleted_at_ms.is_(None))
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.deleted_at_ms = now
            row.updated_at_ms = now
            # ConsoleMessage carries no soft-delete column (spec: only
            # the conversation soft-deletes) — deleting a conversation
            # HARD-deletes its messages, since they are meaningless
            # without their parent and the conversation delete already
            # covers the audit-visible soft-delete semantics.
            await session.execute(
                delete(ConsoleMessage)
                .where(ConsoleMessage.user_sub == user_context.user_id)
                .where(ConsoleMessage.conversation_id == conversation_id)
            )
            await session.commit()
        return True

    @log_call(logger=logger)
    async def get_messages(
        self, user_context: UserContext, conversation_id: str
    ) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ConsoleMessage)
                        # Isolation guard — non-vacuity test neuters this.
                        .filter(ConsoleMessage.user_sub == user_context.user_id)
                        .filter(ConsoleMessage.conversation_id == conversation_id)
                        .order_by(ConsoleMessage.created_at_ms.asc())
                    )
                )
                .scalars()
                .all()
            )
            return [_message_to_dict(r) for r in rows]

    @log_call(logger=logger)
    async def upsert_message(
        self,
        user_context: UserContext,
        conversation_id: str,
        message_id: str,
        *,
        sender: str,
        text: str,
        is_created_by_user: bool,
        parent_message_id: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
        token_count: int | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        async with self._session_factory() as session:
            try:
                existing = (
                    await session.execute(
                        select(ConsoleMessage)
                        # Isolation guard — non-vacuity test neuters this
                        # exact filter (same rationale as
                        # upsert_conversation above).
                        .filter(ConsoleMessage.user_sub == user_context.user_id)
                        .filter(ConsoleMessage.message_id == message_id)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    row = ConsoleMessage(
                        id=str(uuid.uuid4()),
                        message_id=message_id,
                        conversation_id=conversation_id,
                        user_sub=user_context.user_id,
                        parent_message_id=parent_message_id,
                        sender=sender,
                        text=text,
                        is_created_by_user=is_created_by_user,
                        model=model,
                        endpoint=endpoint,
                        token_count=token_count,
                        error=error,
                        created_at_ms=now,
                        metadata_json=metadata or {},
                    )
                    session.add(row)
                else:
                    row = existing
                    row.text = text
                    row.sender = sender
                    row.is_created_by_user = is_created_by_user
                    if parent_message_id is not None:
                        row.parent_message_id = parent_message_id
                    if model is not None:
                        row.model = model
                    if endpoint is not None:
                        row.endpoint = endpoint
                    if token_count is not None:
                        row.token_count = token_count
                    if error is not None:
                        row.error = error
                    if metadata is not None:
                        row.metadata_json = metadata

                # Bump the parent conversation's updated_at_ms so the
                # sidebar's newest-first ordering reflects this message —
                # silently a no-op if the caller never created/owns the
                # conversation (message upsert does not require the
                # conversation to exist first).
                convo = (
                    await session.execute(
                        select(ConsoleConversation)
                        .filter(ConsoleConversation.user_sub == user_context.user_id)
                        .filter(ConsoleConversation.conversation_id == conversation_id)
                    )
                ).scalar_one_or_none()
                if convo is not None:
                    convo.updated_at_ms = now

                await session.commit()
                result = _message_to_dict(row)
            except Exception as exc:
                await session.rollback()
                raise RuntimeError(
                    f"upsert_message({message_id!r}) failed: {exc}"
                ) from exc
        return result

    @log_call(logger=logger)
    async def edit_message(
        self,
        user_context: UserContext,
        conversation_id: str,
        message_id: str,
        *,
        text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsoleMessage)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsoleMessage.user_sub == user_context.user_id)
                    .filter(ConsoleMessage.conversation_id == conversation_id)
                    .filter(ConsoleMessage.message_id == message_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if text is not None:
                row.text = text
            if metadata is not None:
                row.metadata_json = metadata
            await session.commit()
            return _message_to_dict(row)

    @log_call(logger=logger)
    async def delete_message(
        self, user_context: UserContext, conversation_id: str, message_id: str
    ) -> bool:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsoleMessage)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsoleMessage.user_sub == user_context.user_id)
                    .filter(ConsoleMessage.conversation_id == conversation_id)
                    .filter(ConsoleMessage.message_id == message_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            await session.execute(
                delete(ConsoleMessage).where(ConsoleMessage.id == row.id)
            )
            await session.commit()
        return True


@dataclass
class _MockConversation:
    conversation_id: str
    user_sub: str
    title: str
    endpoint: str | None
    model: str | None
    is_temporary: bool
    agent_id: str | None
    chat_project_id: str | None
    created_at_ms: int
    updated_at_ms: int
    deleted_at_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "user_sub": self.user_sub,
            "title": self.title,
            "endpoint": self.endpoint,
            "model": self.model,
            "is_temporary": self.is_temporary,
            "agent_id": self.agent_id,
            "chat_project_id": self.chat_project_id,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "deleted_at_ms": self.deleted_at_ms,
            "metadata": self.metadata,
        }


@dataclass
class _MockMessage:
    message_id: str
    conversation_id: str
    user_sub: str
    sender: str
    text: str
    is_created_by_user: bool
    parent_message_id: str | None = None
    model: str | None = None
    endpoint: str | None = None
    token_count: int | None = None
    error: str | None = None
    created_at_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "user_sub": self.user_sub,
            "parent_message_id": self.parent_message_id,
            "sender": self.sender,
            "text": self.text,
            "is_created_by_user": self.is_created_by_user,
            "model": self.model,
            "endpoint": self.endpoint,
            "token_count": self.token_count,
            "error": self.error,
            "created_at_ms": self.created_at_ms,
            "metadata": self.metadata,
        }


class MockConsoleConversationsService(ConsoleConversationsService):
    """In-process mock for unit tests that don't wire a Postgres factory
    (mirrors ``MockSessionMemoryService``'s shape)."""

    def __init__(self) -> None:
        self._conversations: list[_MockConversation] = []
        self._messages: list[_MockMessage] = []

    def reset(self) -> None:
        self._conversations.clear()
        self._messages.clear()

    def _find_conversation(
        self, user_sub: str, conversation_id: str
    ) -> _MockConversation | None:
        for c in self._conversations:
            if (
                c.user_sub == user_sub
                and c.conversation_id == conversation_id
                and c.deleted_at_ms is None
            ):
                return c
        return None

    @log_call(logger=logger)
    async def upsert_conversation(
        self,
        user_context: UserContext,
        conversation_id: str,
        *,
        title: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
        is_temporary: bool = False,
        agent_id: str | None = None,
        chat_project_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        existing = self._find_conversation(user_context.user_id, conversation_id)
        if existing is None:
            row = _MockConversation(
                conversation_id=conversation_id,
                user_sub=user_context.user_id,
                title=title or "New Chat",
                endpoint=endpoint,
                model=model,
                is_temporary=is_temporary,
                agent_id=agent_id,
                chat_project_id=chat_project_id,
                created_at_ms=now,
                updated_at_ms=now,
                metadata=metadata or {},
            )
            self._conversations.append(row)
        else:
            row = existing
            if title is not None:
                row.title = title
            if endpoint is not None:
                row.endpoint = endpoint
            if model is not None:
                row.model = model
            row.is_temporary = is_temporary
            if agent_id is not None:
                row.agent_id = agent_id
            if chat_project_id is not None:
                row.chat_project_id = chat_project_id
            if metadata is not None:
                row.metadata = metadata
            row.updated_at_ms = now
        return row.to_dict()

    @log_call(logger=logger)
    async def list_conversations(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        effective_limit = _clamp_limit(limit)
        # Isolation guard — non-vacuity test neuters this filter.
        mine = [
            c
            for c in self._conversations
            if c.user_sub == user_context.user_id and c.deleted_at_ms is None
        ]
        mine.sort(key=lambda c: (c.updated_at_ms, c.conversation_id), reverse=True)
        if cursor is not None:
            cursor_updated_at_ms, cursor_conversation_id = _decode_cursor(cursor)
            mine = [
                c
                for c in mine
                if (c.updated_at_ms, c.conversation_id)
                < (cursor_updated_at_ms, cursor_conversation_id)
            ]
        page = mine[: effective_limit + 1]
        has_more = len(page) > effective_limit
        page = page[:effective_limit]
        next_cursor = (
            _encode_cursor(
                updated_at_ms=page[-1].updated_at_ms,
                conversation_id=page[-1].conversation_id,
            )
            if has_more and page
            else None
        )
        return [c.to_dict() for c in page], next_cursor

    @log_call(logger=logger)
    async def get_conversation(
        self, user_context: UserContext, conversation_id: str
    ) -> dict[str, Any] | None:
        row = self._find_conversation(user_context.user_id, conversation_id)
        return row.to_dict() if row is not None else None

    @log_call(logger=logger)
    async def update_conversation_title(
        self, user_context: UserContext, conversation_id: str, title: str
    ) -> dict[str, Any] | None:
        row = self._find_conversation(user_context.user_id, conversation_id)
        if row is None:
            return None
        row.title = title
        row.updated_at_ms = _now_ms()
        return row.to_dict()

    @log_call(logger=logger)
    async def delete_conversation(
        self, user_context: UserContext, conversation_id: str
    ) -> bool:
        row = self._find_conversation(user_context.user_id, conversation_id)
        if row is None:
            return False
        now = _now_ms()
        row.deleted_at_ms = now
        row.updated_at_ms = now
        self._messages = [
            m
            for m in self._messages
            if not (
                m.user_sub == user_context.user_id
                and m.conversation_id == conversation_id
            )
        ]
        return True

    @log_call(logger=logger)
    async def get_messages(
        self, user_context: UserContext, conversation_id: str
    ) -> list[dict[str, Any]]:
        # Isolation guard — non-vacuity test neuters this filter.
        mine = [
            m
            for m in self._messages
            if m.user_sub == user_context.user_id
            and m.conversation_id == conversation_id
        ]
        mine.sort(key=lambda m: m.created_at_ms)
        return [m.to_dict() for m in mine]

    @log_call(logger=logger)
    async def upsert_message(
        self,
        user_context: UserContext,
        conversation_id: str,
        message_id: str,
        *,
        sender: str,
        text: str,
        is_created_by_user: bool,
        parent_message_id: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
        token_count: int | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        existing = next(
            (
                m
                for m in self._messages
                if m.user_sub == user_context.user_id and m.message_id == message_id
            ),
            None,
        )
        if existing is None:
            row = _MockMessage(
                message_id=message_id,
                conversation_id=conversation_id,
                user_sub=user_context.user_id,
                sender=sender,
                text=text,
                is_created_by_user=is_created_by_user,
                parent_message_id=parent_message_id,
                model=model,
                endpoint=endpoint,
                token_count=token_count,
                error=error,
                created_at_ms=now,
                metadata=metadata or {},
            )
            self._messages.append(row)
        else:
            row = existing
            row.text = text
            row.sender = sender
            row.is_created_by_user = is_created_by_user
            if parent_message_id is not None:
                row.parent_message_id = parent_message_id
            if model is not None:
                row.model = model
            if endpoint is not None:
                row.endpoint = endpoint
            if token_count is not None:
                row.token_count = token_count
            if error is not None:
                row.error = error
            if metadata is not None:
                row.metadata = metadata

        convo = self._find_conversation(user_context.user_id, conversation_id)
        if convo is not None:
            convo.updated_at_ms = now

        return row.to_dict()

    @log_call(logger=logger)
    async def edit_message(
        self,
        user_context: UserContext,
        conversation_id: str,
        message_id: str,
        *,
        text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        row = next(
            (
                m
                for m in self._messages
                if m.user_sub == user_context.user_id
                and m.conversation_id == conversation_id
                and m.message_id == message_id
            ),
            None,
        )
        if row is None:
            return None
        if text is not None:
            row.text = text
        if metadata is not None:
            row.metadata = metadata
        return row.to_dict()

    @log_call(logger=logger)
    async def delete_message(
        self, user_context: UserContext, conversation_id: str, message_id: str
    ) -> bool:
        before = len(self._messages)
        self._messages = [
            m
            for m in self._messages
            if not (
                m.user_sub == user_context.user_id
                and m.conversation_id == conversation_id
                and m.message_id == message_id
            )
        ]
        return len(self._messages) < before
