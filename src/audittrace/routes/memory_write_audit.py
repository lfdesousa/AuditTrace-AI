"""The fail-closed write audit of the memory routes (ADR-062 §5, WU-A4).

Every WRITE/DELETE of a memory route records one audit row; an audit-store
failure is an explicit ``HTTPException(500)``, never a silent success. These
helpers live apart from ``routes.memory`` so the write sequences that audit
(``memory_layer_writes``) import them without importing the route module.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from audittrace.identity import UserContext
from audittrace.services.memory_audit import MemoryOp, emit_memory_audit_event


async def emit_write_audits(
    *,
    user: UserContext,
    layer: str,
    filename: str,
    copied_from: str | None,
    collection: str | None = None,
) -> None:
    """One ``emit_write_audit`` per manifest mutation (ADDENDUM-1 S2): the
    create/update row, and -- on a copy-forward -- the legacy tombstone.
    The write carries ``copied_from``; the tombstone ``reason``. Both keys
    are additive ``detail`` fields."""
    await emit_write_audit(
        user=user,
        op="write",
        layer=layer,
        collection=collection,
        key=filename,
        detail_extra={"copied_from": copied_from} if copied_from else None,
    )
    if copied_from:
        await emit_write_audit(
            user=user,
            op="delete",
            layer=layer,
            collection=collection,
            key=filename,
            detail_extra={"reason": "copy_forward"},
        )


async def emit_write_audit(
    *,
    user: UserContext,
    op: MemoryOp,
    layer: str,
    collection: str | None = None,
    key: str | None = None,
    detail_extra: dict[str, Any] | None = None,
) -> None:
    """Fail-closed wrapper around :func:`emit_memory_audit_event` for every
    WRITE/DELETE call site (ADR-062 §5, WU-A4).

    See ``services/memory_audit.py``'s module docstring for the full
    read=fail-open / write=fail-closed rationale. This wrapper converts an
    audit-store failure into an explicit ``HTTPException(500)`` rather than
    letting a bare exception fall through to the app-wide catch-all
    handler — the caller gets a precise, on-brand error (matching every
    other 5xx in this file) instead of a generic unhandled-exception
    envelope, and the failure is deterministically visible to
    ``TestClient`` (Starlette's ``ServerErrorMiddleware`` always re-raises
    after an unhandled exception, which ``TestClient`` in turn re-raises
    into the caller's test — an explicit ``HTTPException`` is the
    established pattern this file already uses everywhere else a service
    call can fail, e.g. ``_require_admin``'s 403, the 502s below)."""
    try:
        await emit_memory_audit_event(
            user=user,
            op=op,
            layer=layer,
            collection=collection,
            key=key,
            detail_extra=detail_extra,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"memory audit-write failed — mutation not confirmed audited: {exc}"
            ),
        ) from exc
