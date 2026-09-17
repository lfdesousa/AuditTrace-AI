"""Console-ACL service — Sovereign Authorization Layer EPIC, WU-1
(READ PATH ONLY).

The AuditTrace-side, RLS-isolated store backing LibreChat's ACL record
(the fork's Mongo ``AclEntry`` collection,
``packages/data-schemas/src/schema/aclEntry.ts``), per the ratified
spec (``2026-09-17-SPEC-sovereign-authorization-layer-acl-WU0-
ratified-candidate.md``) and its operator ratification (``2026-09-18-
SPEC-ADDENDUM-I-...``). This is the highest-blast-radius store in the
codebase: a defect here is cross-user data exposure, not a UX quirk.

**READ PATH ONLY — no write method lives here.** ``grantPermission``/
``revokePermission``/``modifyPermissionBits``/``bulkWriteAclEntries``/
``deleteAclEntries`` are WU-2. No row is ever created by this package;
every test that needs a row inserts one directly (mirroring "seed data
that in production only WU-2 will produce").

**Package layout** (PYTHON-ENGINEERING §11 — the single-file draft of
this module crossed 1000 LOC):

* ``__init__.py`` (this file) — constants shared with the fork
  (``PrincipalType``/``PrincipalModel``/``ResourceType``/
  ``PermissionBits`` mirrors), :func:`_caller_principals`, and the
  abstract :class:`ConsoleAclEntriesService` contract.
* ``_postgres.py`` — :class:`PostgresConsoleAclEntriesService`, the
  Postgres/SQLAlchemy implementation.
* ``_mock.py`` — :class:`MockConsoleAclEntriesService`, the in-process
  mock for fast unit tests.

**Every public method resolves its OWN principal set from the token —
NEVER from a caller-supplied ``principal_id``/``principal_type``
parameter representing "which principal to check as."**
(``feedback_never_trust_caller_metadata_for_security_fields``, epic
invariant 5.) This is a deliberate, documented NARROWING of the raw
Mongo method signatures (which accept an explicit ``principalsList``
parameter): in the fork, that parameter is always populated by
controller code with the AUTHENTICATED caller's own resolved
principals before it ever reaches the store, but nothing in the store
itself enforces that. Since group principals are disabled (ADDENDUM I
ruling 1) and ``userGroup`` is out of WU-1's scope, a caller's resolved
principal set here is always exactly ``[(user, caller_sub),
(public, None)]`` — see :func:`_caller_principals`.

**Two DIFFERENT classes of read, tested separately (ADDENDUM I ruling
2):**

* AUTHORIZATION-DECISION methods (``has_permission``,
  ``get_effective_permissions``, ``get_effective_permissions_for_
  resources``, ``find_accessible_resources``, ``find_public_resource_
  ids``, ``get_sole_owned_resource_ids``, ``get_owner_principal_ids``)
  filter ``expired_at_ms`` on every query — an expired grant must
  NEVER reach an effective-permission result.
* AUDIT-READ methods (``find_entries_by_principal``, ``find_entries_
  by_resource``, ``find_entries_by_principals_and_resource``) do
  **NOT** filter ``expired_at_ms`` — the retained, expired row IS the
  audit trail (ruling 2) and must stay queryable. These three methods
  are additionally OWNER-SCOPED (``user_sub = caller``) rather than
  mirroring Mongo's unscoped ``find`` — the raw Mongo methods carry no
  owner filter at all because in the fork they are only ever invoked
  by trusted internal call sites; a sovereign store exposed to
  arbitrary future callers cannot make the same assumption, so these
  three answer "what have I (as resource owner) granted", never "list
  every row for principal X" (which would be a cross-owner
  enumeration primitive). WU-0 confirmed all three have ZERO
  production consumers today — built here for CRUD parity, no route
  wiring invented for them (per the ratified spec's explicit
  instruction).

**``perm_bits`` containment is ALWAYS ``(perm_bits & :bit) = :bit``,
NEVER ``=``** (epic invariant 2 / Deliverable 4 finding #3), with
exactly ONE documented, deliberate exception:
``PostgresConsoleAclEntriesService.get_owner_principal_ids`` (the
``aggregateAclEntries`` site-1 fold, ``ownerContact.js``) preserves the
live Mongo pipeline's OWN exact-equality semantics
(``permBits == OWNER_PERMISSION_BITS``) byte-for-byte, because
Deliverable 4 finding #3 explicitly defers the decision to "bug-fix"
that specific query to whichever WU actually migrates owner-contact
resolution — not decided here, and not silently changed here either.
See that method's docstring for the full reasoning.

**RLS is NOT owner-only** (unlike every other console-* domain) — see
migration 031 and ``ConsoleAclEntry``'s docstring (db/models.py) for
the exact predicate. ``PostgresConsoleAclEntriesService`` mirrors that
SAME owner-OR-principal-OR-public predicate explicitly at the service
layer (belt-and-suspenders — SQLite has no RLS,
feedback_unit_tests_miss_rls).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from audittrace.identity import UserContext

# ── Fork-mirrored constants (librechat-data-provider's accessPermissions) ──

PRINCIPAL_TYPE_USER = "user"
PRINCIPAL_TYPE_PUBLIC = "public"
PRINCIPAL_TYPE_ROLE = "role"
# 'group' is DELIBERATELY absent — ADDENDUM I ruling 1, enforced at the
# schema level (ck_console_acl_entries_principal_type), not listed here.
ALLOWED_PRINCIPAL_TYPES = frozenset(
    {PRINCIPAL_TYPE_USER, PRINCIPAL_TYPE_PUBLIC, PRINCIPAL_TYPE_ROLE}
)

PRINCIPAL_MODEL_USER = "User"
PRINCIPAL_MODEL_ROLE = "Role"

RESOURCE_TYPES = frozenset(
    {"agent", "promptGroup", "mcpServer", "remoteAgent", "skill", "sharedLink"}
)

# PermissionBits: VIEW=1, EDIT=2, DELETE=4, SHARE=8 (librechat-data-
# provider's accessPermissions.ts). MAX_PERM_BITS is their bitwise-OR.
PERMISSION_BIT_VIEW = 1
PERMISSION_BIT_EDIT = 2
PERMISSION_BIT_DELETE = 4
PERMISSION_BIT_SHARE = 8
MAX_PERM_BITS = (
    PERMISSION_BIT_VIEW
    | PERMISSION_BIT_EDIT
    | PERMISSION_BIT_DELETE
    | PERMISSION_BIT_SHARE
)

# ownerContact.js's OWNER_PERMISSION_BITS — happens to equal MAX_PERM_BITS
# today (there is no 5th bit). See get_owner_principal_ids's docstring
# for why this query alone is exempt from the "never compare perm_bits
# with =" rule.
OWNER_PERMISSION_BITS = MAX_PERM_BITS


def _now_ms() -> int:
    return int(time.time() * 1000)


def _caller_principals(user_context: UserContext) -> list[tuple[str, str | None]]:
    """The CALLER's own resolved principal set: itself as a ``user``
    principal, plus ``public`` (which every principal implicitly
    matches). NEVER includes a group — group membership is not
    sovereign yet (ADDENDUM I ruling 1) and ``userGroup`` is out of
    WU-1's scope, so this is the closed, complete set for WU-1: no
    caller-supplied parameter can widen it (invariant 5)."""
    return [
        (PRINCIPAL_TYPE_USER, user_context.user_id),
        (PRINCIPAL_TYPE_PUBLIC, None),
    ]


class ConsoleAclEntriesService(ABC):
    """Abstract console-ACL store — the sovereign, READ-ONLY (WU-1)
    replacement for LibreChat's Mongo ``AclEntry`` collection's read
    methods."""

    @abstractmethod
    async def has_permission(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_id: str,
        permission_bit: int,
    ) -> bool:
        """Whether the CALLER (self + public) holds ``permission_bit``
        (containment, never equality) on ``resource_id``. Filters
        ``expired_at_ms`` (authorization-decision read)."""

    @abstractmethod
    async def get_effective_permissions(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_id: str,
    ) -> int:
        """The CALLER's combined (bitwise-OR'd) effective permission
        bitmask on ``resource_id``. Zero if no matching, non-expired
        row exists — deny-by-default (invariant 1)."""

    @abstractmethod
    async def get_effective_permissions_for_resources(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_ids: list[str],
    ) -> dict[str, int]:
        """Batch form of :meth:`get_effective_permissions` — one query,
        a map of ``resource_id`` to effective bits. A ``resource_id``
        with no matching row is OMITTED from the map (never present
        with value 0), mirroring the fork's ``Map`` semantics."""

    @abstractmethod
    async def find_accessible_resources(
        self,
        user_context: UserContext,
        resource_type: str,
        required_permission_bit: int,
        resource_ids: list[str] | None = None,
    ) -> list[str]:
        """Resource ids the CALLER (self + public) can access with AT
        LEAST ``required_permission_bit`` (containment). ``resource_ids``
        optionally bounds the candidate set; ``None`` means unbounded."""

    @abstractmethod
    async def find_public_resource_ids(
        self,
        user_context: UserContext,
        resource_type: str,
        required_permission_bit: int,
        resource_ids: list[str] | None = None,
    ) -> list[str]:
        """Resource ids PUBLICLY accessible with at least
        ``required_permission_bit`` — the PUBLIC branch only, no
        principal filtering at all (any authenticated caller gets the
        same answer). ``user_context`` is accepted for the same
        uniform-signature/traceability discipline as every other
        method here; it plays no role in the filter."""

    @abstractmethod
    async def get_sole_owned_resource_ids(
        self,
        user_context: UserContext,
        resource_types: list[str],
    ) -> list[str]:
        """Resource ids where the CALLER holds DELETE (containment) as
        a ``user`` principal and NO OTHER principal also holds DELETE
        on the same resource — i.e. the caller is the resource's SOLE
        owner."""

    @abstractmethod
    async def find_entries_by_principal(
        self,
        user_context: UserContext,
        principal_type: str,
        principal_id: str | None,
        resource_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """AUDIT read (see module docstring): every ACL row the CALLER
        (as resource OWNER, ``user_sub``) granted to ``(principal_type,
        principal_id)``, optionally narrowed by ``resource_type``.
        Does NOT filter ``expired_at_ms`` — an expired grant stays
        listable here."""

    @abstractmethod
    async def find_entries_by_resource(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_id: str,
    ) -> list[dict[str, Any]]:
        """AUDIT read: every ACL row the CALLER (as resource OWNER) has
        on ``(resource_type, resource_id)``. Does NOT filter
        ``expired_at_ms``."""

    @abstractmethod
    async def find_entries_by_principals_and_resource(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_id: str,
    ) -> list[dict[str, Any]]:
        """AUDIT read: every ACL row matching the CALLER's own resolved
        principal set (self + public) on ``(resource_type,
        resource_id)`` — regardless of who owns the row. Does NOT
        filter ``expired_at_ms``."""

    @abstractmethod
    async def get_owner_principal_ids(
        self,
        user_context: UserContext,
        resource_type: str,
        resource_ids: list[str],
    ) -> dict[str, str]:
        """The ``aggregateAclEntries`` SITE-1 fold — see
        ``_postgres.py``'s implementation docstring for the full
        rationale (the one documented exact-equality exception)."""


from audittrace.services.console_acl._mock import (  # noqa: E402
    MockConsoleAclEntriesService,
)
from audittrace.services.console_acl._postgres import (  # noqa: E402
    PostgresConsoleAclEntriesService,
)

__all__ = [
    "ALLOWED_PRINCIPAL_TYPES",
    "MAX_PERM_BITS",
    "OWNER_PERMISSION_BITS",
    "PERMISSION_BIT_DELETE",
    "PERMISSION_BIT_EDIT",
    "PERMISSION_BIT_SHARE",
    "PERMISSION_BIT_VIEW",
    "PRINCIPAL_MODEL_ROLE",
    "PRINCIPAL_MODEL_USER",
    "PRINCIPAL_TYPE_PUBLIC",
    "PRINCIPAL_TYPE_ROLE",
    "PRINCIPAL_TYPE_USER",
    "RESOURCE_TYPES",
    "ConsoleAclEntriesService",
    "MockConsoleAclEntriesService",
    "PostgresConsoleAclEntriesService",
]
