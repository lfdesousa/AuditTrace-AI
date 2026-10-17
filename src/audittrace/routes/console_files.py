"""Console-files routes — the Files-metadata domain of the
MongoDB-elimination EPIC.

The AuditTrace-side sovereign, RLS-isolated store + API for LibreChat's
file METADATA record (the fork's Mongo ``File`` collection), per the
ratified spec
(2026-09-11-SPEC-mongo-repl-wu-files-metadata-store.md). Mirrors
``routes/console_chat_projects.py``'s shape EXACTLY (the spec's
instruction), plus one additional read shape this domain needs:
batch-get-by-ids.

**Scope boundary — metadata only.** This mount owns the file METADATA
record; the file BYTES stay in object storage (S3/MinIO,
``feedback_storage_always_s3``). No route here accepts or returns byte
content — only the record that references it. The existing ephemeral
file-INGEST path (``POST /console/files`` on the BFF,
``POST /memory/upload`` on this orchestrator) is a completely
different, pre-existing concern and is untouched by this module.

Mounted at ``/console/files`` (``server.py``). Every route requires the
``memory:files:read-own`` or ``memory:files:write`` scope AND resolves
the caller's identity via ``require_user`` — ``user_sub`` is ALWAYS
taken from the resolved ``UserContext`` (token-derived), NEVER from the
request body (feedback_never_trust_caller_metadata_for_security_fields):
no request model in ``audittrace.models`` even declares a
``user_sub``/``user_id`` field, so a hostile caller sending one in the
JSON body has nothing to bind to (Pydantic's default ``extra="ignore"``
silently drops it).

A caller can NEVER read/write/delete another sub's file-metadata record
— RLS (migration 027) at the infrastructure layer PLUS the service's
own explicit ``user_sub`` filter at the application layer (the WU-4
cross-user hijack lesson,
feedback_per_user_namespace_shared_store_ids). A file record that
exists but belongs to another user (or doesn't exist at all) both
return 404 — never 403, which would leak existence to a caller probing
IDs. ``POST /console/files/batch-get`` carries the same discipline: any
requested ``file_id`` not found/not owned/deleted is silently omitted
from the response, never raises, never distinguishes "not mine" from
"never existed".
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security

from audittrace.auth import require_user, validate_jwt
from audittrace.dependencies import get_console_files_service
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.models import (
    ConsoleFileBatchGetRequest,
    ConsoleFileBatchGetResponse,
    ConsoleFileItem,
    ConsoleFileListResponse,
    ConsoleFileUpsertRequest,
)
from audittrace.services.console_files import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_SCOPE = "memory:files:read-own"
_WRITE_SCOPE = "memory:files:write"


def _file_item(row: dict[str, Any]) -> ConsoleFileItem:
    return ConsoleFileItem(**row)


@router.post("", response_model=ConsoleFileItem)
@log_call(logger=logger)
async def upsert_file(
    body: ConsoleFileUpsertRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleFileItem:
    """Create or update the CALLER's OWN file-metadata record (upsert by
    ``file_id``). ``user_sub`` is stamped from ``user`` — the body
    carries no such field to override it with."""
    service = get_console_files_service()
    row = await service.upsert_file(
        user,
        body.file_id,
        filename=body.filename,
        type=body.type,
        bytes=body.bytes,
        object_key=body.object_key,
        width=body.width,
        height=body.height,
        context=body.context,
        usage=body.usage,
        embedded=body.embedded,
        temp_file_id=body.temp_file_id,
        metadata=body.metadata,
    )
    return _file_item(row)


@router.get("", response_model=ConsoleFileListResponse)
@log_call(logger=logger)
async def list_files(
    cursor: str | None = Query(None, description="Opaque pagination cursor."),
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleFileListResponse:
    """List the CALLER's OWN (non-deleted) file-metadata records,
    newest-first, cursor-paginated. Never returns another user's file —
    RLS plus the service's own explicit ``user_sub`` filter."""
    service = get_console_files_service()
    try:
        items, next_cursor = await service.list_files(user, cursor=cursor, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ConsoleFileListResponse(
        items=[_file_item(row) for row in items],
        next_cursor=next_cursor,
    )


@router.post("/batch-get", response_model=ConsoleFileBatchGetResponse)
@log_call(logger=logger)
async def batch_get_files(
    body: ConsoleFileBatchGetRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleFileBatchGetResponse:
    """Fetch the CALLER's OWN file-metadata records for a batch of
    ``file_id``s in one round trip. A POST (not a GET with repeated
    query params) so the request body — not the URL — carries the
    (potentially large) id list; this is a READ operation despite the
    verb (same rationale as the fork's own attachment-resolution
    calls), gated on the READ scope, never the write scope."""
    service = get_console_files_service()
    items = await service.batch_get_files(user, body.file_ids)
    return ConsoleFileBatchGetResponse(items=[_file_item(row) for row in items])


@router.get("/{file_id}", response_model=ConsoleFileItem)
@log_call(logger=logger)
async def get_file(
    file_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleFileItem:
    """Fetch the CALLER's OWN file-metadata record. 404 if it doesn't
    exist, is deleted, or belongs to another user — never 403 (would
    leak existence to a caller probing foreign file ids)."""
    service = get_console_files_service()
    row = await service.get_file(user, file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="file not found")
    return _file_item(row)


@router.delete("/{file_id}", status_code=204)
@log_call(logger=logger)
async def delete_file(
    file_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> None:
    """Soft-delete the CALLER's OWN file-metadata record. 404 if not
    found/not owned/already deleted."""
    service = get_console_files_service()
    deleted = await service.delete_file(user, file_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="file not found")
