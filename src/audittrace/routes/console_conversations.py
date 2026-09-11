"""Console-conversations routes — WU-1 of the MongoDB-elimination EPIC.

The AuditTrace-side sovereign, RLS-isolated store + API for LibreChat's
conversations + messages (the fork's Mongo ``Conversation``/``Message``
collections), per the ratified spec
(2026-09-11-SPEC-mongo-repl-wu1-console-conversations-store.md).

Mounted at ``/console/conversations`` (``server.py``). Every route
requires the ``memory:conversations:read-own`` or
``memory:conversations:write`` scope AND resolves the caller's identity
via ``require_user`` — ``user_sub`` is ALWAYS taken from the resolved
``UserContext`` (token-derived), NEVER from the request body
(feedback_never_trust_caller_metadata_for_security_fields): no request
model in ``audittrace.models`` even declares a ``user_sub``/``user_id``
field, so a hostile caller sending one in the JSON body has nothing to
bind to (Pydantic's default ``extra="ignore"`` silently drops it).

A caller can NEVER read/write/delete another sub's conversation or
message — RLS (migration 023) at the infrastructure layer PLUS the
service's own explicit ``user_sub`` filter at the application layer
(the WU-4 cross-user hijack lesson,
feedback_per_user_namespace_shared_store_ids). A conversation/message
that exists but belongs to another user (or doesn't exist at all) both
return 404 — never 403, which would leak existence to a caller probing
IDs.

Distinct from the ``/memory/conversational`` routes
(``routes/memory.py``): that surface reads the OLDER per-session
SUMMARY layer (one row per chat session); this is the message-TREE
store the console's sidebar/history renders from (see the WU-1 spec's
"EPIC CORRECTION" for why a new store was needed).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security

from audittrace.auth import require_user, validate_jwt
from audittrace.dependencies import get_console_conversations_service
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.models import (
    ConsoleConversationItem,
    ConsoleConversationListResponse,
    ConsoleConversationTitleUpdateRequest,
    ConsoleConversationUpsertRequest,
    ConsoleMessageEditRequest,
    ConsoleMessageItem,
    ConsoleMessageListResponse,
    ConsoleMessageUpsertRequest,
)
from audittrace.services.console_conversations import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_SCOPE = "memory:conversations:read-own"
_WRITE_SCOPE = "memory:conversations:write"


def _conversation_item(row: dict[str, Any]) -> ConsoleConversationItem:
    return ConsoleConversationItem(**row)


def _message_item(row: dict[str, Any]) -> ConsoleMessageItem:
    return ConsoleMessageItem(**row)


@router.post("", response_model=ConsoleConversationItem)
@log_call(logger=logger)
async def upsert_conversation(
    body: ConsoleConversationUpsertRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleConversationItem:
    """Create or update the CALLER's OWN conversation (upsert by
    ``conversation_id``). ``user_sub`` is stamped from ``user`` — the
    body carries no such field to override it with."""
    service = get_console_conversations_service()
    row = await service.upsert_conversation(
        user,
        body.conversation_id,
        title=body.title,
        endpoint=body.endpoint,
        model=body.model,
        is_temporary=body.is_temporary,
        agent_id=body.agent_id,
        chat_project_id=body.chat_project_id,
        metadata=body.metadata,
    )
    return _conversation_item(row)


@router.get("", response_model=ConsoleConversationListResponse)
@log_call(logger=logger)
async def list_conversations(
    cursor: str | None = Query(None, description="Opaque pagination cursor."),
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleConversationListResponse:
    """List the CALLER's OWN (non-deleted) conversations, newest-first,
    cursor-paginated. Never returns another user's conversation — RLS
    plus the service's own explicit ``user_sub`` filter."""
    service = get_console_conversations_service()
    try:
        items, next_cursor = await service.list_conversations(
            user, cursor=cursor, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ConsoleConversationListResponse(
        items=[_conversation_item(row) for row in items],
        next_cursor=next_cursor,
    )


@router.get("/{conversation_id}", response_model=ConsoleConversationItem)
@log_call(logger=logger)
async def get_conversation(
    conversation_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleConversationItem:
    """Fetch the CALLER's OWN conversation. 404 if it doesn't exist, is
    deleted, or belongs to another user — never 403 (would leak
    existence to a caller probing foreign conversation ids)."""
    service = get_console_conversations_service()
    row = await service.get_conversation(user, conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _conversation_item(row)


@router.patch("/{conversation_id}", response_model=ConsoleConversationItem)
@log_call(logger=logger)
async def update_conversation_title(
    conversation_id: str,
    body: ConsoleConversationTitleUpdateRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleConversationItem:
    """Rename the CALLER's OWN conversation. 404 if not found/not owned."""
    service = get_console_conversations_service()
    row = await service.update_conversation_title(user, conversation_id, body.title)
    if row is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _conversation_item(row)


@router.delete("/{conversation_id}", status_code=204)
@log_call(logger=logger)
async def delete_conversation(
    conversation_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> None:
    """Soft-delete the CALLER's OWN conversation AND its messages. 404 if
    not found/not owned/already deleted."""
    service = get_console_conversations_service()
    deleted = await service.delete_conversation(user, conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="conversation not found")


@router.get(
    "/{conversation_id}/messages",
    response_model=ConsoleMessageListResponse,
)
@log_call(logger=logger)
async def get_messages(
    conversation_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleMessageListResponse:
    """Return the full message TREE for the CALLER's OWN conversation,
    chronological (``created_at_ms`` ASC). 404 if the conversation
    itself doesn't exist/isn't owned by the caller — checked via
    :func:`get_conversation` first so an empty-but-owned conversation
    (200, empty list) is distinguishable from a not-found/foreign one
    (404)."""
    service = get_console_conversations_service()
    conversation = await service.get_conversation(user, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    rows = await service.get_messages(user, conversation_id)
    return ConsoleMessageListResponse(items=[_message_item(row) for row in rows])


@router.post(
    "/{conversation_id}/messages",
    response_model=ConsoleMessageItem,
)
@log_call(logger=logger)
async def upsert_message(
    conversation_id: str,
    body: ConsoleMessageUpsertRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleMessageItem:
    """Create or update the CALLER's OWN message (upsert by
    ``message_id``). ``user_sub`` is stamped from ``user`` — the body
    carries no such field to override it with."""
    service = get_console_conversations_service()
    row = await service.upsert_message(
        user,
        conversation_id,
        body.message_id,
        sender=body.sender,
        text=body.text,
        is_created_by_user=body.is_created_by_user,
        parent_message_id=body.parent_message_id,
        model=body.model,
        endpoint=body.endpoint,
        token_count=body.token_count,
        error=body.error,
        metadata=body.metadata,
    )
    return _message_item(row)


@router.patch(
    "/{conversation_id}/messages/{message_id}",
    response_model=ConsoleMessageItem,
)
@log_call(logger=logger)
async def edit_message(
    conversation_id: str,
    message_id: str,
    body: ConsoleMessageEditRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleMessageItem:
    """Edit the CALLER's OWN message. 404 if not found/not owned."""
    service = get_console_conversations_service()
    row = await service.edit_message(
        user, conversation_id, message_id, text=body.text, metadata=body.metadata
    )
    if row is None:
        raise HTTPException(status_code=404, detail="message not found")
    return _message_item(row)


@router.delete("/{conversation_id}/messages/{message_id}", status_code=204)
@log_call(logger=logger)
async def delete_message(
    conversation_id: str,
    message_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> None:
    """Delete the CALLER's OWN message. 404 if not found/not owned."""
    service = get_console_conversations_service()
    deleted = await service.delete_message(user, conversation_id, message_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="message not found")
