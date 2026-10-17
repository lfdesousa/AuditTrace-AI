"""Console-files service — the Files-metadata domain of the
MongoDB-elimination EPIC.

The AuditTrace-side, RLS-isolated store backing LibreChat's file
METADATA record (the fork's Mongo ``File`` collection), per the
ratified spec
(2026-09-11-SPEC-mongo-repl-wu-files-metadata-store.md). Mirrors
``services/console_chat_projects.py``'s (and ``services/
console_presets.py``'s) shape and discipline EXACTLY (the spec's
instruction): an ABC + a Postgres-backed implementation with an
explicit ``user_sub`` filter on every query (the Phase-2 pattern) + a
Mock for fast unit tests.

**Scope boundary — metadata only.** This store owns the file METADATA
record; the file BYTES stay in object storage (S3/MinIO,
``feedback_storage_always_s3``). No method here reads, writes, or even
references a byte payload — :attr:`ConsoleFile.object_key` is a
pointer into that store, never the content.

**COMPLETENESS (the WU-2 lesson).** Every method below (other than the
abstract-interface constructor) takes a ``UserContext`` and filters
explicitly by ``user_context.user_id`` at the SERVICE layer — Postgres
RLS (migration 027) is a no-op on SQLite, so this belt-and-suspenders
duplication is required, not decorative
(feedback_unit_tests_miss_rls). ``user_sub`` is ALWAYS taken from the
``UserContext`` the route resolved from the token — a caller-supplied
``user_sub`` in a request body is never read by any method here
(feedback_never_trust_caller_metadata_for_security_fields); the route
layer enforces this by construction (it never passes a body field
named ``user_sub``/``user_id`` into these methods).

**Fourth read shape — batch-get-by-ids.** Beyond the CRUD+cursor
quartet every other Mongo-repl WU carries, this domain additionally
needs :meth:`batch_get_files` (the fork resolves a conversation's
attachments by a batch of ``file_id``s in one round trip). Same
isolation discipline as every other method: the explicit ``user_sub``
filter is the guard the non-vacuity test neuters.
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

from audittrace.db.models import ConsoleFile
from audittrace.identity import UserContext
from audittrace.logging_config import log_call

logger = logging.getLogger(__name__)

# List pages default to 25 rows, capped at 200 — same rationale as
# console_chat_projects'/console_presets' DEFAULT_LIST_LIMIT/MAX_LIST_LIMIT.
# The same MAX_LIST_LIMIT also bounds batch_get_files' input size (see
# ConsoleFileBatchGetRequest in audittrace.models).
DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 200


def _now_ms() -> int:
    return int(time.time() * 1000)


def _encode_cursor(*, updated_at_ms: int, file_id: str) -> str:
    """Opaque pagination cursor: base64 of ``updated_at_ms:file_id``.

    The tie-break on ``file_id`` makes ordering deterministic when two
    files share the same millisecond ``updated_at_ms`` (append-only
    writes issued back-to-back with no I/O between them can tie).
    Callers must treat the cursor as opaque — the encoding is an
    implementation detail, not an API contract.
    """
    raw = f"{updated_at_ms}:{file_id}"
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
    updated_at_ms_str, _, file_id = raw.partition(":")
    if not file_id:
        raise ValueError(f"invalid cursor: {cursor!r}")
    try:
        updated_at_ms = int(updated_at_ms_str)
    except ValueError as exc:
        raise ValueError(f"invalid cursor: {cursor!r}") from exc
    return updated_at_ms, file_id


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIST_LIMIT))


class ConsoleFilesService(ABC):
    """Abstract console-files store — the sovereign replacement for
    LibreChat's Mongo ``File`` collection (metadata only, bytes stay in
    object storage)."""

    @abstractmethod
    async def upsert_file(
        self,
        user_context: UserContext,
        file_id: str,
        *,
        filename: str,
        type: str,
        bytes: int = 0,
        object_key: str | None = None,
        width: int | None = None,
        height: int | None = None,
        context: str | None = None,
        usage: dict[str, Any] | None = None,
        embedded: bool = False,
        temp_file_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update the caller's file-metadata record
        identified by ``(user_sub, file_id)``. Idempotent: calling
        twice with the same ``file_id`` updates the existing row
        (bumping ``updated_at_ms``) rather than creating a duplicate —
        the unique constraint on ``(user_sub, file_id)`` is what this
        method upserts against. Returns the persisted row as a plain
        dict."""

    @abstractmethod
    async def list_files(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return a page of the caller's OWN (non-deleted) file-metadata
        records, newest-first by ``updated_at_ms``, plus the opaque
        cursor for the next page (``None`` when there is no further
        page).

        MUST NEVER include another user's file record — the isolation
        guard the non-vacuity test neuters: drop the explicit
        ``user_sub`` filter and another user's rows leak into the page.
        """

    @abstractmethod
    async def get_file(
        self, user_context: UserContext, file_id: str
    ) -> dict[str, Any] | None:
        """Return the caller's OWN file-metadata record, or ``None`` if
        it doesn't exist, is soft-deleted, or belongs to another user
        (the three cases are indistinguishable from the caller's point
        of view — 404, never a 403 that would leak existence)."""

    @abstractmethod
    async def batch_get_files(
        self, user_context: UserContext, file_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Return the caller's OWN (non-deleted) file-metadata records
        for the given ``file_ids``, in the SAME order as ``file_ids``
        (any id not found/not owned/deleted is simply omitted — never
        raises, never leaks existence of another user's file).

        MUST NEVER include another user's file record — same isolation
        guard as :meth:`get_file`/:meth:`list_files`, the non-vacuity
        test neuters the explicit ``user_sub`` filter here too.
        """

    @abstractmethod
    async def delete_file(self, user_context: UserContext, file_id: str) -> bool:
        """Soft-delete the caller's OWN file-metadata record. Returns
        ``True`` if a (previously non-deleted) record was found and
        deleted, ``False`` otherwise (not found/not owned/already
        deleted — idempotent no-op)."""


def _file_to_dict(row: ConsoleFile) -> dict[str, Any]:
    return {
        "file_id": row.file_id,
        "user_sub": row.user_sub,
        "filename": row.filename,
        "type": row.type,
        "bytes": row.bytes,
        "object_key": row.object_key,
        "width": row.width,
        "height": row.height,
        "context": row.context,
        "usage": row.usage_json,
        "embedded": row.embedded,
        "temp_file_id": row.temp_file_id,
        "created_at_ms": row.created_at_ms,
        "updated_at_ms": row.updated_at_ms,
        "deleted_at_ms": row.deleted_at_ms,
        "metadata": row.metadata_json,
    }


class PostgresConsoleFilesService(ConsoleFilesService):
    """PostgreSQL-backed console-files service."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    @log_call(logger=logger)
    async def upsert_file(
        self,
        user_context: UserContext,
        file_id: str,
        *,
        filename: str,
        type: str,
        bytes: int = 0,
        object_key: str | None = None,
        width: int | None = None,
        height: int | None = None,
        context: str | None = None,
        usage: dict[str, Any] | None = None,
        embedded: bool = False,
        temp_file_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        async with self._session_factory() as session:
            try:
                existing = (
                    await session.execute(
                        select(ConsoleFile)
                        # The isolation guard this upsert's non-vacuity
                        # test neuters — drop this filter and a hostile
                        # caller could update ANOTHER user's row by
                        # guessing their file_id.
                        .filter(ConsoleFile.user_sub == user_context.user_id)
                        .filter(ConsoleFile.file_id == file_id)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    row = ConsoleFile(
                        id=str(uuid.uuid4()),
                        file_id=file_id,
                        user_sub=user_context.user_id,
                        filename=filename,
                        type=type,
                        bytes=bytes,
                        object_key=object_key,
                        width=width,
                        height=height,
                        context=context,
                        usage_json=usage or {},
                        embedded=embedded,
                        temp_file_id=temp_file_id,
                        created_at_ms=now,
                        updated_at_ms=now,
                        metadata_json=metadata or {},
                    )
                    session.add(row)
                else:
                    row = existing
                    row.filename = filename
                    row.type = type
                    row.bytes = bytes
                    if object_key is not None:
                        row.object_key = object_key
                    if width is not None:
                        row.width = width
                    if height is not None:
                        row.height = height
                    if context is not None:
                        row.context = context
                    if usage is not None:
                        row.usage_json = usage
                    row.embedded = embedded
                    if temp_file_id is not None:
                        row.temp_file_id = temp_file_id
                    if metadata is not None:
                        row.metadata_json = metadata
                    row.updated_at_ms = now
                await session.commit()
                result = _file_to_dict(row)
            except Exception as exc:
                await session.rollback()
                raise RuntimeError(f"upsert_file({file_id!r}) failed: {exc}") from exc
        return result

    @log_call(logger=logger)
    async def list_files(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        effective_limit = _clamp_limit(limit)
        async with self._session_factory() as session:
            stmt = (
                select(ConsoleFile)
                # Isolation guard — the non-vacuity test neuters this
                # exact filter.
                .filter(ConsoleFile.user_sub == user_context.user_id)
                .filter(ConsoleFile.deleted_at_ms.is_(None))
            )
            if cursor is not None:
                cursor_updated_at_ms, cursor_file_id = _decode_cursor(cursor)
                stmt = stmt.filter(
                    (ConsoleFile.updated_at_ms < cursor_updated_at_ms)
                    | (
                        (ConsoleFile.updated_at_ms == cursor_updated_at_ms)
                        & (ConsoleFile.file_id < cursor_file_id)
                    )
                )
            stmt = stmt.order_by(
                ConsoleFile.updated_at_ms.desc(),
                ConsoleFile.file_id.desc(),
            ).limit(effective_limit + 1)
            rows = (await session.execute(stmt)).scalars().all()
            plain = [_file_to_dict(r) for r in rows]

        has_more = len(plain) > effective_limit
        page = plain[:effective_limit]
        next_cursor = (
            _encode_cursor(
                updated_at_ms=page[-1]["updated_at_ms"],
                file_id=page[-1]["file_id"],
            )
            if has_more and page
            else None
        )
        return page, next_cursor

    @log_call(logger=logger)
    async def get_file(
        self, user_context: UserContext, file_id: str
    ) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsoleFile)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsoleFile.user_sub == user_context.user_id)
                    .filter(ConsoleFile.file_id == file_id)
                    .filter(ConsoleFile.deleted_at_ms.is_(None))
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return _file_to_dict(row)

    @log_call(logger=logger)
    async def batch_get_files(
        self, user_context: UserContext, file_ids: list[str]
    ) -> list[dict[str, Any]]:
        if not file_ids:
            return []
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ConsoleFile)
                        # Isolation guard — non-vacuity test neuters this
                        # exact filter (the whole point of the batch
                        # route: without it, a hostile caller could
                        # harvest ANY user's file metadata by guessing
                        # file_ids in bulk).
                        .filter(ConsoleFile.user_sub == user_context.user_id)
                        .filter(ConsoleFile.file_id.in_(file_ids))
                        .filter(ConsoleFile.deleted_at_ms.is_(None))
                    )
                )
                .scalars()
                .all()
            )
            # Serialise to plain dicts WHILE the session is still open
            # (#364 — an ORM instance must never be read after its
            # session closes).
            by_id = {r.file_id: _file_to_dict(r) for r in rows}
        # Preserve caller's requested order; silently omit misses (not
        # found/not owned/deleted) rather than raising — same
        # not-leak-existence discipline as get_file's 404.
        return [by_id[fid] for fid in file_ids if fid in by_id]

    @log_call(logger=logger)
    async def delete_file(self, user_context: UserContext, file_id: str) -> bool:
        now = _now_ms()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsoleFile)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsoleFile.user_sub == user_context.user_id)
                    .filter(ConsoleFile.file_id == file_id)
                    .filter(ConsoleFile.deleted_at_ms.is_(None))
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.deleted_at_ms = now
            row.updated_at_ms = now
            await session.commit()
        return True


@dataclass
class _MockFile:
    file_id: str
    user_sub: str
    filename: str
    type: str
    bytes: int = 0
    object_key: str | None = None
    width: int | None = None
    height: int | None = None
    context: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    embedded: bool = False
    temp_file_id: str | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0
    deleted_at_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "user_sub": self.user_sub,
            "filename": self.filename,
            "type": self.type,
            "bytes": self.bytes,
            "object_key": self.object_key,
            "width": self.width,
            "height": self.height,
            "context": self.context,
            "usage": self.usage,
            "embedded": self.embedded,
            "temp_file_id": self.temp_file_id,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "deleted_at_ms": self.deleted_at_ms,
            "metadata": self.metadata,
        }


class MockConsoleFilesService(ConsoleFilesService):
    """In-process mock for unit tests that don't wire a Postgres factory
    (mirrors ``MockConsoleChatProjectsService``'s shape)."""

    def __init__(self) -> None:
        self._files: list[_MockFile] = []

    def reset(self) -> None:
        self._files.clear()

    def _find(self, user_sub: str, file_id: str) -> _MockFile | None:
        for f in self._files:
            if (
                f.user_sub == user_sub
                and f.file_id == file_id
                and f.deleted_at_ms is None
            ):
                return f
        return None

    @log_call(logger=logger)
    async def upsert_file(
        self,
        user_context: UserContext,
        file_id: str,
        *,
        filename: str,
        type: str,
        bytes: int = 0,
        object_key: str | None = None,
        width: int | None = None,
        height: int | None = None,
        context: str | None = None,
        usage: dict[str, Any] | None = None,
        embedded: bool = False,
        temp_file_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        existing = self._find(user_context.user_id, file_id)
        if existing is None:
            row = _MockFile(
                file_id=file_id,
                user_sub=user_context.user_id,
                filename=filename,
                type=type,
                bytes=bytes,
                object_key=object_key,
                width=width,
                height=height,
                context=context,
                usage=usage or {},
                embedded=embedded,
                temp_file_id=temp_file_id,
                created_at_ms=now,
                updated_at_ms=now,
                metadata=metadata or {},
            )
            self._files.append(row)
        else:
            row = existing
            row.filename = filename
            row.type = type
            row.bytes = bytes
            if object_key is not None:
                row.object_key = object_key
            if width is not None:
                row.width = width
            if height is not None:
                row.height = height
            if context is not None:
                row.context = context
            if usage is not None:
                row.usage = usage
            row.embedded = embedded
            if temp_file_id is not None:
                row.temp_file_id = temp_file_id
            if metadata is not None:
                row.metadata = metadata
            row.updated_at_ms = now
        return row.to_dict()

    @log_call(logger=logger)
    async def list_files(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        effective_limit = _clamp_limit(limit)
        # Isolation guard — non-vacuity test neuters this filter.
        mine = [
            f
            for f in self._files
            if f.user_sub == user_context.user_id and f.deleted_at_ms is None
        ]
        mine.sort(key=lambda f: (f.updated_at_ms, f.file_id), reverse=True)
        if cursor is not None:
            cursor_updated_at_ms, cursor_file_id = _decode_cursor(cursor)
            mine = [
                f
                for f in mine
                if (f.updated_at_ms, f.file_id) < (cursor_updated_at_ms, cursor_file_id)
            ]
        page = mine[: effective_limit + 1]
        has_more = len(page) > effective_limit
        page = page[:effective_limit]
        next_cursor = (
            _encode_cursor(
                updated_at_ms=page[-1].updated_at_ms,
                file_id=page[-1].file_id,
            )
            if has_more and page
            else None
        )
        return [f.to_dict() for f in page], next_cursor

    @log_call(logger=logger)
    async def get_file(
        self, user_context: UserContext, file_id: str
    ) -> dict[str, Any] | None:
        row = self._find(user_context.user_id, file_id)
        return row.to_dict() if row is not None else None

    @log_call(logger=logger)
    async def batch_get_files(
        self, user_context: UserContext, file_ids: list[str]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for fid in file_ids:
            # Isolation guard — non-vacuity test neuters this (via
            # ``_find``'s user_sub comparison).
            row = self._find(user_context.user_id, fid)
            if row is not None:
                results.append(row.to_dict())
        return results

    @log_call(logger=logger)
    async def delete_file(self, user_context: UserContext, file_id: str) -> bool:
        row = self._find(user_context.user_id, file_id)
        if row is None:
            return False
        now = _now_ms()
        row.deleted_at_ms = now
        row.updated_at_ms = now
        return True
