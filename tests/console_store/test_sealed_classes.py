"""Class-level and instance-level sealing probes for ``services/console_store``
(split from ``test_console_store_hostile.py``, fix round 1 A3 — that file
exceeded the PYTHON-ENGINEERING §11 500-LOC trigger).

Every test plays a maintainer who tries to subclass a sealed store class,
monkeypatch or shadow a sealed member, or spoof the package fence. Every
attempt must fail INSIDE the base.

What these probes do NOT claim (D14 lesson — never assert more than the
code delivers): ``object.__setattr__``, direct ``__dict__`` writes,
``type.__setattr__``, ``__closure__`` introspection and ``exec`` with a
forged in-package filename all remain possible. They are deliberate
circumvention, not the hurry-mode path the seal exists for, and they are
disclosed in the build record's surface enumeration.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from audittrace.identity import UserContext
from audittrace.services.console_store import (
    ConsoleStoreBase,
    ConsoleStoreSealedError,
    MockConsoleStore,
    PostgresConsoleStore,
)
from audittrace.services.console_store._base import SEALED_STORE_MEMBERS
from tests.console_store.support import KEY_A
from tests.console_store_fixture_domain import SqliteHarness, WidgetDomain, WidgetRow


class TestSealedStoreClasses:
    def test_subclassing_postgres_store_to_override_the_guarded_builder_is_refused(
        self,
    ) -> None:
        with pytest.raises(ConsoleStoreSealedError, match="refused"):

            class Evil(PostgresConsoleStore):  # type: ignore[type-arg]
                def _scoped_select(self, user_sub: str, **_: Any) -> Any:
                    from sqlalchemy import select

                    return select(self._domain.model)  # unscoped

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

    def test_shadowing_the_new_reserved_column_guard_on_an_instance_is_refused(
        self, harness: SqliteHarness
    ) -> None:
        """``_refuse_reserved_value_columns`` (F1 item 2, defence in depth)
        joined ``SEALED_STORE_MEMBERS`` in this round — proves it got the
        SAME instance-shadow protection as every other sealed template
        helper, not a bespoke, weaker one."""
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        with pytest.raises(ConsoleStoreSealedError, match="sealed"):
            store._refuse_reserved_value_columns = lambda: None  # type: ignore[method-assign]
        assert "_refuse_reserved_value_columns" not in vars(store)

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
