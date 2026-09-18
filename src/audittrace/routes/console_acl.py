"""Console-ACL routes — Sovereign Authorization Layer EPIC, WU-1
(READ PATH ONLY).

The AuditTrace-side sovereign, RLS-isolated store + API for LibreChat's
ACL record (the fork's Mongo ``AclEntry`` collection), per the
ratified spec (``2026-09-17-SPEC-sovereign-authorization-layer-acl-
WU0-ratified-candidate.md``) and its operator ratification
(``2026-09-18-SPEC-ADDENDUM-I-...``).

Mounted at ``/console/acl`` (``server.py``). Every route requires the
``memory:acl:read-own`` scope AND resolves the caller's identity via
``require_user`` — every answer is about the CALLER's own resolved
principal set (self + public), NEVER an attacker-suppliable
``principal_id``/``principal_type``
(feedback_never_trust_caller_metadata_for_security_fields): no request
model in ``audittrace.models`` declares such a field.

**Only the methods with real (or plausible future) HTTP consumers are
wired here** — ``find_entries_by_principal``/``find_entries_by_
resource``/``find_entries_by_principals_and_resource`` and
``get_owner_principal_ids`` (the ``aggregateAclEntries`` site-1 fold)
are deliberately NOT exposed as routes (per the ratified spec's
explicit instruction not to invent route wiring for zero-consumer
methods); they are tested at the service layer only
(``tests/test_console_acl_service.py``).

**No write route exists** — WU-2.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security

from audittrace.auth import require_user, validate_jwt
from audittrace.dependencies import get_console_acl_service
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.models import (
    ConsoleAclBatchPermissionsRequest,
    ConsoleAclBatchPermissionsResponse,
    ConsoleAclEffectivePermissionsResponse,
    ConsoleAclHasPermissionResponse,
    ConsoleAclResourceIdsResponse,
)
from audittrace.services.console_acl import MAX_PERM_BITS, RESOURCE_TYPES

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_SCOPE = "memory:acl:read-own"


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
