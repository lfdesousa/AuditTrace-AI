"""The sovereign ACL write path — Sovereign Authorization Layer EPIC,
**ACL 2b-core-A1**
(``2026-09-26-SPEC-acl-2b-core-A-write-path.md`` + ADDENDA U/V/W/X, and
the **Y round** — ADDENDA Y/Z/AA/AB/AC/AD/AE/AF).

**Y round (this build) — the write path now supersedes everything the
read path considers active, not merely NULL-expiry rows.** ADDENDUM
Y-0 measured a live authorization downgrade: a time-limited grant
(``expired_at_ms`` in the future) survived a re-grant untouched,
because :func:`_expire_active`'s predicate (``expired_at_ms IS NULL``)
was narrower than the read path's active predicate (``expired_at_ms IS
NULL OR expired_at_ms > now``, ``_postgres.py``'s ``_not_expired_
clause``) — every row the reader counted active and the writer failed
to expire was exactly a time-limited grant, and ``get_effective_
permissions`` ORs all active rows, so a stale future-expiry 15 masked
a fresh 1. ADDENDUM AB (closing the two-drift-lesson of U-1/Y-1) rules
that the fix is a **derivation, not a second copy of the predicate**:
:func:`_active_clause` lazily imports and returns — unmodified,
verified by return-identity (ADDENDUM AC-1 item 3) —
``_postgres._not_expired_clause(now_ms)``, the SAME object the read
path's every authorization decision uses. ADDENDUM AD/AE/AF's
``AC-T-HIST`` behavioural history-equality guard (``tests/
test_acl_write_path_lapsed_clock.py``) is what actually proves this,
after five structural pins fell to five successive escapes (spelling,
deletion, result-discarding, threshold, grace) that all preserved the
code's SHAPE while breaking its behaviour — ``AB-G``/``AB-G+`` (the
grep and the positive call-site pin, ``tests/test_acl_write_path_rls.
py``) are kept only as drift-detectors from ADDENDUM AD on, never as
the correctness guard.

``_PostgresAclWrites`` is a MIXIN inherited by
:class:`~audittrace.services.console_acl._postgres.
PostgresConsoleAclEntriesService` — kept in its own module (rather than
edited into ``_postgres.py`` directly) so that file's diff stays exactly
"an import line + the one class statement gaining this mixin" (spec §11 /
ADDENDUM U-5(iii)'s gate: ``git diff -U0 main..HEAD -- _postgres.py
_mock.py`` must touch only import lines and the two ``class …(``
statements — any hunk inside a READ method body is a REJECT).

**Scope — A1 + A2.** :func:`grant_permission` and
:func:`bulk_write_acl_entries` are A1. :func:`revoke_permission`,
:func:`modify_permission_bits` and :func:`delete_acl_entries` are
**2b-core-A2**
(``2026-09-28-SPEC-acl-2b-core-A2-revoke-modify-delete-CONSOLIDATED-v4.md``)
— zero production callers of any of the five write methods exist in
``src/`` today; no route reaches any of them until 2c.

**A2's helpers (spec §4.0), one SQL spelling each, shared by every A2
method:** :func:`_visible_ids` (C4/C8 — the "how many rows can the
caller SEE" SELECT, always evaluated BEFORE the corresponding UPDATE,
at the same ``stamp.now_ms``, spec E-G) and :func:`_active_rows`
(C6 — the entity-returning twin ``modify_permission_bits`` mutates
in place, in Python, before superseding them) both derive their
"still active" half from :func:`_active_clause`, the SAME function
A1's :func:`_expire_active` already used — never a second spelling.
:func:`_expire_where` (C5/C7/C10) now holds A1's and A2's ONE UPDATE
statement; :func:`_expire_active` is unchanged in signature and
return value (spec S-c: A1's ``expired_ids`` RETURNING order is
untouched) but is now a thin wrapper over :func:`_expire_where`.
:func:`_predicate_clause` (C3, new) is :func:`delete_acl_entries`'s
own key-matcher — **one ``==`` per key PRESENT in the caller's dict,
never** :func:`_key_clause`'s ``.get()`` reuse, which would silently
turn an absent key into an ``IS NULL`` match (spec §4.0's ``keyclause``
escape).

**The tenant-carriage fix (A2v3-BL-1, spec §4.2/§4.5 W1/W3).**
``modify_permission_bits``'s new row takes **every one of its five key
columns — including ``tenant_id`` — from the caller's ``key``
argument, never from a captured row.** The A2v3 gate reproduced a
defect where inserting the new row at ``tenant_id=None`` (as if the
column were merely carried through from whatever the SELECT happened
to return) let a LATER ``revoke_permission(tenant_id='t1')`` succeed
against the NULL-tenant row while the original ``t1`` grant stayed
active — a revocation that looks like it worked and does not. §7.4's
KSEL-t rows and §7.5's W1/W2/W3 written-value class are the guards.

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
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.db.models import ConsoleAclEntry
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
    # B6, MEASURED (this round): a module-level (unconditionally
    # executed) import of a name from ``audittrace.services.console_acl``
    # — the PACKAGE's own ``__init__.py`` — makes the pinned pre-commit
    # mypy hook (v1.8.0) fail from a COLD cache when this module is
    # checked WITHOUT ``_postgres.py`` also in the file set: resolving
    # ``__init__.py`` pulls in ``_postgres.py`` (its own bottom import
    # block) as a dependency, and mypy 1.8.0 cannot then determine the
    # type of a `@log_call`-decorated mixin method across that boundary
    # ("Cannot determine type of ... in base class _PostgresAclWrites").
    # Reproduced in isolation (a minimal two-file package with the same
    # self-referential-import + decorated-mixin-method shape) and
    # confirmed the FIX: a ``TYPE_CHECKING``-only import of the SAME name
    # does not trigger it (mypy resolves the annotation without forcing
    # eager cross-module attribute-type inference), whereas an identical
    # import outside ``TYPE_CHECKING`` does, on every cold-cache run.
    # ``AclGrantOp`` is used ONLY as a type annotation below (``ops:
    # list[AclGrantOp]``) — under ``from __future__ import annotations``
    # it is never evaluated at runtime, so this is the correct, and only
    # necessary, import site for it.
    from audittrace.services.console_acl import AclGrantOp

logger = logging.getLogger(__name__)


def _acl_constants() -> dict[str, str | None]:
    """Lazy import of the write path's principal-type/model constants.

    **Not the same kind of cycle as** :func:`_acl_audit` **below.**
    ``_acl_audit``'s lazy import breaks a genuine RUNTIME import cycle
    (a module-level import there would raise ``ImportError`` at package
    load time — measured). This one breaks a **mypy-only** resolution
    cycle: a module-level ``from audittrace.services.console_acl import
    PRINCIPAL_MODEL_ROLE, ...`` here does NOT raise at runtime (measured
    — the constants are already bound on the partially-initialised
    ``console_acl`` module by the time this file is reached from
    ``__init__.py``'s bottom import block). It is what makes mypy 1.8.0
    resolve ``_postgres.py`` as a dependency and fail to determine the
    decorated mixin methods' types, from a cold cache — the fix-round-2
    review measured this and the fix-round-3 review confirmed the
    twins agree on it.
    Called fresh on every call from :func:`_principal_model` — **not
    memoised.** There is no ``cache_info``, and
    ``_acl_constants() is _acl_constants()`` is ``False``; the dict is
    small and rebuilt each time rather than cached, since the whole
    point of this function is only to satisfy the cold-cache mypy hook,
    not to avoid repeated work."""
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


def _max_perm_bits() -> int:
    """Lazy import of ``MAX_PERM_BITS`` — same mypy-cold-cache rationale
    as :func:`_acl_constants` (called fresh each time; the value is a
    single int, not worth memoising)."""
    from audittrace.services.console_acl import MAX_PERM_BITS  # noqa: PLC0415

    return MAX_PERM_BITS


def _validate_bit_operands(add_bits: int | None, remove_bits: int | None) -> None:
    """``modify_permission_bits``'s pre-I/O validation (spec §3, D-A2-1)
    — a caller defect, never a database round-trip and never an audit
    row: ``modifyPermissionBits`` writes no DB row for a rejected call.
    Both operands ``None`` is refused (nothing to do); a non-``None``
    operand that is not an ``int`` in ``[0, MAX_PERM_BITS]`` is refused
    — unlike ``grant_permission``'s ``perm_bits`` (a column value the
    DATABASE's CHECK constraint polices, spec S-4/D-A2-7), these
    operands are combined in Python BEFORE ever reaching a column, so no
    DB control can see a bad one (an out-of-range ``remove_bits`` can
    still yield an in-range ``new_bits`` from garbage inputs)."""
    if add_bits is None and remove_bits is None:
        raise ValueError("modify_permission_bits requires add_bits and/or remove_bits")
    max_bits = _max_perm_bits()
    for name, value in (("add_bits", add_bits), ("remove_bits", remove_bits)):
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not (0 <= value <= max_bits)
        ):
            raise ValueError(f"{name}={value!r} must be an int in [0, {max_bits}]")


# ``delete_acl_entries``'s predicate validation (spec §3, D-A2-1) — every
# key a predicate dict may name; an unknown key is refused before any
# I/O. Matches the five columns _key_clause/_predicate_clause read.
_ALLOWED_PREDICATE_KEYS: frozenset[str] = frozenset(
    {"principal_type", "principal_id", "resource_type", "resource_id", "tenant_id"}
)


def _validate_predicate(pred: dict[str, Any]) -> None:
    """One ``delete_acl_entries`` predicate, validated BEFORE any I/O
    (spec §3, D-A2-1): an empty dict, an unknown key, or a value that is
    not a non-empty ``str`` (``None`` included) is a caller defect, not
    an authorization event — ``ValueError``, no audit row. A predicate
    naming none of ``principal_id``, ``resource_id`` or
    ``principal_type='public'`` is ALSO refused — every real caller
    shape anchors on at least one of the three (spec §3's "the four fork
    caller shapes all do"), so a predicate that only narrows by
    ``resource_type``/``tenant_id`` alone would be far broader than any
    shape this method is built to serve."""
    if not pred:
        raise ValueError("delete_acl_entries: predicate must not be empty")
    unknown = set(pred) - _ALLOWED_PREDICATE_KEYS
    if unknown:
        raise ValueError(
            f"delete_acl_entries: unknown predicate key(s) {sorted(unknown)!r}"
        )
    for key, value in pred.items():
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"delete_acl_entries: predicate[{key!r}]={value!r} must be a "
                "non-empty str"
            )
    if not (
        "principal_id" in pred
        or "resource_id" in pred
        or pred.get("principal_type") == "public"
    ):
        raise ValueError(
            "delete_acl_entries: predicate must name principal_id, "
            "resource_id, or principal_type='public'"
        )


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


def _principal_model(principal_type: str) -> str | None:
    """``principal_model`` derived from ``principal_type`` — as the fork
    does for the three permitted types (ADDENDUM U-9: the fork's
    ``GROUP -> 'Group'`` branch is not mirrored). **Deliberately NO
    app-level rejection of an unrecognised ``principal_type`` here**
    (spec 5.3 — R-8 forbids an application pre-check; the control is
    migration 031's DB-level CHECK constraints). An unrecognised type
    falls back to ``None`` and is left for the database to refuse."""
    return _acl_constants().get(principal_type)


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


def _active_clause(now_ms: int) -> Any:
    """The write path's "still active" predicate — DERIVED from, never a
    second spelling of, the read path's own active predicate (ADDENDUM
    Y-1/AB-1(1)). A module-level import of ``audittrace.services.
    console_acl._postgres`` here is a genuine import CYCLE (``_postgres.
    py:28`` imports THIS module before its own ``_not_expired_clause`` is
    defined at ``:54``) — the lazy, function-scoped import resolves in
    every import order, which is why it is spelled this way rather than
    at module level. **Delegates ONLY** — returns the callee's result
    completely unmodified (ADDENDUM AC-1 item 3's wrapper-legitimacy
    check: with a spy installed in place of ``_not_expired_clause``,
    ``_active_clause(n)`` must record exactly ``[n]`` and return the very
    object the spy returned, checked by identity — post-processing,
    re-spelling or re-wrapping the clause in a further ``or_(...)`` fails
    that check even when the compiled SQL still reads identically, which
    is exactly the escape ADDENDUM AC's ``v3`` demonstrated)."""
    from audittrace.services.console_acl._postgres import (  # noqa: PLC0415
        _not_expired_clause,
    )

    return _not_expired_clause(now_ms)


def _predicate_clause(pred: dict[str, Any]) -> Any:
    """A2's ``delete_acl_entries`` matcher (spec §4.0, clause C3) — ONE
    ``==`` per key PRESENT in ``pred``, never :func:`_key_clause`'s
    ``.get()`` reuse (which turns an absent key into an ``IS NULL``
    match — the ``keyclause`` escape spec §4.0 names by name). SQLAlchemy
    translates ``Column == None`` to ``IS NULL`` on its own, so an
    explicit ``None`` value in ``pred`` is still correct without a
    manual branch — unlike :func:`_key_clause`, which spells the branch
    out because ALL five of its keys are always present."""
    return sa.and_(
        *(getattr(ConsoleAclEntry, key) == value for key, value in pred.items())
    )


async def _visible_ids(session: AsyncSession, *, where: Any, now_ms: int) -> list[str]:
    """The rows at ``where`` the caller can SEE (RLS ``SELECT`` policy) —
    ``visible_matched_count``'s SELECT half (spec §4.5, clauses C4/C8).
    Always evaluated BEFORE the corresponding :func:`_expire_where` call,
    at the SAME ``now_ms`` (E-G) — the caller passes the SAME ``where``
    to both, never a narrower/wider re-spelling."""
    stmt = sa.select(ConsoleAclEntry.id).where(where, _active_clause(now_ms))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _active_rows(
    session: AsyncSession, *, where: Any, now_ms: int
) -> list[ConsoleAclEntry]:
    """``modify_permission_bits``'s entity-returning twin of
    :func:`_visible_ids` (spec §4.0, clause C6) — the SAME ``where``/
    ``_active_clause`` pair, but returning the ORM entities themselves
    so the caller can capture their columns (``expired_at_ms``,
    ``role_id``, ``created_at_ms``, ``perm_bits``) BEFORE the UPDATE
    that supersedes them (S-2)."""
    stmt = sa.select(ConsoleAclEntry).where(where, _active_clause(now_ms))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _expire_where(
    session: AsyncSession, *, where: Any, now_ms: int
) -> list[tuple[str, int]]:
    """Holds A1's and A2's ONE ``sa.update(ConsoleAclEntry)`` statement
    (spec §4.0, clauses C5/C7/C10) — ORM-enabled Core UPDATE (ADDENDUM
    V-1), touching ONLY ``expired_at_ms``/``updated_at_ms`` (Q-4.1).
    Returns ``(id, created_at_ms)`` pairs in RETURNING order, unsorted —
    :func:`_expire_active` (A1's shape, kept for grant/bulk/revoke/
    modify) discards the second element; ``delete_acl_entries`` (the
    only A2 caller that sorts) keeps both and orders by
    ``(created_at_ms, id)`` itself (spec §4.3)."""
    stmt = (
        sa.update(ConsoleAclEntry)
        .where(where, _active_clause(now_ms))
        .values(expired_at_ms=now_ms, updated_at_ms=now_ms)
        .returning(ConsoleAclEntry.id, ConsoleAclEntry.created_at_ms)
    )
    result = await session.execute(stmt)
    return [(row[0], row[1]) for row in result.all()]


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
    ``trace_id``. Returns the ids it expired (``RETURNING id``).

    **Y round (ADDENDUM Y-1/AB-1):** the predicate is :func:`_active_clause`
    — ``expired_at_ms IS NULL OR expired_at_ms > now`` — not merely
    ``IS NULL``: the write path must supersede EVERY row the read path
    would still count active, including a time-limited grant that has
    not yet lapsed, or a stale future expiry silently outlives the
    re-grant that was meant to replace it (ADDENDUM Y-0's measured
    downgrade). A future ``expired_at_ms`` matched here is OVERWRITTEN
    with the moment of supersession, never left at its scheduled value.

    **A2 (spec §4.0):** now a thin wrapper over :func:`_expire_where` —
    same signature, same return value, same RETURNING order (S-c: A1's
    tests assert on this order and A2 must not disturb it)."""
    pairs = await _expire_where(
        session,
        where=_key_clause(
            principal_type=principal_type,
            principal_id=principal_id,
            resource_type=resource_type,
            resource_id=resource_id,
            tenant_id=tenant_id,
        ),
        now_ms=now_ms,
    )
    return [row_id for row_id, _created_at_ms in pairs]


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
    db_error_class_override: str | None = None,
) -> AclWriteRefused:
    """Takes NO session and touches none (spec §5.0): ``_classify(exc)``
    (or the caller's override, for the two cases — past-expiry and
    bulk-rollback — whose ``failure_class`` is determined by WHERE the
    refusal happened, not by the exception's own text) -> ``_audit.
    record_denial(...)`` -> returns the named error to raise. The ONLY
    caller of ``record_denial`` in this module.

    **``db_error_class`` follows ratified spec §5.6, never a bare
    ``type(exc).__name__`` (a corrected, previously undisclosed
    departure caught on review — B5).** A ``failure_class_override``
    means the call site KNOWS the ratified literal for that override
    (past-expiry: the app-level literal ``"app:past_expiry"``, passed
    explicitly as ``db_error_class_override``) OR it does NOT know one
    (bulk-rollback: §5.6 says "as above", i.e. the SAME derivation
    ``_classify`` performs for the generic case — SQLSTATE when
    present, else constraint name, else exception class name) — so
    ``db_error_class`` still comes from ``_classify(exc)``'s second
    element unless a caller explicitly supplies the literal."""
    _audit = _acl_audit()
    if failure_class_override is not None:
        failure_class = failure_class_override
        if db_error_class_override is not None:
            db_error_class = db_error_class_override
        else:
            _, db_error_class = _classify(exc)
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
                # spec §5.6's ratified literal — there is no real DB
                # exception here (refused before any I/O), so
                # type(exc).__name__ ("ValueError") would be a made-up
                # value; the spec names the exact string instead.
                db_error_class_override="app:past_expiry",
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
        """Expire the active row(s) at ``key`` (spec §4.1) — never a
        hard delete (retention #13; migration 033's trigger refuses a
        real ``DELETE`` on Postgres regardless). The visible-count
        SELECT runs BEFORE the expiring UPDATE, at the SAME
        ``stamp.now_ms`` (E-G) — never after."""
        _audit = _acl_audit()
        stamp = build_write_stamp(user_context)
        attempted = {
            "principal_type": principal_type,
            "principal_id": principal_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "tenant_id": tenant_id,
        }

        async with self._session_factory() as db:
            # Stage 1 — the domain write: SELECT (visible count) then
            # UPDATE (expire), same shape-3 abort as _expire_active
            # alone (spec §5).
            try:
                where = _key_clause(
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    tenant_id=tenant_id,
                )
                visible_matched_count = len(
                    await _visible_ids(db, where=where, now_ms=stamp.now_ms)
                )
                expired_ids = await _expire_active(
                    db,
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    tenant_id=tenant_id,
                    now_ms=stamp.now_ms,
                )
            except Exception as exc:  # noqa: BLE001 - reclassified below
                await db.rollback()
                raise await _write_denial(
                    user_context=user_context,
                    op="revokePermission",
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    perm_bits=0,
                    attempted=attempted,
                    exc=exc,
                    tenant_id=tenant_id,
                ) from exc

            # Stage 2 — the audit row (N4, caller half).
            try:
                await _audit.record(
                    db,
                    user_context=user_context,
                    op="revokePermission",
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    perm_bits=0,
                    acl_entry_ids=[],
                    expired_ids=expired_ids,
                    visible_matched_count=visible_matched_count,
                    expired_at_ms=None,
                    tenant_id=tenant_id,
                )
                await db.flush()
            except Exception as audit_exc:  # noqa: BLE001 - N4, re-raised below
                await db.rollback()
                await _audit.record_denial(
                    user_context=user_context,
                    op="revokePermission",
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    perm_bits=0,
                    failure_class=_audit.FAILURE_CLASS_ACL_AUDIT_WRITE_FAILED,
                    predicate_or_attempted_row=attempted,
                    db_error_class=type(audit_exc).__name__,
                    tenant_id=tenant_id,
                )
                raise

            # Stage 3 — the commit.
            try:
                await db.commit()
            except Exception as exc:  # noqa: BLE001 - reclassified below
                await db.rollback()
                raise await _write_denial(
                    user_context=user_context,
                    op="revokePermission",
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    perm_bits=0,
                    attempted=attempted,
                    exc=exc,
                    tenant_id=tenant_id,
                ) from exc

            return {
                "expired_ids": expired_ids,
                "visible_matched_count": visible_matched_count,
            }

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
        """O-6 expire-and-insert with the bits recomputed (spec §4.2) —
        NEVER an in-place ``UPDATE ... SET perm_bits`` (E-B). Every key
        column of the new row, including ``tenant_id``, comes from the
        caller's key — never from a captured row (A2v3-BL-1, D-A2-9)."""
        _validate_bit_operands(add_bits, remove_bits)
        _audit = _acl_audit()
        stamp = build_write_stamp(user_context)
        key: dict[str, Any] = {
            "principal_type": principal_type,
            "principal_id": principal_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "tenant_id": tenant_id,
        }
        attempted = {**key, "add_bits": add_bits, "remove_bits": remove_bits}

        async with self._session_factory() as db:
            where = _key_clause(**key)
            active = await _active_rows(db, where=where, now_ms=stamp.now_ms)
            # Captured BEFORE the UPDATE (S-2) — every active row, never
            # active[0] (the "firstrow" escape leaks a sibling's bits).
            scheduled = [
                (row.id, row.expired_at_ms, row.role_id, row.created_at_ms)
                for row in active
            ]
            old_bits = 0
            for row in active:
                old_bits |= row.perm_bits
            new_bits = (old_bits | (add_bits or 0)) & ~(remove_bits or 0)

            # Stage 1 — the domain write: expire, then (unless the race
            # branch fires) insert the new row.
            try:
                expired_ids: list[str] = []
                new_row: ConsoleAclEntry | None = None
                inherited_expiry: int | None = None
                if active:
                    expired_ids = await _expire_active(db, **key, now_ms=stamp.now_ms)
                    if expired_ids:
                        # S-1's injection point: a concurrent expiry of
                        # the SAME key between the SELECT above and this
                        # UPDATE makes expired_ids == [] here — handled
                        # as the race branch below, never as a refusal.
                        role_id = max(scheduled, key=lambda s: s[3])[2]
                        captured_expiries = [s[1] for s in scheduled]
                        inherited_expiry = (
                            None
                            if any(e is None for e in captured_expiries)
                            else max(e for e in captured_expiries if e is not None)
                        )
                        new_row = ConsoleAclEntry(
                            id=str(uuid.uuid4()),
                            user_sub=stamp.user_sub,
                            principal_type=principal_type,
                            principal_id=principal_id,
                            principal_model=_principal_model(principal_type),
                            resource_type=resource_type,
                            resource_id=resource_id,
                            perm_bits=new_bits,
                            tenant_id=tenant_id,
                            role_id=role_id,
                            granted_by=stamp.user_sub,
                            granted_at_ms=stamp.now_ms,
                            expired_at_ms=inherited_expiry,
                            created_at_ms=stamp.now_ms,
                            updated_at_ms=stamp.now_ms,
                            trace_id=stamp.trace_id,
                        )
                        db.add(new_row)
                        # shape (1), spec §4.2 — flush the INSERT HERE,
                        # inside Stage 1, so a genuine DB refusal (e.g.
                        # 031's public-resource unique index, §8 item 12)
                        # is classified by Stage 1's own except below
                        # (_write_denial -> _classify -> acl_denied_policy)
                        # and never misclassified as an N4 audit-write
                        # failure by Stage 2's except (BL-1).
                        await db.flush()
            except Exception as exc:  # noqa: BLE001 - reclassified below
                await db.rollback()
                raise await _write_denial(
                    user_context=user_context,
                    op="modifyPermissionBits",
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    perm_bits=new_bits,
                    attempted=attempted,
                    exc=exc,
                    tenant_id=tenant_id,
                ) from exc

            acl_entry_ids = [new_row.id] if new_row is not None else []
            visible_matched_count = len(active)

            # Stage 2 — the audit row (N4, caller half). Every branch
            # (no-match / race / insert) writes perm_bits=new_bits —
            # never 0 (S-1, D-A2-6).
            try:
                await _audit.record(
                    db,
                    user_context=user_context,
                    op="modifyPermissionBits",
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    perm_bits=new_bits,
                    acl_entry_ids=acl_entry_ids,
                    expired_ids=expired_ids,
                    visible_matched_count=visible_matched_count,
                    expired_at_ms=inherited_expiry,
                    tenant_id=tenant_id,
                )
                await db.flush()
            except Exception as audit_exc:  # noqa: BLE001 - N4, re-raised below
                await db.rollback()
                await _audit.record_denial(
                    user_context=user_context,
                    op="modifyPermissionBits",
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    perm_bits=new_bits,
                    failure_class=_audit.FAILURE_CLASS_ACL_AUDIT_WRITE_FAILED,
                    predicate_or_attempted_row=attempted,
                    db_error_class=type(audit_exc).__name__,
                    tenant_id=tenant_id,
                )
                raise

            # Stage 3 — the commit.
            try:
                await db.commit()
            except Exception as exc:  # noqa: BLE001 - reclassified below
                await db.rollback()
                raise await _write_denial(
                    user_context=user_context,
                    op="modifyPermissionBits",
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    perm_bits=new_bits,
                    attempted=attempted,
                    exc=exc,
                    tenant_id=tenant_id,
                ) from exc

            return _row_to_dict(new_row) if new_row is not None else None

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
        """Expire-by-predicate, CALLER-SCOPED (spec §4.3) — migration
        032's owner-only UPDATE ``USING`` silently omits a row the
        caller does not own from the UPDATE (never a refusal; item 4).
        ALL the visibility SELECTs run before ANY expiring UPDATE; the
        UPDATEs then run one predicate at a time, in order, each
        producing its OWN audit row (D-A2-4). A refusal at predicate
        *i* rolls back every row staged so far — zero partial success
        (D-A2-4r)."""
        if not predicates:
            raise ValueError("delete_acl_entries: predicates must not be empty")
        for pred in predicates:
            _validate_predicate(pred)

        _audit = _acl_audit()
        stamp = build_write_stamp(user_context)

        def _pred_fields(pred: dict[str, Any]) -> dict[str, Any]:
            return {
                "principal_type": pred.get("principal_type", "-"),
                "principal_id": pred.get("principal_id"),
                "resource_type": pred.get("resource_type", "-"),
                "resource_id": pred.get("resource_id", "-"),
                "tenant_id": pred.get("tenant_id"),
            }

        async with self._session_factory() as db:
            # Stage 0 — ALL the visibility SELECTs, before ANY UPDATE
            # (spec §4.3): per-predicate counts plus the DISTINCT count
            # over the OR of every predicate — never sum(visible_i).
            try:
                visible_by_predicate = [
                    len(
                        await _visible_ids(
                            db,
                            where=_predicate_clause(pred),
                            now_ms=stamp.now_ms,
                        )
                    )
                    for pred in predicates
                ]
                visible_all = len(
                    await _visible_ids(
                        db,
                        where=sa.or_(*(_predicate_clause(pred) for pred in predicates)),
                        now_ms=stamp.now_ms,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - reclassified below
                await db.rollback()
                raise await _write_denial(
                    user_context=user_context,
                    op="deleteAclEntries",
                    principal_type="-",
                    principal_id=None,
                    resource_type="-",
                    resource_id="-",
                    perm_bits=0,
                    attempted={
                        "predicate_index": None,
                        "predicate": None,
                        "predicate_count": len(predicates),
                    },
                    exc=exc,
                    tenant_id=None,
                ) from exc

            expired_id_lists: list[list[str]] = []
            for index, pred in enumerate(predicates):
                fields = _pred_fields(pred)
                attempted = {
                    "predicate_index": index,
                    "predicate": pred,
                    "predicate_count": len(predicates),
                }

                # Stage 1 — this predicate's domain write (the UPDATE).
                try:
                    pairs = await _expire_where(
                        db, where=_predicate_clause(pred), now_ms=stamp.now_ms
                    )
                except Exception as exc:  # noqa: BLE001 - reclassified below
                    await db.rollback()
                    raise await _write_denial(
                        user_context=user_context,
                        op="deleteAclEntries",
                        perm_bits=0,
                        attempted=attempted,
                        exc=exc,
                        **fields,
                    ) from exc

                # delete's own order (S-a) — (created_at_ms, id), never
                # RETURNING/insertion order (MP's tie-break).
                pairs.sort(key=lambda pair: (pair[1], pair[0]))
                expired_ids_i = [row_id for row_id, _created_at_ms in pairs]

                # Stage 2 — this predicate's audit row (N4, caller half).
                try:
                    await _audit.record(
                        db,
                        user_context=user_context,
                        op="deleteAclEntries",
                        perm_bits=0,
                        acl_entry_ids=[],
                        expired_ids=expired_ids_i,
                        visible_matched_count=visible_by_predicate[index],
                        expired_at_ms=None,
                        **fields,
                    )
                    await db.flush()
                except Exception as audit_exc:  # noqa: BLE001 - N4, re-raised below
                    await db.rollback()
                    await _audit.record_denial(
                        user_context=user_context,
                        op="deleteAclEntries",
                        perm_bits=0,
                        failure_class=_audit.FAILURE_CLASS_ACL_AUDIT_WRITE_FAILED,
                        predicate_or_attempted_row=attempted,
                        db_error_class=type(audit_exc).__name__,
                        **fields,
                    )
                    raise

                expired_id_lists.append(expired_ids_i)

            # Stage 3 — the commit.
            try:
                await db.commit()
            except Exception as exc:  # noqa: BLE001 - reclassified below
                await db.rollback()
                raise await _write_denial(
                    user_context=user_context,
                    op="deleteAclEntries",
                    principal_type="-",
                    principal_id=None,
                    resource_type="-",
                    resource_id="-",
                    perm_bits=0,
                    attempted={
                        "predicate_index": None,
                        "predicate": None,
                        "predicate_count": len(predicates),
                    },
                    exc=exc,
                    tenant_id=None,
                ) from exc

            # #364 — every value this method returns is a plain str/int,
            # extracted and assembled HERE, inside the session block,
            # never read from a session-bound object after `db` closes.
            seen: set[str] = set()
            all_expired_ids: list[str] = []
            for ids in expired_id_lists:
                for entry_id in ids:
                    if entry_id not in seen:
                        seen.add(entry_id)
                        all_expired_ids.append(entry_id)

            return {
                "expired_ids": all_expired_ids,
                "visible_matched_count": visible_all,
            }
