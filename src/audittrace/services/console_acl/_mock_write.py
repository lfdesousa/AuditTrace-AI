"""The MOCK's ACL write path — Sovereign Authorization Layer EPIC,
**ACL 2b-core-A1**
(``2026-09-26-SPEC-acl-2b-core-A-write-path.md`` §7).

``_MockAclWrites`` is a MIXIN inherited by
:class:`~audittrace.services.console_acl._mock.MockConsoleAclEntriesService`
— kept in its own module for the SAME "additions-only diff" reason
``_postgres_write.py`` documents for ``_postgres.py`` (spec §11 /
ADDENDUM U-5(iii)'s gate).

**Same writer, same rows (spec §7).** The mock's write methods produce
audit rows through the REAL ``services/console_acl/_audit`` writer,
exactly like the Postgres implementation — success rows via
``_audit.record(db, …)`` on a session from
``get_postgres_factory().get_session_factory()`` **committed by the
mock itself** (the mock has no real domain transaction for the audit
row to ride inside, unlike the Postgres path where the ACL row and the
audit row share one transaction); denials via ``_audit.record_denial``
(which always opens and commits its own independent session
regardless of caller). **A mock write with no wired factory raises** —
fail-closed, never a silent in-memory-only "audit": ``get_postgres_
factory()`` (``dependencies.py``) raises ``KeyError`` when
``postgres_factory`` was never registered on the active container.

**R-8 (group principals) — the mock's stand-in for migration 031's DB
CHECK.** The mock has no CHECK constraint, so it refuses
``principal_type not in ALLOWED_PRINCIPAL_TYPES`` itself, with the SAME
``AclPrincipalTypeRefused`` + denial row the Postgres path produces via
the (doubly redundant — see ``_postgres_write.py``'s module docstring)
DB constraints.

**All-or-nothing bulk, in-memory (O-4).** Since the mock's ``_entries``
list mutations are not themselves transactional, a refused op in
:func:`bulk_write_acl_entries` must UNDO every already-applied in-memory
mutation from earlier ops in the SAME call (newly-expired rows restored
to active, newly-inserted rows never appended) as well as rolling back
the shared audit session — mirroring the Postgres path's "a rolled-back
write with a surviving audit row is a false record" rule at the
in-memory layer too.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.services.console_acl._errors import (
    AclBulkRolledBackError,
    AclPastExpiryError,
    AclPrincipalTypeRefused,
    AclWriteRefused,
)
from audittrace.services.console_store import build_write_stamp

if TYPE_CHECKING:
    # B6, MEASURED (this round) — SAME rationale as
    # ``_postgres_write.py``'s identical block: a module-level
    # (unconditionally executed) import of a name from
    # ``audittrace.services.console_acl`` (the PACKAGE's own
    # ``__init__.py``) makes the pinned pre-commit mypy hook (v1.8.0)
    # fail from a cold cache on this file's `@log_call`-decorated mixin
    # methods, because resolving ``__init__.py`` pulls in ``_mock.py``
    # (its own bottom import block) as a dependency. A
    # ``TYPE_CHECKING``-only import of the SAME name does not trigger
    # it — reproduced and confirmed in isolation (see
    # ``_postgres_write.py``'s module docstring for the full
    # measurement). ``AclGrantOp`` is used ONLY as a type annotation
    # below (``ops: list[AclGrantOp]``) — under ``from __future__
    # import annotations`` it is never evaluated at runtime.
    from audittrace.services.console_acl import AclGrantOp
    from audittrace.services.console_acl._mock import _MockAclEntry

logger = logging.getLogger(__name__)


def _entry_cls() -> type[_MockAclEntry]:
    """Lazy import of :class:`~audittrace.services.console_acl._mock.
    _MockAclEntry` — SAME rationale as the ``TYPE_CHECKING`` import
    above. Unlike the constants above, this name is INSTANTIATED at
    runtime (not merely used in annotations), so it cannot be
    ``TYPE_CHECKING``-only; this accessor is the lazy-import
    equivalent for a runtime-needed class."""
    from audittrace.services.console_acl._mock import _MockAclEntry  # noqa: PLC0415

    return _MockAclEntry


def _acl_constants() -> dict[str, str | None]:
    """Lazy import of the write path's principal-type/model constants —
    SAME cycle and SAME fix rationale as the ``TYPE_CHECKING`` import
    above. Memoisation is unnecessary here (this dict is tiny and built
    fresh per call, mirroring ``_postgres_write.py``'s twin)."""
    from audittrace.services.console_acl import (  # noqa: PLC0415
        PRINCIPAL_MODEL_ROLE,
        PRINCIPAL_MODEL_USER,
        PRINCIPAL_TYPE_PUBLIC,
        PRINCIPAL_TYPE_ROLE,
        PRINCIPAL_TYPE_USER,
    )

    return {
        PRINCIPAL_TYPE_USER: PRINCIPAL_MODEL_USER,
        PRINCIPAL_TYPE_ROLE: PRINCIPAL_MODEL_ROLE,
        PRINCIPAL_TYPE_PUBLIC: None,
    }


def _allowed_principal_types() -> frozenset[str]:
    """Lazy import of ``ALLOWED_PRINCIPAL_TYPES`` — same rationale."""
    from audittrace.services.console_acl import (  # noqa: PLC0415
        ALLOWED_PRINCIPAL_TYPES,
    )

    return ALLOWED_PRINCIPAL_TYPES


def _principal_model(principal_type: str) -> str | None:
    return _acl_constants().get(principal_type)


def _acl_audit() -> Any:
    """Lazy import — same import-cycle rationale as ``_postgres_write.
    py``'s ``_acl_audit`` (``_audit.py`` imports ``dependencies``, which
    imports names from ``console_acl.__init__``, which (via
    ``_mock.py``) imports this module at package-import time)."""
    from audittrace.services.console_acl import _audit  # noqa: PLC0415

    return _audit


def _postgres_factory() -> Any:
    """Lazy import of :func:`audittrace.dependencies.get_postgres_factory`
    — SAME cycle as :func:`_acl_audit` (``dependencies.py`` imports names
    from ``console_acl.__init__`` at its own top level)."""
    from audittrace.dependencies import get_postgres_factory  # noqa: PLC0415

    return get_postgres_factory()


def _key_matches(
    row: _MockAclEntry,
    *,
    principal_type: str,
    principal_id: str | None,
    resource_type: str,
    resource_id: str,
    tenant_id: str | None,
) -> bool:
    return (
        row.principal_type == principal_type
        and row.principal_id == principal_id
        and row.resource_type == resource_type
        and row.resource_id == resource_id
        and row.tenant_id == tenant_id
    )


async def _mock_denial(
    *,
    user_context: UserContext,
    op: str,
    principal_type: str,
    principal_id: str | None,
    resource_type: str,
    resource_id: str,
    perm_bits: int,
    attempted: Any,
    failure_class: str,
    tenant_id: str | None = None,
    db_error_class: str | None = None,
) -> AclWriteRefused:
    """The mock's counterpart to ``_postgres_write._write_denial`` — no
    exception to classify (every mock refusal is an APP-LEVEL check, not
    a DB error), so the caller names ``failure_class`` directly.

    ``db_error_class`` follows ratified spec §5.6 when the caller knows
    the literal (past-expiry: ``"app:past_expiry"``; principal-type: the
    constraint name the mock stands in for,
    ``"ck_console_acl_entries_principal_type"``, spec §7). When the
    caller does not supply one (bulk-rollback, whose underlying cause
    the mock does not distinguish), falls back to a disclosed
    ``"mock:<failure_class>"`` placeholder — never a value that could be
    mistaken for a real Postgres one."""
    _audit = _acl_audit()
    if db_error_class is None:
        db_error_class = f"mock:{failure_class}"
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
        f"mock: {op} refused ({failure_class})",
        failure_class=failure_class,
        db_error_class=db_error_class,
    )


