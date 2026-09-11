"""Console-prompts service — Mongo-repl WU-prompts of the MongoDB-
elimination EPIC.

The AuditTrace-side, RLS-isolated store backing LibreChat's saved
prompts (the fork's Mongo ``PromptGroup``/``Prompt`` collections), per
the ratified spec (2026-09-11-SPEC-mongo-repl-wu-prompts-store.md).
Mirrors ``services/console_conversations.py``'s shape and discipline
EXACTLY (the spec's instruction): an ABC + a Postgres-backed
implementation with an explicit ``user_sub`` filter on every query (the
Phase-2 pattern) + a Mock for fast unit tests.

**Model shape (RESOLVED per the spec's design note).** LibreChat's
prompts are two Mongo collections — ``PromptGroup`` (name/category/
oneliner/productionId) and ``Prompt`` (groupId FK/prompt text/type/
version-by-creation-order). Modelled here as a group row
(``ConsolePromptGroup``) plus one row per prompt VERSION
(``ConsolePromptVersion``), exactly mirroring how
``console_conversations`` models convo/message — this preserves
LibreChat's versioning + ``productionId`` semantics: each edit is a
NEW, immutable version row (upsert-by-``prompt_id`` is idempotent, same
as every other Mongo-repl WU — resubmitting the SAME ``prompt_id``
updates that one row rather than minting a new version), and
``production_prompt_id`` on the group points at whichever version is
"live".

**COMPLETENESS (the WU-2 lesson).** Every method below (other than the
abstract-interface constructor) takes a ``UserContext`` and filters
explicitly by ``user_context.user_id`` at the SERVICE layer — Postgres
RLS (migration 025) is a no-op on SQLite, so this belt-and-suspenders
duplication is required, not decorative
(feedback_unit_tests_miss_rls). ``upsert_version`` additionally
verifies the target GROUP exists and is owned by the caller before
attaching a version to it (never trusts a bare ``group_id`` string);
``set_production`` additionally verifies the target VERSION belongs to
BOTH the caller AND the named group before promoting it — no operation
here can act on another user's group or version, full stop.
``user_sub`` is ALWAYS taken from the ``UserContext`` the route
resolved from the token — a caller-supplied ``user_sub`` in a request
body is never read by any method here
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

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.db.models import ConsolePromptGroup, ConsolePromptVersion
from audittrace.identity import UserContext
from audittrace.logging_config import log_call

logger = logging.getLogger(__name__)

# List pages default to 25 rows, capped at 200 — same rationale as
# console_conversations'/console_presets' DEFAULT_LIST_LIMIT/MAX_LIST_LIMIT.
DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 200


def _now_ms() -> int:
    return int(time.time() * 1000)


def _encode_cursor(*, updated_at_ms: int, group_id: str) -> str:
    """Opaque pagination cursor: base64 of ``updated_at_ms:group_id``.

    The tie-break on ``group_id`` makes ordering deterministic when two
    groups share the same millisecond ``updated_at_ms`` (append-only
    writes issued back-to-back with no I/O between them can tie).
    Callers must treat the cursor as opaque — the encoding is an
    implementation detail, not an API contract.
    """
    raw = f"{updated_at_ms}:{group_id}"
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
    updated_at_ms_str, _, group_id = raw.partition(":")
    if not group_id:
        raise ValueError(f"invalid cursor: {cursor!r}")
    try:
        updated_at_ms = int(updated_at_ms_str)
    except ValueError as exc:
        raise ValueError(f"invalid cursor: {cursor!r}") from exc
    return updated_at_ms, group_id


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIST_LIMIT))


class ConsolePromptsService(ABC):
    """Abstract console-prompts store — the group+versions-shaped
    replacement for LibreChat's Mongo PromptGroup/Prompt collections."""

    @abstractmethod
    async def upsert_group(
        self,
        user_context: UserContext,
        group_id: str,
        *,
        name: str,
        category: str | None = None,
        oneliner: str | None = None,
        command: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update the caller's prompt group identified by
        ``(user_sub, group_id)``. Idempotent: calling twice with the
        same ``group_id`` updates the existing row (bumping
        ``updated_at_ms``) rather than creating a duplicate — the
        unique constraint on ``(user_sub, group_id)`` is what this
        upserts against. Returns the persisted row as a plain dict
        (WITHOUT its versions — see :meth:`get_group` for the
        versions-included shape)."""

    @abstractmethod
    async def list_groups(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return a page of the caller's OWN (non-deleted) prompt
        groups, newest-first by ``updated_at_ms``, plus the opaque
        cursor for the next page (``None`` when there is no further
        page). Group rows here carry NO versions (list view is
        summary-only) — see :meth:`get_group` for the full shape.

        MUST NEVER include another user's group — the isolation guard
        the non-vacuity test neuters: drop the explicit ``user_sub``
        filter and another user's rows leak into the page.
        """

    @abstractmethod
    async def get_group(
        self, user_context: UserContext, group_id: str
    ) -> dict[str, Any] | None:
        """Return the caller's OWN group INCLUDING its full version
        history (key ``versions``, oldest-first by ``version``
        number), or ``None`` if the group doesn't exist, is
        soft-deleted, or belongs to another user (the three cases are
        indistinguishable from the caller's point of view — 404, never
        a 403 that would leak existence).

        MUST NEVER include another user's versions — the isolation
        guard the non-vacuity test neuters: drop the explicit
        ``user_sub`` filter on the versions fetch and another user's
        versions leak into the response.
        """

    @abstractmethod
    async def upsert_version(
        self,
        user_context: UserContext,
        group_id: str,
        prompt_id: str,
        *,
        text: str,
        type: str = "text",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Create or update the caller's OWN version identified by
        ``(user_sub, prompt_id)``, attached to ``group_id``.

        Returns ``None`` if the group doesn't exist, is deleted, or
        belongs to another user — a hostile caller can never attach a
        version to a group they don't own by guessing its ``group_id``
        (the completeness/isolation guard the non-vacuity test
        neuters). On success, a NEW version is assigned the next
        monotonic ``version`` number for the group (max existing + 1,
        starting at 1); resubmitting an EXISTING ``prompt_id`` updates
        that same row's ``text``/``type``/``metadata`` in place
        (idempotent upsert, same shape as every other Mongo-repl WU)
        without renumbering it."""

    @abstractmethod
    async def set_production(
        self, user_context: UserContext, group_id: str, prompt_id: str
    ) -> dict[str, Any] | None:
        """Mark the caller's OWN version ``prompt_id`` (which MUST
        belong to ``group_id``) as the group's production version.
        Returns the updated group (with versions), or ``None`` if the
        group doesn't exist/isn't owned, OR the named version doesn't
        exist/isn't owned/doesn't belong to this group (the isolation
        + completeness guard the non-vacuity test neuters: a hostile
        caller can never point ``production_prompt_id`` at another
        user's version, nor at a version from a DIFFERENT group)."""

    @abstractmethod
    async def delete_group(self, user_context: UserContext, group_id: str) -> bool:
        """Soft-delete the caller's OWN group AND hard-delete every one
        of its versions. Returns ``True`` if a (previously
        non-deleted) group was found and deleted, ``False`` otherwise
        (not found/not owned/already deleted — idempotent no-op)."""


def _group_to_dict(row: ConsolePromptGroup) -> dict[str, Any]:
    return {
        "group_id": row.group_id,
        "user_sub": row.user_sub,
        "name": row.name,
        "category": row.category,
        "oneliner": row.oneliner,
        "command": row.command,
        "production_prompt_id": row.production_prompt_id,
        "created_at_ms": row.created_at_ms,
        "updated_at_ms": row.updated_at_ms,
        "deleted_at_ms": row.deleted_at_ms,
        "metadata": row.metadata_json,
    }


def _version_to_dict(row: ConsolePromptVersion) -> dict[str, Any]:
    return {
        "prompt_id": row.prompt_id,
        "group_id": row.group_id,
        "user_sub": row.user_sub,
        "text": row.text,
        "type": row.type,
        "version": row.version,
        "created_at_ms": row.created_at_ms,
        "updated_at_ms": row.updated_at_ms,
        "metadata": row.metadata_json,
    }


class PostgresConsolePromptsService(ConsolePromptsService):
    """PostgreSQL-backed console-prompts service."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    @log_call(logger=logger)
    async def upsert_group(
        self,
        user_context: UserContext,
        group_id: str,
        *,
        name: str,
        category: str | None = None,
        oneliner: str | None = None,
        command: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        async with self._session_factory() as session:
            try:
                existing = (
                    await session.execute(
                        select(ConsolePromptGroup)
                        # The isolation guard this upsert's non-vacuity
                        # test neuters — drop this filter and a hostile
                        # caller could update ANOTHER user's row by
                        # guessing their group_id.
                        .filter(ConsolePromptGroup.user_sub == user_context.user_id)
                        .filter(ConsolePromptGroup.group_id == group_id)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    row = ConsolePromptGroup(
                        id=str(uuid.uuid4()),
                        group_id=group_id,
                        user_sub=user_context.user_id,
                        name=name,
                        category=category or "",
                        oneliner=oneliner or "",
                        command=command,
                        created_at_ms=now,
                        updated_at_ms=now,
                        metadata_json=metadata or {},
                    )
                    session.add(row)
                else:
                    row = existing
                    row.name = name
                    if category is not None:
                        row.category = category
                    if oneliner is not None:
                        row.oneliner = oneliner
                    if command is not None:
                        row.command = command
                    if metadata is not None:
                        row.metadata_json = metadata
                    row.updated_at_ms = now
                await session.commit()
                result = _group_to_dict(row)
            except Exception as exc:
                await session.rollback()
                raise RuntimeError(f"upsert_group({group_id!r}) failed: {exc}") from exc
        return result

    @log_call(logger=logger)
    async def list_groups(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        effective_limit = _clamp_limit(limit)
        async with self._session_factory() as session:
            stmt = (
                select(ConsolePromptGroup)
                # Isolation guard — the non-vacuity test neuters this
                # exact filter.
                .filter(ConsolePromptGroup.user_sub == user_context.user_id)
                .filter(ConsolePromptGroup.deleted_at_ms.is_(None))
            )
            if cursor is not None:
                cursor_updated_at_ms, cursor_group_id = _decode_cursor(cursor)
                stmt = stmt.filter(
                    (ConsolePromptGroup.updated_at_ms < cursor_updated_at_ms)
                    | (
                        (ConsolePromptGroup.updated_at_ms == cursor_updated_at_ms)
                        & (ConsolePromptGroup.group_id < cursor_group_id)
                    )
                )
            stmt = stmt.order_by(
                ConsolePromptGroup.updated_at_ms.desc(),
                ConsolePromptGroup.group_id.desc(),
            ).limit(effective_limit + 1)
            rows = (await session.execute(stmt)).scalars().all()
            plain = [_group_to_dict(r) for r in rows]

        has_more = len(plain) > effective_limit
        page = plain[:effective_limit]
        next_cursor = (
            _encode_cursor(
                updated_at_ms=page[-1]["updated_at_ms"],
                group_id=page[-1]["group_id"],
            )
            if has_more and page
            else None
        )
        return page, next_cursor

    @log_call(logger=logger)
    async def get_group(
        self, user_context: UserContext, group_id: str
    ) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsolePromptGroup)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsolePromptGroup.user_sub == user_context.user_id)
                    .filter(ConsolePromptGroup.group_id == group_id)
                    .filter(ConsolePromptGroup.deleted_at_ms.is_(None))
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            versions = (
                (
                    await session.execute(
                        select(ConsolePromptVersion)
                        # Isolation guard — non-vacuity test neuters this.
                        .filter(ConsolePromptVersion.user_sub == user_context.user_id)
                        .filter(ConsolePromptVersion.group_id == group_id)
                        .order_by(ConsolePromptVersion.version.asc())
                    )
                )
                .scalars()
                .all()
            )
            result = _group_to_dict(row)
            result["versions"] = [_version_to_dict(v) for v in versions]
            return result

    @log_call(logger=logger)
    async def upsert_version(
        self,
        user_context: UserContext,
        group_id: str,
        prompt_id: str,
        *,
        text: str,
        type: str = "text",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = _now_ms()
        async with self._session_factory() as session:
            try:
                # Completeness guard (WU-2 lesson): a version can only be
                # attached to a group the caller owns — never trust a
                # bare group_id string. Non-vacuity test neuters this.
                group = (
                    await session.execute(
                        select(ConsolePromptGroup)
                        .filter(ConsolePromptGroup.user_sub == user_context.user_id)
                        .filter(ConsolePromptGroup.group_id == group_id)
                        .filter(ConsolePromptGroup.deleted_at_ms.is_(None))
                    )
                ).scalar_one_or_none()
                if group is None:
                    return None

                existing = (
                    await session.execute(
                        select(ConsolePromptVersion)
                        # Isolation guard — non-vacuity test neuters this
                        # exact filter (same rationale as upsert_group).
                        .filter(ConsolePromptVersion.user_sub == user_context.user_id)
                        .filter(ConsolePromptVersion.prompt_id == prompt_id)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    max_version = (
                        await session.execute(
                            select(func.max(ConsolePromptVersion.version))
                            .filter(
                                ConsolePromptVersion.user_sub == user_context.user_id
                            )
                            .filter(ConsolePromptVersion.group_id == group_id)
                        )
                    ).scalar_one()
                    row = ConsolePromptVersion(
                        id=str(uuid.uuid4()),
                        prompt_id=prompt_id,
                        group_id=group_id,
                        user_sub=user_context.user_id,
                        text=text,
                        type=type,
                        version=(max_version or 0) + 1,
                        created_at_ms=now,
                        updated_at_ms=now,
                        metadata_json=metadata or {},
                    )
                    session.add(row)
                else:
                    row = existing
                    row.text = text
                    row.type = type
                    if metadata is not None:
                        row.metadata_json = metadata
                    row.updated_at_ms = now

                group.updated_at_ms = now
                await session.commit()
                result = _version_to_dict(row)
            except Exception as exc:
                await session.rollback()
                raise RuntimeError(
                    f"upsert_version({prompt_id!r}) failed: {exc}"
                ) from exc
        return result

    @log_call(logger=logger)
    async def set_production(
        self, user_context: UserContext, group_id: str, prompt_id: str
    ) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            group = (
                await session.execute(
                    select(ConsolePromptGroup)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsolePromptGroup.user_sub == user_context.user_id)
                    .filter(ConsolePromptGroup.group_id == group_id)
                    .filter(ConsolePromptGroup.deleted_at_ms.is_(None))
                )
            ).scalar_one_or_none()
            if group is None:
                return None

            # Completeness + isolation guard (WU-2 lesson): the version
            # must belong to BOTH this caller AND this exact group —
            # never trust a bare prompt_id. Non-vacuity test neuters
            # the group_id half of this filter (cross-group hijack)
            # and the user_sub half (cross-user hijack) independently.
            version = (
                await session.execute(
                    select(ConsolePromptVersion)
                    .filter(ConsolePromptVersion.user_sub == user_context.user_id)
                    .filter(ConsolePromptVersion.group_id == group_id)
                    .filter(ConsolePromptVersion.prompt_id == prompt_id)
                )
            ).scalar_one_or_none()
            if version is None:
                return None

            group.production_prompt_id = prompt_id
            group.updated_at_ms = _now_ms()
            await session.commit()

            versions = (
                (
                    await session.execute(
                        select(ConsolePromptVersion)
                        .filter(ConsolePromptVersion.user_sub == user_context.user_id)
                        .filter(ConsolePromptVersion.group_id == group_id)
                        .order_by(ConsolePromptVersion.version.asc())
                    )
                )
                .scalars()
                .all()
            )
            result = _group_to_dict(group)
            result["versions"] = [_version_to_dict(v) for v in versions]
            return result

    @log_call(logger=logger)
    async def delete_group(self, user_context: UserContext, group_id: str) -> bool:
        now = _now_ms()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsolePromptGroup)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsolePromptGroup.user_sub == user_context.user_id)
                    .filter(ConsolePromptGroup.group_id == group_id)
                    .filter(ConsolePromptGroup.deleted_at_ms.is_(None))
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.deleted_at_ms = now
            row.updated_at_ms = now
            # ConsolePromptVersion carries no soft-delete column (spec:
            # only the group soft-deletes) — deleting a group HARD-
            # deletes its versions, since they are meaningless without
            # their parent and the group delete already covers the
            # audit-visible soft-delete semantics (same convention as
            # ConsoleConversation.delete_conversation).
            await session.execute(
                delete(ConsolePromptVersion)
                .where(ConsolePromptVersion.user_sub == user_context.user_id)
                .where(ConsolePromptVersion.group_id == group_id)
            )
            await session.commit()
        return True


@dataclass
class _MockGroup:
    group_id: str
    user_sub: str
    name: str
    category: str
    oneliner: str
    command: str | None
    created_at_ms: int
    updated_at_ms: int
    production_prompt_id: str | None = None
    deleted_at_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "user_sub": self.user_sub,
            "name": self.name,
            "category": self.category,
            "oneliner": self.oneliner,
            "command": self.command,
            "production_prompt_id": self.production_prompt_id,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "deleted_at_ms": self.deleted_at_ms,
            "metadata": self.metadata,
        }


@dataclass
class _MockVersion:
    prompt_id: str
    group_id: str
    user_sub: str
    text: str
    type: str
    version: int
    created_at_ms: int
    updated_at_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "group_id": self.group_id,
            "user_sub": self.user_sub,
            "text": self.text,
            "type": self.type,
            "version": self.version,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "metadata": self.metadata,
        }


class MockConsolePromptsService(ConsolePromptsService):
    """In-process mock for unit tests that don't wire a Postgres factory
    (mirrors ``MockConsoleConversationsService``'s shape)."""

    def __init__(self) -> None:
        self._groups: list[_MockGroup] = []
        self._versions: list[_MockVersion] = []

    def reset(self) -> None:
        self._groups.clear()
        self._versions.clear()

    def _find_group(self, user_sub: str, group_id: str) -> _MockGroup | None:
        for g in self._groups:
            if (
                g.user_sub == user_sub
                and g.group_id == group_id
                and g.deleted_at_ms is None
            ):
                return g
        return None

    def _find_version(self, user_sub: str, prompt_id: str) -> _MockVersion | None:
        for v in self._versions:
            if v.user_sub == user_sub and v.prompt_id == prompt_id:
                return v
        return None

    @log_call(logger=logger)
    async def upsert_group(
        self,
        user_context: UserContext,
        group_id: str,
        *,
        name: str,
        category: str | None = None,
        oneliner: str | None = None,
        command: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        existing = self._find_group(user_context.user_id, group_id)
        if existing is None:
            row = _MockGroup(
                group_id=group_id,
                user_sub=user_context.user_id,
                name=name,
                category=category or "",
                oneliner=oneliner or "",
                command=command,
                created_at_ms=now,
                updated_at_ms=now,
                metadata=metadata or {},
            )
            self._groups.append(row)
        else:
            row = existing
            row.name = name
            if category is not None:
                row.category = category
            if oneliner is not None:
                row.oneliner = oneliner
            if command is not None:
                row.command = command
            if metadata is not None:
                row.metadata = metadata
            row.updated_at_ms = now
        return row.to_dict()

    @log_call(logger=logger)
    async def list_groups(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        effective_limit = _clamp_limit(limit)
        # Isolation guard — non-vacuity test neuters this filter.
        mine = [
            g
            for g in self._groups
            if g.user_sub == user_context.user_id and g.deleted_at_ms is None
        ]
        mine.sort(key=lambda g: (g.updated_at_ms, g.group_id), reverse=True)
        if cursor is not None:
            cursor_updated_at_ms, cursor_group_id = _decode_cursor(cursor)
            mine = [
                g
                for g in mine
                if (g.updated_at_ms, g.group_id)
                < (cursor_updated_at_ms, cursor_group_id)
            ]
        page = mine[: effective_limit + 1]
        has_more = len(page) > effective_limit
        page = page[:effective_limit]
        next_cursor = (
            _encode_cursor(
                updated_at_ms=page[-1].updated_at_ms,
                group_id=page[-1].group_id,
            )
            if has_more and page
            else None
        )
        return [g.to_dict() for g in page], next_cursor

    @log_call(logger=logger)
    async def get_group(
        self, user_context: UserContext, group_id: str
    ) -> dict[str, Any] | None:
        row = self._find_group(user_context.user_id, group_id)
        if row is None:
            return None
        # Isolation guard — non-vacuity test neuters this filter.
        versions = sorted(
            (
                v
                for v in self._versions
                if v.user_sub == user_context.user_id and v.group_id == group_id
            ),
            key=lambda v: v.version,
        )
        result = row.to_dict()
        result["versions"] = [v.to_dict() for v in versions]
        return result

    @log_call(logger=logger)
    async def upsert_version(
        self,
        user_context: UserContext,
        group_id: str,
        prompt_id: str,
        *,
        text: str,
        type: str = "text",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = _now_ms()
        # Completeness guard (WU-2 lesson) — non-vacuity test neuters this.
        group = self._find_group(user_context.user_id, group_id)
        if group is None:
            return None

        existing = self._find_version(user_context.user_id, prompt_id)
        if existing is None:
            group_versions = [
                v
                for v in self._versions
                if v.user_sub == user_context.user_id and v.group_id == group_id
            ]
            next_version = max((v.version for v in group_versions), default=0) + 1
            row = _MockVersion(
                prompt_id=prompt_id,
                group_id=group_id,
                user_sub=user_context.user_id,
                text=text,
                type=type,
                version=next_version,
                created_at_ms=now,
                updated_at_ms=now,
                metadata=metadata or {},
            )
            self._versions.append(row)
        else:
            row = existing
            row.text = text
            row.type = type
            if metadata is not None:
                row.metadata = metadata
            row.updated_at_ms = now

        group.updated_at_ms = now
        return row.to_dict()

    @log_call(logger=logger)
    async def set_production(
        self, user_context: UserContext, group_id: str, prompt_id: str
    ) -> dict[str, Any] | None:
        group = self._find_group(user_context.user_id, group_id)
        if group is None:
            return None

        # Completeness + isolation guard (WU-2 lesson) — non-vacuity
        # test neuters this filter.
        version = next(
            (
                v
                for v in self._versions
                if v.user_sub == user_context.user_id
                and v.group_id == group_id
                and v.prompt_id == prompt_id
            ),
            None,
        )
        if version is None:
            return None

        group.production_prompt_id = prompt_id
        group.updated_at_ms = _now_ms()
        versions = sorted(
            (
                v
                for v in self._versions
                if v.user_sub == user_context.user_id and v.group_id == group_id
            ),
            key=lambda v: v.version,
        )
        result = group.to_dict()
        result["versions"] = [v.to_dict() for v in versions]
        return result

    @log_call(logger=logger)
    async def delete_group(self, user_context: UserContext, group_id: str) -> bool:
        row = self._find_group(user_context.user_id, group_id)
        if row is None:
            return False
        now = _now_ms()
        row.deleted_at_ms = now
        row.updated_at_ms = now
        self._versions = [
            v
            for v in self._versions
            if not (v.user_sub == user_context.user_id and v.group_id == group_id)
        ]
        return True
