"""Session memory service — the ``session`` layer, WU-1 of the
Sovereign-Attach EPIC.

Ephemeral, per-user, Postgres-backed memory layer: the enforced
least-privilege wall the ratified *ephemeral-default* decision requires.
Distinct from episodic/procedural (S3-backed, dual-tier per ADR-062
Phase B) — session content has **no corpus tier** and **no promote path**
in this WU (promotion to a durable layer is WU-4, out of scope here) and
**no recall/GC surface** (WU-5/WU-6). This module is write-path +
isolation ONLY, per the ratified spec
(2026-09-03-SPEC-wu1-session-layer-narrow-ingest-scope.md).

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.db.models import SessionMemoryItem
from audittrace.identity import UserContext
from audittrace.logging_config import log_call

logger = logging.getLogger(__name__)


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

    Write-path + isolation only (WU-1 scope) — no search/list/delete/
    promote here; those land in later WUs (WU-4 promote, WU-5 recall,
    WU-6 GC).
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

    def reset(self) -> None:
        """Clear all session-memory items."""
        self._items.clear()