class _MockAclWrites:
    """Mixin providing the ACL write path for
    :class:`~audittrace.services.console_acl._mock.
    MockConsoleAclEntriesService`. Declares ``_entries`` as a type-only
    attribute — the concrete class's ``__init__`` sets it."""

    _entries: list[_MockAclEntry]

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
        attempted = {
            "principal_type": principal_type,
            "principal_id": principal_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "perm_bits": perm_bits,
            "tenant_id": tenant_id,
        }

        # R-2 — refused BEFORE any I/O.
        if expired_at_ms is not None and expired_at_ms <= stamp.now_ms:
            raise await _mock_denial(
                user_context=user_context,
                op="grantPermission",
                principal_type=principal_type,
                principal_id=principal_id,
                resource_type=resource_type,
                resource_id=resource_id,
                perm_bits=perm_bits,
                attempted={"expired_at_ms": expired_at_ms, "now_ms": stamp.now_ms},
                failure_class=_audit.FAILURE_CLASS_ACL_DENIED_PAST_EXPIRY,
                tenant_id=tenant_id,
                db_error_class="app:past_expiry",
            )

        # R-8 — the mock's stand-in for migration 031's DB CHECK.
        if principal_type not in _allowed_principal_types():
            raise await _mock_denial(
                user_context=user_context,
                op="grantPermission",
                principal_type=principal_type,
                principal_id=principal_id,
                resource_type=resource_type,
                resource_id=resource_id,
                perm_bits=perm_bits,
                attempted=attempted,
                failure_class=_audit.FAILURE_CLASS_ACL_DENIED_PRINCIPAL_TYPE,
                tenant_id=tenant_id,
                db_error_class="ck_console_acl_entries_principal_type",
            )

        expired_ids = [
            row.id
            for row in self._entries
            if row.expired_at_ms is None
            and _key_matches(
                row,
                principal_type=principal_type,
                principal_id=principal_id,
                resource_type=resource_type,
                resource_id=resource_id,
                tenant_id=tenant_id,
            )
        ]
        # Undo log for the N4 failure path below — restores the EXACT
        # prior updated_at_ms, never a hardcoded 0 (a partial mutation
        # must not survive a failed audit write, S3).
        prior_updated_at_ms: dict[str, int] = {}
        for row in self._entries:
            if row.id in expired_ids:
                prior_updated_at_ms[row.id] = row.updated_at_ms
                row.expired_at_ms = stamp.now_ms
                row.updated_at_ms = stamp.now_ms

        new_row = _entry_cls()(
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
        )

        pg = _postgres_factory()
        session_factory = pg.get_session_factory()
        async with session_factory() as db:
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
                await db.commit()
            except Exception as audit_exc:  # noqa: BLE001 - N4, re-raised below
                await db.rollback()
                # Undo the in-memory expiry — fail-closed, no partial
                # mutation survives a failed audit write.
                for row in self._entries:
                    if row.id in expired_ids:
                        row.expired_at_ms = None
                        row.updated_at_ms = prior_updated_at_ms[row.id]
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

        self._entries.append(new_row)
        return new_row.to_dict()

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
        """**2b-core-A2 scope (ADDENDUM U-6) — not built in A1.**"""
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
        staged_new_rows: list[_MockAclEntry] = []
        # (row, prior_expired_at_ms, prior_updated_at_ms) — undo log for
        # the in-memory expiry mutations, since Python list mutation is
        # not itself transactional (O-4 all-or-nothing).
        undo_log: list[tuple[_MockAclEntry, int | None, int]] = []

        pg = _postgres_factory()
        session_factory = pg.get_session_factory()

        async def _undo() -> None:
            for row, prior_expired, prior_updated in reversed(undo_log):
                row.expired_at_ms = prior_expired
                row.updated_at_ms = prior_updated

        async with session_factory() as db:
            for index, grant_op in enumerate(ops):
                attempted = {
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
                }
                try:
                    if grant_op.principal_type not in _allowed_principal_types():
                        raise ValueError(  # noqa: TRY301 - caught immediately below
                            f"principal_type={grant_op.principal_type!r} not allowed"
                        )
                    if (
                        grant_op.expired_at_ms is not None
                        and grant_op.expired_at_ms <= stamp.now_ms
                    ):
                        raise ValueError(  # noqa: TRY301 - caught immediately below
                            f"expired_at_ms={grant_op.expired_at_ms} is not "
                            f"strictly after now_ms={stamp.now_ms}"
                        )
                    expired_ids: list[str] = []
                    for row in self._entries:
                        if row.expired_at_ms is None and _key_matches(
                            row,
                            principal_type=grant_op.principal_type,
                            principal_id=grant_op.principal_id,
                            resource_type=grant_op.resource_type,
                            resource_id=grant_op.resource_id,
                            tenant_id=grant_op.tenant_id,
                        ):
                            undo_log.append((row, row.expired_at_ms, row.updated_at_ms))
                            row.expired_at_ms = stamp.now_ms
                            row.updated_at_ms = stamp.now_ms
                            expired_ids.append(row.id)
                    new_row = _entry_cls()(
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
                    )
                    staged_new_rows.append(new_row)
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
                    await db.flush()
                except Exception as exc:  # noqa: BLE001 - reclassified below
                    await db.rollback()
                    await _undo()
                    raise await _mock_denial(
                        user_context=user_context,
                        op="bulkWriteAclEntries",
                        principal_type=grant_op.principal_type,
                        principal_id=grant_op.principal_id,
                        resource_type=grant_op.resource_type,
                        resource_id=grant_op.resource_id,
                        perm_bits=grant_op.perm_bits,
                        attempted=attempted,
                        failure_class=_audit.FAILURE_CLASS_ACL_DENIED_BULK_ROLLBACK,
                        tenant_id=grant_op.tenant_id,
                    ) from exc
                acl_entry_ids.append(new_row.id)
                all_expired_ids.extend(expired_ids)

            await db.commit()

        self._entries.extend(staged_new_rows)
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
