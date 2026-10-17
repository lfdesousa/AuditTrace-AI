"""Service-layer tests for the console-ACL store (Sovereign
Authorization Layer EPIC, WU-1 — READ PATH ONLY).

Every read method is exercised against BOTH ``MockConsoleAclEntriesService``
(fast, in-process) and ``PostgresConsoleAclEntriesService`` (aiosqlite via
``InMemoryPostgresFactory``, no real PostgreSQL required) through a
shared ``harness`` fixture, so a guard that only "happens to work" on
one implementation is caught.

**Per-guard neuter table (see the build record for the run log):**

1. PUBLIC-as-everyone — ``TestPublicIsNotEveryone`` (ranked/probed
   first, per the spec).
2. Bitmask containment vs equality, PER BIT — ``TestBitmaskContainment``
   (one test per bit, never batched).
3. Principal-pair binding — ``TestPrincipalPairBinding``.
4. Expired-row filtering, the two classes tested SEPARATELY —
   ``TestExpiredRowFiltering`` (authorization-decision reads) and
   ``TestAuditReadsIncludeExpired`` (audit reads).
5. RLS cross-user isolation through the real HTTP route —
   ``tests/test_console_acl_routes.py``.
6. The group-principal CHECK — ``tests/test_console_acl_migration.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

import pytest
import pytest_asyncio

from audittrace.db.models import ConsoleAclEntry
from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.db.rls import set_current_user_id
from audittrace.services.console_acl import (
    MAX_PERM_BITS,
    OWNER_PERMISSION_BITS,
    PERMISSION_BIT_DELETE,
    PERMISSION_BIT_EDIT,
    PERMISSION_BIT_SHARE,
    PERMISSION_BIT_VIEW,
    ConsoleAclEntriesService,
    MockConsoleAclEntriesService,
)
from audittrace.services.console_acl._postgres import PostgresConsoleAclEntriesService

# ── Harness: run every test against BOTH implementations ─────────────────


@dataclass
class _Harness:
    service: ConsoleAclEntriesService
    seed: Callable[..., Awaitable[None]]


async def _pg_seed(session_factory, **kwargs) -> None:
    kwargs.setdefault("id", str(uuid.uuid4()))
    kwargs.setdefault("granted_at_ms", 0)
    kwargs.setdefault("created_at_ms", 0)
    kwargs.setdefault("updated_at_ms", 0)
    kwargs.setdefault("expired_at_ms", None)
    kwargs.setdefault("tenant_id", None)
    async with session_factory() as session:
        session.add(ConsoleAclEntry(**kwargs))
        await session.commit()


async def _mock_seed(service: MockConsoleAclEntriesService, **kwargs) -> None:
    kwargs.pop("id", None)
    service.seed_entry(**kwargs)


@pytest_asyncio.fixture
async def mock_harness() -> _Harness:
    service = MockConsoleAclEntriesService()

    async def seed(**kwargs):
        await _mock_seed(service, **kwargs)

    return _Harness(service=service, seed=seed)


@pytest_asyncio.fixture
async def pg_harness() -> _Harness:
    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    session_factory = factory.get_session_factory()
    service = PostgresConsoleAclEntriesService(session_factory=session_factory)

    async def seed(**kwargs):
        await _pg_seed(session_factory, **kwargs)

    return _Harness(service=service, seed=seed)


@pytest.fixture(params=["mock", "postgres"])
def harness(request, mock_harness, pg_harness) -> _Harness:
    return {"mock": mock_harness, "postgres": pg_harness}[request.param]


# ── Interface + deny-by-default ───────────────────────────────────────────


class TestAbstractInterface:
    def test_mock_is_instance(self, mock_harness) -> None:
        assert isinstance(mock_harness.service, ConsoleAclEntriesService)

    def test_postgres_is_instance(self, pg_harness) -> None:
        assert isinstance(pg_harness.service, ConsoleAclEntriesService)


class TestDenyByDefault:
    """Invariant 1 — absence of a grant is a denial, every read path
    fails CLOSED. No seed data at all in these tests."""

    async def test_has_permission_false_with_no_rows(
        self, harness, user_context
    ) -> None:
        assert (
            await harness.service.has_permission(
                user_context, "agent", "nope", PERMISSION_BIT_VIEW
            )
            is False
        )

    async def test_effective_permissions_zero_with_no_rows(
        self, harness, user_context
    ) -> None:
        assert (
            await harness.service.get_effective_permissions(
                user_context, "agent", "nope"
            )
            == 0
        )

    async def test_effective_permissions_for_resources_empty_map(
        self, harness, user_context
    ) -> None:
        result = await harness.service.get_effective_permissions_for_resources(
            user_context, "agent", ["r1", "r2"]
        )
        assert result == {}

    async def test_find_accessible_resources_empty(self, harness, user_context) -> None:
        assert (
            await harness.service.find_accessible_resources(
                user_context, "agent", PERMISSION_BIT_VIEW
            )
            == []
        )

    async def test_find_public_resource_ids_empty(self, harness, user_context) -> None:
        assert (
            await harness.service.find_public_resource_ids(
                user_context, "agent", PERMISSION_BIT_VIEW
            )
            == []
        )

    async def test_sole_owned_empty(self, harness, user_context) -> None:
        assert (
            await harness.service.get_sole_owned_resource_ids(user_context, ["agent"])
            == []
        )


# ── Guard #1 — PUBLIC-as-everyone (ranked/probed FIRST and HARDEST) ──────


class TestPublicIsNotEveryone:
    """A missing/NULL principal must NEVER be treated as PUBLIC. The
    schema makes this structurally impossible (CHECK constraint, see
    the migration test); this class proves the SERVICE layer also
    never conflates "no row for me" with "a PUBLIC row exists"."""

    async def test_strangers_grant_does_not_leak_as_public(
        self, harness, user_context
    ) -> None:
        stranger = replace(user_context, user_id="stranger-sub")
        await harness.seed(
            user_sub="owner-1",
            principal_type="user",
            principal_id=stranger.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-1",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        # The CALLER (not the stranger) must see NOTHING — a USER-typed
        # grant to someone else must never be interpreted as PUBLIC.
        assert (
            await harness.service.has_permission(
                user_context, "agent", "res-1", PERMISSION_BIT_VIEW
            )
            is False
        )
        assert (
            await harness.service.get_effective_permissions(
                user_context, "agent", "res-1"
            )
            == 0
        )

    async def test_only_an_explicit_public_row_grants_everyone(
        self, harness, user_context
    ) -> None:
        someone_else = replace(user_context, user_id="someone-else")
        await harness.seed(
            user_sub="owner-2",
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-2",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        for caller in (user_context, someone_else):
            assert (
                await harness.service.has_permission(
                    caller, "agent", "res-2", PERMISSION_BIT_VIEW
                )
                is True
            ), f"PUBLIC row did not grant {caller.user_id}"

    async def test_find_public_resource_ids_never_returns_user_scoped_row(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub="owner-3",
            principal_type="user",
            principal_id="some-user",
            principal_model="User",
            resource_type="agent",
            resource_id="res-3",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        ids = await harness.service.find_public_resource_ids(
            user_context, "agent", PERMISSION_BIT_VIEW
        )
        assert "res-3" not in ids


# ── Guard #2 — bitmask containment vs equality, PER BIT ──────────────────


class TestBitmaskContainment:
    """Each bit tested INDEPENDENTLY — a batched assertion cannot
    detect a single dead bit (feedback_neuter_guards_individually_
    never_batched)."""

    @pytest.mark.parametrize(
        "bit",
        [
            PERMISSION_BIT_VIEW,
            PERMISSION_BIT_EDIT,
            PERMISSION_BIT_DELETE,
            PERMISSION_BIT_SHARE,
        ],
    )
    async def test_containment_grants_when_bit_is_set_among_others(
        self, harness, user_context, bit
    ) -> None:
        # perm_bits carries EVERY bit except `bit` itself, plus `bit`.
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id=f"res-bit-{bit}",
            perm_bits=MAX_PERM_BITS,
        )
        assert (
            await harness.service.has_permission(
                user_context, "agent", f"res-bit-{bit}", bit
            )
            is True
        )

    @pytest.mark.parametrize(
        "bit",
        [
            PERMISSION_BIT_VIEW,
            PERMISSION_BIT_EDIT,
            PERMISSION_BIT_DELETE,
            PERMISSION_BIT_SHARE,
        ],
    )
    async def test_containment_denies_when_bit_is_absent(
        self, harness, user_context, bit
    ) -> None:
        other_bits = MAX_PERM_BITS & ~bit
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id=f"res-missing-bit-{bit}",
            perm_bits=other_bits,
        )
        assert (
            await harness.service.has_permission(
                user_context, "agent", f"res-missing-bit-{bit}", bit
            )
            is False
        ), f"bit {bit} falsely reported present (equality-style rot)"

    async def test_effective_permissions_ors_multiple_rows(
        self, harness, user_context
    ) -> None:
        """Two rows, each carrying a different single bit — the
        effective permission must be their bitwise-OR, proving
        containment composition (not just single-row equality)."""
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-or",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-or",
            perm_bits=PERMISSION_BIT_EDIT,
        )
        bits = await harness.service.get_effective_permissions(
            user_context, "agent", "res-or"
        )
        assert bits == (PERMISSION_BIT_VIEW | PERMISSION_BIT_EDIT)


# ── Guard #2b — bitmask containment on find_accessible_resources ─────────
#
# WU-1 fix round 1, F2: ``TestBitmaskContainment`` above only exercises
# ``has_permission``. The Mock has FIVE containment call sites total
# (_mock.py:167 has_permission, :243 find_accessible_resources, :271
# find_public_resource_ids, :298 sole-owned loop 1, :313 sole-owned
# loop 2) and the reviewer proved THREE of them vacuous — an aggregate
# "8 RED x 2 implementations" total masked that
# find_accessible_resources/find_public_resource_ids/sole-owned-loop-2
# had NO per-bit containment test at all
# (feedback_neuter_guards_individually_never_batched). Re-tabled PER
# SITE below, never per-implementation.


class TestFindAccessibleResourcesBitmaskContainment:
    """Site 2/5 — ``find_accessible_resources`` (_mock.py, the
    ``(row.perm_bits & required_permission_bit) != required_permission_bit``
    check). Same superset-grants / subset-denies shape as
    ``TestBitmaskContainment``, one test per bit, never batched."""

    @pytest.mark.parametrize(
        "bit",
        [
            PERMISSION_BIT_VIEW,
            PERMISSION_BIT_EDIT,
            PERMISSION_BIT_DELETE,
            PERMISSION_BIT_SHARE,
        ],
    )
    async def test_containment_grants_when_bit_is_set_among_others(
        self, harness, user_context, bit
    ) -> None:
        resource_id = f"res-accessible-bit-{bit}"
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id=resource_id,
            perm_bits=MAX_PERM_BITS,
        )
        ids = await harness.service.find_accessible_resources(
            user_context, "agent", bit
        )
        assert resource_id in ids, (
            f"bit {bit} present among others but find_accessible_resources "
            "did not grant — containment broken (equality-style rot)"
        )

    @pytest.mark.parametrize(
        "bit",
        [
            PERMISSION_BIT_VIEW,
            PERMISSION_BIT_EDIT,
            PERMISSION_BIT_DELETE,
            PERMISSION_BIT_SHARE,
        ],
    )
    async def test_containment_denies_when_bit_is_absent(
        self, harness, user_context, bit
    ) -> None:
        resource_id = f"res-accessible-missing-bit-{bit}"
        other_bits = MAX_PERM_BITS & ~bit
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id=resource_id,
            perm_bits=other_bits,
        )
        ids = await harness.service.find_accessible_resources(
            user_context, "agent", bit
        )
        assert resource_id not in ids, (
            f"bit {bit} absent but find_accessible_resources falsely "
            "reported the resource accessible (equality-style rot)"
        )


class TestFindPublicResourceIdsBitmaskContainment:
    """Site 3/5 — ``find_public_resource_ids`` (_mock.py, the PUBLIC
    total-exposure surface). Same superset-grants / subset-denies
    shape, one test per bit, never batched."""

    @pytest.mark.parametrize(
        "bit",
        [
            PERMISSION_BIT_VIEW,
            PERMISSION_BIT_EDIT,
            PERMISSION_BIT_DELETE,
            PERMISSION_BIT_SHARE,
        ],
    )
    async def test_containment_grants_when_bit_is_set_among_others(
        self, harness, user_context, bit
    ) -> None:
        resource_id = f"res-public-bit-{bit}"
        await harness.seed(
            user_sub="owner-public-containment",
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id=resource_id,
            perm_bits=MAX_PERM_BITS,
        )
        ids = await harness.service.find_public_resource_ids(user_context, "agent", bit)
        assert resource_id in ids, (
            f"bit {bit} present among others but find_public_resource_ids "
            "did not grant — containment broken (equality-style rot)"
        )

    @pytest.mark.parametrize(
        "bit",
        [
            PERMISSION_BIT_VIEW,
            PERMISSION_BIT_EDIT,
            PERMISSION_BIT_DELETE,
            PERMISSION_BIT_SHARE,
        ],
    )
    async def test_containment_denies_when_bit_is_absent(
        self, harness, user_context, bit
    ) -> None:
        resource_id = f"res-public-missing-bit-{bit}"
        other_bits = MAX_PERM_BITS & ~bit
        await harness.seed(
            user_sub="owner-public-containment",
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id=resource_id,
            perm_bits=other_bits,
        )
        ids = await harness.service.find_public_resource_ids(user_context, "agent", bit)
        assert resource_id not in ids, (
            f"bit {bit} absent but find_public_resource_ids falsely "
            "reported the resource publicly accessible (equality-style rot)"
        )


# ── Guard #3 — principal-pair binding ────────────────────────────────────


class TestPrincipalPairBinding:
    """``(principal_type, principal_id)`` must bind together — a
    mismatched pair (e.g. a ROLE named identically to a USER id) must
    never grant."""

    async def test_role_with_same_id_string_as_caller_does_not_grant(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub="owner-4",
            principal_type="role",
            principal_id=user_context.user_id,  # textually identical
            principal_model="Role",
            resource_type="agent",
            resource_id="res-role-collision",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        assert (
            await harness.service.has_permission(
                user_context, "agent", "res-role-collision", PERMISSION_BIT_VIEW
            )
            is False
        ), "a ROLE principal leaked permission to a USER with the same id string"

    async def test_owner_cannot_inherit_a_grant_made_to_someone_else(
        self, harness, user_context
    ) -> None:
        """Isolates the ``_principals_clause``/``_mock_matches_principal``
        guard from its ``_rls_mirror_clause`` sibling: the CALLER owns
        the resource (``user_sub == caller``, so the RLS-mirror OWNER
        branch alone would make the row VISIBLE), but the row's actual
        principal is someone else. Effective permission must still be
        denied — row VISIBILITY (RLS) and row APPLICABILITY (does this
        grant apply to ME) are different questions."""
        await harness.seed(
            user_sub=user_context.user_id,  # caller OWNS the resource
            principal_type="user",
            principal_id="someone-else-entirely",  # granted to another user
            principal_model="User",
            resource_type="agent",
            resource_id="res-owner-grant-to-other",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        assert (
            await harness.service.has_permission(
                user_context, "agent", "res-owner-grant-to-other", PERMISSION_BIT_VIEW
            )
            is False
        ), (
            "the resource owner inherited a grant they made to a "
            "DIFFERENT principal — principal-pair binding is not "
            "independently enforced from RLS visibility"
        )

    async def test_user_grant_to_a_different_user_does_not_leak(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub="owner-5",
            principal_type="user",
            principal_id="a-completely-different-user",
            principal_model="User",
            resource_type="agent",
            resource_id="res-other-user",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        assert (
            await harness.service.has_permission(
                user_context, "agent", "res-other-user", PERMISSION_BIT_VIEW
            )
            is False
        )


# ── Guard #4a — expired-row filtering (authorization-decision reads) ─────


class TestExpiredRowFiltering:
    """ADDENDUM I ruling 2 — an expired grant must NEVER reach an
    effective-permission result. ``expired_at_ms`` in the past."""

    async def test_expired_row_does_not_grant_has_permission(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-expired",
            perm_bits=PERMISSION_BIT_VIEW,
            expired_at_ms=1,  # epoch ms 1 — long past
        )
        assert (
            await harness.service.has_permission(
                user_context, "agent", "res-expired", PERMISSION_BIT_VIEW
            )
            is False
        )

    async def test_expired_row_excluded_from_effective_permissions(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-expired-2",
            perm_bits=PERMISSION_BIT_VIEW,
            expired_at_ms=1,
        )
        assert (
            await harness.service.get_effective_permissions(
                user_context, "agent", "res-expired-2"
            )
            == 0
        )

    async def test_non_expired_row_still_grants(self, harness, user_context) -> None:
        """Non-vacuity companion — a row with ``expired_at_ms`` far in
        the future (or ``None``) must still grant."""
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-not-expired",
            perm_bits=PERMISSION_BIT_VIEW,
            expired_at_ms=99999999999999,
        )
        assert (
            await harness.service.has_permission(
                user_context, "agent", "res-not-expired", PERMISSION_BIT_VIEW
            )
            is True
        )

    async def test_expired_row_excluded_from_accessible_resources(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-expired-3",
            perm_bits=PERMISSION_BIT_VIEW,
            expired_at_ms=1,
        )
        ids = await harness.service.find_accessible_resources(
            user_context, "agent", PERMISSION_BIT_VIEW
        )
        assert "res-expired-3" not in ids

    async def test_expired_public_row_excluded_from_public_resource_ids(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub="owner-pub",
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-expired-pub",
            perm_bits=PERMISSION_BIT_VIEW,
            expired_at_ms=1,
        )
        ids = await harness.service.find_public_resource_ids(
            user_context, "agent", PERMISSION_BIT_VIEW
        )
        assert "res-expired-pub" not in ids

    async def test_expired_row_excluded_from_sole_owned(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-expired-owner",
            perm_bits=OWNER_PERMISSION_BITS,
            expired_at_ms=1,
        )
        ids = await harness.service.get_sole_owned_resource_ids(user_context, ["agent"])
        assert "res-expired-owner" not in ids


# ── Guard #4b — audit reads INCLUDE expired rows (the other half) ────────


class TestAuditReadsIncludeExpired:
    """The SAME expired row must stay visible through the AUDIT-read
    methods — "prove who could see this on date X" requires the
    expired grant to still be queryable (ruling 2)."""

    async def test_find_entries_by_resource_includes_expired(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="some-grantee",
            principal_model="User",
            resource_type="agent",
            resource_id="res-audit-1",
            perm_bits=PERMISSION_BIT_VIEW,
            expired_at_ms=1,
        )
        entries = await harness.service.find_entries_by_resource(
            user_context, "agent", "res-audit-1"
        )
        assert len(entries) == 1
        assert entries[0]["expired_at_ms"] == 1

    async def test_find_entries_by_principal_includes_expired(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="grantee-2",
            principal_model="User",
            resource_type="agent",
            resource_id="res-audit-2",
            perm_bits=PERMISSION_BIT_VIEW,
            expired_at_ms=1,
        )
        entries = await harness.service.find_entries_by_principal(
            user_context, "user", "grantee-2"
        )
        assert len(entries) == 1

    async def test_find_entries_by_principals_and_resource_includes_expired(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub="some-owner",
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-audit-3",
            perm_bits=PERMISSION_BIT_VIEW,
            expired_at_ms=1,
        )
        entries = await harness.service.find_entries_by_principals_and_resource(
            user_context, "agent", "res-audit-3"
        )
        assert len(entries) == 1

    async def test_find_entries_by_principal_public(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-audit-pub",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        entries = await harness.service.find_entries_by_principal(
            user_context, "public", None
        )
        assert len(entries) == 1

    async def test_find_entries_by_resource_owner_scoped(
        self, harness, user_context
    ) -> None:
        """Non-vacuity: a resource owned by ANOTHER user must not
        appear — audit reads are owner-scoped, not a global list."""
        await harness.seed(
            user_sub="a-different-owner",
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-not-mine",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        entries = await harness.service.find_entries_by_resource(
            user_context, "agent", "res-not-mine"
        )
        assert entries == []

    async def test_find_entries_by_principal_narrows_by_resource_type(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="grantee-3",
            principal_model="User",
            resource_type="agent",
            resource_id="res-narrow-1",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="grantee-3",
            principal_model="User",
            resource_type="skill",
            resource_id="res-narrow-2",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        entries = await harness.service.find_entries_by_principal(
            user_context, "user", "grantee-3", resource_type="skill"
        )
        assert len(entries) == 1
        assert entries[0]["resource_type"] == "skill"


# ── Batch permissions + accessible-resources bounding ─────────────────────


class TestBatchAndBounding:
    async def test_effective_permissions_for_resources_omits_missing(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-batch-1",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        result = await harness.service.get_effective_permissions_for_resources(
            user_context, "agent", ["res-batch-1", "res-batch-missing"]
        )
        assert result == {"res-batch-1": PERMISSION_BIT_VIEW}

    async def test_effective_permissions_for_resources_empty_input(
        self, harness, user_context
    ) -> None:
        assert (
            await harness.service.get_effective_permissions_for_resources(
                user_context, "agent", []
            )
            == {}
        )

    async def test_find_accessible_resources_bounded_by_resource_ids(
        self, harness, user_context
    ) -> None:
        for rid in ("res-bound-1", "res-bound-2"):
            await harness.seed(
                user_sub=user_context.user_id,
                principal_type="user",
                principal_id=user_context.user_id,
                principal_model="User",
                resource_type="agent",
                resource_id=rid,
                perm_bits=PERMISSION_BIT_VIEW,
            )
        ids = await harness.service.find_accessible_resources(
            user_context, "agent", PERMISSION_BIT_VIEW, resource_ids=["res-bound-1"]
        )
        assert ids == ["res-bound-1"]


# ── Sole-owned resource ids ────────────────────────────────────────────────


class TestSoleOwnedResourceIds:
    async def test_sole_owner_when_no_one_else_holds_delete(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-sole",
            perm_bits=OWNER_PERMISSION_BITS,
        )
        ids = await harness.service.get_sole_owned_resource_ids(user_context, ["agent"])
        assert ids == ["res-sole"]

    async def test_not_sole_owner_when_another_user_holds_delete(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-shared-delete",
            perm_bits=OWNER_PERMISSION_BITS,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="co-owner",
            principal_model="User",
            resource_type="agent",
            resource_id="res-shared-delete",
            perm_bits=PERMISSION_BIT_DELETE,
        )
        ids = await harness.service.get_sole_owned_resource_ids(user_context, ["agent"])
        assert "res-shared-delete" not in ids

    async def test_view_only_does_not_count_as_ownership(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-view-only",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        ids = await harness.service.get_sole_owned_resource_ids(user_context, ["agent"])
        assert ids == []

    async def test_not_sole_owner_when_competitor_holds_delete_among_other_bits(
        self, harness, user_context
    ) -> None:
        """WU-1 fix round 1, F2 — site 5/5, the multi_owner cross-check
        loop. ``test_not_sole_owner_when_another_user_holds_delete``
        above seeds the competitor with ``perm_bits=PERMISSION_BIT_DELETE``
        EXACTLY — indistinguishable from an equality check. Here the
        competitor's DELETE bit is a SUPERSET (MAX_PERM_BITS) so an
        equality-neutered loop 2 (``row.perm_bits != PERMISSION_BIT_DELETE``)
        would WRONGLY skip the competitor and misreport the caller as
        sole owner — a privilege-escalation-shaped false grant."""
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-shared-delete-superset",
            perm_bits=OWNER_PERMISSION_BITS,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="co-owner-superset",
            principal_model="User",
            resource_type="agent",
            resource_id="res-shared-delete-superset",
            perm_bits=MAX_PERM_BITS,
        )
        ids = await harness.service.get_sole_owned_resource_ids(user_context, ["agent"])
        assert "res-shared-delete-superset" not in ids, (
            "a co-owner holding DELETE among OTHER bits was not counted "
            "as a competing owner — sole-owned loop 2 containment is "
            "equality-style rot, not containment"
        )

    async def test_sole_owner_when_competitor_lacks_delete_among_other_bits(
        self, harness, user_context
    ) -> None:
        """Mirror of the test above: a competitor holding every OTHER
        bit but NOT delete must NOT count as a competing owner — the
        caller remains sole owner. Proves loop 2 isn't over-eager
        either (denies-when-absent, same discipline as
        TestBitmaskContainment)."""
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-sole-competitor-no-delete",
            perm_bits=OWNER_PERMISSION_BITS,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="competitor-no-delete",
            principal_model="User",
            resource_type="agent",
            resource_id="res-sole-competitor-no-delete",
            perm_bits=MAX_PERM_BITS & ~PERMISSION_BIT_DELETE,
        )
        ids = await harness.service.get_sole_owned_resource_ids(user_context, ["agent"])
        assert "res-sole-competitor-no-delete" in ids, (
            "a competitor genuinely lacking DELETE was still counted as "
            "a competing owner — sole-owned loop 2 is over-eager"
        )


# ── get_owner_principal_ids — the aggregateAclEntries site-1 fold ────────


class TestOwnerPrincipalIds:
    """The documented exact-equality exception — see
    ``_postgres.PostgresConsoleAclEntriesService.
    get_owner_principal_ids``'s docstring."""

    async def test_resolves_the_owner(self, harness, user_context) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-owner-1",
            perm_bits=OWNER_PERMISSION_BITS,
            granted_at_ms=100,
        )
        owners = await harness.service.get_owner_principal_ids(
            user_context, "agent", ["res-owner-1"]
        )
        assert owners == {"res-owner-1": user_context.user_id}

    async def test_owner_lookup_is_isolated_by_caller(
        self, harness, user_context
    ) -> None:
        """Non-vacuity/isolation guard: a resource entirely unrelated
        to the caller (a stranger owns it, a DIFFERENT stranger is
        recorded as the ACL owner-principal, caller is neither owner
        nor principal nor is the row PUBLIC) must NOT resolve — this
        is the guard the RLS-mirror clause carries here (unlike
        ``has_permission``/``get_effective_permissions``, THIS method
        has no separate caller-scoped principal filter of its own, so
        removing the RLS mirror here is NOT redundant with anything
        else)."""
        stranger = replace(user_context, user_id="stranger-owner-lookup")
        await harness.seed(
            user_sub="totally-unrelated-owner",
            principal_type="user",
            principal_id=stranger.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-owner-stranger",
            perm_bits=OWNER_PERMISSION_BITS,
        )
        owners = await harness.service.get_owner_principal_ids(
            user_context, "agent", ["res-owner-stranger"]
        )
        assert "res-owner-stranger" not in owners, (
            "a caller resolved a stranger's owner-principal for a "
            "resource they have no relationship to — RLS-mirror "
            "isolation is broken in get_owner_principal_ids"
        )

    async def test_partial_bits_do_not_count_as_owner(
        self, harness, user_context
    ) -> None:
        """The deliberate exact-equality: VIEW|EDIT|DELETE (missing
        SHARE) must NOT resolve as owner, even though it contains
        DELETE."""
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-owner-2",
            perm_bits=PERMISSION_BIT_VIEW | PERMISSION_BIT_EDIT | PERMISSION_BIT_DELETE,
        )
        owners = await harness.service.get_owner_principal_ids(
            user_context, "agent", ["res-owner-2"]
        )
        assert "res-owner-2" not in owners

    async def test_earliest_granted_wins_the_tie(self, harness, user_context) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="later-user",
            principal_model="User",
            resource_type="agent",
            resource_id="res-owner-tie",
            perm_bits=OWNER_PERMISSION_BITS,
            granted_at_ms=200,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-owner-tie",
            perm_bits=OWNER_PERMISSION_BITS,
            granted_at_ms=100,
        )
        owners = await harness.service.get_owner_principal_ids(
            user_context, "agent", ["res-owner-tie"]
        )
        assert owners["res-owner-tie"] == user_context.user_id

    async def test_empty_resource_ids_returns_empty_map(
        self, harness, user_context
    ) -> None:
        assert (
            await harness.service.get_owner_principal_ids(user_context, "agent", [])
            == {}
        )

    async def test_owner_branch_alone_makes_a_grant_to_someone_else_visible(
        self, harness, user_context
    ) -> None:
        """WU-1 fix round 1, A1 — the RLS mirror's OWNER branch
        (``row.user_sub == user_context.user_id`` / Postgres
        ``user_sub = current_setting(...)``) is unfalsified alone in
        every OTHER test here: they all set ``user_sub == principal_id``,
        so branch 2 (direct-principal match) would ALSO make the row
        visible even with the owner branch deleted. This row breaks
        that: the caller GRANTED OWNER_PERMISSION_BITS to someone else
        entirely — visible ONLY via "you granted this, you may see who
        it went to", never via "you ARE the principal". Deleting the
        owner branch must make this resource's owner unresolvable."""
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="someone-else-entirely",
            principal_model="User",
            resource_type="agent",
            resource_id="res-owner-branch-alone",
            perm_bits=OWNER_PERMISSION_BITS,
        )
        owners = await harness.service.get_owner_principal_ids(
            user_context, "agent", ["res-owner-branch-alone"]
        )
        assert owners == {"res-owner-branch-alone": "someone-else-entirely"}, (
            "the caller granted OWNER_PERMISSION_BITS to another "
            "principal but could not resolve it — the RLS mirror's "
            "owner branch is not doing its job"
        )


