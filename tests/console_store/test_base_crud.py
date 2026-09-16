"""Round-trip, RLS-scoping and D13 soft-delete invariants for
``services/console_store`` (split from ``test_console_store_base.py``, fix
round 1 A3 — that file exceeded the PYTHON-ENGINEERING §11 500-LOC
trigger).

Every invariant the base OWNS is proven here ONCE, against BOTH shared
implementations (``MockConsoleStore`` and ``PostgresConsoleStore`` on a
real aiosqlite engine), through the same parametrized ``sut`` fixture. A
domain migrated onto the base inherits these guarantees; it does not
re-prove them.

**Side-effect discipline** (``feedback_vacuous_neuter_test_antipattern``):
every isolation assertion is made against ``raw_rows()`` — a read that
BYPASSES the guarded API and returns every row of every user — never only
against the API under test. "bob got ``None``" is paired with "alice's
row is still alice's, unchanged".

Neuter map (each base invariant → the test that goes RED when it is removed):

| invariant (where)                                   | RED test                                                   |
|-----------------------------------------------------|--------------------------------------------------------------|
| RLS predicate in ``_scoped_select`` / ``_scoped_rows`` | ``TestRlsReadsAreUserScoped`` + ``TestRlsWritesAreUserScoped`` |
| user-scoped COUNT (``_scoped_count`` / ``len(_scoped_rows)``) | ``test_cap_is_per_user_not_global``, ``test_count_is_user_scoped`` |
| D13 tombstone lookup + un-tombstone (``upsert``)    | ``TestSoftDeleteThenRecreate``                              |
"""

from __future__ import annotations

import pytest

from audittrace.identity import UserContext
from audittrace.services.console_store import ConsoleStoreCapExceededError
from tests.console_store.support import KEY_A, KEY_B, KEY_C, Sut
from tests.console_store_fixture_domain import CappedWidgetDomain


class TestRoundTrip:
    async def test_upsert_get_list_round_trip(
        self, sut: Sut, alice: UserContext
    ) -> None:
        created = await sut.store.upsert(alice, KEY_A, {"payload": "p1", "priority": 7})
        assert created["kind"] == "tool" and created["name"] == "web-search"
        assert created["user_sub"] == "alice-sub"
        assert created["payload"] == "p1" and created["priority"] == 7
        assert created["deleted_at_ms"] is None
        assert created["created_at_ms"] == created["updated_at_ms"]

        got = await sut.store.get(alice, KEY_A)
        assert got == created
        items, cursor = await sut.store.list(alice)
        assert items == [created] and cursor is None
        assert await sut.store.count(alice) == 1

    async def test_defaults_hook_fills_omitted_value_columns(
        self, sut: Sut, alice: UserContext
    ) -> None:
        created = await sut.store.upsert(alice, KEY_A, {})
        assert created["payload"] is None and created["priority"] == 0

    async def test_upsert_existing_updates_same_row(
        self, sut: Sut, alice: UserContext
    ) -> None:
        first = await sut.store.upsert(alice, KEY_A, {"payload": "p1"})
        second = await sut.store.upsert(alice, KEY_A, {"priority": 3})
        assert second["id"] == first["id"]
        assert second["created_at_ms"] == first["created_at_ms"]
        assert second["updated_at_ms"] >= first["updated_at_ms"]
        assert second["payload"] == "p1" and second["priority"] == 3
        assert len(await sut.raw_rows()) == 1

    async def test_batch_get_aligns_to_input_order(
        self, sut: Sut, alice: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {})
        await sut.store.upsert(alice, KEY_C, {})
        got = await sut.store.batch_get(alice, [KEY_C, KEY_B, KEY_A])
        assert [None if g is None else g["name"] for g in got] == [
            "summarize",
            None,
            "web-search",
        ]
        assert await sut.store.batch_get(alice, []) == []

    async def test_domain_property_exposes_descriptor(self, sut: Sut) -> None:
        from tests.console_store_fixture_domain import WidgetDomain

        assert isinstance(sut.store.domain, WidgetDomain)


class TestRlsReadsAreUserScoped:
    async def test_get_never_returns_another_users_row(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {"payload": "alice-secret"})
        assert await sut.store.get(bob, KEY_A) is None, (
            "bob read alice's row — the user_sub predicate is missing/neutered"
        )
        raw = await sut.raw_rows()
        assert [(r["user_sub"], r["payload"]) for r in raw] == [
            ("alice-sub", "alice-secret")
        ]

    async def test_list_never_includes_another_users_row(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {})
        await sut.store.upsert(alice, KEY_B, {})
        items, _ = await sut.store.list(bob)
        assert items == [], "bob's list included alice's rows"
        alice_items, _ = await sut.store.list(alice)
        assert len(alice_items) == 2

    async def test_batch_get_never_includes_another_users_row(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {})
        assert await sut.store.batch_get(bob, [KEY_A]) == [None]
        assert (await sut.store.batch_get(alice, [KEY_A]))[0] is not None

    async def test_count_is_user_scoped(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {})
        await sut.store.upsert(alice, KEY_B, {})
        assert await sut.store.count(bob) == 0, "bob's COUNT included alice's rows"
        assert await sut.store.count(alice) == 2


