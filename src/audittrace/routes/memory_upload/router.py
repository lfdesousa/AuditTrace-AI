"""GET /memory/upload/status endpoint.

PR-B3 introduces a polling endpoint that lets clients (WebUI
chips, Bruno collection, LibreChat Day-2) check whether their
PDF upload has been scanned. The POST surface is owned by
``routes/memory.py:upload_memory_file``; only the status read
lives here.

Returns ``manifest.scan_status`` (closed-set per ADR-048) plus
the timestamps needed for an operator-facing UI:
``created_at_ms`` (= upload time) and ``modified_at_ms`` (= the
last verdict-consumer write). 404 when the scan_id is unknown.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.auth import require_user, validate_jwt
from audittrace.identity import UserContext
from audittrace.routes.memory_upload.manifest import get_by_scan_id

router = APIRouter(prefix="/memory/upload", tags=["memory"])


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Lazy lookup of the FastAPI app's session factory.

    Imported lazily so test harnesses that wire the upload
    endpoint without a real Postgres can patch the dependency
    (``client.app.dependency_overrides[_get_session_factory] = ...``).
    """
    from audittrace.dependencies import get_postgres_factory  # noqa: PLC0415

    factory: async_sessionmaker[AsyncSession] = (
        get_postgres_factory().get_session_factory()
    )
    return factory


@router.get("/status")
async def get_upload_status(
    scan_id: str = Query(..., description="Scan ID returned by POST /memory/upload"),
    _auth: dict[str, Any] = Security(validate_jwt, scopes=[]),
    user: UserContext = Depends(require_user),
    session_factory: async_sessionmaker[AsyncSession] = Depends(_get_session_factory),
) -> dict[str, Any]:
    """Return the current scan_status of the named upload.

    Authorization: any authenticated user who can read the
    layer can poll status. We additionally check that the
    requesting user is the same one who created the row OR an
    admin — content-control verdicts shouldn't leak across
    tenant boundaries even pre-PR-B7 IAM split."""
    async with session_factory() as session:
        row = await get_by_scan_id(session, scan_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"scan_id not found: {scan_id}")
    if not (
        user.is_admin
        or "audittrace:admin" in user.scopes
        or row.created_by_user_id == user.user_id
    ):
        # Don't reveal whether the scan_id exists for some other
        # tenant — return the same 404 shape.
        raise HTTPException(status_code=404, detail=f"scan_id not found: {scan_id}")

    return {
        "scan_id": row.id,
        "status": row.scan_status,
        "object_uri": row.key,
        "object_sha256": row.document_sha256,
        "size_bytes": row.size_bytes,
        "created_at_ms": row.created_at_ms,
        "modified_at_ms": row.modified_at_ms,
        "trace_id": row.trace_id,
    }