# ── Cross-user isolation at the SERVICE layer (SQLite has no RLS) ────────


class TestCrossUserIsolationServiceLayer:
    """Belt-and-suspenders proof mirroring every sibling console-*
    domain (feedback_unit_tests_miss_rls) — full HTTP-route coverage
    lives in ``tests/test_console_acl_routes.py``."""

    async def test_user_b_cannot_see_user_as_grant_via_effective_permissions(
        self, harness, user_context
    ) -> None:
        alice = replace(user_context, user_id="alice-svc")
        bob = replace(user_context, user_id="bob-svc")
        set_current_user_id(alice.user_id)
        try:
            await harness.seed(
                user_sub=alice.user_id,
                principal_type="user",
                principal_id=alice.user_id,
                principal_model="User",
                resource_type="agent",
                resource_id="res-alice-only",
                perm_bits=PERMISSION_BIT_VIEW,
            )
        finally:
            set_current_user_id(None)

        set_current_user_id(bob.user_id)
        try:
            bits = await harness.service.get_effective_permissions(
                bob, "agent", "res-alice-only"
            )
        finally:
            set_current_user_id(None)
        assert bits == 0, "bob saw alice's private grant — isolation broken"


# ── Real cross-user sharing — a grant TO someone else must actually work ──
# (the positive counterpart to every isolation test above; also closes a
# branch-coverage gap: a row where user_sub != caller but principal_id ==
# caller was previously never exercised).


