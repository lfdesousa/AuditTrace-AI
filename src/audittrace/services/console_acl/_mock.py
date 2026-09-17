"""``MockConsoleAclEntriesService`` — the in-process mock implementation
of :class:`~audittrace.services.console_acl.ConsoleAclEntriesService`
for fast unit tests (mirrors ``MockConsoleAgentsService``'s shape). See
the package ``__init__.py`` for the full module-level discipline.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.services.console_acl import (
    ALLOWED_PRINCIPAL_TYPES,
    OWNER_PERMISSION_BITS,
    PERMISSION_BIT_DELETE,
    PRINCIPAL_TYPE_PUBLIC,
    PRINCIPAL_TYPE_USER,
    ConsoleAclEntriesService,
    _caller_principals,
    _now_ms,
)

logger = logging.getLogger(__name__)


@dataclass
class _MockAclEntry:
    id: str
    user_sub: str
    principal_type: str
    principal_id: str | None
    principal_model: str | None
    resource_type: str
    resource_id: str
    perm_bits: int
    tenant_id: str | None = None
    role_id: str | None = None
    inherited_from: str | None = None
    granted_by: str | None = None
    granted_at_ms: int = 0
    expired_at_ms: int | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_sub": self.user_sub,
            "principal_type": self.principal_type,
            "principal_id": self.principal_id,
            "principal_model": self.principal_model,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "perm_bits": self.perm_bits,
            "tenant_id": self.tenant_id,
            "role_id": self.role_id,
            "inherited_from": self.inherited_from,
            "granted_by": self.granted_by,
            "granted_at_ms": self.granted_at_ms,
            "expired_at_ms": self.expired_at_ms,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
        }


def _mock_matches_principal(
    row: _MockAclEntry, principal_type: str, principal_id: str | None
) -> bool:
    if principal_type == PRINCIPAL_TYPE_PUBLIC:
        return row.principal_type == PRINCIPAL_TYPE_PUBLIC
    return row.principal_type == principal_type and row.principal_id == principal_id


def _mock_not_expired(row: _MockAclEntry, now_ms: int) -> bool:
    return row.expired_at_ms is None or row.expired_at_ms > now_ms


def _mock_rls_visible(row: _MockAclEntry, user_context: UserContext) -> bool:
    if row.user_sub == user_context.user_id:
        return True
    if (
        row.principal_type == PRINCIPAL_TYPE_USER
        and row.principal_id == user_context.user_id
    ):
        return True
    return row.principal_type == PRINCIPAL_TYPE_PUBLIC


class MockConsoleAclEntriesService(ConsoleAclEntriesService):
    """In-process mock for unit tests that don't wire a Postgres
    factory. Test setup inserts rows directly via :meth:`seed_entry` —
    there is no upsert method (WU-1 is read-only)."""

    def __init__(self) -> None:
        self._entries: list[_MockAclEntry] = []

    def reset(self) -> None:
        self._entries.clear()

    def seed_entry(
        self,
        *,
        user_sub: str,
        principal_type: str,
        principal_id: str | None,
        principal_model: str | None,
        resource_type: str,
        resource_id: str,
        perm_bits: int,
        tenant_id: str | None = None,
        role_id: str | None = None,
        inherited_from: str | None = None,
        granted_by: str | None = None,
        granted_at_ms: int = 0,
        expired_at_ms: int | None = None,
        created_at_ms: int = 0,
        updated_at_ms: int = 0,
    ) -> dict[str, Any]:
        """Test-only seam — inserts a row bypassing every guard (the
        Postgres CHECK constraints are exercised by
        ``tests/test_console_acl_migration.py`` against real schema
        creation, not through this mock)."""
        if principal_type not in ALLOWED_PRINCIPAL_TYPES:
            raise ValueError(f"invalid principal_type: {principal_type!r}")
        row = _MockAclEntry(
            id=str(uuid.uuid4()),
            user_sub=user_sub,
            principal_type=principal_type,
            principal_id=principal_id,
            principal_model=principal_model,
            resource_type=resource_type,
            resource_id=resource_id,
            perm_bits=perm_bits,
            tenant_id=tenant_id,
            role_id=role_id,
            inherited_from=inherited_from,
            granted_by=granted_by,
            granted_at_ms=granted_at_ms,
            expired_at_ms=expired_at_ms,
            created_at_ms=created_at_ms,
            updated_at_ms=updated_at_ms,
        )
        self._entries.append(row)
        return row.to_dict()

    @log_call(logger=logger)
    async def has_permission(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_id: str,
        permission_bit: int,
    ) -> bool:
        now_ms = _now_ms()
        principals = _caller_principals(user_context)
        for row in self._entries:
            if not _mock_rls_visible(row, user_context):
                continue
            if row.resource_type != resource_type or row.resource_id != resource_id:
                continue
            if not any(_mock_matches_principal(row, pt, pid) for pt, pid in principals):
                continue
            if (row.perm_bits & permission_bit) != permission_bit:
                continue
            if not _mock_not_expired(row, now_ms):
                continue
            return True
        return False

    @log_call(logger=logger)
    async def get_effective_permissions(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_id: str,
    ) -> int:
        now_ms = _now_ms()
        principals = _caller_principals(user_context)
        effective = 0
        for row in self._entries:
            if not _mock_rls_visible(row, user_context):
                continue
            if row.resource_type != resource_type or row.resource_id != resource_id:
                continue
            if not any(_mock_matches_principal(row, pt, pid) for pt, pid in principals):
                continue
            if not _mock_not_expired(row, now_ms):
                continue
            effective |= row.perm_bits
        return effective

    @log_call(logger=logger)
    async def get_effective_permissions_for_resources(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_ids: list[str],
    ) -> dict[str, int]:
        if not resource_ids:
            return {}
        now_ms = _now_ms()
        principals = _caller_principals(user_context)
        result: dict[str, int] = {}
        wanted = set(resource_ids)
        for row in self._entries:
            if not _mock_rls_visible(row, user_context):
                continue
            if row.resource_type != resource_type or row.resource_id not in wanted:
                continue
            if not any(_mock_matches_principal(row, pt, pid) for pt, pid in principals):
                continue
            if not _mock_not_expired(row, now_ms):
                continue
            result[row.resource_id] = result.get(row.resource_id, 0) | row.perm_bits
        return result

    @log_call(logger=logger)
    async def find_accessible_resources(
        self,
        user_context: UserContext,
        resource_type: str,
        required_permission_bit: int,
        resource_ids: list[str] | None = None,
    ) -> list[str]:
        now_ms = _now_ms()
        principals = _caller_principals(user_context)
        bound = set(resource_ids) if resource_ids is not None else None
        found: list[str] = []
        seen: set[str] = set()
        for row in self._entries:
            if not _mock_rls_visible(row, user_context):
                continue
            if row.resource_type != resource_type:
                continue
            if bound is not None and row.resource_id not in bound:
                continue
            if not any(_mock_matches_principal(row, pt, pid) for pt, pid in principals):
                continue
            if (row.perm_bits & required_permission_bit) != required_permission_bit:
                continue
            if not _mock_not_expired(row, now_ms):
                continue
            if row.resource_id not in seen:
                seen.add(row.resource_id)
                found.append(row.resource_id)
        return found

    @log_call(logger=logger)
    async def find_public_resource_ids(
        self,
        user_context: UserContext,  # noqa: ARG002 — see ABC docstring
        resource_type: str,
        required_permission_bit: int,
        resource_ids: list[str] | None = None,
    ) -> list[str]:
        now_ms = _now_ms()
        bound = set(resource_ids) if resource_ids is not None else None
        found: list[str] = []
        seen: set[str] = set()
        for row in self._entries:
            if row.principal_type != PRINCIPAL_TYPE_PUBLIC:
                continue
            if row.resource_type != resource_type:
                continue
            if bound is not None and row.resource_id not in bound:
                continue
            if (row.perm_bits & required_permission_bit) != required_permission_bit:
                continue
            if not _mock_not_expired(row, now_ms):
                continue
            if row.resource_id not in seen:
                seen.add(row.resource_id)
                found.append(row.resource_id)
        return found

    @log_call(logger=logger)
    async def get_sole_owned_resource_ids(
        self,
        user_context: UserContext,
        resource_types: list[str],
    ) -> list[str]:
        now_ms = _now_ms()
        owned: list[str] = []
        seen: set[str] = set()
        for row in self._entries:
            if not _mock_rls_visible(row, user_context):
                continue
            if row.resource_type not in resource_types:
                continue
            if row.principal_type != PRINCIPAL_TYPE_USER:
                continue
            if row.principal_id != user_context.user_id:
                continue
            if (row.perm_bits & PERMISSION_BIT_DELETE) != PERMISSION_BIT_DELETE:
                continue
            if not _mock_not_expired(row, now_ms):
                continue
            if row.resource_id not in seen:
                seen.add(row.resource_id)
                owned.append(row.resource_id)
        if not owned:
            return []
        multi_owner: set[str] = set()
        for row in self._entries:
            if row.resource_type not in resource_types:
                continue
            if row.resource_id not in owned:
                continue
            if (row.perm_bits & PERMISSION_BIT_DELETE) != PERMISSION_BIT_DELETE:
                continue
            if not _mock_not_expired(row, now_ms):
                continue
            is_caller_user = (
                row.principal_type == PRINCIPAL_TYPE_USER
                and row.principal_id == user_context.user_id
            )
            if not is_caller_user:
                multi_owner.add(row.resource_id)
        return [rid for rid in owned if rid not in multi_owner]

    @log_call(logger=logger)
    async def find_entries_by_principal(
        self,
        user_context: UserContext,
        principal_type: str,
        principal_id: str | None,
        resource_type: str | None = None,
    ) -> list[dict[str, Any]]:
        results = []
        for row in self._entries:
            if row.user_sub != user_context.user_id:
                continue
            if row.principal_type != principal_type:
                continue
            if (
                principal_type != PRINCIPAL_TYPE_PUBLIC
                and row.principal_id != principal_id
            ):
                continue
            if resource_type is not None and row.resource_type != resource_type:
                continue
            results.append(row.to_dict())
        return results

    @log_call(logger=logger)
    async def find_entries_by_resource(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_id: str,
    ) -> list[dict[str, Any]]:
        return [
            row.to_dict()
            for row in self._entries
            if row.user_sub == user_context.user_id
            and row.resource_type == resource_type
            and row.resource_id == resource_id
        ]

    @log_call(logger=logger)
    async def find_entries_by_principals_and_resource(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_id: str,
    ) -> list[dict[str, Any]]:
        principals = _caller_principals(user_context)
        results = []
        for row in self._entries:
            if row.resource_type != resource_type or row.resource_id != resource_id:
                continue
            if any(_mock_matches_principal(row, pt, pid) for pt, pid in principals):
                results.append(row.to_dict())
        return results

    @log_call(logger=logger)
    async def get_owner_principal_ids(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_ids: list[str],
    ) -> dict[str, str]:
        """Mock mirror of the Postgres site-1 fold — see
        ``_postgres.PostgresConsoleAclEntriesService.
        get_owner_principal_ids`` for the full rationale (same
        deliberate exact-equality)."""
        if not resource_ids:
            return {}
        now_ms = _now_ms()
        wanted = set(resource_ids)
        candidates = [
            row
            for row in self._entries
            if _mock_rls_visible(row, user_context)
            and row.resource_type == resource_type
            and row.resource_id in wanted
            and row.principal_type == PRINCIPAL_TYPE_USER
            and row.perm_bits == OWNER_PERMISSION_BITS
            and _mock_not_expired(row, now_ms)
        ]
        candidates.sort(
            key=lambda r: (r.resource_id, r.granted_at_ms, r.created_at_ms, r.id)
        )
        owners: dict[str, str] = {}
        for row in candidates:
            if row.resource_id not in owners and row.principal_id is not None:
                owners[row.resource_id] = row.principal_id
        return owners
