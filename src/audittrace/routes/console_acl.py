"""Console-ACL routes — Sovereign Authorization Layer EPIC: the WU-1 READ
routes plus the WU-2c-A WRITE routes.

The AuditTrace-side sovereign, RLS-isolated store + API for LibreChat's
ACL record (the fork's Mongo ``AclEntry`` collection).

Mounted at ``/console/acl`` (``server.py``). Read routes require
``memory:acl:read-own``; write routes require ``memory:acl:write`` —
per-route scope sets, never a superset (holding the write scope alone
does not open a read route). Every route also resolves the caller via
``require_user``: ``user_sub``, ``granted_by``, ``trace_id``,
``session_id`` and timestamps are NEVER request fields
(feedback_never_trust_caller_metadata_for_security_fields) — every
write request model is ``extra="forbid"``, so a hostile key answers 422.

**The database decides who may write.** The write routes INSERT/UPDATE
as the caller (token-derived ``user_sub``) and let migration 032's RLS
refuse everything else; the service records the refusal (denial row,
own transaction) and raises ``AclWriteRefused``, which this module maps
to a status code — it writes nothing, swallows nothing and carries no
authority model of its own (this module imports nothing from
``_ownership`` and names no ``PERMISSION_BIT_*`` — pinned by a test).
Until the 2d share-authority unit the policy is owner-only, so a
non-owner's grant answers 403.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Security
from fastapi.responses import JSONResponse

from audittrace.auth import require_user, validate_jwt
from audittrace.dependencies import get_console_acl_service
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.models import (
    ACL_PRINCIPAL_TYPES,
    ConsoleAclBatchPermissionsRequest,
    ConsoleAclBatchPermissionsResponse,
    ConsoleAclBulkRequest,
    ConsoleAclBulkResponse,
    ConsoleAclEffectivePermissionsResponse,
    ConsoleAclEntryItem,
    ConsoleAclExpireRequest,
    ConsoleAclExpireResponse,
    ConsoleAclGrantRequest,
    ConsoleAclHasPermissionResponse,
    ConsoleAclModifyRequest,
    ConsoleAclResourceIdsResponse,
)
from audittrace.services.console_acl import (
    MAX_PERM_BITS,
    RESOURCE_TYPES,
    AclGrantOp,
)
from audittrace.services.console_acl._audit import _bit_breakdown
from audittrace.services.console_acl._errors import AclWriteRefused
from audittrace.services.console_acl._postgres_write import (
    _validate_bit_operands,
    _validate_predicate,
)
from audittrace.services.console_store._context import current_trace_id_hex

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_SCOPE = "memory:acl:read-own"
_WRITE_SCOPE = "memory:acl:write"

# ``AclWriteRefused.failure_class`` -> HTTP status. CLOSED: a class not
# listed here fails closed (500) rather than guessing a 4xx.
_REFUSAL_STATUS: dict[str, int] = {
    "acl_denied_policy": 403,
    "acl_denied_bulk_rollback": 403,
    "acl_denied_principal_type": 400,
    "acl_denied_past_expiry": 400,
}

# 031 ``resource_id`` column width.
_ResourceIdPath = Path(..., min_length=1, max_length=36)


def _validated_resource_type(resource_type: str) -> str:
    if resource_type not in RESOURCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown resource_type: {resource_type!r}",
        )
    return resource_type


@router.get(
    "/{resource_type}/{resource_id:path}/permissions",
    response_model=ConsoleAclEffectivePermissionsResponse,
)
@log_call(logger=logger)
async def get_effective_permissions(
    resource_type: str,
    resource_id: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleAclEffectivePermissionsResponse:
    """The CALLER's combined effective permission bitmask on the
    resource. ``0`` (never an error) when no matching, non-expired
    grant exists — deny-by-default."""
    resource_type = _validated_resource_type(resource_type)
    service = get_console_acl_service()
    bits = await service.get_effective_permissions(user, resource_type, resource_id)
    return ConsoleAclEffectivePermissionsResponse(perm_bits=bits)


@router.get(
    "/{resource_type}/{resource_id:path}/has-permission",
    response_model=ConsoleAclHasPermissionResponse,
)
@log_call(logger=logger)
async def has_permission(
    resource_type: str,
    resource_id: str,
    bit: int = Query(..., ge=1, le=MAX_PERM_BITS),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleAclHasPermissionResponse:
    """Whether the CALLER holds AT LEAST ``bit`` (containment, never
    equality) on the resource."""
    resource_type = _validated_resource_type(resource_type)
    service = get_console_acl_service()
    result = await service.has_permission(user, resource_type, resource_id, bit)
    return ConsoleAclHasPermissionResponse(has_permission=result)


@router.post(
    "/{resource_type}/permissions/batch",
    response_model=ConsoleAclBatchPermissionsResponse,
)
@log_call(logger=logger)
async def get_effective_permissions_batch(
    resource_type: str,
    body: ConsoleAclBatchPermissionsRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleAclBatchPermissionsResponse:
    """Batch form of :func:`get_effective_permissions` — one round
    trip for many resource ids. A POST (not a GET with repeated query
    params) so the (potentially large) id list travels in the body;
    this is a READ operation despite the verb, gated on the READ
    scope, never a write scope (WU-1 has none)."""
    resource_type = _validated_resource_type(resource_type)
    service = get_console_acl_service()
    permissions = await service.get_effective_permissions_for_resources(
        user, resource_type, body.resource_ids
    )
    return ConsoleAclBatchPermissionsResponse(permissions=permissions)


@router.get(
    "/{resource_type}/accessible",
    response_model=ConsoleAclResourceIdsResponse,
)
@log_call(logger=logger)
async def find_accessible_resources(
    resource_type: str,
    bit: int = Query(..., ge=1, le=MAX_PERM_BITS),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleAclResourceIdsResponse:
    """Resource ids the CALLER can access with at least ``bit``
    (self + public), unbounded (no ``resource_ids`` candidate filter
    at this route — the full-scan form)."""
    resource_type = _validated_resource_type(resource_type)
    service = get_console_acl_service()
    ids = await service.find_accessible_resources(user, resource_type, bit)
    return ConsoleAclResourceIdsResponse(resource_ids=ids)


@router.get(
    "/{resource_type}/public",
    response_model=ConsoleAclResourceIdsResponse,
)
@log_call(logger=logger)
async def find_public_resource_ids(
    resource_type: str,
    bit: int = Query(..., ge=1, le=MAX_PERM_BITS),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleAclResourceIdsResponse:
    """Resource ids PUBLICLY accessible with at least ``bit`` — the
    same answer for any authenticated caller."""
    resource_type = _validated_resource_type(resource_type)
    service = get_console_acl_service()
    ids = await service.find_public_resource_ids(user, resource_type, bit)
    return ConsoleAclResourceIdsResponse(resource_ids=ids)


@router.get(
    "/{resource_type}/sole-owned",
    response_model=ConsoleAclResourceIdsResponse,
)
@log_call(logger=logger)
async def get_sole_owned_resource_ids(
    resource_type: str,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_READ_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleAclResourceIdsResponse:
    """Resource ids where the CALLER is the SOLE holder of DELETE."""
    resource_type = _validated_resource_type(resource_type)
    service = get_console_acl_service()
    ids = await service.get_sole_owned_resource_ids(user, [resource_type])
    return ConsoleAclResourceIdsResponse(resource_ids=ids)


# ── WRITE routes (WU-2c-A) ─────────────────────────────────────────────────


def refusal_to_http(exc: AclWriteRefused) -> HTTPException:
    """Map a service-recorded refusal to its HTTP error. The denial row
    was already written by the service in its own transaction — this
    function only translates."""
    return HTTPException(
        status_code=_REFUSAL_STATUS.get(exc.failure_class, 500),
        detail={
            "failure_class": exc.failure_class,
            "db_error_class": exc.db_error_class,
            "trace_id": current_trace_id_hex(),
        },
    )


def _entry_item(row: dict[str, Any]) -> ConsoleAclEntryItem:
    # ``user_sub`` is dropped (pydantic ignores the extra response key).
    return ConsoleAclEntryItem(**row, bits=_bit_breakdown(row["perm_bits"]))


def _op_from_body(
    body: ConsoleAclGrantRequest, resource_type: str, resource_id: str
) -> AclGrantOp:
    return AclGrantOp(
        principal_type=body.principal_type,
        principal_id=body.principal_id,
        resource_type=resource_type,
        resource_id=resource_id,
        perm_bits=body.perm_bits,
        role_id=body.role_id,
        expired_at_ms=body.expired_at_ms,
        tenant_id=body.tenant_id,
    )


@router.post(
    "/{resource_type}/{resource_id:path}/grants/bulk",
    response_model=ConsoleAclBulkResponse,
)
@log_call(logger=logger)
async def bulk_grants(
    resource_type: str,
    body: ConsoleAclBulkRequest,
    resource_id: str = _ResourceIdPath,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleAclBulkResponse:
    """All-or-nothing batch of grants on ONE resource (the path's)."""
    resource_type = _validated_resource_type(resource_type)
    ops = [_op_from_body(op, resource_type, resource_id) for op in body.ops]
    service = get_console_acl_service()
    try:
        result = await service.bulk_write_acl_entries(user, ops)
    except AclWriteRefused as exc:
        raise refusal_to_http(exc) from exc
    return ConsoleAclBulkResponse(
        acl_entry_ids=result["acl_entry_ids"],
        expired_ids=result["expired_ids"],
        trace_id=current_trace_id_hex(),
    )


@router.post(
    "/{resource_type}/{resource_id:path}/grants",
    response_model=ConsoleAclEntryItem,
    status_code=201,
)
@log_call(logger=logger)
async def grant(
    resource_type: str,
    body: ConsoleAclGrantRequest,
    resource_id: str = _ResourceIdPath,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleAclEntryItem:
    """Grant (expire-and-insert) on the resource as the CALLER."""
    resource_type = _validated_resource_type(resource_type)
    service = get_console_acl_service()
    try:
        row = await service.grant_permission(
            user,
            principal_type=body.principal_type,
            principal_id=body.principal_id,
            resource_type=resource_type,
            resource_id=resource_id,
            perm_bits=body.perm_bits,
            role_id=body.role_id,
            expired_at_ms=body.expired_at_ms,
            tenant_id=body.tenant_id,
        )
    except AclWriteRefused as exc:
        raise refusal_to_http(exc) from exc
    return _entry_item(row)


@router.delete(
    "/{resource_type}/{resource_id:path}/grants",
    response_model=ConsoleAclExpireResponse,
)
@log_call(logger=logger)
async def revoke(
    resource_type: str,
    principal_type: ACL_PRINCIPAL_TYPES = Query(...),
    principal_id: str | None = Query(None, min_length=1, max_length=64),
    tenant_id: str | None = Query(None, min_length=1, max_length=64),
    resource_id: str = _ResourceIdPath,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleAclExpireResponse:
    """Expire the caller's active grant at the key. 200 with empty ids is
    the idempotent answer (an unowned row is omitted by 032, not refused)."""
    resource_type = _validated_resource_type(resource_type)
    service = get_console_acl_service()
    try:
        result = await service.revoke_permission(
            user,
            principal_type=principal_type,
            principal_id=principal_id,
            resource_type=resource_type,
            resource_id=resource_id,
            tenant_id=tenant_id,
        )
    except AclWriteRefused as exc:
        raise refusal_to_http(exc) from exc
    return ConsoleAclExpireResponse(
        expired_ids=result["expired_ids"],
        visible_matched_count=result["visible_matched_count"],
        trace_id=current_trace_id_hex(),
    )


@router.patch(
    "/{resource_type}/{resource_id:path}/grants",
    response_model=ConsoleAclEntryItem,
)
@log_call(logger=logger)
async def modify_grant(
    resource_type: str,
    body: ConsoleAclModifyRequest,
    resource_id: str = _ResourceIdPath,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> Any:
    """Recompute the bits of the active grant (expire-and-insert). 404
    (with the success row's trace_id) when no active grant matched."""
    resource_type = _validated_resource_type(resource_type)
    try:
        _validate_bit_operands(body.add_bits, body.remove_bits)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    service = get_console_acl_service()
    try:
        row = await service.modify_permission_bits(
            user,
            principal_type=body.principal_type,
            principal_id=body.principal_id,
            resource_type=resource_type,
            resource_id=resource_id,
            add_bits=body.add_bits,
            remove_bits=body.remove_bits,
            tenant_id=body.tenant_id,
        )
    except AclWriteRefused as exc:
        raise refusal_to_http(exc) from exc
    if row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "no active grant", "trace_id": current_trace_id_hex()},
        )
    return _entry_item(row)


@router.post("/expire", response_model=ConsoleAclExpireResponse)
@log_call(logger=logger)
async def expire_by_predicate(
    body: ConsoleAclExpireRequest,
    _scope: dict[str, Any] = Security(validate_jwt, scopes=[_WRITE_SCOPE]),
    user: UserContext = Depends(require_user),
) -> ConsoleAclExpireResponse:
    """Expire-by-predicate, caller-scoped (the fork's
    ``removeAllPermissions`` / revoke-list shapes)."""
    predicates = [p.model_dump(exclude_none=True) for p in body.predicates]
    try:
        for pred in predicates:
            _validate_predicate(pred)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    service = get_console_acl_service()
    try:
        result = await service.delete_acl_entries(user, predicates)
    except AclWriteRefused as exc:
        raise refusal_to_http(exc) from exc
    return ConsoleAclExpireResponse(
        expired_ids=result["expired_ids"],
        visible_matched_count=result["visible_matched_count"],
        trace_id=current_trace_id_hex(),
    )
