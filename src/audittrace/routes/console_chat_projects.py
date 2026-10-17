"""Console-chat-projects routes — the Chat-Projects domain of the
MongoDB-elimination EPIC.

The AuditTrace-side sovereign, RLS-isolated store + API for LibreChat's
first-class chat-projects (the fork's Mongo ``ChatProject`` collection),
per the ratified spec
(2026-09-11-SPEC-mongo-repl-wu-chatprojects-store.md). Mirrors
``routes/console_presets.py``'s shape EXACTLY (the spec's instruction),
a single flat resource (no message tree, no group+versions split).

Mounted at ``/console/chat-projects`` (``server.py``). Every route
requires the ``memory:chat_projects:read-own`` or
``memory:chat_projects:write`` scope AND resolves the caller's identity
via ``require_user`` — ``user_sub`` is ALWAYS taken from the resolved
``UserContext`` (token-derived), NEVER from the request body
(feedback_never_trust_caller_metadata_for_security_fields): no request
model in ``audittrace.models`` even declares a ``user_sub``/``user_id``
field, so a hostile caller sending one in the JSON body has nothing to
bind to (Pydantic's default ``extra="ignore"`` silently drops it).

A caller can NEVER read/write/delete another sub's chat-project — RLS
(migration 026) at the infrastructure layer PLUS the service's own
explicit ``user_sub`` filter at the application layer (the WU-4
cross-user hijack lesson,
feedback_per_user_namespace_shared_store_ids). A chat-project that
exists but belongs to another user (or doesn't exist at all) both
return 404 — never 403, which would leak existence to a caller probing
IDs.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security

from audittrace.auth import require_user, validate_jwt
from audittrace.dependencies import get_console_chat_projects_service
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.models import (
    ConsoleChatProjectItem,
    ConsoleChatProjectListResponse,
    ConsoleChatProjectUpsertRequest,
)
from audittrace.services.console_chat_projects import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_SCOPE = "memory:chat_projects:read-own"
_WRITE_SCOPE = "memory:chat_projects:write"


def _chat_project_item(row: dict[str, Any]) -> ConsoleChatProjectItem:
    return ConsoleChatProjectItem(**row)


@router.post("", response_model=ConsoleChatProjectItem)
@log_call(logger=logger)
async def upsert_chat_project(
    body: ConsoleChatProjectUpsertRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleChatProjectItem:
    """Create or update the CALLER's OWN chat-project (upsert by
    ``chat_project_id``). ``user_sub`` is stamped from ``user`` — the
    body carries no such field to override it with."""
    service = get_console_chat_projects_service()
    row = await service.upsert_chat_project(
        user,
        body.chat_project_id,
        name=body.name,
        description=body.description,
        metadata=body.metadata,
    )
    return _chat_project_item(row)


@router.get("", response_model=ConsoleChatProjectListResponse)
@log_call(logger=logger)
async def list_chat_projects(
    cursor: str | None = Query(None, description="Opaque pagination cursor."),
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleChatProjectListResponse:
    """List the CALLER's OWN (non-deleted) chat-projects, newest-first,
    cursor-paginated. Never returns another user's chat-project — RLS
    plus the service's own explicit ``user_sub`` filter."""
    service = get_console_chat_projects_service()
    try:
        items, next_cursor = await service.list_chat_projects(
            user, cursor=cursor, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ConsoleChatProjectListResponse(
        items=[_chat_project_item(row) for row in items],
        next_cursor=next_cursor,
    )


@router.get("/{chat_project_id}", response_model=ConsoleChatProjectItem)
@log_call(logger=logger)
async def get_chat_project(
    chat_project_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleChatProjectItem:
    """Fetch the CALLER's OWN chat-project. 404 if it doesn't exist, is
    deleted, or belongs to another user — never 403 (would leak
    existence to a caller probing foreign chat-project ids)."""
    service = get_console_chat_projects_service()
    row = await service.get_chat_project(user, chat_project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="chat project not found")
    return _chat_project_item(row)


@router.delete("/{chat_project_id}", status_code=204)
@log_call(logger=logger)
async def delete_chat_project(
    chat_project_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> None:
    """Soft-delete the CALLER's OWN chat-project. 404 if not found/not
    owned/already deleted."""
    service = get_console_chat_projects_service()
    deleted = await service.delete_chat_project(user, chat_project_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="chat project not found")
