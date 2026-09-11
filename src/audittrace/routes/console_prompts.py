"""Console-prompts routes — Mongo-repl WU-prompts of the MongoDB-
elimination EPIC.

The AuditTrace-side sovereign, RLS-isolated store + API for LibreChat's
prompts (the fork's Mongo ``PromptGroup``/``Prompt`` collections), per
the ratified spec (2026-09-11-SPEC-mongo-repl-wu-prompts-store.md).
Mirrors ``routes/console_conversations.py``'s shape (the spec's
instruction): a group resource (``ConsolePromptGroup``) with a nested
versions sub-resource (``ConsolePromptVersion``), same as
conversations/messages.

Mounted at ``/console/prompts`` (``server.py``). Every route requires
the ``memory:prompts:read-own`` or ``memory:prompts:write`` scope AND
resolves the caller's identity via ``require_user`` — ``user_sub`` is
ALWAYS taken from the resolved ``UserContext`` (token-derived), NEVER
from the request body
(feedback_never_trust_caller_metadata_for_security_fields): no request
model in ``audittrace.models`` even declares a ``user_sub``/``user_id``
field, so a hostile caller sending one in the JSON body has nothing to
bind to (Pydantic's default ``extra="ignore"`` silently drops it).

A caller can NEVER read/write/delete another sub's prompt group or
version — RLS (migration 025) at the infrastructure layer PLUS the
service's own explicit ``user_sub`` filter at the application layer
(the WU-4 cross-user hijack lesson,
feedback_per_user_namespace_shared_store_ids). A group/version that
exists but belongs to another user (or doesn't exist at all) both
return 404 — never 403, which would leak existence to a caller probing
IDs. **Completeness (the WU-2 lesson):** every route below carries the
``user_sub`` RLS filter through to the service call — there is no
route here that bypasses isolation, including the nested
versions/production endpoints (a version can only be attached to or
promoted within a group the caller owns).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security

from audittrace.auth import require_user, validate_jwt
from audittrace.dependencies import get_console_prompts_service
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.models import (
    ConsolePromptGroupItem,
    ConsolePromptGroupListResponse,
    ConsolePromptGroupUpsertRequest,
    ConsolePromptGroupWithVersionsItem,
    ConsolePromptSetProductionRequest,
    ConsolePromptVersionItem,
    ConsolePromptVersionUpsertRequest,
)
from audittrace.services.console_prompts import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_SCOPE = "memory:prompts:read-own"
_WRITE_SCOPE = "memory:prompts:write"


def _group_item(row: dict[str, Any]) -> ConsolePromptGroupItem:
    return ConsolePromptGroupItem(**row)


def _group_with_versions_item(
    row: dict[str, Any],
) -> ConsolePromptGroupWithVersionsItem:
    versions = row.get("versions", [])
    group_fields = {k: v for k, v in row.items() if k != "versions"}
    return ConsolePromptGroupWithVersionsItem(
        **group_fields,
        versions=[ConsolePromptVersionItem(**v) for v in versions],
    )


@router.post("", response_model=ConsolePromptGroupItem)
@log_call(logger=logger)
async def upsert_group(
    body: ConsolePromptGroupUpsertRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsolePromptGroupItem:
    """Create or update the CALLER's OWN prompt group (upsert by
    ``group_id``). ``user_sub`` is stamped from ``user`` — the body
    carries no such field to override it with."""
    service = get_console_prompts_service()
    row = await service.upsert_group(
        user,
        body.group_id,
        name=body.name,
        category=body.category,
        oneliner=body.oneliner,
        command=body.command,
        metadata=body.metadata,
    )
    return _group_item(row)


@router.get("", response_model=ConsolePromptGroupListResponse)
@log_call(logger=logger)
async def list_groups(
    cursor: str | None = Query(None, description="Opaque pagination cursor."),
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsolePromptGroupListResponse:
    """List the CALLER's OWN (non-deleted) prompt groups, newest-first,
    cursor-paginated (summary shape, no versions per row). Never
    returns another user's group — RLS plus the service's own explicit
    ``user_sub`` filter."""
    service = get_console_prompts_service()
    try:
        items, next_cursor = await service.list_groups(user, cursor=cursor, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ConsolePromptGroupListResponse(
        items=[_group_item(row) for row in items],
        next_cursor=next_cursor,
    )


@router.get("/{group_id}", response_model=ConsolePromptGroupWithVersionsItem)
@log_call(logger=logger)
async def get_group(
    group_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsolePromptGroupWithVersionsItem:
    """Fetch the CALLER's OWN prompt group INCLUDING its full version
    history. 404 if it doesn't exist, is deleted, or belongs to
    another user — never 403 (would leak existence to a caller probing
    foreign group ids)."""
    service = get_console_prompts_service()
    row = await service.get_group(user, group_id)
    if row is None:
        raise HTTPException(status_code=404, detail="prompt group not found")
    return _group_with_versions_item(row)


@router.delete("/{group_id}", status_code=204)
@log_call(logger=logger)
async def delete_group(
    group_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> None:
    """Soft-delete the CALLER's OWN prompt group AND its versions. 404
    if not found/not owned/already deleted."""
    service = get_console_prompts_service()
    deleted = await service.delete_group(user, group_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="prompt group not found")


@router.post(
    "/{group_id}/versions",
    response_model=ConsolePromptVersionItem,
)
@log_call(logger=logger)
async def upsert_version(
    group_id: str,
    body: ConsolePromptVersionUpsertRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsolePromptVersionItem:
    """Create or update the CALLER's OWN prompt version (upsert by
    ``prompt_id``), attached to the CALLER's OWN ``group_id``. 404 if
    the group doesn't exist/isn't owned by the caller — a hostile
    caller can never attach a version to another user's group by
    guessing its ``group_id``."""
    service = get_console_prompts_service()
    row = await service.upsert_version(
        user,
        group_id,
        body.prompt_id,
        text=body.text,
        type=body.type,
        metadata=body.metadata,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="prompt group not found")
    return ConsolePromptVersionItem(**row)


@router.patch(
    "/{group_id}/production",
    response_model=ConsolePromptGroupWithVersionsItem,
)
@log_call(logger=logger)
async def set_production(
    group_id: str,
    body: ConsolePromptSetProductionRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsolePromptGroupWithVersionsItem:
    """Mark ``body.prompt_id`` as the production version of the
    CALLER's OWN ``group_id``. 404 if the group doesn't exist/isn't
    owned, OR the named version doesn't exist/isn't owned/doesn't
    belong to this group — a hostile caller can never point
    ``production_prompt_id`` at another user's version or at a
    version from a different group."""
    service = get_console_prompts_service()
    row = await service.set_production(user, group_id, body.prompt_id)
    if row is None:
        raise HTTPException(status_code=404, detail="prompt group or version not found")
    return _group_with_versions_item(row)