class TestGrantToAnotherUserIsHonored:
    async def test_the_grantee_can_use_a_grant_someone_else_owns(
        self, harness, user_context
    ) -> None:
        owner = replace(user_context, user_id="grant-owner")
        await harness.seed(
            user_sub=owner.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-shared-to-me",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        assert (
            await harness.service.has_permission(
                user_context, "agent", "res-shared-to-me", PERMISSION_BIT_VIEW
            )
            is True
        )
        assert (
            await harness.service.get_effective_permissions(
                user_context, "agent", "res-shared-to-me"
            )
            == PERMISSION_BIT_VIEW
        )
        ids = await harness.service.find_accessible_resources(
            user_context, "agent", PERMISSION_BIT_VIEW
        )
        assert "res-shared-to-me" in ids


# ── Multi-row mixed scenarios — branch coverage for the per-row skip/ ────
# continue paths that a single-matching-row test can never exercise ──────
# (this class targets BOTH implementations' loops/WHERE clauses with a
# MIX of a non-matching row and a matching row in the SAME query).


class TestMixedRowScenarios:
    async def test_invisible_row_is_skipped_visible_row_still_grants(
        self, harness, user_context
    ) -> None:
        stranger = "totally-unrelated-stranger"
        await harness.seed(
            user_sub=stranger,
            principal_type="user",
            principal_id=stranger,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-1",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-1",
            perm_bits=PERMISSION_BIT_EDIT,
        )
        bits = await harness.service.get_effective_permissions(
            user_context, "agent", "res-mixed-1"
        )
        assert bits == PERMISSION_BIT_EDIT

    async def test_wrong_resource_type_row_is_skipped_in_batch(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="skill",
            resource_id="res-mixed-2",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-2",
            perm_bits=PERMISSION_BIT_EDIT,
        )
        result = await harness.service.get_effective_permissions_for_resources(
            user_context, "agent", ["res-mixed-2"]
        )
        assert result == {"res-mixed-2": PERMISSION_BIT_EDIT}

    async def test_non_matching_principal_row_is_skipped_in_accessible(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub="another-owner",
            principal_type="user",
            principal_id="another-principal",
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-3",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub="another-owner-2",
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-mixed-3",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        ids = await harness.service.find_accessible_resources(
            user_context, "agent", PERMISSION_BIT_VIEW
        )
        assert "res-mixed-3" in ids

    async def test_bounded_resource_ids_skips_out_of_bound_row(
        self, harness, user_context
    ) -> None:
        for rid in ("res-mixed-4a", "res-mixed-4b"):
            await harness.seed(
                user_sub=user_context.user_id,
                principal_type="public",
                principal_id=None,
                principal_model=None,
                resource_type="agent",
                resource_id=rid,
                perm_bits=PERMISSION_BIT_VIEW,
            )
        ids = await harness.service.find_public_resource_ids(
            user_context, "agent", PERMISSION_BIT_VIEW, resource_ids=["res-mixed-4a"]
        )
        assert ids == ["res-mixed-4a"]

    async def test_expired_row_skipped_non_expired_row_still_found(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-mixed-5",
            perm_bits=PERMISSION_BIT_VIEW,
            expired_at_ms=1,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-5",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        ids = await harness.service.find_accessible_resources(
            user_context, "agent", PERMISSION_BIT_VIEW
        )
        assert "res-mixed-5" in ids

    async def test_non_delete_row_skipped_in_sole_owned_scan(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="skill",
            resource_id="res-mixed-6",
            perm_bits=OWNER_PERMISSION_BITS,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-6",
            perm_bits=OWNER_PERMISSION_BITS,
        )
        ids = await harness.service.get_sole_owned_resource_ids(user_context, ["agent"])
        assert ids == ["res-mixed-6"]

    async def test_other_owner_view_only_does_not_block_sole_ownership(
        self, harness, user_context
    ) -> None:
        """A co-principal holding only VIEW (not DELETE) must not count
        as a competing owner in the second (multi-owner) scan."""
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-7",
            perm_bits=OWNER_PERMISSION_BITS,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="viewer-only",
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-7",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        ids = await harness.service.get_sole_owned_resource_ids(user_context, ["agent"])
        assert ids == ["res-mixed-7"]

    async def test_find_entries_by_principal_skips_other_principal(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="principal-a",
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-8a",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="principal-b",
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-8b",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        entries = await harness.service.find_entries_by_principal(
            user_context, "user", "principal-b"
        )
        assert [e["resource_id"] for e in entries] == ["res-mixed-8b"]

    async def test_find_entries_by_principals_and_resource_skips_non_matching(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub="owner-mixed-9",
            principal_type="user",
            principal_id="not-the-caller",
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-9",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub="owner-mixed-9",
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-mixed-9",
            perm_bits=PERMISSION_BIT_EDIT,
        )
        entries = await harness.service.find_entries_by_principals_and_resource(
            user_context, "agent", "res-mixed-9"
        )
        assert len(entries) == 1
        assert entries[0]["principal_type"] == "public"

    async def test_seed_entry_rejects_invalid_principal_type(
        self, mock_harness
    ) -> None:
        with pytest.raises(ValueError, match="invalid principal_type"):
            mock_harness.service.seed_entry(
                user_sub="x",
                principal_type="bogus",
                principal_id="y",
                principal_model=None,
                resource_type="agent",
                resource_id="z",
                perm_bits=1,
            )

    def test_mock_reset_clears_entries(self, mock_harness) -> None:
        mock_harness.service.seed_entry(
            user_sub="x",
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="z",
            perm_bits=1,
        )
        assert len(mock_harness.service._entries) == 1
        mock_harness.service.reset()
        assert mock_harness.service._entries == []

    async def test_has_permission_skips_wrong_resource_id_row(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-10-other",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-10",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        assert (
            await harness.service.has_permission(
                user_context, "agent", "res-mixed-10", PERMISSION_BIT_VIEW
            )
            is True
        )

    async def test_effective_permissions_skips_wrong_resource_and_principal(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-11-other",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub="another-owner-11",
            principal_type="user",
            principal_id="not-the-caller-11",
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-11",
            perm_bits=PERMISSION_BIT_EDIT,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-11",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        bits = await harness.service.get_effective_permissions(
            user_context, "agent", "res-mixed-11"
        )
        assert bits == PERMISSION_BIT_VIEW

    async def test_effective_permissions_for_resources_skips_invisible_and_expired(
        self, harness, user_context
    ) -> None:
        stranger = replace(user_context, user_id="stranger-batch-12")
        await harness.seed(
            user_sub="another-owner-12",
            principal_type="user",
            principal_id=stranger.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-12",
            perm_bits=PERMISSION_BIT_EDIT,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-12",
            perm_bits=PERMISSION_BIT_VIEW,
            expired_at_ms=1,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-12",
            perm_bits=PERMISSION_BIT_SHARE,
        )
        result = await harness.service.get_effective_permissions_for_resources(
            user_context, "agent", ["res-mixed-12"]
        )
        assert result == {"res-mixed-12": PERMISSION_BIT_SHARE}

    async def test_find_accessible_resources_full_multi_row_and_dedup(
        self, harness, user_context
    ) -> None:
        stranger = "stranger-13"
        await harness.seed(
            user_sub=stranger,
            principal_type="user",
            principal_id=stranger,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-13",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="skill",
            resource_id="res-mixed-13",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-13",
            perm_bits=PERMISSION_BIT_EDIT,
        )
        # Two DISTINCT valid grants for the SAME resource — exercises the
        # dedup ("already seen") branch on the second match.
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-mixed-13",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        ids = await harness.service.find_accessible_resources(
            user_context, "agent", PERMISSION_BIT_VIEW
        )
        assert ids == ["res-mixed-13"]

    async def test_find_public_resource_ids_skips_wrong_bit_dedups_duplicate(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub="owner-14",
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-mixed-14-wrong-bit",
            perm_bits=PERMISSION_BIT_EDIT,
        )
        await harness.seed(
            user_sub="owner-14",
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-mixed-14",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        ids = await harness.service.find_public_resource_ids(
            user_context, "agent", PERMISSION_BIT_VIEW
        )
        assert ids == ["res-mixed-14"]
        assert "res-mixed-14-wrong-bit" not in ids

    async def test_sole_owned_skips_unrelated_resource_type_and_expired_competitor(
        self, harness, user_context
    ) -> None:
        # First loop: a row for an UNRELATED resource_type, skipped, PLUS
        # a row with the wrong principal_type.
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="skill",
            resource_id="res-mixed-15-unrelated",
            perm_bits=OWNER_PERMISSION_BITS,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-mixed-15",
            # VIEW only (no DELETE) — must NOT count as a competing
            # owner in the second loop.
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-15",
            perm_bits=OWNER_PERMISSION_BITS,
        )
        # Second loop: an unrelated resource_type row (skip #1), an
        # EXPIRED competing DELETE grant (skip #2) — neither should
        # count as a competing owner.
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="unrelated-type-competitor",
            principal_model="User",
            resource_type="skill",
            resource_id="res-mixed-15",
            perm_bits=PERMISSION_BIT_DELETE,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="expired-competitor",
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-15",
            perm_bits=PERMISSION_BIT_DELETE,
            expired_at_ms=1,
        )
        ids = await harness.service.get_sole_owned_resource_ids(user_context, ["agent"])
        assert ids == ["res-mixed-15"]

    async def test_find_entries_by_principal_skips_other_owner_and_type(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub="another-owner-16",
            principal_type="user",
            principal_id="principal-16",
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-16-other-owner",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-mixed-16-other-type",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="principal-16",
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-16",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        entries = await harness.service.find_entries_by_principal(
            user_context, "user", "principal-16"
        )
        assert [e["resource_id"] for e in entries] == ["res-mixed-16"]

    async def test_find_entries_by_principals_and_resource_skips_wrong_resource(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub="owner-17",
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-mixed-17-other",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub="owner-17",
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-mixed-17",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        entries = await harness.service.find_entries_by_principals_and_resource(
            user_context, "agent", "res-mixed-17"
        )
        assert len(entries) == 1
        assert entries[0]["resource_id"] == "res-mixed-17"

    async def test_effective_permissions_skips_non_matching_principal_row(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="not-the-caller-18",
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-18",
            perm_bits=PERMISSION_BIT_EDIT,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-18",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        bits = await harness.service.get_effective_permissions(
            user_context, "agent", "res-mixed-18"
        )
        assert bits == PERMISSION_BIT_VIEW

    async def test_effective_permissions_for_resources_skips_non_matching_principal(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="not-the-caller-19",
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-19",
            perm_bits=PERMISSION_BIT_EDIT,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-19",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        result = await harness.service.get_effective_permissions_for_resources(
            user_context, "agent", ["res-mixed-19"]
        )
        assert result == {"res-mixed-19": PERMISSION_BIT_VIEW}

    async def test_find_accessible_resources_skips_non_matching_principal_and_dedups(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id="not-the-caller-20",
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-20",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        # Two DISTINCT matches for the SAME resource — closes the dedup
        # ("already seen") branch.
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-20",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-mixed-20",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        ids = await harness.service.find_accessible_resources(
            user_context, "agent", PERMISSION_BIT_VIEW
        )
        assert ids == ["res-mixed-20"]

    async def test_find_public_resource_ids_skips_wrong_type_and_dedups(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub="owner-21",
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="skill",
            resource_id="res-mixed-21",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        await harness.seed(
            user_sub="owner-21",
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-mixed-21",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        ids = await harness.service.find_public_resource_ids(
            user_context, "agent", PERMISSION_BIT_VIEW
        )
        assert ids == ["res-mixed-21"]

    async def test_sole_owned_first_loop_skips_invisible_row(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub="a-stranger-22",
            principal_type="user",
            principal_id="a-stranger-22",
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-22-invisible",
            perm_bits=OWNER_PERMISSION_BITS,
        )
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-22",
            perm_bits=OWNER_PERMISSION_BITS,
        )
        ids = await harness.service.get_sole_owned_resource_ids(user_context, ["agent"])
        assert ids == ["res-mixed-22"]

    async def test_sole_owned_second_loop_skips_unrelated_resource_id(
        self, harness, user_context
    ) -> None:
        await harness.seed(
            user_sub=user_context.user_id,
            principal_type="user",
            principal_id=user_context.user_id,
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-23",
            perm_bits=OWNER_PERMISSION_BITS,
        )
        # A DELETE-holding row for a DIFFERENT agent resource_id (not in
        # `owned`) — must be skipped, never treated as a competitor for
        # res-mixed-23.
        await harness.seed(
            user_sub="another-owner-23",
            principal_type="user",
            principal_id="another-owner-23",
            principal_model="User",
            resource_type="agent",
            resource_id="res-mixed-23-unrelated",
            perm_bits=OWNER_PERMISSION_BITS,
        )
        ids = await harness.service.get_sole_owned_resource_ids(user_context, ["agent"])
        assert "res-mixed-23" in ids

    async def test_find_public_resource_ids_dedups_two_matches_same_resource(
        self, mock_harness, user_context
    ) -> None:
        """Mock-only: seeds two PUBLIC rows for the SAME resource (the
        live schema's partial unique index forbids this in Postgres —
        this closes the Mock loop's dedup branch specifically)."""
        for bits in (PERMISSION_BIT_VIEW, PERMISSION_BIT_EDIT):
            mock_harness.service.seed_entry(
                user_sub="owner-24",
                principal_type="public",
                principal_id=None,
                principal_model=None,
                resource_type="agent",
                resource_id="res-mixed-24",
                perm_bits=bits | PERMISSION_BIT_VIEW,
            )
        ids = await mock_harness.service.find_public_resource_ids(
            user_context, "agent", PERMISSION_BIT_VIEW
        )
        assert ids == ["res-mixed-24"]

    async def test_sole_owned_first_loop_dedups_two_matches_same_resource(
        self, mock_harness, user_context
    ) -> None:
        """Mock-only: two DELETE-holding rows the caller owns for the
        SAME resource_id (closes the first loop's dedup branch)."""
        for principal_id in (user_context.user_id, user_context.user_id):
            mock_harness.service.seed_entry(
                user_sub=user_context.user_id,
                principal_type="user",
                principal_id=principal_id,
                principal_model="User",
                resource_type="agent",
                resource_id="res-mixed-25",
                perm_bits=OWNER_PERMISSION_BITS,
            )
        ids = await mock_harness.service.get_sole_owned_resource_ids(
            user_context, ["agent"]
        )
        assert ids == ["res-mixed-25"]
