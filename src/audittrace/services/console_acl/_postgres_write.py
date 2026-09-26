"""The sovereign ACL write path — Sovereign Authorization Layer EPIC,
**ACL 2b-core-A1**
(``2026-09-26-SPEC-acl-2b-core-A-write-path.md`` + ADDENDA U/V/W/X).

``_PostgresAclWrites`` is a MIXIN inherited by
:class:`~audittrace.services.console_acl._postgres.
PostgresConsoleAclEntriesService` — kept in its own module (rather than
edited into ``_postgres.py`` directly) so that file's diff stays exactly
"an import line + the one class statement gaining this mixin" (spec §11 /
ADDENDUM U-5(iii)'s gate: ``git diff -U0 main..HEAD -- _postgres.py
_mock.py`` must touch only import lines and the two ``class …(``
statements — any hunk inside a READ method body is a REJECT).

**Scope — A1 only (ADDENDUM U-6).** :func:`grant_permission` and
:func:`bulk_write_acl_entries` are FULLY implemented here.
``revoke_permission``/``modify_permission_bits``/``delete_acl_entries``
raise :class:`NotImplementedError` — a REAL, disclosed gap: zero
production callers of any of the five write methods exist in ``src/``
today, no route reaches them until 2c, and 2b-core-A2 fills them in.

**The abort-shape discipline (spec §0/§6, ADDENDA U-2/V-1/V-2/W-1/W-2/
X-1) — derived from the SQLAlchemy call each statement actually makes,
never asserted from prose:**

* :func:`_expire_active` is an ORM-ENABLED ``sa.update(ConsoleAclEntry)
  …returning(...)`` — never a literal Core SQL string executed via ``session.execute`` (that
  spelling is barred from this module — spec §11 / ADDENDUM X-4 add a
  grep-based documentation check for it, mirroring the savepoint check
  below). A literal-SQL-string's autoflush behaviour is version-dependent (fires on
  SQLAlchemy 2.1.x, not on 2.0.51 — ADDENDUM V-1's measured table); the
  ORM-enabled construct autoflushes on BOTH, making the write core's
  behaviour independent of which SQLAlchemy a venv resolves.
* A refusal AT ``_expire_active`` (a Core-level statement, no flush) is
  shape (3): zero ``rollback`` events at catch, the backend left ``idle
  in transaction (aborted)``, the caller's own ``await db.rollback()``
  emits exactly ONE further event (ADDENDUM X-1 — this is the corrected,
  STRONGER form: the *total* of 1 cannot discriminate the two shapes,
  the *split* — events at catch vs. events after the explicit rollback —
  can).
* A refusal at an ORM ``add()`` + ``flush()``/``commit()`` is shape (1):
  ONE ``rollback`` event fires INSIDE the failing call (SQLAlchemy has
  already rolled back the connection before control returns), the
  backend is left ``idle``, and the caller's ``await db.rollback()``
  emits NO further event (nothing left to roll back at the connection
  level — ADDENDUM X-1).
* SQLAlchemy's savepoint-scoped nested-transaction primitive (shape 2)
  is NEVER used anywhere in this module — a grep-based documentation
  check on the package (spec §11) backs this; the real control is the
  connection-event pin in the harness (``tests/test_acl_write_path_rls.py``).
* Bulk (:func:`bulk_write_acl_entries`) additionally FLUSHES per op
  (``await db.flush()`` after each op's ``_audit.record`` — ADDENDUM
  V-2), so a refusal always surfaces INSIDE the iteration that caused
  it — never one iteration late via the next op's autoflush — and
  ``attempted["op_index"]`` is therefore the refused op's index,
  exactly, on EITHER abort shape.

**Denial classification (spec §5.6) — a genuine, MEASURED correction to
the spec's own text, recorded here because the build record is where a
measured correction belongs (ADDENDUM X-2's own lesson: a "restated"
claim is unproven until measured).** Spec §5.3 states the group-
principal refusal is "classified by constraint name to
``acl_denied_principal_type``", naming
``ck_console_acl_entries_principal_type`` as the constraint. **Measured
against real ``postgres:16`` (this build): migration 031 declares a
SECOND CHECK, ``ck_console_acl_entries_principal_model_matches_type``,
whose three OR-branches exhaustively enumerate ONLY
``user``/``role``/``public`` — a ``principal_type`` outside that set
violates BOTH constraints simultaneously, and Postgres reports
``ck_console_acl_entries_principal_model_matches_type`` (not
``ck_console_acl_entries_principal_type``) for the row Postgres actually
evaluates first.** :func:`_classify` therefore recognises EITHER
constraint name as the same ``acl_denied_principal_type`` failure_class
— see the build record's own R-8 finding for the full measurement (both
constraints solo-neutered independently, both stay GREEN; only the JOINT
neuter reproduces RED, meaning migration 031's group-principal defence
is doubly redundant, not singly-owned by the constraint spec §11's
neuter instruction names).
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.db.models import ConsoleAclEntry
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.services.console_acl import (
    PRINCIPAL_MODEL_ROLE,
    PRINCIPAL_MODEL_USER,
    PRINCIPAL_TYPE_PUBLIC,
    PRINCIPAL_TYPE_ROLE,
    PRINCIPAL_TYPE_USER,
    AclGrantOp,
)
from audittrace.services.console_acl._errors import (
    AclBulkRolledBackError,
    AclPastExpiryError,
    AclPrincipalTypeRefused,
    AclWriteRefused,
)
from audittrace.services.console_store import build_write_stamp

logger = logging.getLogger(__name__)


def _acl_audit() -> Any:
    """Lazy import of :mod:`audittrace.services.console_acl._audit` —
    breaks an import cycle. ``dependencies.py`` imports names FROM
    ``console_acl.__init__``; ``console_acl.__init__``'s bottom import
    block imports ``_postgres.py``, which imports THIS module at
    package-import time; ``_audit.py`` (B's, frozen — not editable to
    fix this here) itself imports ``dependencies`` at its own top level.
    A module-level import of ``_audit`` here would close that loop while
    ``console_acl.__init__`` is still mid-import. Safe: by the time any
    function in this module is actually CALLED (app startup / test setup
    has already completed), every one of these modules has finished
    importing — same precedented pattern as ``_ownership.py``'s
    ``_owns_agent``/``_owns_prompt_group`` local ``dependencies``
    imports."""
    from audittrace.services.console_acl import _audit  # noqa: PLC0415

    return _audit


# ── The two Postgres CHECK constraints that (redundantly — see module
# docstring) refuse a principal_type outside {user, public, role}. Both
# map to the SAME failure_class; which one appears in a given exception's
# text depends on Postgres's own (unspecified, measured-not-assumed)
# constraint-evaluation order, not on anything this module controls.
_PRINCIPAL_TYPE_CONSTRAINTS: tuple[str, ...] = (
    "ck_console_acl_entries_principal_type",
    "ck_console_acl_entries_principal_model_matches_type",
)

# constraint/index names this module's classifier recognises by NAME —
# a substring match on the live exception text (spec §5.6), never a
# rendered-SQL copy (barred as a RED target, spec §9).
_CONSTRAINT_NAME_RE = re.compile(r'"([A-Za-z0-9_]+)"')

_PRINCIPAL_MODEL_BY_TYPE: dict[str, str | None] = {
    PRINCIPAL_TYPE_USER: PRINCIPAL_MODEL_USER,
    PRINCIPAL_TYPE_ROLE: PRINCIPAL_MODEL_ROLE,
    PRINCIPAL_TYPE_PUBLIC: None,
}


def _principal_model(principal_type: str) -> str | None:
    """``principal_model`` derived from ``principal_type`` — as the fork
    does for the three permitted types (ADDENDUM U-9: the fork's
    ``GROUP -> 'Group'`` branch is not mirrored). **Deliberately NO
    app-level rejection of an unrecognised ``principal_type`` here**
    (spec 5.3 — R-8 forbids an application pre-check; the control is
    migration 031's DB-level CHECK constraints). An unrecognised type
    falls back to ``None`` and is left for the database to refuse."""
    return _PRINCIPAL_MODEL_BY_TYPE.get(principal_type)


def _row_to_dict(row: ConsoleAclEntry) -> dict[str, Any]:
    """Same shape as ``_postgres.py``'s ``_entry_to_dict`` — duplicated
    (not imported) because that module is gated additions-only for A1
    (spec §11 / ADDENDUM U-5(iii)): importing from it here would force a
    change to its body, or a circular import the other way around."""
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
        "trace_id": row.trace_id,
    }


def _key_clause(
    *,
    principal_type: str,
    principal_id: str | None,
    resource_type: str,
    resource_id: str,
    tenant_id: str | None,
) -> Any:
    """The five-column key O-3's unique index is scoped on — used by
    :func:`_expire_active` for grant/bulk's re-grant expiry."""
    clauses = [
        ConsoleAclEntry.principal_type == principal_type,
        ConsoleAclEntry.resource_type == resource_type,
        ConsoleAclEntry.resource_id == resource_id,
    ]
    clauses.append(
        ConsoleAclEntry.principal_id.is_(None)
        if principal_id is None
        else ConsoleAclEntry.principal_id == principal_id
    )
    clauses.append(
        ConsoleAclEntry.tenant_id.is_(None)
        if tenant_id is None
        else ConsoleAclEntry.tenant_id == tenant_id
    )
    return sa.and_(*clauses)


async def _expire_active(
    session: AsyncSession,
    *,
    principal_type: str,
    principal_id: str | None,
    resource_type: str,
    resource_id: str,
    tenant_id: str | None,
    now_ms: int,
) -> list[str]:
    """ORM-enabled Core UPDATE (ADDENDUM V-1) — touches ONLY
    ``expired_at_ms``/``updated_at_ms`` (Q-4.1); never ``perm_bits``,
    ``user_sub``, ``granted_by``, ``granted_at_ms`` or the grant's own
    ``trace_id``. Returns the ids it expired (``RETURNING id``)."""
    stmt = (
        sa.update(ConsoleAclEntry)
        .where(
            _key_clause(
                principal_type=principal_type,
                principal_id=principal_id,
                resource_type=resource_type,
                resource_id=resource_id,
                tenant_id=tenant_id,
            ),
            ConsoleAclEntry.expired_at_ms.is_(None),
        )
        .values(expired_at_ms=now_ms, updated_at_ms=now_ms)
        .returning(ConsoleAclEntry.id)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _classify(exc: Exception) -> tuple[str, str]:
    """The closed mapping (spec §5.6, corrected by this module's own
    measurement — see module docstring). Reads the LIVE exception text,
    never a hand-typed copy of a constraint's SQL, so a constraint rename
    in the migration reflects here automatically."""
    _audit = _acl_audit()
    text = str(exc)
    for constraint in _PRINCIPAL_TYPE_CONSTRAINTS:
        if constraint in text:
            return _audit.FAILURE_CLASS_ACL_DENIED_PRINCIPAL_TYPE, constraint
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate:
        return _audit.FAILURE_CLASS_ACL_DENIED_POLICY, str(sqlstate)
    match = _CONSTRAINT_NAME_RE.search(text)
    if match:
        return _audit.FAILURE_CLASS_ACL_DENIED_POLICY, match.group(1)
    return _audit.FAILURE_CLASS_ACL_DENIED_POLICY, type(exc).__name__


async def _write_denial(
    *,
    user_context: UserContext,
    op: str,
    principal_type: str,
    principal_id: str | None,
    resource_type: str,
    resource_id: str,
    perm_bits: int,
    attempted: Any,
    exc: Exception,
    tenant_id: str | None = None,
    failure_class_override: str | None = None,
) -> AclWriteRefused:
    """Takes NO session and touches none (spec §5.0): ``_classify(exc)``
    (or the caller's override, for the two cases — past-expiry and
    bulk-rollback — whose ``failure_class`` is determined by WHERE the
    refusal happened, not by the exception's own text) -> ``_audit.
    record_denial(...)`` -> returns the named error to raise. The ONLY
    caller of ``record_denial`` in this module."""
    _audit = _acl_audit()
    if failure_class_override is not None:
        failure_class = failure_class_override
        db_error_class = type(exc).__name__
    else:
        failure_class, db_error_class = _classify(exc)
    await _audit.record_denial(
        user_context=user_context,
        op=op,
        principal_type=principal_type,
        principal_id=principal_id,
        resource_type=resource_type,
        resource_id=resource_id,
        perm_bits=perm_bits,
        failure_class=failure_class,
        predicate_or_attempted_row=attempted,
        db_error_class=db_error_class,
        tenant_id=tenant_id,
    )
    failure_class_to_error: dict[str, type[AclWriteRefused]] = {
        _audit.FAILURE_CLASS_ACL_DENIED_POLICY: AclWriteRefused,
        _audit.FAILURE_CLASS_ACL_DENIED_PRINCIPAL_TYPE: AclPrincipalTypeRefused,
        _audit.FAILURE_CLASS_ACL_DENIED_PAST_EXPIRY: AclPastExpiryError,
        _audit.FAILURE_CLASS_ACL_DENIED_BULK_ROLLBACK: AclBulkRolledBackError,
    }
    error_cls = failure_class_to_error[failure_class]
    return error_cls(
        str(exc), failure_class=failure_class, db_error_class=db_error_class
    )


class _PostgresAclWrites:
    """Mixin providing the ACL write path for
    :class:`~audittrace.services.console_acl._postgres.
    PostgresConsoleAclEntriesService`. Declares ``_session_factory`` as a
    type-only attribute — the concrete class's ``__init__`` sets it; this
    mixin defines no ``__init__`` of its own."""

    _session_factory: async_sessionmaker[AsyncSession]

    @log_call(logger=logger)
    async def grant_permission(
        self,
        user_context: UserContext,
        *,
        principal_type: str,
        principal_id: str | None,
        resource_type: str,
        resource_id: str,
        perm_bits: int,
        role_id: str | None = None,
        expired_at_ms: int | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        _audit = _acl_audit()
        stamp = build_write_stamp(user_context)

        # R-2 (spec §5.2) — refused BEFORE any I/O, by name.
        if expired_at_ms is not None and expired_at_ms <= stamp.now_ms:
            past_expiry_exc = ValueError(
                f"expired_at_ms={expired_at_ms} is not strictly after "
                f"now_ms={stamp.now_ms}"
            )
            raise await _write_denial(
                user_context=user_context,
                op="grantPermission",
                principal_type=principal_type,
                principal_id=principal_id,
                resource_type=resource_type,
                resource_id=resource_id,
                perm_bits=perm_bits,
                attempted={"expired_at_ms": expired_at_ms, "now_ms": stamp.now_ms},
                exc=past_expiry_exc,
                tenant_id=tenant_id,
                failure_class_override=_audit.FAILURE_CLASS_ACL_DENIED_PAST_EXPIRY,
            ) from past_expiry_exc

        attempted = {
            "principal_type": principal_type,
            "principal_id": principal_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "perm_bits": perm_bits,
            "tenant_id": tenant_id,
        }

        async with self._session_factory() as db:
            # Stage 1 — the domain write. A refusal here is a genuine DB
            # denial, classified by _classify(exc) inside _write_denial.
            try:
                expired_ids = await _expire_active(
                    db,
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    tenant_id=tenant_id,
                    now_ms=stamp.now_ms,
                )
                new_row = ConsoleAclEntry(
                    id=str(uuid.uuid4()),
                    user_sub=stamp.user_sub,
                    principal_type=principal_type,
                    principal_id=principal_id,
                    principal_model=_principal_model(principal_type),
                    resource_type=resource_type,
                    resource_id=resource_id,
                    perm_bits=perm_bits,
                    tenant_id=tenant_id,
                    role_id=role_id,
                    granted_by=stamp.user_sub,
                    granted_at_ms=stamp.now_ms,
                    expired_at_ms=expired_at_ms,
                    created_at_ms=stamp.now_ms,
                    updated_at_ms=stamp.now_ms,
                    trace_id=stamp.trace_id,
                )
                db.add(new_row)
            except Exception as exc:  # noqa: BLE001 - reclassified below
                await db.rollback()
                raise await _write_denial(
                    user_context=user_context,
                    op="grantPermission",
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    perm_bits=perm_bits,
                    attempted=attempted,
                    exc=exc,
                    tenant_id=tenant_id,
                ) from exc

            # Stage 2 — the audit row (N4, caller half, spec §5.6's
            # closing row). A failure HERE is never re-classified as a
            # generic policy denial and never wrapped in one of the
            # named AclWriteRefused subclasses — the ORIGINAL exception
            # is re-raised, exactly as 5.6 states, after a best-effort
            # acl_audit_write_failed denial row (this module never
            # catches-and-discards the writer's own failure — that is
            # the "caller's half" _audit.py's docstring names as A's
            # obligation).
            try:
                await _audit.record(
                    db,
                    user_context=user_context,
                    op="grantPermission",
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    perm_bits=perm_bits,
                    acl_entry_ids=[new_row.id],
                    expired_ids=expired_ids,
                    expired_at_ms=expired_at_ms,
                    tenant_id=tenant_id,
                )
            except Exception as audit_exc:  # noqa: BLE001 - N4, re-raised below
                await db.rollback()
                await _audit.record_denial(
                    user_context=user_context,
                    op="grantPermission",
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    perm_bits=perm_bits,
                    failure_class=_audit.FAILURE_CLASS_ACL_AUDIT_WRITE_FAILED,
                    predicate_or_attempted_row=attempted,
                    db_error_class=type(audit_exc).__name__,
                    tenant_id=tenant_id,
                )
                raise

            # Stage 3 — the commit. A refusal here (e.g. 033's O-3
            # uniqueness racing a concurrent grant) is a genuine DB
            # denial again.
            try:
                await db.commit()
            except Exception as exc:  # noqa: BLE001 - reclassified below
                await db.rollback()
                raise await _write_denial(
                    user_context=user_context,
                    op="grantPermission",
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    perm_bits=perm_bits,
                    attempted=attempted,
                    exc=exc,
                    tenant_id=tenant_id,
                ) from exc

            return _row_to_dict(new_row)

    @log_call(logger=logger)
    async def revoke_permission(
        self,
        user_context: UserContext,
        *,
        principal_type: str,
        principal_id: str | None,
        resource_type: str,
        resource_id: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """**2b-core-A2 scope (ADDENDUM U-6) — not built in A1.** A real
        gap, not a stub: zero production callers of this method exist in
        ``src/`` today, and no route reaches it until 2c."""
        raise NotImplementedError(
            "revoke_permission is 2b-core-A2 scope — see ADDENDUM U-6"
        )

    @log_call(logger=logger)
    async def modify_permission_bits(
        self,
        user_context: UserContext,
        *,
        principal_type: str,
        principal_id: str | None,
        resource_type: str,
        resource_id: str,
        add_bits: int | None = None,
        remove_bits: int | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        """**2b-core-A2 scope (ADDENDUM U-6) — not built in A1.**"""
        raise NotImplementedError(
            "modify_permission_bits is 2b-core-A2 scope — see ADDENDUM U-6"
        )

    @log_call(logger=logger)
    async def bulk_write_acl_entries(
        self,
        user_context: UserContext,
        ops: list[AclGrantOp],
    ) -> dict[str, Any]:
        _audit = _acl_audit()
        stamp = build_write_stamp(user_context)
        op_count = len(ops)
        acl_entry_ids: list[str] = []
        all_expired_ids: list[str] = []

        async with self._session_factory() as db:
            for index, grant_op in enumerate(ops):
                try:
                    if (
                        grant_op.expired_at_ms is not None
                        and grant_op.expired_at_ms <= stamp.now_ms
                    ):
                        raise ValueError(  # noqa: TRY301 - caught immediately below
                            f"expired_at_ms={grant_op.expired_at_ms} is not "
                            f"strictly after now_ms={stamp.now_ms}"
                        )
                    expired_ids = await _expire_active(
                        db,
                        principal_type=grant_op.principal_type,
                        principal_id=grant_op.principal_id,
                        resource_type=grant_op.resource_type,
                        resource_id=grant_op.resource_id,
                        tenant_id=grant_op.tenant_id,
                        now_ms=stamp.now_ms,
                    )
                    new_row = ConsoleAclEntry(
                        id=str(uuid.uuid4()),
                        user_sub=stamp.user_sub,
                        principal_type=grant_op.principal_type,
                        principal_id=grant_op.principal_id,
                        principal_model=_principal_model(grant_op.principal_type),
                        resource_type=grant_op.resource_type,
                        resource_id=grant_op.resource_id,
                        perm_bits=grant_op.perm_bits,
                        tenant_id=grant_op.tenant_id,
                        role_id=grant_op.role_id,
                        granted_by=stamp.user_sub,
                        granted_at_ms=stamp.now_ms,
                        expired_at_ms=grant_op.expired_at_ms,
                        created_at_ms=stamp.now_ms,
                        updated_at_ms=stamp.now_ms,
                        trace_id=stamp.trace_id,
                    )
                    db.add(new_row)
                    await _audit.record(
                        db,
                        user_context=user_context,
                        op="bulkWriteAclEntries",
                        principal_type=grant_op.principal_type,
                        principal_id=grant_op.principal_id,
                        resource_type=grant_op.resource_type,
                        resource_id=grant_op.resource_id,
                        perm_bits=grant_op.perm_bits,
                        acl_entry_ids=[new_row.id],
                        expired_ids=expired_ids,
                        expired_at_ms=grant_op.expired_at_ms,
                        tenant_id=grant_op.tenant_id,
                    )
                    # ADDENDUM V-2 — flush INSIDE the iteration so a
                    # refusal (at the UPDATE, shape 3, or here, shape 1)
                    # surfaces before op index+1 ever runs; op_index is
                    # therefore exact on EITHER abort shape.
                    await db.flush()
                except Exception as exc:  # noqa: BLE001 - reclassified below
                    await db.rollback()
                    raise await _write_denial(
                        user_context=user_context,
                        op="bulkWriteAclEntries",
                        principal_type=grant_op.principal_type,
                        principal_id=grant_op.principal_id,
                        resource_type=grant_op.resource_type,
                        resource_id=grant_op.resource_id,
                        perm_bits=grant_op.perm_bits,
                        attempted={
                            "op_index": index,
                            "op": {
                                "principal_type": grant_op.principal_type,
                                "principal_id": grant_op.principal_id,
                                "resource_type": grant_op.resource_type,
                                "resource_id": grant_op.resource_id,
                                "perm_bits": grant_op.perm_bits,
                                "tenant_id": grant_op.tenant_id,
                            },
                            "op_count": op_count,
                        },
                        exc=exc,
                        tenant_id=grant_op.tenant_id,
                        failure_class_override=(
                            _audit.FAILURE_CLASS_ACL_DENIED_BULK_ROLLBACK
                        ),
                    ) from exc
                acl_entry_ids.append(new_row.id)
                all_expired_ids.extend(expired_ids)

            # ADDENDUM V-2 — nothing left pending; this commit cannot
            # itself be refused by anything this loop already staged.
            await db.commit()

        return {"acl_entry_ids": acl_entry_ids, "expired_ids": all_expired_ids}

    @log_call(logger=logger)
    async def delete_acl_entries(
        self,
        user_context: UserContext,
        predicates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """**2b-core-A2 scope (ADDENDUM U-6) — not built in A1.**"""
        raise NotImplementedError(
            "delete_acl_entries is 2b-core-A2 scope — see ADDENDUM U-6"
        )
