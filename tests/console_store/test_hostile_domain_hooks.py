"""Hostile-domain-hook probes for ``services/console_store`` (split from
``test_console_store_hostile.py``, fix round 1 A3 — that file exceeded the
PYTHON-ENGINEERING §11 500-LOC trigger).

Each test plays a domain author who TRIES to bypass a base invariant:
return a forged ``user_sub``/``trace_id`` from a hook, widen a filter,
mutate a snapshot, declare a reserved column writable. Every attempt must
fail INSIDE the base — and every test also asserts the SIDE EFFECT did not
happen against ``raw_rows()`` (alice's row untouched, no forged row
written), so a green result means "the bypass was stopped", not "the test
was shaped so the bypass never ran".

Non-vacuity (each captured in the build record): neuter the base's closure
→ the corresponding test here goes RED with the cross-user side effect
observed → restore → GREEN.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy.ext.asyncio import AsyncSession

from audittrace.identity import UserContext
from audittrace.services.console_store import (
    ConsoleStoreBase,
    ConsoleStoreDomainError,
    ConsoleStoreForbiddenFieldError,
    ConsoleStoreSealedError,
)
from tests.console_store.support import KEY_A, _builders, _raw
from tests.console_store_fixture_domain import SqliteHarness, WidgetDomain, WidgetRow


class ForgingMergeDomain(WidgetDomain):
    """A merge() that tries to move the row to another user."""

    name = "hostile_merge"

    def merge(
        self, current: Mapping[str, Any], patch: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return {**current, **patch, "user_sub": "alice-sub"}


class ForgingDefaultsDomain(WidgetDomain):
    """A defaults() that tries to insert under another user with no trace."""

    name = "hostile_defaults"

    def defaults(self, key: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "payload": None,
            "priority": 0,
            "user_sub": "alice-sub",
            "trace_id": None,
        }


class BlankTraceMergeDomain(WidgetDomain):
    name = "hostile_blank_trace"

    def merge(
        self, current: Mapping[str, Any], patch: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return {**current, **patch, "trace_id": None, "session_id": None}


class UnknownColumnMergeDomain(WidgetDomain):
    name = "hostile_unknown_merge"

    def merge(
        self, current: Mapping[str, Any], patch: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return {**current, **patch, "colour": "red"}


class NonMappingMergeDomain(WidgetDomain):
    name = "hostile_non_mapping_merge"

    def merge(
        self, current: Mapping[str, Any], patch: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return [("payload", "x")]  # type: ignore[return-value]


class DynamicFilterDomain(WidgetDomain):
    """Benign at construction, then tries to filter on user_sub.

    ``armed`` is flipped at the CLASS level (``DynamicFilterDomain.armed =
    True``), never on the instance: F1 seals every ``ConsoleDomain``
    instance against attribute assignment, so ``domain.armed = True`` would
    itself raise. The hook still reads ``self.armed`` per call, so a
    class-level flip is observed on every existing instance exactly like an
    instance-level one would have been — this test is about the base
    re-validating ``equality_filters()`` on EVERY call, not about how a
    domain author stores the flag.
    """

    name = "hostile_dynamic_filter"
    armed = False

    def equality_filters(self) -> Mapping[str, Any]:
        return {"user_sub": "alice-sub"} if self.armed else {}


class DynamicCapDomain(WidgetDomain):
    """See :class:`DynamicFilterDomain` — ``armed`` is flipped at the class
    level for the same reason (F1 seals instance attribute assignment)."""

    name = "hostile_dynamic_cap"
    armed = False

    def cap(self) -> int | None:
        return -1 if self.armed else None  # type: ignore[return-value]


class MutatingToItemDomain(WidgetDomain):
    name = "hostile_mutating_to_item"

    def to_item(self, row: Mapping[str, Any]) -> dict[str, Any]:
        assert isinstance(row, dict)
        row["payload"] = "MUTATED"
        row["user_sub"] = "bob-sub"
        return dict(row)


class RecordingDomain(WidgetDomain):
    """Records the TYPE (and key set) of everything the base hands its hooks."""

    name = "recording"
    received: list[tuple[str, type]] = []
    merge_current_keys: list[set[str]] = []

    def defaults(self, key: Mapping[str, Any]) -> Mapping[str, Any]:
        self.received.append(("defaults.key", type(key)))
        return {"payload": None, "priority": 0}

    def merge(
        self, current: Mapping[str, Any], patch: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.received.append(("merge.current", type(current)))
        self.received.append(("merge.patch", type(patch)))
        self.merge_current_keys.append(set(current))
        return {**current, **patch}

    def to_item(self, row: Mapping[str, Any]) -> dict[str, Any]:
        self.received.append(("to_item.row", type(row)))
        return dict(row)


class KeyMutatingDefaultsDomain(WidgetDomain):
    """A defaults() that tries to rewrite the key it was shown."""

    name = "hostile_key_mutating_defaults"

    def defaults(self, key: Mapping[str, Any]) -> Mapping[str, Any]:
        assert isinstance(key, dict)
        key["name"] = "hijacked"
        return {"payload": None, "priority": 0}


class TestHostileDomainHooks:
    async def test_merge_cannot_move_a_row_to_another_user(
        self, harness: SqliteHarness, alice: UserContext, bob: UserContext
    ) -> None:
        for build in _builders(harness):
            store = build(ForgingMergeDomain())
            await store.upsert(alice, KEY_A, {"payload": "alice"})
            await store.upsert(
                bob, KEY_A, {"payload": "bob"}
            )  # insert: merge not called
            with pytest.raises(ConsoleStoreForbiddenFieldError, match="merge"):
                await store.upsert(bob, KEY_A, {"priority": 1})  # update: merge fires
            raw = sorted(await _raw(store, harness), key=lambda r: r["user_sub"])
            assert [(r["user_sub"], r["payload"], r["priority"]) for r in raw] == [
                ("alice-sub", "alice", 0),
                ("bob-sub", "bob", 0),
            ], "the forged merge reached a row"

    async def test_defaults_cannot_insert_under_another_user(
        self, harness: SqliteHarness, alice: UserContext, bob: UserContext
    ) -> None:
        for build in _builders(harness):
            store = build(ForgingDefaultsDomain())
            with pytest.raises(ConsoleStoreForbiddenFieldError, match="defaults"):
                await store.upsert(bob, KEY_A, {})
            assert await _raw(store, harness) == [], "the forged insert was written"

    async def test_merge_cannot_blank_the_trace_and_session_stamps(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        tracer = TracerProvider().get_tracer("hostile")
        for build in _builders(harness):
            store = build(BlankTraceMergeDomain())
            with tracer.start_as_current_span("w") as span:
                await store.upsert(alice, KEY_A, {})
                with pytest.raises(ConsoleStoreForbiddenFieldError, match="merge"):
                    await store.upsert(alice, KEY_A, {"priority": 1})
                expected = format(span.get_span_context().trace_id, "032x")
            raw = await _raw(store, harness)
            assert raw[0]["trace_id"] == expected and raw[0]["priority"] == 0

    async def test_hook_naming_an_unknown_or_non_mapping_output_is_refused(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        for build in _builders(harness):
            store = build(UnknownColumnMergeDomain())
            await store.upsert(alice, KEY_A, {})
            with pytest.raises(ConsoleStoreDomainError, match="unknown column"):
                await store.upsert(alice, KEY_A, {"priority": 1})
            store = build(NonMappingMergeDomain())
            other = {"kind": "tool", "name": "other"}
            await store.upsert(alice, other, {})
            with pytest.raises(ConsoleStoreDomainError, match="must return a mapping"):
                await store.upsert(alice, other, {"priority": 1})

    @pytest.mark.parametrize(
        "overrides",
        [
            {"value_columns": ("payload", "user_sub")},
            {"key_columns": ("kind", "trace_id")},
            {"value_columns": ("payload", "deleted_at_ms")},
        ],
    )
    def test_declaring_a_reserved_column_writable_is_refused_at_construction(
        self, harness: SqliteHarness, overrides: dict[str, Any]
    ) -> None:
        domain_cls = type(
            "Hostile", (WidgetDomain,), {"name": "hostile_decl", **overrides}
        )
        for build in _builders(harness):
            with pytest.raises(ConsoleStoreDomainError, match="reserved"):
                build(domain_cls())

    def test_static_filter_on_user_sub_is_refused_at_construction(
        self, harness: SqliteHarness
    ) -> None:
        domain_cls = type(
            "Hostile",
            (WidgetDomain,),
            {
                "name": "hostile_static",
                "equality_filters": lambda self: {"user_sub": "x"},
            },
        )
        for build in _builders(harness):
            with pytest.raises(ConsoleStoreDomainError, match="may not name"):
                build(domain_cls())

    async def test_dynamic_filter_on_user_sub_is_refused_at_query_time(
        self, harness: SqliteHarness, alice: UserContext, bob: UserContext
    ) -> None:
        for build in _builders(harness):
            domain = DynamicFilterDomain()
            store = build(domain)
            await store.upsert(alice, KEY_A, {"payload": "alice"})
            type(domain).armed = True
            try:
                for call in (
                    lambda: store.get(bob, KEY_A),
                    lambda: store.list(bob),
                    lambda: store.count(bob),
                    lambda: store.upsert(bob, KEY_A, {}),
                    lambda: store.delete(bob, KEY_A),
                ):
                    with pytest.raises(ConsoleStoreDomainError, match="may not name"):
                        await call()
                raw = await _raw(store, harness)
                assert [(r["user_sub"], r["payload"]) for r in raw] == [
                    ("alice-sub", "alice")
                ]
            finally:
                # armed is a CLASS attribute (F1 seals instance assignment):
                # reset it so it does not leak into the next backend's
                # iteration or the next test.
                type(domain).armed = False

    async def test_a_domain_filter_can_only_narrow_never_widen(
        self, harness: SqliteHarness, alice: UserContext, bob: UserContext
    ) -> None:
        widen_cls = type(
            "Widen",
            (WidgetDomain,),
            {
                "name": "hostile_widen",
                "equality_filters": lambda self: {"kind": "tool"},
            },
        )
        for build in _builders(harness):
            store = build(widen_cls())
            await store.upsert(alice, KEY_A, {})
            assert (await store.list(bob))[0] == []
            assert await store.get(bob, KEY_A) is None
            assert await store.count(bob) == 0

    async def test_dynamic_cap_garbage_is_refused_without_writing(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        for build in _builders(harness):
            domain = DynamicCapDomain()
            store = build(domain)
            type(domain).armed = True
            try:
                with pytest.raises(ConsoleStoreDomainError, match="cap"):
                    await store.upsert(alice, KEY_A, {})
                assert await _raw(store, harness) == []
            finally:
                # armed is a CLASS attribute (F1 seals instance assignment):
                # reset it so it does not leak into the next backend's
                # iteration or the next test.
                type(domain).armed = False

    async def test_to_item_mutating_its_snapshot_cannot_touch_store_state(
        self, harness: SqliteHarness, alice: UserContext, bob: UserContext
    ) -> None:
        for build in _builders(harness):
            store = build(MutatingToItemDomain())
            await store.upsert(alice, KEY_A, {"payload": "real"})
            item = await store.get(alice, KEY_A)
            assert item is not None and item["payload"] == "MUTATED"
            raw = await _raw(store, harness)
            assert [(r["user_sub"], r["payload"]) for r in raw] == [
                ("alice-sub", "real")
            ]
            assert await store.get(bob, KEY_A) is None

    async def test_hooks_only_ever_receive_plain_dicts(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        for build in _builders(harness):
            domain = RecordingDomain()
            # Reset the SHARED class-level lists in place (F1 seals every
            # ConsoleDomain instance against attribute assignment, so
            # `domain.received = []` would itself raise).
            RecordingDomain.received.clear()
            RecordingDomain.merge_current_keys.clear()
            store = build(domain)
            await store.upsert(alice, KEY_A, {})
            await store.upsert(alice, KEY_A, {"priority": 1})
            await store.get(alice, KEY_A)
            await store.list(alice)
            assert domain.received, "hooks never ran"
            for label, kind in domain.received:
                assert kind is dict, (
                    f"{label} received {kind.__name__}, not a plain dict"
                )
                assert not issubclass(kind, (AsyncSession, WidgetRow, ConsoleStoreBase))
            assert domain.merge_current_keys == [{"payload", "priority"}], (
                "merge() was shown reserved/key columns, not only the value columns"
            )

    async def test_defaults_mutating_its_key_view_cannot_change_the_key(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        for build in _builders(harness):
            store = build(KeyMutatingDefaultsDomain())
            created = await store.upsert(alice, KEY_A, {})
            assert created["name"] == "web-search"
            raw = await _raw(store, harness)
            assert [r["name"] for r in raw] == ["web-search"], (
                "the hook rewrote the key"
            )

    def test_model_without_reserved_columns_is_refused(
        self, harness: SqliteHarness
    ) -> None:
        class NoUserSub:
            id = WidgetRow.id
            kind = WidgetRow.kind
            name = WidgetRow.name
            payload = WidgetRow.payload
            priority = WidgetRow.priority
            created_at_ms = WidgetRow.created_at_ms
            updated_at_ms = WidgetRow.updated_at_ms
            deleted_at_ms = WidgetRow.deleted_at_ms
            trace_id = WidgetRow.trace_id

        domain_cls = type(
            "Hostile", (WidgetDomain,), {"name": "hostile_model", "model": NoUserSub}
        )
        for build in _builders(harness):
            with pytest.raises(ConsoleStoreDomainError, match="lacks column"):
                build(domain_cls())

    def test_overriding_a_sealed_domain_template_member_is_refused(self) -> None:
        with pytest.raises(ConsoleStoreSealedError, match="sealed member"):

            class Hostile(WidgetDomain):
                def snapshot_columns(self) -> tuple[str, ...]:
                    return ("id",)

    def test_domain_hierarchy_is_open_but_the_seal_is_inherited(self) -> None:
        class Fine(WidgetDomain):
            name = "fine"

        with pytest.raises(ConsoleStoreSealedError):

            class NotFine(Fine):
                def has_session_id(self) -> bool:
                    return False