class TestRlsWritesAreUserScoped:
    async def test_upsert_same_key_creates_own_row_never_touches_other_users(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {"payload": "alice"})
        bob_row = await sut.store.upsert(bob, KEY_A, {"payload": "bob"})
        assert bob_row["user_sub"] == "bob-sub"
        raw = sorted(await sut.raw_rows(), key=lambda r: r["user_sub"])
        assert [(r["user_sub"], r["payload"]) for r in raw] == [
            ("alice-sub", "alice"),
            ("bob-sub", "bob"),
        ], "bob's upsert overwrote alice's row (existence lookup unscoped)"

    async def test_delete_never_tombstones_another_users_row(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {})
        assert await sut.store.delete(bob, KEY_A) is False
        raw = await sut.raw_rows()
        assert raw[0]["user_sub"] == "alice-sub" and raw[0]["deleted_at_ms"] is None, (
            "bob soft-deleted alice's row"
        )

    async def test_upsert_never_resurrects_another_users_tombstone(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        alice_row = await sut.store.upsert(alice, KEY_A, {"payload": "alice"})
        assert await sut.store.delete(alice, KEY_A) is True
        bob_row = await sut.store.upsert(bob, KEY_A, {"payload": "bob"})
        assert bob_row["id"] != alice_row["id"]
        raw = {r["id"]: r for r in await sut.raw_rows()}
        assert raw[alice_row["id"]]["deleted_at_ms"] is not None, (
            "bob's upsert un-tombstoned ALICE's row (tombstone lookup unscoped)"
        )
        assert raw[alice_row["id"]]["payload"] == "alice"
        assert raw[bob_row["id"]]["user_sub"] == "bob-sub"


class TestSoftDeleteThenRecreate:
    async def test_delete_then_upsert_untombstones_the_same_row(
        self, sut: Sut, alice: UserContext
    ) -> None:
        first = await sut.store.upsert(alice, KEY_A, {"payload": "v1"})
        assert await sut.store.delete(alice, KEY_A) is True
        assert await sut.store.get(alice, KEY_A) is None
        assert await sut.store.count(alice) == 0

        again = await sut.store.upsert(alice, KEY_A, {"priority": 9})
        assert again["id"] == first["id"], "re-create must reuse the tombstoned row"
        assert again["deleted_at_ms"] is None, "tombstone was not cleared (D13)"
        assert again["payload"] == "v1" and again["priority"] == 9
        assert len(await sut.raw_rows()) == 1
        assert await sut.store.count(alice) == 1

    async def test_deleted_row_invisible_everywhere(
        self, sut: Sut, alice: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {})
        await sut.store.delete(alice, KEY_A)
        assert await sut.store.get(alice, KEY_A) is None
        assert (await sut.store.list(alice))[0] == []
        assert await sut.store.batch_get(alice, [KEY_A]) == [None]
        assert await sut.store.count(alice) == 0

    async def test_delete_is_idempotent(self, sut: Sut, alice: UserContext) -> None:
        assert await sut.store.delete(alice, KEY_A) is False
        await sut.store.upsert(alice, KEY_A, {})
        assert await sut.store.delete(alice, KEY_A) is True
        assert await sut.store.delete(alice, KEY_A) is False


class TestCapHook:
    async def test_cap_blocks_transition_into_active_at_limit(
        self, sut: Sut, alice: UserContext
    ) -> None:
        capped = sut.make(CappedWidgetDomain())
        for i in range(3):
            await capped.store.upsert(alice, {"kind": "tool", "name": f"n{i}"}, {})
        with pytest.raises(ConsoleStoreCapExceededError) as excinfo:
            await capped.store.upsert(alice, {"kind": "tool", "name": "n3"}, {})
        assert excinfo.value.cap == 3
        assert excinfo.value.domain == "console_store_test_widgets_capped"
        assert len(await capped.raw_rows()) == 3

    async def test_reaffirm_active_row_at_cap_is_allowed(
        self, sut: Sut, alice: UserContext
    ) -> None:
        capped = sut.make(CappedWidgetDomain())
        for i in range(3):
            await capped.store.upsert(alice, {"kind": "tool", "name": f"n{i}"}, {})
        updated = await capped.store.upsert(
            alice, {"kind": "tool", "name": "n0"}, {"priority": 5}
        )
        assert updated["priority"] == 5

    async def test_cap_is_per_user_not_global(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        capped = sut.make(CappedWidgetDomain())
        for i in range(3):
            await capped.store.upsert(alice, {"kind": "tool", "name": f"n{i}"}, {})
        try:
            created = await capped.store.upsert(bob, {"kind": "tool", "name": "b0"}, {})
        except ConsoleStoreCapExceededError as exc:
            pytest.fail(
                "bob's FIRST upsert was refused because ALICE is at the cap — "
                f"the cap COUNT is not user-scoped: {exc}"
            )
        assert created["user_sub"] == "bob-sub"
        assert await capped.store.count(bob) == 1

    async def test_tombstoned_rows_do_not_count_but_resurrect_is_capped(
        self, sut: Sut, alice: UserContext
    ) -> None:
        capped = sut.make(CappedWidgetDomain())
        await capped.store.upsert(alice, {"kind": "tool", "name": "gone"}, {})
        await capped.store.delete(alice, {"kind": "tool", "name": "gone"})
        for i in range(3):
            await capped.store.upsert(alice, {"kind": "tool", "name": f"n{i}"}, {})
        with pytest.raises(ConsoleStoreCapExceededError):
            await capped.store.upsert(alice, {"kind": "tool", "name": "gone"}, {})
