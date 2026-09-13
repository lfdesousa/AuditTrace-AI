"""Console-tool-favorites routes — the Tool-Favorites domain of the
MongoDB-elimination EPIC.

The AuditTrace-side sovereign, RLS-isolated store + API for LibreChat's
``ToolFavorite`` record (the fork's Mongo ``ToolFavorite`` collection),
per the ratified spec
(2026-09-13-SPEC-mongo-repl-wu-tool-favorites-store.md). Mirrors
``routes/console_conversation_tags.py``'s shape EXACTLY (the spec's
instruction), except this domain exposes only the three operations the
spec names — list / add(upsert) / remove(delete) — no standalone
get-by-key route (the fork's own ``methods/favorite.ts`` never exposes
one either).

Mounted at ``/console/tool-favorites`` (``server.py``). Every route
requires the ``memory:tool_favorites:read-own`` or
``memory:tool_favorites:write`` scope AND resolves the caller's
identity via ``require_user`` — ``user_sub`` is ALWAYS taken from the
resolved ``UserContext`` (token-derived), NEVER from the request body
(feedback_never_trust_caller_metadata_for_security_fields): no request
model in ``audittrace.models`` even declares a ``user_sub``/``user_id``
field, so a hostile caller sending one in the JSON body has nothing to
bind to (Pydantic's default ``extra="ignore"`` silently drops it).

A caller can NEVER read/write/delete another sub's favorite — RLS
(migration 030) at the infrastructure layer PLUS the service's own
explicit ``user_sub`` filter at the application layer (the WU-4
cross-user hijack lesson,
feedback_per_user_namespace_shared_store_ids).

``{item_id:path}`` on the delete route: ``item_id`` is a
CLIENT-SUPPLIED free-form string (e.g. an MCP-qualified tool name) and
may itself contain a ``/``, so the route param must accept path
segments, not just a single path component (same rationale as
``routes/console_conversation_tags.py``'s ``{tag:path}``).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Security

from audittrace.auth import require_user, validate_jwt
from audittrace.dependencies import get_console_tool_favorites_service
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.models import (
    ConsoleToolFavoriteAddRequest,
    ConsoleToolFavoriteItem,
    ConsoleToolFavoriteListResponse,
)
from audittrace.services.console_tool_favorites import ToolFavoritesCapExceededError

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_SCOPE = "memory:tool_favorites:read-own"
_WRITE_SCOPE = "memory:tool_favorites:write"


def _tool_favorite_item(row: dict[str, Any]) -> ConsoleToolFavoriteItem:
    return ConsoleToolFavoriteItem(**row)


@router.post("", response_model=ConsoleToolFavoriteItem)
@log_call(logger=logger)
async def add_tool_favorite(
    body: ConsoleToolFavoriteAddRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleToolFavoriteItem:
    """Add (or idempotently re-affirm) the CALLER's OWN tool-favorite.
    ``user_sub`` is stamped from ``user`` — the body carries no such
    field to override it with. 409 if the caller already owns
    ``MAX_TOOL_FAVORITES`` active favorites and this pair is a new one."""
    service = get_console_tool_favorites_service()
    try:
        row = await service.add_tool_favorite(
            user,
            body.item_type,
            body.item_id,
            tenant_id=body.tenant_id,
            metadata=body.metadata,
        )
    except ToolFavoritesCapExceededError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _tool_favorite_item(row)


@router.get("", response_model=ConsoleToolFavoriteListResponse)
@log_call(logger=logger)
async def list_tool_favorites(
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleToolFavoriteListResponse:
    """List ALL of the CALLER's OWN (non-deleted) tool-favorites,
    oldest-first. Never returns another user's favorite — RLS plus the
    service's own explicit ``user_sub`` filter."""
    service = get_console_tool_favorites_service()
    items = await service.list_tool_favorites(user)
    return ConsoleToolFavoriteListResponse(
        items=[_tool_favorite_item(row) for row in items]
    )


@router.delete("/{item_type}/{item_id:path}", status_code=204)
@log_call(logger=logger)
async def remove_tool_favorite(
    item_type: str,
    item_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> None:
    """Soft-delete the CALLER's OWN tool-favorite. 404 if not
    found/not owned/already removed."""
    service = get_console_tool_favorites_service()
    removed = await service.remove_tool_favorite(user, item_type, item_id)
    if not removed:
        raise HTTPException(status_code=404, detail="tool favorite not found")
