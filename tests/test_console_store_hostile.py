"""HOSTILE-subclass and hostile-domain probes for ``services/console_store``
(ADDENDUM A §3 — the test that would have caught F-A1 / D14).

Each test plays a domain author who TRIES to bypass a base invariant:
subclass a sealed store and override the guarded query builder, monkeypatch
or shadow a sealed member, declare a reserved column writable, return a
forged ``user_sub``/``trace_id`` from a hook, widen a filter, mutate a
snapshot, reach for a session. Every attempt must fail INSIDE the base —
and every test also asserts the SIDE EFFECT did not happen against
``raw_rows()`` (alice's row untouched, no forged row written), so a green
result means "the bypass was stopped", not "the test was shaped so the
bypass never ran".

Non-vacuity (each captured in the build record): neuter the base's closure
→ the corresponding test here goes RED with the cross-user side effect
observed → restore → GREEN.

What these probes do NOT claim (D14 lesson — never assert more than the
code delivers): ``object.__setattr__``, direct ``__dict__`` writes,
``type.__setattr__``, ``__closure__`` introspection and ``exec`` with a
forged in-package filename all remain possible. They are deliberate
circumvention, not the hurry-mode path the seal exists for, and they are
disclosed in the build record's surface enumeration.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest
import pytest_asyncio
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy.ext.asyncio import AsyncSession

from audittrace.db.rls import set_current_user_id
from audittrace.identity import UserContext
from audittrace.services.console_store import (
    ConsoleDomain,
    ConsoleStoreBase,
    ConsoleStoreDomainError,
    ConsoleStoreForbiddenFieldError,
    ConsoleStoreScopeError,
    ConsoleStoreSealedError,
    MockConsoleStore,
    PostgresConsoleStore,
)
from audittrace.services.console_store._base import SEALED_STORE_MEMBERS
from audittrace.services.console_store._postgres import _GuardedSessions
from tests.console_store_fixture_domain import SqliteHarness, WidgetDomain, WidgetRow

KEY_A = {"kind": "tool", "name": "web-search"}


@pytest_asyncio.fixture
async def harness() -> Any:
    h = SqliteHarness()
    await h.create()
    try:
        yield h
    finally:
        await h.dispose()


@pytest.fixture
def alice(user_context: UserContext) -> UserContext:
    return replace(user_context, user_id="alice-sub", is_admin=False)


@pytest.fixture
def bob(user_context: UserContext) -> UserContext:
    return replace(user_context, user_id="bob-sub", is_admin=False)


@pytest.fixture(autouse=True)
def _clear_context() -> Any:
    set_current_user_id(None)
    yield
    set_current_user_id(None)


# ── sealed classes ────────────────────────────────────────────────────────


class TestSealedStoreClasses:
    def test_subclassing_postgres_store_to_override_the_guarded_builder_is_refused(
        self,
    ) -> None:
        with pytest.raises(ConsoleStoreSealedError, match="refused"):

            class Evil(PostgresConsoleStore):  # type: ignore[type-arg]
                def _scoped_select(self, user_sub: str, **_: Any) -> Any:
                    from sqlalchemy import select

                    return select(self._model)  # unscoped

    def test_subclassing_mock_store_is_refused(self) -> None:
        with pytest.raises(ConsoleStoreSealedError, match="refused"):

            class Evil(MockConsoleStore):  # type: ignore[type-arg]
                pass

    def test_subclassing_the_base_outside_the_package_is_refused(self) -> None:
        with pytest.raises(ConsoleStoreSealedError, match="refused"):

            class Evil(ConsoleStoreBase):  # type: ignore[type-arg]
                pass

    def test_module_spoofing_does_not_pass_the_fence(self) -> None:
        with pytest.raises(ConsoleStoreSealedError, match="refused"):

            class Evil(MockConsoleStore):  # type: ignore[type-arg]
                __module__ = "audittrace.services.console_store._mock"

    def test_types_new_class_is_refused(self) -> None:
        with pytest.raises(ConsoleStoreSealedError, match="refused"):
            types.new_class("Evil", (MockConsoleStore,), {})

    @pytest.mark.parametrize("cls", [PostgresConsoleStore, MockConsoleStore])
    def test_monkeypatching_a_sealed_member_on_the_class_is_refused(
        self, cls: type
    ) -> None:
        name = "_snapshot"
        original = cls.__dict__[name]
        try:
            with pytest.raises(ConsoleStoreSealedError, match="sealed"):
                setattr(cls, name, lambda self, row: {})
            with pytest.raises(ConsoleStoreSealedError, match="sealed"):
                delattr(cls, name)
        finally:
            if cls.__dict__.get(name) is not original:  # pragma: no cover - restore
                type.__setattr__(cls, name, original)
        assert cls.__dict__[name] is original

    async def test_shadowing_a_sealed_member_on_an_instance_is_refused(
        self, harness: SqliteHarness, alice: UserContext, bob: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        await store.upsert(alice, KEY_A, {"payload": "secret"})
        from sqlalchemy import select

        with pytest.raises(ConsoleStoreSealedError, match="sealed"):
            store._scoped_select = lambda *a, **k: select(WidgetRow)  # type: ignore[method-assign]
        assert "_scoped_select" not in vars(store)
        assert await store.get(bob, KEY_A) is None, "bob read alice's row"

    def test_in_package_redefinition_of_a_sealed_member_is_refused(self) -> None:
        """Reaches the SECOND seal (redefinition) by deliberately using the
        disclosed residual — ``exec`` with a forged in-package filename —
        to get past the package fence. This is what an in-package edit
        (or that residual, used hostilely) would hit."""
        from audittrace.services.console_store._sealing import PACKAGE_DIR

        src = (
            "class Evil(MockConsoleStore):\n"
            "    def _scoped_rows(self, user_sub, **_):\n"
            "        return list(self._rows)\n"
        )
        code = compile(src, str(PACKAGE_DIR / "__init__.py"), "exec")
        with pytest.raises(ConsoleStoreSealedError, match="may not be overridden"):
            exec(code, {"MockConsoleStore": MockConsoleStore})  # noqa: S102

    def test_forged_filename_passes_the_fence_only_when_nothing_is_redefined(
        self, harness: SqliteHarness
    ) -> None:
        """Pins the DISCLOSED residual honestly: a forged in-package filename
        does pass the fence (this is deliberate circumvention, not a hurry
        path), and such a subclass still cannot redefine a sealed member
        (previous test) — so what it inherits is the intact guarded API."""
        from audittrace.services.console_store._sealing import PACKAGE_DIR

        code = compile(
            "class Forged(MockConsoleStore):\n    pass\n",
            str(PACKAGE_DIR / "__init__.py"),
            "exec",
        )
        namespace: dict[str, Any] = {"MockConsoleStore": MockConsoleStore}
        exec(code, namespace)  # noqa: S102
        assert issubclass(namespace["Forged"], MockConsoleStore)

    def test_non_sealed_class_attributes_are_still_settable_and_deletable(self) -> None:
        PostgresConsoleStore.scratch_marker = 1  # type: ignore[attr-defined]
        assert PostgresConsoleStore.scratch_marker == 1  # type: ignore[attr-defined]
        del PostgresConsoleStore.scratch_marker  # type: ignore[attr-defined]
        assert not hasattr(PostgresConsoleStore, "scratch_marker")

    def test_fence_fails_closed_when_no_class_statement_frame_is_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from audittrace.services.console_store import _sealing

        monkeypatch.setattr(_sealing, "_MAX_FRAME_WALK", 0)
        with pytest.raises(ConsoleStoreSealedError, match="refused"):

            class Evil(MockConsoleStore):  # type: ignore[type-arg]
                pass

    def test_sealed_members_are_marked_final_for_the_type_checker(self) -> None:
        for cls in (ConsoleStoreBase, PostgresConsoleStore, MockConsoleStore):
            for name, member in vars(cls).items():
                if name in SEALED_STORE_MEMBERS:
                    assert getattr(member, "__final__", False) is True, (
                        f"{cls.__name__}.{name} is sealed at runtime but not @final"
                    )


# ── hostile domain hooks ──────────────────────────────────────────────────


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
    """Benign at construction, then tries to filter on user_sub."""

    name = "hostile_dynamic_filter"
    armed = False

    def equality_filters(self) -> Mapping[str, Any]:
        return {"user_sub": "alice-sub"} if self.armed else {}


class DynamicCapDomain(WidgetDomain):
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


def _builders(harness: SqliteHarness) -> list[Any]:
    return [
        lambda d: MockConsoleStore(d),
        lambda d: PostgresConsoleStore(d, harness.factory),
    ]


async def _raw(
    store: ConsoleStoreBase[dict[str, Any]], harness: SqliteHarness
) -> list[dict[str, Any]]:
    if isinstance(store, MockConsoleStore):
        return [dict(r) for r in store._rows]
    return await harness.raw_rows()


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
            domain.armed = True
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
            domain.armed = True
            with pytest.raises(ConsoleStoreDomainError, match="cap"):
                await store.upsert(alice, KEY_A, {})
            assert await _raw(store, harness) == []

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
            domain.received = []
            domain.merge_current_keys = []
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


# ── no raw resource on the surface ────────────────────────────────────────


class TestNoRawResourceOnTheSurface:
    def test_store_holds_no_session_factory_or_engine_attribute(
        self, harness: SqliteHarness
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        exposed = {k for k in vars(store)} | set(dir(store))
        for needle in (
            "session_factory",
            "_session_factory",
            "engine",
            "_engine",
            "session",
        ):
            assert needle not in exposed, (
                f"{needle!r} is attribute-reachable on the store"
            )
        assert _GuardedSessions.__slots__ == ("_open",)
        assert not hasattr(store._sessions, "__dict__")

    async def test_even_the_guarded_opener_is_anchored_to_the_token(
        self, harness: SqliteHarness, alice: UserContext, bob: UserContext
    ) -> None:
        """Whoever digs out ``_sessions`` still cannot open a session for a
        sub other than the request's: the opener re-resolves the sub from
        the UserContext and cross-checks the RLS ContextVar."""
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        set_current_user_id("bob-sub")
        with pytest.raises(ConsoleStoreScopeError):
            async with store._sessions.get_session_scoped(alice):
                pass
        async with store._sessions.get_session_scoped(bob) as session:
            assert isinstance(session, AsyncSession)

    def test_domain_never_receives_the_store(self, harness: SqliteHarness) -> None:
        domain = WidgetDomain()
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            domain, harness.factory
        )
        assert vars(domain) == {}
        assert store.domain is domain

    def test_no_public_hook_exposes_a_session(self) -> None:
        import inspect

        for name in ("cap", "defaults", "merge", "equality_filters", "to_item"):
            params = inspect.signature(getattr(ConsoleDomain, name)).parameters
            for p in params.values():
                assert "session" not in p.name.lower()


# ── _domain is not swappable post-construction ──────────────────────────────


class ReservedAsValueColumnDomain(WidgetDomain):
    """A descriptor that declares a RESERVED column as writable — exactly
    what ``validate_domain()`` refuses at construction. Only reachable by
    swapping ``_domain`` in AFTER construction, since a store built with
    this domain directly is refused (``test_declaring_a_reserved_column_
    writable_is_refused_at_construction``)."""

    name = "hostile_domain_swap"
    value_columns = ("payload", "priority", "user_sub")


class TestDomainReassignmentSealed:
    """``_domain`` is not a template helper (it is not in
    ``SEALED_STORE_MEMBERS``), so it needed its OWN check: found during the
    ADDENDUM A surface enumeration (this build's pass) as a hurry-mode-reachable
    residual — plain ``store._domain = other`` is ordinary Python, no forged
    filename or closure introspection required, unlike the sealed-member
    class of residual. Closed in ``ConsoleStoreBase.__setattr__``."""

    async def test_reassigning_domain_after_construction_is_refused(
        self, harness: SqliteHarness
    ) -> None:
        domain = WidgetDomain()
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            domain, harness.factory
        )
        with pytest.raises(ConsoleStoreSealedError, match="cannot be reassigned"):
            store._domain = WidgetDomain()  # type: ignore[misc]
        assert store.domain is domain, "the reassignment attempt still took effect"

    async def test_domain_swap_cannot_smuggle_a_reserved_column_past_the_one_time_check(
        self, harness: SqliteHarness, alice: UserContext, bob: UserContext
    ) -> None:
        """The concrete exploit the previous test's guard exists to stop:
        without it, swapping in ``ReservedAsValueColumnDomain`` reaches the
        insert loop, which then sets ``row["user_sub"]`` from
        ``defaults().get("user_sub")`` (``None``, since ``defaults()`` never
        mentions it) — silently clobbering the base's own stamp AFTER it was
        set. With the guard, the swap itself is refused before any row is
        touched."""
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        await store.upsert(alice, KEY_A, {"payload": "alice-secret"})
        with pytest.raises(ConsoleStoreSealedError):
            store._domain = ReservedAsValueColumnDomain()  # type: ignore[misc]
        other = {"kind": "tool", "name": "other"}
        bob_row = await store.upsert(bob, other, {"priority": 1})
        assert bob_row["user_sub"] == "bob-sub", (
            "the blocked swap still let a row get written with a clobbered user_sub"
        )
        raw = await _raw(store, harness)
        assert {r["user_sub"] for r in raw} == {"alice-sub", "bob-sub"}, (
            "a row was written with no owner (user_sub=None) — the swap "
            "reached the insert path"
        )
