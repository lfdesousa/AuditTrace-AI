"""``PostgresConsoleAclEntriesService`` — the Postgres/SQLAlchemy
implementation of :class:`~audittrace.services.console_acl.
ConsoleAclEntriesService`. See the package ``__init__.py`` for the
full module-level discipline (read-only scope, principal-resolution
narrowing, the two read classes, the perm_bits containment rule).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.db.models import ConsoleAclEntry
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.services.console_acl import (
    OWNER_PERMISSION_BITS,
    PERMISSION_BIT_DELETE,
    PRINCIPAL_TYPE_PUBLIC,
    PRINCIPAL_TYPE_USER,
    ConsoleAclEntriesService,
    _caller_principals,
    _now_ms,
)

logger = logging.getLogger(__name__)


def _entry_to_dict(row: ConsoleAclEntry) -> dict[str, Any]:
    return {
        "id": row.id,
        "user_sub": row.user_sub,
        "principal_type": row.principal_type,
        "principal_id": row.principal_id,
        "principal_model": row.principal_model,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "perm_bits": row.perm_bits,
        "tenant_id": row.tenant_id,
        "role_id": row.role_id,
        "inherited_from": row.inherited_from,
        "granted_by": row.granted_by,
        "granted_at_ms": row.granted_at_ms,
        "expired_at_ms": row.expired_at_ms,
        "created_at_ms": row.created_at_ms,
        "updated_at_ms": row.updated_at_ms,
    }


def _not_expired_clause(now_ms: int) -> Any:
    """``expired_at_ms IS NULL OR expired_at_ms > now`` — the ONE guard
    every authorization-decision method applies (ruling 2). Factored
    out so every call site uses the identical predicate (a copy-pasted
    variant is exactly the kind of drift the guard's own neuter test
    exists to catch)."""
    return or_(
        ConsoleAclEntry.expired_at_ms.is_(None),
        ConsoleAclEntry.expired_at_ms > now_ms,
    )


def _principals_clause(principals: list[tuple[str, str | None]]) -> Any:
    """``OR`` across the caller's resolved principals — mirrors the
    fork's ``$or: principalsQuery`` (one query, not N merged queries;
    WU-0's correction of the draft's mis-citation)."""
    clauses = []
    for principal_type, principal_id in principals:
        if principal_type == PRINCIPAL_TYPE_PUBLIC:
            clauses.append(ConsoleAclEntry.principal_type == PRINCIPAL_TYPE_PUBLIC)
        else:
            clauses.append(
                and_(
                    ConsoleAclEntry.principal_type == principal_type,
                    ConsoleAclEntry.principal_id == principal_id,
                )
            )
    return or_(*clauses)


def _contains_bit(bit: int) -> Any:
    """``(perm_bits & :bit) = :bit`` — containment, NEVER ``=``
    (Deliverable 4 finding #3)."""
    return (ConsoleAclEntry.perm_bits.op("&")(bit)) == bit


def _owner_scope_clause(user_context: UserContext) -> Any:
    """The service-layer mirror of RLS's owner branch — used by the
    three AUDIT-read methods, which answer "what have I granted"
    (never a cross-owner enumeration)."""
    return ConsoleAclEntry.user_sub == user_context.user_id


def _rls_mirror_clause(user_context: UserContext) -> Any:
    """The service-layer mirror of the FULL RLS predicate (migration
    031's ``USING`` clause) — owner OR direct-user-principal OR
    public. SQLite has no RLS; this is the guard the cross-user
    isolation neuter test removes (feedback_unit_tests_miss_rls)."""
    return or_(
        ConsoleAclEntry.user_sub == user_context.user_id,
        and_(
            ConsoleAclEntry.principal_type == PRINCIPAL_TYPE_USER,
            ConsoleAclEntry.principal_id == user_context.user_id,
        ),
        ConsoleAclEntry.principal_type == PRINCIPAL_TYPE_PUBLIC,
    )


class PostgresConsoleAclEntriesService(ConsoleAclEntriesService):
    """PostgreSQL-backed console-ACL service (WU-1, read path only)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    @log_call(logger=logger)
    async def has_permission(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_id: str,
        permission_bit: int,
    ) -> bool:
        principals = _caller_principals(user_context)
        now_ms = _now_ms()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsoleAclEntry.id)
                    # RLS mirror — the non-vacuity guard this method's
                    # cross-user neuter removes.
                    .filter(_rls_mirror_clause(user_context))
                    .filter(_principals_clause(principals))
                    .filter(ConsoleAclEntry.resource_type == resource_type)
                    .filter(ConsoleAclEntry.resource_id == resource_id)
                    .filter(_contains_bit(permission_bit))
                    .filter(_not_expired_clause(now_ms))
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row is not None

    @log_call(logger=logger)
    async def get_effective_permissions(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_id: str,
    ) -> int:
        principals = _caller_principals(user_context)
        now_ms = _now_ms()
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ConsoleAclEntry.perm_bits)
                        .filter(_rls_mirror_clause(user_context))
                        .filter(_principals_clause(principals))
                        .filter(ConsoleAclEntry.resource_type == resource_type)
                        .filter(ConsoleAclEntry.resource_id == resource_id)
                        .filter(_not_expired_clause(now_ms))
                    )
                )
                .scalars()
                .all()
            )
            # Reduced to a plain int INSIDE the session block (#364 —
            # an ORM/Row result must never be read after the session
            # that produced it closes).
            effective = 0
            for bits in rows:
                effective |= bits
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
        principals = _caller_principals(user_context)
        now_ms = _now_ms()
        result: dict[str, int] = {}
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ConsoleAclEntry.resource_id, ConsoleAclEntry.perm_bits)
                    .filter(_rls_mirror_clause(user_context))
                    .filter(_principals_clause(principals))
                    .filter(ConsoleAclEntry.resource_type == resource_type)
                    .filter(ConsoleAclEntry.resource_id.in_(resource_ids))
                    .filter(_not_expired_clause(now_ms))
                )
            ).all()
            # Reduced to plain dict entries INSIDE the session block
            # (#364).
            for resource_id, bits in rows:
                result[resource_id] = result.get(resource_id, 0) | bits
        return result

    @log_call(logger=logger)
    async def find_accessible_resources(
        self,
        user_context: UserContext,
        resource_type: str,
        required_permission_bit: int,
        resource_ids: list[str] | None = None,
    ) -> list[str]:
        principals = _caller_principals(user_context)
        now_ms = _now_ms()
        async with self._session_factory() as session:
            stmt = (
                select(ConsoleAclEntry.resource_id)
                .filter(_rls_mirror_clause(user_context))
                .filter(_principals_clause(principals))
                .filter(ConsoleAclEntry.resource_type == resource_type)
                .filter(_contains_bit(required_permission_bit))
                .filter(_not_expired_clause(now_ms))
                .distinct()
            )
            if resource_ids is not None:
                stmt = stmt.filter(ConsoleAclEntry.resource_id.in_(resource_ids))
            rows = (await session.execute(stmt)).scalars().all()
            # Materialised to a plain list INSIDE the session block
            # (#364).
            result = list(rows)
        return result

    @log_call(logger=logger)
    async def find_public_resource_ids(
        self,
        user_context: UserContext,  # noqa: ARG002 — see docstring
        resource_type: str,
        required_permission_bit: int,
        resource_ids: list[str] | None = None,
    ) -> list[str]:
        now_ms = _now_ms()
        async with self._session_factory() as session:
            stmt = (
                select(ConsoleAclEntry.resource_id)
                # PUBLIC rows are readable by any authenticated session
                # under RLS (migration 031's third USING branch) — no
                # owner/principal filter here, by design.
                .filter(ConsoleAclEntry.principal_type == PRINCIPAL_TYPE_PUBLIC)
                .filter(ConsoleAclEntry.resource_type == resource_type)
                .filter(_contains_bit(required_permission_bit))
                .filter(_not_expired_clause(now_ms))
                .distinct()
            )
            if resource_ids is not None:
                stmt = stmt.filter(ConsoleAclEntry.resource_id.in_(resource_ids))
            rows = (await session.execute(stmt)).scalars().all()
            # Materialised to a plain list INSIDE the session block
            # (#364).
            result = list(rows)
        return result

    @log_call(logger=logger)
    async def get_sole_owned_resource_ids(
        self,
        user_context: UserContext,
        resource_types: list[str],
    ) -> list[str]:
        now_ms = _now_ms()
        async with self._session_factory() as session:
            owned_rows = (
                (
                    await session.execute(
                        select(ConsoleAclEntry.resource_id)
                        .filter(_rls_mirror_clause(user_context))
                        .filter(ConsoleAclEntry.principal_type == PRINCIPAL_TYPE_USER)
                        .filter(ConsoleAclEntry.principal_id == user_context.user_id)
                        .filter(ConsoleAclEntry.resource_type.in_(resource_types))
                        .filter(_contains_bit(PERMISSION_BIT_DELETE))
                        .filter(_not_expired_clause(now_ms))
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
            owned_ids = list(owned_rows)
            if not owned_ids:
                return []

            other_owner_rows = (
                (
                    await session.execute(
                        select(ConsoleAclEntry.resource_id)
                        .filter(ConsoleAclEntry.resource_type.in_(resource_types))
                        .filter(ConsoleAclEntry.resource_id.in_(owned_ids))
                        .filter(_contains_bit(PERMISSION_BIT_DELETE))
                        .filter(_not_expired_clause(now_ms))
                        .filter(
                            or_(
                                ConsoleAclEntry.principal_id != user_context.user_id,
                                ConsoleAclEntry.principal_type != PRINCIPAL_TYPE_USER,
                            )
                        )
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
            # Reduced to a plain list INSIDE the session block (#364).
            multi_owner_ids = set(other_owner_rows)
            result = [rid for rid in owned_ids if rid not in multi_owner_ids]
        return result

    @log_call(logger=logger)
    async def find_entries_by_principal(
        self,
        user_context: UserContext,
        principal_type: str,
        principal_id: str | None,
        resource_type: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            stmt = (
                select(ConsoleAclEntry)
                # Owner-scoped — audit read, see package docstring for
                # why this deliberately narrows the raw Mongo signature.
                .filter(_owner_scope_clause(user_context))
                .filter(ConsoleAclEntry.principal_type == principal_type)
            )
            if principal_type != PRINCIPAL_TYPE_PUBLIC:
                stmt = stmt.filter(ConsoleAclEntry.principal_id == principal_id)
            if resource_type is not None:
                stmt = stmt.filter(ConsoleAclEntry.resource_type == resource_type)
            rows = (await session.execute(stmt)).scalars().all()
            return [_entry_to_dict(r) for r in rows]

    @log_call(logger=logger)
    async def find_entries_by_resource(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_id: str,
    ) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ConsoleAclEntry)
                        .filter(_owner_scope_clause(user_context))
                        .filter(ConsoleAclEntry.resource_type == resource_type)
                        .filter(ConsoleAclEntry.resource_id == resource_id)
                    )
                )
                .scalars()
                .all()
            )
            return [_entry_to_dict(r) for r in rows]

    @log_call(logger=logger)
    async def find_entries_by_principals_and_resource(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_id: str,
    ) -> list[dict[str, Any]]:
        principals = _caller_principals(user_context)
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ConsoleAclEntry)
                        .filter(_principals_clause(principals))
                        .filter(ConsoleAclEntry.resource_type == resource_type)
                        .filter(ConsoleAclEntry.resource_id == resource_id)
                    )
                )
                .scalars()
                .all()
            )
            return [_entry_to_dict(r) for r in rows]

    @log_call(logger=logger)
    async def get_owner_principal_ids(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_ids: list[str],
    ) -> dict[str, str]:
        """The ``aggregateAclEntries`` SITE-1 fold
        (``ownerContact.js``'s ``getFirstOwnerIdsByResource``) — ACL
        ONLY, no cross-collection identity join (that is sites 2/3,
        explicitly moved to WU-3). Returns, per ``resource_id``, the
        earliest-granted ``user``-principal holding EXACTLY
        ``OWNER_PERMISSION_BITS`` (VIEW|EDIT|DELETE|SHARE) — tie-broken
        by ``(granted_at_ms, created_at_ms, id)``, mirroring the live
        Mongo pipeline's ``$sort(grantedAt, createdAt, _id)`` +
        ``$group($first)`` exactly.

        **Deliberate exact-equality (``==``, not containment) — the
        SOLE exception in this package to "never compare perm_bits with
        =".** Deliverable 4 finding #3 is explicit: whether to
        bug-fix this specific query's Mongo-inherited behaviour during
        the fold, versus preserving it byte-for-byte, is a product-
        visible decision (more/fewer agents would show an owner
        contact) left to whichever WU actually migrates owner-contact
        resolution — NOT decided in WU-1. Preserving the exact-equality
        byte-for-byte is therefore the fail-closed choice here: it
        changes NO observable behaviour relative to the live system,
        where silently "fixing" it to containment would be an
        undocumented product behaviour change smuggled into a store
        migration. (Note ``OWNER_PERMISSION_BITS == MAX_PERM_BITS``
        today, so containment and equality currently coincide; the
        divergence Deliverable 4 warns about is latent, triggered only
        if a 5th ``PermissionBits`` value is ever added without
        revisiting this method.)

        Only resolves owners among rows the CALLER can see (RLS mirror)
        — see package docstring: WU-1 has no cross-owner escape hatch,
        so a caller only ever gets back ownership for resources they
        already have visibility of (their own, today — before any
        grant exists, since WU-2 hasn't landed).
        """
        if not resource_ids:
            return {}
        now_ms = _now_ms()
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        ConsoleAclEntry.resource_id,
                        ConsoleAclEntry.principal_id,
                        ConsoleAclEntry.granted_at_ms,
                        ConsoleAclEntry.created_at_ms,
                        ConsoleAclEntry.id,
                    )
                    .filter(_rls_mirror_clause(user_context))
                    .filter(ConsoleAclEntry.resource_type == resource_type)
                    .filter(ConsoleAclEntry.resource_id.in_(resource_ids))
                    .filter(ConsoleAclEntry.principal_type == PRINCIPAL_TYPE_USER)
                    # The documented exception — see docstring above.
                    .filter(ConsoleAclEntry.perm_bits == OWNER_PERMISSION_BITS)
                    .filter(_not_expired_clause(now_ms))
                    .order_by(
                        ConsoleAclEntry.resource_id,
                        ConsoleAclEntry.granted_at_ms,
                        ConsoleAclEntry.created_at_ms,
                        ConsoleAclEntry.id,
                    )
                )
            ).all()
            # Reduced to a plain dict INSIDE the session block (#364).
            owners: dict[str, str] = {}
            for resource_id, principal_id, _granted_at_ms, _created_at_ms, _id in rows:
                # Rows arrive sorted earliest-first per resource_id;
                # keep only the FIRST (earliest) owner per resource_id.
                if resource_id not in owners and principal_id is not None:
                    owners[resource_id] = principal_id
        return owners
