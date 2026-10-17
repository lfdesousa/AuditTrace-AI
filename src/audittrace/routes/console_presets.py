"""Console-presets routes — WU-presets of the MongoDB-elimination EPIC.

The AuditTrace-side sovereign, RLS-isolated store + API for LibreChat's
saved model/endpoint presets (the fork's Mongo ``Preset`` collection),
per the ratified spec
(2026-09-11-SPEC-mongo-repl-wu-presets-store.md). Mirrors
``routes/console_conversations.py``'s shape EXACTLY (the spec's
instruction), scaled down to a single resource (no message tree).

Mounted at ``/console/presets`` (``server.py``). Every route requires
the ``memory:presets:read-own`` or ``memory:presets:write`` scope AND
resolves the caller's identity via ``require_user`` — ``user_sub`` is
ALWAYS taken from the resolved ``UserContext`` (token-derived), NEVER
from the request body
(feedback_never_trust_caller_metadata_for_security_fields): no request
model in ``audittrace.models`` even declares a ``user_sub``/``user_id``
field, so a hostile caller sending one in the JSON body has nothing to
bind to (Pydantic's default ``extra="ignore"`` silently drops it).

A caller can NEVER read/write/delete another sub's preset — RLS
(migration 024) at the infrastructure layer PLUS the service's own
explicit ``user_sub`` filter at the application layer (the WU-4
cross-user hijack lesson,
feedback_per_user_namespace_shared_store_ids). A preset that exists but
belongs to another user (or doesn't exist at all) both return 404 —
never 403, which would leak existence to a caller probing IDs.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security

from audittrace.auth import require_user, validate_jwt
from audittrace.dependencies import get_console_presets_service
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.models import (
    ConsolePresetItem,
    ConsolePresetListResponse,
    ConsolePresetUpsertRequest,
)
from audittrace.services.console_presets import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_SCOPE = "memory:presets:read-own"
_WRITE_SCOPE = "memory:presets:write"


def _preset_item(row: dict[str, Any]) -> ConsolePresetItem:
    return ConsolePresetItem(**row)


@router.post("", response_model=ConsolePresetItem)
@log_call(logger=logger)
async def upsert_preset(
    body: ConsolePresetUpsertRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsolePresetItem:
    """Create or update the CALLER's OWN preset (upsert by
    ``preset_id``). ``user_sub`` is stamped from ``user`` — the body
    carries no such field to override it with."""
    service = get_console_presets_service()
    row = await service.upsert_preset(
        user,
        body.preset_id,
        title=body.title,
        data=body.data,
        metadata=body.metadata,
    )
    return _preset_item(row)


@router.get("", response_model=ConsolePresetListResponse)
@log_call(logger=logger)
async def list_presets(
    cursor: str | None = Query(None, description="Opaque pagination cursor."),
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsolePresetListResponse:
    """List the CALLER's OWN (non-deleted) presets, newest-first,
    cursor-paginated. Never returns another user's preset — RLS plus
    the service's own explicit ``user_sub`` filter."""
    service = get_console_presets_service()
    try:
        items, next_cursor = await service.list_presets(
            user, cursor=cursor, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ConsolePresetListResponse(
        items=[_preset_item(row) for row in items],
        next_cursor=next_cursor,
    )


@router.get("/{preset_id}", response_model=ConsolePresetItem)
@log_call(logger=logger)
async def get_preset(
    preset_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsolePresetItem:
    """Fetch the CALLER's OWN preset. 404 if it doesn't exist, is
    deleted, or belongs to another user — never 403 (would leak
    existence to a caller probing foreign preset ids)."""
    service = get_console_presets_service()
    row = await service.get_preset(user, preset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="preset not found")
    return _preset_item(row)


@router.delete("/{preset_id}", status_code=204)
@log_call(logger=logger)
async def delete_preset(
    preset_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> None:
    """Soft-delete the CALLER's OWN preset. 404 if not found/not
    owned/already deleted."""
    service = get_console_presets_service()
    deleted = await service.delete_preset(user, preset_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="preset not found")
