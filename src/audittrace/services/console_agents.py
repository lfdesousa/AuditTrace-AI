"""Console-agents service — the Agents domain of the MongoDB-elimination
EPIC.

The AuditTrace-side, RLS-isolated store backing LibreChat's Agent record
(the fork's Mongo ``Agent`` collection), per the ratified spec
(2026-09-12-SPEC-mongo-repl-wu-agents-store.md). Mirrors
``services/console_files.py``'s (and ``services/
console_chat_projects.py``'s) shape and discipline EXACTLY (the spec's
instruction): an ABC + a Postgres-backed implementation with an
explicit ``user_sub`` filter on every query (the Phase-2 pattern) + a
Mock for fast unit tests.

**Own-agents-only v1.** LibreChat has agent SHARING / marketplace
(``author``/``projectIds``/global agents). Cross-user sharing and a
marketplace are explicitly OUT OF SCOPE for this store — every row is
owned by exactly one ``user_sub``, RLS strictly own (like the prompts/
chat-projects domains' own no-sharing v1).

**Tools/model config are opaque.** ``model_parameters``/``tools``/
``artifacts`` are persisted as opaque jsonb blobs — this service
persists the agent record; it never executes or validates the tools a
row references.

**COMPLETENESS (the WU-2 lesson).** Every method below (other than the
abstract-interface constructor) takes a ``UserContext`` and filters
explicitly by ``user_context.user_id`` at the SERVICE layer — Postgres
RLS (migration 028) is a no-op on SQLite, so this belt-and-suspenders
duplication is required, not decorative
(feedback_unit_tests_miss_rls). ``user_sub`` is ALWAYS taken from the
``UserContext`` the route resolved from the token — a caller-supplied
``user_sub`` in a request body is never read by any method here
(feedback_never_trust_caller_metadata_for_security_fields); the route
layer enforces this by construction (it never passes a body field
named ``user_sub``/``user_id`` into these methods).

**Fourth read shape — batch-get-by-ids.** Beyond the CRUD+cursor
quartet every other Mongo-repl WU carries, this domain additionally
needs :meth:`batch_get_agents` (the fork resolves a batch of referenced
agents — e.g. for a conversation's agent picker — in one round trip).
Same isolation discipline as every other method: the explicit
``user_sub`` filter is the guard the non-vacuity test neuters.
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

from audittrace.db.models import ConsoleAgent
from audittrace.identity import UserContext
from audittrace.logging_config import log_call

logger = logging.getLogger(__name__)

# List pages default to 25 rows, capped at 200 — same rationale as
# console_files'/console_chat_projects' DEFAULT_LIST_LIMIT/MAX_LIST_LIMIT.
# The same MAX_LIST_LIMIT also bounds batch_get_agents' input size (see
# ConsoleAgentBatchGetRequest in audittrace.models).
DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 200


def _now_ms() -> int:
    return int(time.time() * 1000)


def _encode_cursor(*, updated_at_ms: int, agent_id: str) -> str:
    """Opaque pagination cursor: base64 of ``updated_at_ms:agent_id``.

    The tie-break on ``agent_id`` makes ordering deterministic when two
    agents share the same millisecond ``updated_at_ms`` (append-only
    writes issued back-to-back with no I/O between them can tie).
    Callers must treat the cursor as opaque — the encoding is an
    implementation detail, not an API contract.
    """
    raw = f"{updated_at_ms}:{agent_id}"
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
    updated_at_ms_str, _, agent_id = raw.partition(":")
    if not agent_id:
        raise ValueError(f"invalid cursor: {cursor!r}")
    try:
        updated_at_ms = int(updated_at_ms_str)
    except ValueError as exc:
        raise ValueError(f"invalid cursor: {cursor!r}") from exc
    return updated_at_ms, agent_id


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIST_LIMIT))


class ConsoleAgentsService(ABC):
    """Abstract console-agents store — the sovereign replacement for
    LibreChat's Mongo ``Agent`` collection (own-agents-only v1)."""

    @abstractmethod
    async def upsert_agent(
        self,
        user_context: UserContext,
        agent_id: str,
        *,
        name: str,
        description: str | None = None,
        instructions: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        tools: list[Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        end_after_tools: bool = False,
        project_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update the caller's own agent record identified by
        ``(user_sub, agent_id)``. Idempotent: calling twice with the
        same ``agent_id`` updates the existing row (bumping
        ``updated_at_ms``) rather than creating a duplicate — the
        unique constraint on ``(user_sub, agent_id)`` is what this
        method upserts against. Returns the persisted row as a plain
        dict."""

    @abstractmethod
    async def list_agents(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return a page of the caller's OWN (non-deleted) agent
        records, newest-first by ``updated_at_ms``, plus the opaque
        cursor for the next page (``None`` when there is no further
        page).

        MUST NEVER include another user's agent — the isolation guard
        the non-vacuity test neuters: drop the explicit ``user_sub``
        filter and another user's rows leak into the page.
        """

    @abstractmethod
    async def get_agent(
        self, user_context: UserContext, agent_id: str
    ) -> dict[str, Any] | None:
        """Return the caller's OWN agent record, or ``None`` if it
        doesn't exist, is soft-deleted, or belongs to another user (the
        three cases are indistinguishable from the caller's point of
        view — 404, never a 403 that would leak existence)."""

    @abstractmethod
    async def batch_get_agents(
        self, user_context: UserContext, agent_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Return the caller's OWN (non-deleted) agent records for the
        given ``agent_ids``, in the SAME order as ``agent_ids`` (any id
        not found/not owned/deleted is simply omitted — never raises,
        never leaks existence of another user's agent).

        MUST NEVER include another user's agent — same isolation guard
        as :meth:`get_agent`/:meth:`list_agents`, the non-vacuity test
        neuters the explicit ``user_sub`` filter here too.
        """

    @abstractmethod
    async def delete_agent(self, user_context: UserContext, agent_id: str) -> bool:
        """Soft-delete the caller's OWN agent record. Returns ``True``
        if a (previously non-deleted) record was found and deleted,
        ``False`` otherwise (not found/not owned/already deleted —
        idempotent no-op)."""


def _agent_to_dict(row: ConsoleAgent) -> dict[str, Any]:
    return {
        "agent_id": row.agent_id,
        "user_sub": row.user_sub,
        "name": row.name,
        "description": row.description,
        "instructions": row.instructions,
        "provider": row.provider,
        "model": row.model,
        "model_parameters": row.model_parameters_json,
        "tools": row.tools_json,
        "artifacts": row.artifacts_json,
        "end_after_tools": row.end_after_tools,
        "project_ids": row.project_ids_json,
        "created_at_ms": row.created_at_ms,
        "updated_at_ms": row.updated_at_ms,
        "deleted_at_ms": row.deleted_at_ms,
        "metadata": row.metadata_json,
    }


class PostgresConsoleAgentsService(ConsoleAgentsService):
    """PostgreSQL-backed console-agents service."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    @log_call(logger=logger)
    async def upsert_agent(
        self,
        user_context: UserContext,
        agent_id: str,
        *,
        name: str,
        description: str | None = None,
        instructions: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        tools: list[Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        end_after_tools: bool = False,
        project_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        async with self._session_factory() as session:
            try:
                existing = (
                    await session.execute(
                        select(ConsoleAgent)
                        # The isolation guard this upsert's non-vacuity
                        # test neuters — drop this filter and a hostile
                        # caller could update ANOTHER user's row by
                        # guessing their agent_id.
                        .filter(ConsoleAgent.user_sub == user_context.user_id)
                        .filter(ConsoleAgent.agent_id == agent_id)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    row = ConsoleAgent(
                        id=str(uuid.uuid4()),
                        agent_id=agent_id,
                        user_sub=user_context.user_id,
                        name=name,
                        description=description or "",
                        instructions=instructions,
                        provider=provider,
                        model=model,
                        model_parameters_json=model_parameters or {},
                        tools_json=tools or [],
                        artifacts_json=artifacts or {},
                        end_after_tools=end_after_tools,
                        project_ids_json=project_ids or [],
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
                    if instructions is not None:
                        row.instructions = instructions
                    if provider is not None:
                        row.provider = provider
                    if model is not None:
                        row.model = model
                    if model_parameters is not None:
                        row.model_parameters_json = model_parameters
                    if tools is not None:
                        row.tools_json = tools
                    if artifacts is not None:
                        row.artifacts_json = artifacts
                    row.end_after_tools = end_after_tools
                    if project_ids is not None:
                        row.project_ids_json = project_ids
                    if metadata is not None:
                        row.metadata_json = metadata
                    row.updated_at_ms = now
                await session.commit()
                result = _agent_to_dict(row)
            except Exception as exc:
                await session.rollback()
                raise RuntimeError(f"upsert_agent({agent_id!r}) failed: {exc}") from exc
        return result

    @log_call(logger=logger)
    async def list_agents(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        effective_limit = _clamp_limit(limit)
        async with self._session_factory() as session:
            stmt = (
                select(ConsoleAgent)
                # Isolation guard — the non-vacuity test neuters this
                # exact filter.
                .filter(ConsoleAgent.user_sub == user_context.user_id)
                .filter(ConsoleAgent.deleted_at_ms.is_(None))
            )
            if cursor is not None:
                cursor_updated_at_ms, cursor_agent_id = _decode_cursor(cursor)
                stmt = stmt.filter(
                    (ConsoleAgent.updated_at_ms < cursor_updated_at_ms)
                    | (
                        (ConsoleAgent.updated_at_ms == cursor_updated_at_ms)
                        & (ConsoleAgent.agent_id < cursor_agent_id)
                    )
                )
            stmt = stmt.order_by(
                ConsoleAgent.updated_at_ms.desc(),
                ConsoleAgent.agent_id.desc(),
            ).limit(effective_limit + 1)
            rows = (await session.execute(stmt)).scalars().all()
            plain = [_agent_to_dict(r) for r in rows]

        has_more = len(plain) > effective_limit
        page = plain[:effective_limit]
        next_cursor = (
            _encode_cursor(
                updated_at_ms=page[-1]["updated_at_ms"],
                agent_id=page[-1]["agent_id"],
            )
            if has_more and page
            else None
        )
        return page, next_cursor

    @log_call(logger=logger)
    async def get_agent(
        self, user_context: UserContext, agent_id: str
    ) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsoleAgent)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsoleAgent.user_sub == user_context.user_id)
                    .filter(ConsoleAgent.agent_id == agent_id)
                    .filter(ConsoleAgent.deleted_at_ms.is_(None))
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return _agent_to_dict(row)

    @log_call(logger=logger)
    async def batch_get_agents(
        self, user_context: UserContext, agent_ids: list[str]
    ) -> list[dict[str, Any]]:
        if not agent_ids:
            return []
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ConsoleAgent)
                        # Isolation guard — non-vacuity test neuters this
                        # exact filter (the whole point of the batch
                        # route: without it, a hostile caller could
                        # harvest ANY user's agents by guessing agent_ids
                        # in bulk).
                        .filter(ConsoleAgent.user_sub == user_context.user_id)
                        .filter(ConsoleAgent.agent_id.in_(agent_ids))
                        .filter(ConsoleAgent.deleted_at_ms.is_(None))
                    )
                )
                .scalars()
                .all()
            )
            # Serialise to plain dicts WHILE the session is still open
            # (#364 — an ORM instance must never be read after its
            # session closes).
            by_id = {r.agent_id: _agent_to_dict(r) for r in rows}
        # Preserve caller's requested order; silently omit misses (not
        # found/not owned/deleted) rather than raising — same
        # not-leak-existence discipline as get_agent's 404.
        return [by_id[aid] for aid in agent_ids if aid in by_id]

    @log_call(logger=logger)
    async def delete_agent(self, user_context: UserContext, agent_id: str) -> bool:
        now = _now_ms()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsoleAgent)
                    # Isolation guard — non-vacuity test neuters this.
                    .filter(ConsoleAgent.user_sub == user_context.user_id)
                    .filter(ConsoleAgent.agent_id == agent_id)
                    .filter(ConsoleAgent.deleted_at_ms.is_(None))
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.deleted_at_ms = now
            row.updated_at_ms = now
            await session.commit()
        return True


@dataclass
class _MockAgent:
    agent_id: str
    user_sub: str
    name: str
    description: str = ""
    instructions: str | None = None
    provider: str | None = None
    model: str | None = None
    model_parameters: dict[str, Any] = field(default_factory=dict)
    tools: list[Any] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    end_after_tools: bool = False
    project_ids: list[str] = field(default_factory=list)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    deleted_at_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "user_sub": self.user_sub,
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "provider": self.provider,
            "model": self.model,
            "model_parameters": self.model_parameters,
            "tools": self.tools,
            "artifacts": self.artifacts,
            "end_after_tools": self.end_after_tools,
            "project_ids": self.project_ids,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "deleted_at_ms": self.deleted_at_ms,
            "metadata": self.metadata,
        }


class MockConsoleAgentsService(ConsoleAgentsService):
    """In-process mock for unit tests that don't wire a Postgres factory
    (mirrors ``MockConsoleFilesService``'s shape)."""

    def __init__(self) -> None:
        self._agents: list[_MockAgent] = []

    def reset(self) -> None:
        self._agents.clear()

    def _find(self, user_sub: str, agent_id: str) -> _MockAgent | None:
        for a in self._agents:
            if (
                a.user_sub == user_sub
                and a.agent_id == agent_id
                and a.deleted_at_ms is None
            ):
                return a
        return None

    @log_call(logger=logger)
    async def upsert_agent(
        self,
        user_context: UserContext,
        agent_id: str,
        *,
        name: str,
        description: str | None = None,
        instructions: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        tools: list[Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        end_after_tools: bool = False,
        project_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        existing = self._find(user_context.user_id, agent_id)
        if existing is None:
            row = _MockAgent(
                agent_id=agent_id,
                user_sub=user_context.user_id,
                name=name,
                description=description or "",
                instructions=instructions,
                provider=provider,
                model=model,
                model_parameters=model_parameters or {},
                tools=tools or [],
                artifacts=artifacts or {},
                end_after_tools=end_after_tools,
                project_ids=project_ids or [],
                created_at_ms=now,
                updated_at_ms=now,
                metadata=metadata or {},
            )
            self._agents.append(row)
        else:
            row = existing
            row.name = name
            if description is not None:
                row.description = description
            if instructions is not None:
                row.instructions = instructions
            if provider is not None:
                row.provider = provider
            if model is not None:
                row.model = model
            if model_parameters is not None:
                row.model_parameters = model_parameters
            if tools is not None:
                row.tools = tools
            if artifacts is not None:
                row.artifacts = artifacts
            row.end_after_tools = end_after_tools
            if project_ids is not None:
                row.project_ids = project_ids
            if metadata is not None:
                row.metadata = metadata
            row.updated_at_ms = now
        return row.to_dict()

    @log_call(logger=logger)
    async def list_agents(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        effective_limit = _clamp_limit(limit)
        # Isolation guard — non-vacuity test neuters this filter.
        mine = [
            a
            for a in self._agents
            if a.user_sub == user_context.user_id and a.deleted_at_ms is None
        ]
        mine.sort(key=lambda a: (a.updated_at_ms, a.agent_id), reverse=True)
        if cursor is not None:
            cursor_updated_at_ms, cursor_agent_id = _decode_cursor(cursor)
            mine = [
                a
                for a in mine
                if (a.updated_at_ms, a.agent_id)
                < (cursor_updated_at_ms, cursor_agent_id)
            ]
        page = mine[: effective_limit + 1]
        has_more = len(page) > effective_limit
        page = page[:effective_limit]
        next_cursor = (
            _encode_cursor(
                updated_at_ms=page[-1].updated_at_ms,
                agent_id=page[-1].agent_id,
            )
            if has_more and page
            else None
        )
        return [a.to_dict() for a in page], next_cursor

    @log_call(logger=logger)
    async def get_agent(
        self, user_context: UserContext, agent_id: str
    ) -> dict[str, Any] | None:
        row = self._find(user_context.user_id, agent_id)
        return row.to_dict() if row is not None else None

    @log_call(logger=logger)
    async def batch_get_agents(
        self, user_context: UserContext, agent_ids: list[str]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for aid in agent_ids:
            # Isolation guard — non-vacuity test neuters this (via
            # ``_find``'s user_sub comparison).
            row = self._find(user_context.user_id, aid)
            if row is not None:
                results.append(row.to_dict())
        return results

    @log_call(logger=logger)
    async def delete_agent(self, user_context: UserContext, agent_id: str) -> bool:
        row = self._find(user_context.user_id, agent_id)
        if row is None:
            return False
        now = _now_ms()
        row.deleted_at_ms = now
        row.updated_at_ms = now
        return True
