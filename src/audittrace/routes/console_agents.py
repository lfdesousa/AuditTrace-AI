"""Console-agents routes — the Agents domain of the MongoDB-elimination
EPIC.

The AuditTrace-side sovereign, RLS-isolated store + API for LibreChat's
Agent record (the fork's Mongo ``Agent`` collection), per the ratified
spec (2026-09-12-SPEC-mongo-repl-wu-agents-store.md). Mirrors
``routes/console_files.py``'s shape EXACTLY (the spec's instruction),
including its extra read shape: batch-get-by-ids.

**Own-agents-only v1.** Agent SHARING / marketplace (a fork ``author``/
global-agent concept) is explicitly OUT OF SCOPE — every route here
resolves and mutates ONLY the caller's own rows.

Mounted at ``/console/agents`` (``server.py``). Every route requires
the ``memory:agents:read-own`` or ``memory:agents:write`` scope AND
resolves the caller's identity via ``require_user`` — ``user_sub`` is
ALWAYS taken from the resolved ``UserContext`` (token-derived), NEVER
from the request body
(feedback_never_trust_caller_metadata_for_security_fields): no request
model in ``audittrace.models`` even declares a ``user_sub``/``user_id``
field, so a hostile caller sending one in the JSON body has nothing to
bind to (Pydantic's default ``extra="ignore"`` silently drops it).

A caller can NEVER read/write/delete another sub's agent — RLS
(migration 028) at the infrastructure layer PLUS the service's own
explicit ``user_sub`` filter at the application layer (the WU-4
cross-user hijack lesson,
feedback_per_user_namespace_shared_store_ids). An agent that exists but
belongs to another user (or doesn't exist at all) both return 404 —
never 403, which would leak existence to a caller probing IDs.
``POST /console/agents/batch-get`` carries the same discipline: any
requested ``agent_id`` not found/not owned/deleted is silently omitted
from the response, never raises, never distinguishes "not mine" from
"never existed".
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security

from audittrace.auth import require_user, validate_jwt
from audittrace.dependencies import get_console_agents_service
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.models import (
    ConsoleAgentBatchGetRequest,
    ConsoleAgentBatchGetResponse,
    ConsoleAgentItem,
    ConsoleAgentListResponse,
    ConsoleAgentUpsertRequest,
)
from audittrace.services.console_agents import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_SCOPE = "memory:agents:read-own"
_WRITE_SCOPE = "memory:agents:write"


def _agent_item(row: dict[str, Any]) -> ConsoleAgentItem:
    return ConsoleAgentItem(**row)


@router.post("", response_model=ConsoleAgentItem)
@log_call(logger=logger)
async def upsert_agent(
    body: ConsoleAgentUpsertRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleAgentItem:
    """Create or update the CALLER's OWN agent (upsert by ``agent_id``).
    ``user_sub`` is stamped from ``user`` — the body carries no such
    field to override it with."""
    service = get_console_agents_service()
    row = await service.upsert_agent(
        user,
        body.agent_id,
        name=body.name,
        description=body.description,
        instructions=body.instructions,
        provider=body.provider,
        model=body.model,
        model_parameters=body.model_parameters,
        tools=body.tools,
        artifacts=body.artifacts,
        end_after_tools=body.end_after_tools,
        project_ids=body.project_ids,
        metadata=body.metadata,
    )
    return _agent_item(row)


@router.get("", response_model=ConsoleAgentListResponse)
@log_call(logger=logger)
async def list_agents(
    cursor: str | None = Query(None, description="Opaque pagination cursor."),
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleAgentListResponse:
    """List the CALLER's OWN (non-deleted) agents, newest-first,
    cursor-paginated. Never returns another user's agent — RLS plus the
    service's own explicit ``user_sub`` filter."""
    service = get_console_agents_service()
    try:
        items, next_cursor = await service.list_agents(user, cursor=cursor, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ConsoleAgentListResponse(
        items=[_agent_item(row) for row in items],
        next_cursor=next_cursor,
    )


@router.post("/batch-get", response_model=ConsoleAgentBatchGetResponse)
@log_call(logger=logger)
async def batch_get_agents(
    body: ConsoleAgentBatchGetRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleAgentBatchGetResponse:
    """Fetch the CALLER's OWN agents for a batch of ``agent_id``s in one
    round trip. A POST (not a GET with repeated query params) so the
    request body — not the URL — carries the (potentially large) id
    list; this is a READ operation despite the verb (same rationale as
    the console-files batch-get route), gated on the READ scope, never
    the write scope."""
    service = get_console_agents_service()
    items = await service.batch_get_agents(user, body.agent_ids)
    return ConsoleAgentBatchGetResponse(items=[_agent_item(row) for row in items])


@router.get("/{agent_id:path}", response_model=ConsoleAgentItem)
@log_call(logger=logger)
async def get_agent(
    agent_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleAgentItem:
    """Fetch the CALLER's OWN agent. 404 if it doesn't exist, is
    deleted, or belongs to another user — never 403 (would leak
    existence to a caller probing foreign agent ids). ``:path`` on the
    route param — LibreChat agent ids can carry a ``/`` (e.g. provider-
    prefixed ids)."""
    service = get_console_agents_service()
    row = await service.get_agent(user, agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return _agent_item(row)


@router.delete("/{agent_id:path}", status_code=204)
@log_call(logger=logger)
async def delete_agent(
    agent_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> None:
    """Soft-delete the CALLER's OWN agent. 404 if not found/not
    owned/already deleted."""
    service = get_console_agents_service()
    deleted = await service.delete_agent(user, agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="agent not found")
