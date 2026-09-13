"""Console-conversation-tags routes — the Conversation-Tags domain of
the MongoDB-elimination EPIC.

The AuditTrace-side sovereign, RLS-isolated store + API for LibreChat's
``conversationTag`` record (the fork's Mongo ``ConversationTag``
collection), per the ratified spec
(2026-09-13-SPEC-mongo-repl-wu-conversation-tags-store.md). Mirrors
``routes/console_chat_projects.py``'s shape EXACTLY (the spec's
instruction), a single flat resource (no message tree, no group+versions
split, no batch-get shape).

Mounted at ``/console/conversation-tags`` (``server.py``). Every route
requires the ``memory:conversation_tags:read-own`` or
``memory:conversation_tags:write`` scope AND resolves the caller's
identity via ``require_user`` — ``user_sub`` is ALWAYS taken from the
resolved ``UserContext`` (token-derived), NEVER from the request body
(feedback_never_trust_caller_metadata_for_security_fields): no request
model in ``audittrace.models`` even declares a ``user_sub``/``user_id``
field, so a hostile caller sending one in the JSON body has nothing to
bind to (Pydantic's default ``extra="ignore"`` silently drops it).

A caller can NEVER read/write/delete another sub's conversation-tag —
RLS (migration 029) at the infrastructure layer PLUS the service's own
explicit ``user_sub`` filter at the application layer (the WU-4
cross-user hijack lesson,
feedback_per_user_namespace_shared_store_ids). A conversation-tag that
exists but belongs to another user (or doesn't exist at all) both
return 404 — never 403, which would leak existence to a caller probing
tag names.

``{tag:path}`` — ``:path`` on the get/delete route params: a tag is a
CLIENT-SUPPLIED free-form string (unlike the plain ``chat_project_id``
this route shape otherwise mirrors) and may itself contain a ``/``, so
the route param must accept path segments, not just a single path
component (same rationale as ``routes/console_agents.py``'s
``{agent_id:path}``).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security

from audittrace.auth import require_user, validate_jwt
from audittrace.dependencies import get_console_conversation_tags_service
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.models import (
    ConsoleConversationTagItem,
    ConsoleConversationTagListResponse,
    ConsoleConversationTagUpsertRequest,
)
from audittrace.services.console_conversation_tags import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_SCOPE = "memory:conversation_tags:read-own"
_WRITE_SCOPE = "memory:conversation_tags:write"


def _conversation_tag_item(row: dict[str, Any]) -> ConsoleConversationTagItem:
    return ConsoleConversationTagItem(**row)


@router.post("", response_model=ConsoleConversationTagItem)
@log_call(logger=logger)
async def upsert_conversation_tag(
    body: ConsoleConversationTagUpsertRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleConversationTagItem:
    """Create or update the CALLER's OWN conversation-tag (upsert by
    ``tag``). ``user_sub`` is stamped from ``user`` — the body carries
    no such field to override it with."""
    service = get_console_conversation_tags_service()
    row = await service.upsert_conversation_tag(
        user,
        body.tag,
        description=body.description,
        count=body.count,
        position=body.position,
        metadata=body.metadata,
    )
    return _conversation_tag_item(row)


@router.get("", response_model=ConsoleConversationTagListResponse)
@log_call(logger=logger)
async def list_conversation_tags(
    cursor: str | None = Query(None, description="Opaque pagination cursor."),
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleConversationTagListResponse:
    """List the CALLER's OWN (non-deleted) conversation-tags, newest-
    first, cursor-paginated. Never returns another user's tag — RLS
    plus the service's own explicit ``user_sub`` filter."""
    service = get_console_conversation_tags_service()
    try:
        items, next_cursor = await service.list_conversation_tags(
            user, cursor=cursor, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ConsoleConversationTagListResponse(
        items=[_conversation_tag_item(row) for row in items],
        next_cursor=next_cursor,
    )


@router.get("/{tag:path}", response_model=ConsoleConversationTagItem)
@log_call(logger=logger)
async def get_conversation_tag(
    tag: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleConversationTagItem:
    """Fetch the CALLER's OWN conversation-tag. 404 if it doesn't exist,
    is deleted, or belongs to another user — never 403 (would leak
    existence to a caller probing foreign tag names)."""
    service = get_console_conversation_tags_service()
    row = await service.get_conversation_tag(user, tag)
    if row is None:
        raise HTTPException(status_code=404, detail="conversation tag not found")
    return _conversation_tag_item(row)


@router.delete("/{tag:path}", status_code=204)
@log_call(logger=logger)
async def delete_conversation_tag(
    tag: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> None:
    """Soft-delete the CALLER's OWN conversation-tag. 404 if not
    found/not owned/already deleted."""
    service = get_console_conversation_tags_service()
    deleted = await service.delete_conversation_tag(user, tag)
    if not deleted:
        raise HTTPException(status_code=404, detail="conversation tag not found")
