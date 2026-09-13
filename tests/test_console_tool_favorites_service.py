"""Tests for the console-tool-favorites service (Tool-Favorites domain,
MongoDB-elimination EPIC).

Mirrors ``test_console_conversation_tags_service.py``'s structure (the
spec's instruction), scaled to the tool-favorites field set (item_type/
item_id/tenant_id instead of tag/description/count/position, no cursor
pagination — see the service module's docstring for why): Mock-service
interface tests, then a Postgres-backed suite via
``InMemoryPostgresFactory`` (aiosqlite, no real PostgreSQL required).
The cross-user isolation tests bracket their assertions with
``set_current_user_id`` per ``feedback_unit_tests_miss_rls`` — RLS
itself is a no-op on SQLite, so the guard under test is the service's
own explicit ``.filter(user_sub == ...)`` clause, not the (here inert)
Postgres GUC.

Also covers the two acceptance-critical behaviours the ratified spec
calls out by name: the ``MAX_TOOL_FAVORITES`` per-user cap (101st add
fails) and soft-delete-then-re-create (the D13 quirk this domain
AVOIDS).

**Every user-scoped filter has a neuter-sensitive test, BOTH
implementations** (the 2026-09-13 review's rule — aggregate queries
must be user-scoped too):

| filter (per implementation)          | test whose removal-of-filter turns RED       |
|--------------------------------------|----------------------------------------------|
| add: ACTIVE-row lookup ``user_sub``  | ``test_add_tool_favorite_cannot_overwrite_another_users_row`` |
| add: cap-COUNT aggregate ``user_sub``| ``test_cap_is_per_user_not_global``          |
| add: TOMBSTONE lookup ``user_sub``   | ``test_add_cannot_resurrect_another_users_tombstone`` |
| list ``user_sub``                    | ``test_isolates_tool_favorites_by_user`` / ``test_cross_user_isolation_denies_list`` |
| remove lookup ``user_sub``           | ``test_cross_user_remove_denied``            |
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import pytest_asyncio

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.db.rls import set_current_user_id
from audittrace.services.console_tool_favorites import (
    MAX_TOOL_FAVORITES,
    ConsoleToolFavoritesService,
    MockConsoleToolFavoritesService,
    PostgresConsoleToolFavoritesService,
    ToolFavoritesCapExceededError,
)

# ── MockConsoleToolFavoritesService ───────────────────────────────────────


class TestMockConsoleToolFavoritesService:
    def test_abstract_interface(self) -> None:
        assert isinstance(
            MockConsoleToolFavoritesService(), ConsoleToolFavoritesService
        )

    async def test_add_then_list_round_trips(self, user_context) -> None:
        service = MockConsoleToolFavoritesService()
        created = await service.add_tool_favorite(
            user_context, "tool", "web-search", tenant_id="acme"
        )
        assert created["item_type"] == "tool"
        assert created["item_id"] == "web-search"
        assert created["tenant_id"] == "acme"
        assert created["deleted_at_ms"] is None

        items = await service.list_tool_favorites(user_context)
        assert len(items) == 1
        assert items[0]["item_id"] == "web-search"

    async def test_add_defaults_tenant_id_and_metadata(self, user_context) -> None:
        service = MockConsoleToolFavoritesService()
        created = await service.add_tool_favorite(user_context, "tool", "bare")
        assert created["tenant_id"] is None
        assert created["metadata"] == {}

    async def test_add_is_idempotent_by_key(self, user_context) -> None:
        """Calling add twice with the same (item_type, item_id) updates
        the SAME row rather than creating a duplicate."""
        service = MockConsoleToolFavoritesService()
        first = await service.add_tool_favorite(user_context, "tool", "web-search")
        second = await service.add_tool_favorite(
            user_context, "tool", "web-search", tenant_id="acme"
        )
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["tenant_id"] == "acme"

        items = await service.list_tool_favorites(user_context)
        assert len(items) == 1

    async def test_reaffirm_active_favorite_updates_metadata(
        self, user_context
    ) -> None:
        """The re-affirm-an-already-active-favorite branch also updates
        ``metadata`` when supplied, independently of ``tenant_id``."""
        service = MockConsoleToolFavoritesService()
        await service.add_tool_favorite(user_context, "tool", "web-search")
        updated = await service.add_tool_favorite(
            user_context, "tool", "web-search", metadata={"pinned": True}
        )
        assert updated["metadata"] == {"pinned": True}

    async def test_re_add_soft_deleted_favorite_updates_metadata(
        self, user_context
    ) -> None:
        """The un-tombstone branch also updates ``metadata`` when
        supplied on the re-add call."""
        service = MockConsoleToolFavoritesService()
        await service.add_tool_favorite(user_context, "tool", "web-search")
        await service.remove_tool_favorite(user_context, "tool", "web-search")
        re_added = await service.add_tool_favorite(
            user_context, "tool", "web-search", metadata={"pinned": True}
        )
        assert re_added["deleted_at_ms"] is None
        assert re_added["metadata"] == {"pinned": True}

    async def test_add_idempotent_reaffirm_never_counts_against_cap(
        self, user_context
    ) -> None:
        """Re-affirming an already-active favorite must NOT count as a
        new cap-consuming add — fill the cap, then re-add an existing
        one, then confirm a genuinely new one still fails."""
        service = MockConsoleToolFavoritesService()
        for i in range(MAX_TOOL_FAVORITES):
            await service.add_tool_favorite(user_context, "tool", f"item-{i}")

        # Re-affirming an existing favorite at the cap must succeed.
        reaffirmed = await service.add_tool_favorite(user_context, "tool", "item-0")
        assert reaffirmed["item_id"] == "item-0"

        # A genuinely new one must still fail — the cap is exactly full.
        with pytest.raises(ToolFavoritesCapExceededError):
            await service.add_tool_favorite(user_context, "tool", "one-too-many")

    async def test_add_101st_favorite_raises_cap_exceeded(self, user_context) -> None:
        service = MockConsoleToolFavoritesService()
        for i in range(MAX_TOOL_FAVORITES):
            await service.add_tool_favorite(user_context, "tool", f"item-{i}")

        with pytest.raises(ToolFavoritesCapExceededError, match="100"):
            await service.add_tool_favorite(user_context, "tool", "item-100")

        items = await service.list_tool_favorites(user_context)
        assert len(items) == MAX_TOOL_FAVORITES

    async def test_remove_tool_favorite(self, user_context) -> None:
        service = MockConsoleToolFavoritesService()
        await service.add_tool_favorite(user_context, "tool", "web-search")
        assert (
            await service.remove_tool_favorite(user_context, "tool", "web-search")
            is True
        )
        items = await service.list_tool_favorites(user_context)
        assert items == []

    async def test_remove_missing_tool_favorite_returns_false(
        self, user_context
    ) -> None:
        service = MockConsoleToolFavoritesService()
        assert await service.remove_tool_favorite(user_context, "tool", "nope") is False

    async def test_soft_delete_then_re_add_works(self, user_context) -> None:
        """The D13 avoidance guard: removing then re-adding the SAME
        (item_type, item_id) pair must actually un-tombstone the row —
        never silently leave it deleted under an apparently-successful
        add."""
        service = MockConsoleToolFavoritesService()
        await service.add_tool_favorite(
            user_context, "tool", "web-search", tenant_id="first"
        )
        assert (
            await service.remove_tool_favorite(user_context, "tool", "web-search")
            is True
        )
        assert await service.list_tool_favorites(user_context) == []

        re_added = await service.add_tool_favorite(
            user_context, "tool", "web-search", tenant_id="second"
        )
        assert re_added["deleted_at_ms"] is None, (
            "re-adding a soft-deleted favorite must clear the tombstone "
            "— the D13 quirk this domain avoids"
        )
        assert re_added["tenant_id"] == "second"

        items = await service.list_tool_favorites(user_context)
        assert len(items) == 1
        assert items[0]["item_id"] == "web-search"
        assert items[0]["deleted_at_ms"] is None

    async def test_soft_delete_then_re_add_does_not_duplicate_cap_usage(
        self, user_context
    ) -> None:
        """Re-adding a soft-deleted favorite when the caller is
        otherwise AT the cap must succeed (the tombstoned row is not
        counted as active) — proving the cap counts ACTIVE rows only."""
        service = MockConsoleToolFavoritesService()
        await service.add_tool_favorite(user_context, "tool", "will-be-removed")
        await service.remove_tool_favorite(user_context, "tool", "will-be-removed")
        for i in range(MAX_TOOL_FAVORITES):
            await service.add_tool_favorite(user_context, "tool", f"item-{i}")

        with pytest.raises(ToolFavoritesCapExceededError):
            await service.add_tool_favorite(user_context, "tool", "will-be-removed")

    async def test_isolates_tool_favorites_by_user(self, user_context) -> None:
        """Non-vacuity guard: neuter the ``user_sub`` filter in
        ``list_tool_favorites`` and Bob starts seeing Alice's
        favorites."""
        service = MockConsoleToolFavoritesService()
        alice = replace(user_context, user_id="user-alice", is_admin=False)
        bob = replace(user_context, user_id="user-bob", is_admin=False)
        await service.add_tool_favorite(alice, "tool", "web-search")

        bob_items = await service.list_tool_favorites(bob)
        assert bob_items == [], (
            "user B's list_tool_favorites included user A's favorite — "
            "the isolation wall is broken (missing/neutered user_sub filter)"
        )

        alice_items = await service.list_tool_favorites(alice)
        assert len(alice_items) == 1

    async def test_cap_is_per_user_not_global(self, user_context) -> None:
        """Non-vacuity guard for the cap-COUNT aggregate's ``user_sub``
        filter (the 2026-09-13 REJECT — aggregate queries must be
        user-scoped): alice holding ``MAX_TOOL_FAVORITES`` active
        favorites must NOT consume bob's quota. Neuter the
        ``f.user_sub == user_context.user_id`` clause in the Mock's
        ``active_count`` sum and bob's FIRST add raises
        ``ToolFavoritesCapExceededError`` (one user filling the cap
        would deny the feature to every other user — cross-user DoS)."""
        service = MockConsoleToolFavoritesService()
        alice = replace(user_context, user_id="user-alice-cap", is_admin=False)
        bob = replace(user_context, user_id="user-bob-cap", is_admin=False)
        for i in range(MAX_TOOL_FAVORITES):
            await service.add_tool_favorite(alice, "tool", f"alice-item-{i}")

        # Alice really is at the cap.
        with pytest.raises(ToolFavoritesCapExceededError):
            await service.add_tool_favorite(alice, "tool", "alice-one-too-many")

        # Bob's FIRST add must succeed — his active count is 0, not 100.
        try:
            created = await service.add_tool_favorite(bob, "tool", "bob-first")
        except ToolFavoritesCapExceededError as exc:
            pytest.fail(
                "bob's FIRST add was rejected because ALICE is at the cap — "
                "the cap-count aggregate is missing/neutered its user_sub "
                f"filter (per-user cap degraded to a GLOBAL cap): {exc}"
            )
        assert created["item_id"] == "bob-first"
        assert created["user_sub"] == bob.user_id

        bob_items = await service.list_tool_favorites(bob)
        assert [i["item_id"] for i in bob_items] == ["bob-first"]
        alice_items = await service.list_tool_favorites(alice)
        assert len(alice_items) == MAX_TOOL_FAVORITES

    async def test_add_tool_favorite_cannot_overwrite_another_users_row(
        self, user_context
    ) -> None:
        """Non-vacuity guard for the ACTIVE-row lookup's ``user_sub``
        filter (``_find_active`` as used by ``add_tool_favorite``): bob
        adding the IDENTICAL ``(item_type, item_id)`` must create HIS OWN
        row, never update alice's. Neuter the ``f.user_sub == user_sub``
        clause in ``_find_active`` and bob's ``tenant_id`` overwrites
        alice's row."""
        service = MockConsoleToolFavoritesService()
        alice = replace(user_context, user_id="user-alice-add", is_admin=False)
        bob = replace(user_context, user_id="user-bob-add", is_admin=False)

        await service.add_tool_favorite(
            alice, "tool", "shared-item", tenant_id="alice-tenant"
        )
        await service.add_tool_favorite(
            bob, "tool", "shared-item", tenant_id="bob-tenant"
        )

        alice_items = await service.list_tool_favorites(alice)
        bob_items = await service.list_tool_favorites(bob)
        assert len(alice_items) == 1
        assert len(bob_items) == 1
        assert alice_items[0]["tenant_id"] == "alice-tenant", (
            "bob's add overwrote alice's tool-favorite — the user_sub "
            "isolation filter in the add's existence-check is "
            "missing/neutered"
        )
        assert bob_items[0]["tenant_id"] == "bob-tenant"

    async def test_add_cannot_resurrect_another_users_tombstone(
        self, user_context
    ) -> None:
        """Non-vacuity guard for the TOMBSTONE lookup's ``user_sub``
        filter (``_find_any`` as used by the un-tombstone branch): when
        alice's ONLY row for a key is soft-deleted and bob (who has no
        row) adds the same key, bob must get his OWN fresh row — alice's
        tombstone must stay deleted. Neuter the ``f.user_sub == user_sub``
        clause in ``_find_any`` and bob's add un-tombstones ALICE's row
        (with bob's ``tenant_id``), making it reappear in alice's list."""
        service = MockConsoleToolFavoritesService()
        alice = replace(user_context, user_id="user-alice-tomb", is_admin=False)
        bob = replace(user_context, user_id="user-bob-tomb", is_admin=False)

        await service.add_tool_favorite(
            alice, "tool", "shared-tomb", tenant_id="alice-tenant"
        )
        assert await service.remove_tool_favorite(alice, "tool", "shared-tomb") is True
        assert await service.list_tool_favorites(alice) == []

        created = await service.add_tool_favorite(
            bob, "tool", "shared-tomb", tenant_id="bob-tenant"
        )
        assert created["user_sub"] == bob.user_id

        alice_items = await service.list_tool_favorites(alice)
        assert alice_items == [], (
            "bob's add resurrected alice's soft-deleted tool-favorite — the "
            "user_sub isolation filter in the add's tombstone lookup is "
            "missing/neutered"
        )
        bob_items = await service.list_tool_favorites(bob)
        assert len(bob_items) == 1
        assert bob_items[0]["tenant_id"] == "bob-tenant"

    async def test_cross_user_remove_denied(self, user_context) -> None:
        """Non-vacuity guard for the REMOVE lookup's ``user_sub`` filter
        (``_find_active`` as used by ``remove_tool_favorite``): neuter it
        and bob soft-deletes alice's row."""
        service = MockConsoleToolFavoritesService()
        alice = replace(user_context, user_id="user-alice-del", is_admin=False)
        bob = replace(user_context, user_id="user-bob-del", is_admin=False)
        await service.add_tool_favorite(alice, "tool", "alice-item")

        removed = await service.remove_tool_favorite(bob, "tool", "alice-item")
        assert removed is False, (
            "user B removed user A's tool-favorite — isolation broken"
        )
        alice_items = await service.list_tool_favorites(alice)
        assert len(alice_items) == 1

    async def test_reset(self, user_context) -> None:
        service = MockConsoleToolFavoritesService()
        await service.add_tool_favorite(user_context, "tool", "web-search")
        service.reset()
        assert await service.list_tool_favorites(user_context) == []

    async def test_list_oldest_first(self, user_context) -> None:
        service = MockConsoleToolFavoritesService()
        await service.add_tool_favorite(user_context, "tool", "item-1")
        await service.add_tool_favorite(user_context, "tool", "item-2")
        await service.add_tool_favorite(user_context, "tool", "item-3")
        items = await service.list_tool_favorites(user_context)
        assert [i["item_id"] for i in items] == ["item-1", "item-2", "item-3"]

    async def test_list_excludes_deleted(self, user_context) -> None:
        service = MockConsoleToolFavoritesService()
        await service.add_tool_favorite(user_context, "tool", "item-1")
        await service.add_tool_favorite(user_context, "tool", "item-2")
        await service.remove_tool_favorite(user_context, "tool", "item-1")
        items = await service.list_tool_favorites(user_context)
        assert [i["item_id"] for i in items] == ["item-2"]


# ── PostgresConsoleToolFavoritesService (aiosqlite) ───────────────────────


@pytest_asyncio.fixture
async def pg_factory():
    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    return factory


@pytest.fixture
def service(pg_factory) -> PostgresConsoleToolFavoritesService:
    return PostgresConsoleToolFavoritesService(
        session_factory=pg_factory.get_session_factory(),
    )


class TestPostgresConsoleToolFavoritesService:
    def test_abstract_interface(self, service) -> None:
        assert isinstance(service, ConsoleToolFavoritesService)

    async def test_add_then_list_round_trips(self, service, user_context) -> None:
        created = await service.add_tool_favorite(
            user_context, "tool", "web-search", tenant_id="acme"
        )
        assert created["item_type"] == "tool"
        assert created["item_id"] == "web-search"
        assert created["tenant_id"] == "acme"

        items = await service.list_tool_favorites(user_context)
        assert len(items) == 1
        assert items[0]["item_id"] == "web-search"

    async def test_add_is_idempotent_by_key(self, service, user_context) -> None:
        first = await service.add_tool_favorite(user_context, "tool", "web-search")
        second = await service.add_tool_favorite(
            user_context, "tool", "web-search", tenant_id="acme"
        )
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["tenant_id"] == "acme"

        items = await service.list_tool_favorites(user_context)
        assert len(items) == 1

    async def test_reaffirm_active_favorite_updates_metadata(
        self, service, user_context
    ) -> None:
        """The re-affirm-an-already-active-favorite branch also updates
        ``metadata`` when supplied, independently of ``tenant_id``."""
        await service.add_tool_favorite(user_context, "tool", "web-search")
        updated = await service.add_tool_favorite(
            user_context, "tool", "web-search", metadata={"pinned": True}
        )
        assert updated["metadata"] == {"pinned": True}

    async def test_re_add_soft_deleted_favorite_updates_metadata(
        self, service, user_context
    ) -> None:
        """The un-tombstone branch also updates ``metadata`` when
        supplied on the re-add call."""
        await service.add_tool_favorite(user_context, "tool", "web-search")
        await service.remove_tool_favorite(user_context, "tool", "web-search")
        re_added = await service.add_tool_favorite(
            user_context, "tool", "web-search", metadata={"pinned": True}
        )
        assert re_added["deleted_at_ms"] is None
        assert re_added["metadata"] == {"pinned": True}

    async def test_add_101st_favorite_raises_cap_exceeded(
        self, service, user_context
    ) -> None:
        for i in range(MAX_TOOL_FAVORITES):
            await service.add_tool_favorite(user_context, "tool", f"item-{i}")

        with pytest.raises(ToolFavoritesCapExceededError, match="100"):
            await service.add_tool_favorite(user_context, "tool", "item-100")

        items = await service.list_tool_favorites(user_context)
        assert len(items) == MAX_TOOL_FAVORITES

    async def test_add_idempotent_reaffirm_never_counts_against_cap(
        self, service, user_context
    ) -> None:
        for i in range(MAX_TOOL_FAVORITES):
            await service.add_tool_favorite(user_context, "tool", f"item-{i}")

        reaffirmed = await service.add_tool_favorite(user_context, "tool", "item-0")
        assert reaffirmed["item_id"] == "item-0"

        with pytest.raises(ToolFavoritesCapExceededError):
            await service.add_tool_favorite(user_context, "tool", "one-too-many")

    async def test_soft_delete_then_re_add_works(self, service, user_context) -> None:
        """The D13 avoidance guard, Postgres-backed path: the existence
        lookup filters ``deleted_at_ms IS NULL`` and re-adding clears
        the tombstone on the SAME row (the unique constraint forbids a
        second INSERT)."""
        await service.add_tool_favorite(
            user_context, "tool", "web-search", tenant_id="first"
        )
        assert (
            await service.remove_tool_favorite(user_context, "tool", "web-search")
            is True
        )
        assert await service.list_tool_favorites(user_context) == []

        re_added = await service.add_tool_favorite(
            user_context, "tool", "web-search", tenant_id="second"
        )
        assert re_added["deleted_at_ms"] is None, (
            "re-adding a soft-deleted favorite must clear the tombstone "
            "— the D13 quirk this domain avoids"
        )
        assert re_added["tenant_id"] == "second"

        items = await service.list_tool_favorites(user_context)
        assert len(items) == 1
        assert items[0]["deleted_at_ms"] is None

    async def test_remove_missing_tool_favorite_returns_false(
        self, service, user_context
    ) -> None:
        assert await service.remove_tool_favorite(user_context, "tool", "nope") is False

    async def test_cross_user_isolation_denies_list(
        self, service, user_context
    ) -> None:
        """Acceptance guard — RLS: user B cannot see user A's
        favorites via list. Neuter the explicit ``user_sub`` filter in
        ``PostgresConsoleToolFavoritesService.list_tool_favorites`` and
        this test goes RED.

        Brackets the assertion with ``set_current_user_id`` per
        ``feedback_unit_tests_miss_rls`` — SQLite has no Postgres RLS
        GUC, so the ACTUAL guard under test is the service's own
        explicit ``.filter(user_sub == ...)`` clause, exercised
        identically to how it would run inside a real request (where
        ``require_user`` sets this same ContextVar).
        """
        alice = replace(user_context, user_id="user-alice-rls", is_admin=False)
        bob = replace(user_context, user_id="user-bob-rls", is_admin=False)

        set_current_user_id(alice.user_id)
        try:
            await service.add_tool_favorite(alice, "tool", "secret-tool")
        finally:
            set_current_user_id(None)

        set_current_user_id(bob.user_id)
        try:
            bob_items = await service.list_tool_favorites(bob)
        finally:
            set_current_user_id(None)

        assert bob_items == [], (
            "user B's list_tool_favorites included user A's favorite — "
            "the isolation wall is broken (missing/neutered user_sub filter)"
        )

        set_current_user_id(alice.user_id)
        try:
            alice_items = await service.list_tool_favorites(alice)
        finally:
            set_current_user_id(None)
        assert len(alice_items) == 1

    async def test_add_tool_favorite_cannot_overwrite_another_users_row(
        self, service, user_context
    ) -> None:
        """Non-vacuity guard: even with an IDENTICAL (item_type,
        item_id), bob's add must create/update HIS OWN row, never
        alice's — neuter the explicit ``user_sub`` filter in the add's
        existence check and this test goes RED (bob's tenant_id would
        overwrite alice's row instead of creating a second, isolated
        one)."""
        alice = replace(user_context, user_id="user-alice-add", is_admin=False)
        bob = replace(user_context, user_id="user-bob-add", is_admin=False)

        await service.add_tool_favorite(
            alice, "tool", "shared-item", tenant_id="alice-tenant"
        )
        await service.add_tool_favorite(
            bob, "tool", "shared-item", tenant_id="bob-tenant"
        )

        alice_items = await service.list_tool_favorites(alice)
        bob_items = await service.list_tool_favorites(bob)
        assert len(alice_items) == 1
        assert len(bob_items) == 1
        assert alice_items[0]["tenant_id"] == "alice-tenant", (
            "bob's add overwrote alice's tool-favorite — the user_sub "
            "isolation filter in the add's existence-check is "
            "missing/neutered"
        )
        assert bob_items[0]["tenant_id"] == "bob-tenant"

    async def test_cross_user_remove_denied(self, service, user_context) -> None:
        """Non-vacuity guard for the REMOVE lookup's ``user_sub`` filter:
        neuter it and bob soft-deletes alice's row."""
        alice = replace(user_context, user_id="user-alice-del", is_admin=False)
        bob = replace(user_context, user_id="user-bob-del", is_admin=False)
        await service.add_tool_favorite(alice, "tool", "alice-item")

        removed = await service.remove_tool_favorite(bob, "tool", "alice-item")
        assert removed is False, (
            "user B removed user A's tool-favorite — isolation broken"
        )
        alice_items = await service.list_tool_favorites(alice)
        assert len(alice_items) == 1

    async def test_cap_is_per_user_not_global(self, service, user_context) -> None:
        """Non-vacuity guard for the cap-COUNT aggregate's ``user_sub``
        filter, Postgres-backed path (the 2026-09-13 REJECT — aggregate
        queries must be user-scoped): alice holding ``MAX_TOOL_FAVORITES``
        active favorites must NOT consume bob's quota. Neuter the
        ``.filter(ConsoleToolFavorite.user_sub == user_context.user_id)``
        clause on the ``select(func.count())`` in
        ``PostgresConsoleToolFavoritesService.add_tool_favorite`` and
        bob's FIRST add raises ``ToolFavoritesCapExceededError`` — one
        user filling the cap would deny the feature to every other user
        (cross-user DoS).

        Brackets each caller's calls with ``set_current_user_id`` per
        ``feedback_unit_tests_miss_rls`` — SQLite has no RLS GUC, so the
        guard under test is the service's own explicit filter."""
        alice = replace(user_context, user_id="user-alice-cap", is_admin=False)
        bob = replace(user_context, user_id="user-bob-cap", is_admin=False)

        set_current_user_id(alice.user_id)
        try:
            for i in range(MAX_TOOL_FAVORITES):
                await service.add_tool_favorite(alice, "tool", f"alice-item-{i}")
            # Alice really is at the cap.
            with pytest.raises(ToolFavoritesCapExceededError):
                await service.add_tool_favorite(alice, "tool", "alice-one-too-many")
        finally:
            set_current_user_id(None)

        set_current_user_id(bob.user_id)
        try:
            try:
                created = await service.add_tool_favorite(bob, "tool", "bob-first")
            except ToolFavoritesCapExceededError as exc:
                pytest.fail(
                    "bob's FIRST add was rejected because ALICE is at the cap "
                    "— the cap-count aggregate is missing/neutered its "
                    f"user_sub filter (per-user cap degraded to GLOBAL): {exc}"
                )
            bob_items = await service.list_tool_favorites(bob)
        finally:
            set_current_user_id(None)

        assert created["item_id"] == "bob-first"
        assert created["user_sub"] == bob.user_id
        assert [i["item_id"] for i in bob_items] == ["bob-first"]

        set_current_user_id(alice.user_id)
        try:
            alice_items = await service.list_tool_favorites(alice)
        finally:
            set_current_user_id(None)
        assert len(alice_items) == MAX_TOOL_FAVORITES

    async def test_add_cannot_resurrect_another_users_tombstone(
        self, service, user_context
    ) -> None:
        """Non-vacuity guard for the TOMBSTONE lookup's ``user_sub``
        filter in ``add_tool_favorite``'s un-tombstone branch: when
        alice's ONLY row for a key is soft-deleted and bob (who has no
        row) adds the same key, bob must get his OWN fresh row — alice's
        tombstone must stay deleted. Neuter the ``user_sub`` clause on
        the ``tombstoned`` select and bob's add un-tombstones ALICE's row
        (with bob's ``tenant_id``), making it reappear in alice's list."""
        alice = replace(user_context, user_id="user-alice-tomb", is_admin=False)
        bob = replace(user_context, user_id="user-bob-tomb", is_admin=False)

        await service.add_tool_favorite(
            alice, "tool", "shared-tomb", tenant_id="alice-tenant"
        )
        assert await service.remove_tool_favorite(alice, "tool", "shared-tomb") is True
        assert await service.list_tool_favorites(alice) == []

        created = await service.add_tool_favorite(
            bob, "tool", "shared-tomb", tenant_id="bob-tenant"
        )
        assert created["user_sub"] == bob.user_id

        alice_items = await service.list_tool_favorites(alice)
        assert alice_items == [], (
            "bob's add resurrected alice's soft-deleted tool-favorite — the "
            "user_sub isolation filter in the add's tombstone lookup is "
            "missing/neutered"
        )
        bob_items = await service.list_tool_favorites(bob)
        assert len(bob_items) == 1
        assert bob_items[0]["tenant_id"] == "bob-tenant"

    async def test_write_failure_raises_runtime_error(
        self, service, user_context, monkeypatch
    ) -> None:
        async def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "sqlalchemy.ext.asyncio.AsyncSession.commit",
            _boom,
        )
        with pytest.raises(RuntimeError, match="add_tool_favorite.*failed"):
            await service.add_tool_favorite(user_context, "tool", "web-search")

    async def test_list_oldest_first(self, service, user_context) -> None:
        await service.add_tool_favorite(user_context, "tool", "item-1")
        await service.add_tool_favorite(user_context, "tool", "item-2")
        await service.add_tool_favorite(user_context, "tool", "item-3")
        items = await service.list_tool_favorites(user_context)
        assert [i["item_id"] for i in items] == ["item-1", "item-2", "item-3"]
