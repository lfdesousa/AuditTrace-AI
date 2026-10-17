"""SPEC ADDENDUM D (fix round 3) / F7 (fix round 4, 2026-09-17) — the FOUR
remaining ``DomainContract`` fields the round-3 reviewer found unproven:
``snapshot_columns``, ``order_columns``, ``order_directions`` and
``max_list_limit``. ``has_session_id`` (the fifth F7 field) is proven in
``test_toctou_declarative_surfaces_sealed.py``; ``value_columns`` /
``key_columns`` / ``model`` / ``order_by`` (the round-3 siblings) are
proven in ``test_toctou_value_columns_sealed.py`` /
``test_toctou_declarative_surfaces_sealed.py``.

These four are DERIVED fields (computed once by
:func:`~audittrace.services.console_store._domain_validate.validate_domain`
from ``key_columns`` / ``value_columns`` / ``order_by`` / ``max_list_limit``
and cached on :class:`DomainContract`), consumed directly by
``_postgres.py`` / ``_mock.py`` / ``_base.py`` — never recomputed by
calling a domain's SEALED template methods again. Each test below builds
the SAME per-field proof the independent reviewer's out-of-repo pytest
plugin used (a proxy that serves ONE field live and everything else
cached) as an IN-TREE fixture: a hostile domain whose underlying
declarative surface is safe on validation's ONE read and hostile on every
read after, plus a byte-for-byte copy of the ONE consuming method with
ONLY the field under test switched from ``self._contract.X`` (cached) to
a live read — restored in ``finally`` regardless of outcome, one field
per run (Addendum B Req 1), mirroring
``test_toctou_value_columns_sealed.py``'s
``_old_insert_values_reading_live_domain`` pattern.
"""

from __future__ import annotations

from typing import Any

import pytest

from audittrace.identity import UserContext
from audittrace.services.console_store import PostgresConsoleStore
from audittrace.services.console_store import _base as _base_module
from audittrace.services.console_store import _postgres as _postgres_module
from audittrace.services.console_store._cursor import keyset_predicate
from tests.console_store.support import KEY_A
from tests.console_store_fixture_domain import SqliteHarness, WidgetDomain


def _instance_counter(obj: Any) -> int:
    return int(object.__getattribute__(obj, "_n"))


def _bump_instance_counter(obj: Any) -> int:
    n = _instance_counter(obj)
    object.__setattr__(obj, "_n", n + 1)
    return n


# ── snapshot_columns ─────────────────────────────────────────────────────


class TocTouSnapshotColumnsDomain(WidgetDomain):
    """Safe ``("payload", "priority")`` on the ONE ``validate_domain()``
    read; hostile ``("payload",)`` (drops ``priority``) on every read
    after — ``snapshot_columns()`` (a sealed, @final template method) is
    DERIVED from ``value_columns``, so this is the same TOCTOU shape as
    ``test_toctou_value_columns_sealed.py``'s ``TocTouPropertyDomain``,
    aimed at a different downstream consumer."""

    name = "toctou_snapshot_columns_widget"

    def __init__(self) -> None:
        object.__setattr__(self, "_n", 0)

    @property
    def value_columns(self) -> tuple[str, ...]:  # type: ignore[override]
        n = _bump_instance_counter(self)
        return ("payload", "priority") if n < 1 else ("payload",)


def _snapshot_reading_domain_live(self: Any, row: Any) -> dict[str, Any]:
    """``PostgresConsoleStore._snapshot`` with ``snapshot_columns`` read
    LIVE off ``self._domain`` instead of the cached
    ``self._contract.snapshot_columns`` — isolates this ONE field."""
    return {column: getattr(row, column) for column in self._domain.snapshot_columns()}


class TestSnapshotColumnsNonVacuous:
    async def test_the_fixed_code_keeps_priority_on_every_read(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouSnapshotColumnsDomain(), harness.factory
        )
        assert store.domain.value_columns == ("payload",), (
            "the fixture must actually be hostile post-construction"
        )
        await store.upsert(alice, KEY_A, {"payload": "p", "priority": 7})
        item = await store.get(alice, KEY_A)
        assert item is not None
        assert item["priority"] == 7, (
            "a live re-read of snapshot_columns (derived from a hostile "
            "value_columns) would have dropped priority from the read path"
        )

    async def test_neutering_the_cached_snapshot_columns_drops_priority_silently(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        original = _postgres_module.PostgresConsoleStore.__dict__["_snapshot"]
        type.__setattr__(
            _postgres_module.PostgresConsoleStore,
            "_snapshot",
            _snapshot_reading_domain_live,
        )
        try:
            hostile_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
                TocTouSnapshotColumnsDomain(), harness.factory
            )
            await hostile_store.upsert(alice, KEY_A, {"payload": "p", "priority": 7})
            item = await hostile_store.get(alice, KEY_A)
            assert item is not None
            assert "priority" not in item, (
                "neutering the cached snapshot_columns should have let the "
                "hostile value_columns drop priority from the read path"
            )
        finally:
            type.__setattr__(
                _postgres_module.PostgresConsoleStore, "_snapshot", original
            )
        assert _postgres_module.PostgresConsoleStore.__dict__["_snapshot"] is original

        # Restored: a FRESH store (fresh counter) proves the fix is back.
        fixed_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouSnapshotColumnsDomain(), harness.factory
        )
        await fixed_store.upsert(alice, KEY_A, {"payload": "p2", "priority": 9})
        item2 = await fixed_store.get(alice, KEY_A)
        assert item2 is not None
        assert item2["priority"] == 9


# ── order_columns / order_directions (both derived from order_by) ──────────


class TocTouOrderByLengthDomain(WidgetDomain):
    """Safe 3-column ``order_by`` on the ONE ``validate_domain()`` read
    (matching ``WidgetDomain``'s own ordering, so construction's
    total-order check passes); hostile SINGLE-column ``order_by`` on every
    read after. Shared by both the ``order_columns`` and
    ``order_directions`` tests below — each test's patched method reads
    only the ONE field named in its own test class live, leaving the
    OTHER cached, so a length mismatch between the two can only be
    attributed to the field actually under neuter."""

    name = "toctou_order_by_length_widget"

    def __init__(self) -> None:
        object.__setattr__(self, "_n", 0)

    @property
    def order_by(self) -> tuple[tuple[str, str], ...]:  # type: ignore[override]
        n = _bump_instance_counter(self)
        if n < 1:
            return (("updated_at_ms", "desc"), ("kind", "desc"), ("name", "desc"))
        return (("updated_at_ms", "desc"),)


async def _list_reading_order_columns_live(
    self: Any,
    user_context: UserContext,
    *,
    cursor: str | None = None,
    limit: int | None = None,
) -> tuple[list[Any], str | None]:
    """``PostgresConsoleStore.list`` with ONLY ``order_columns`` read live
    off ``self._domain`` — ``order_directions`` stays cached."""
    user_sub = self._read_sub(user_context)
    effective_limit = self._clamp_limit(limit)
    cursor_values = self._cursor_values(cursor)
    stmt = self._scoped_select(user_sub)
    if cursor_values is not None:
        columns = [
            getattr(self._contract.model, c) for c in self._domain.order_columns()
        ]
        stmt = stmt.where(
            keyset_predicate(columns, self._contract.order_directions, cursor_values)
        )
    stmt = self._ordered(stmt).limit(effective_limit + 1)
    async with self._sessions.get_session_scoped(user_context) as session:
        rows = (await session.execute(stmt)).scalars().all()
        snapshots = [self._snapshot(row) for row in rows]
    return self._page(snapshots, effective_limit)


async def _list_reading_order_directions_live(
    self: Any,
    user_context: UserContext,
    *,
    cursor: str | None = None,
    limit: int | None = None,
) -> tuple[list[Any], str | None]:
    """The sibling of the above: ``order_columns`` stays cached, ONLY
    ``order_directions`` is read live off ``self._domain``."""
    user_sub = self._read_sub(user_context)
    effective_limit = self._clamp_limit(limit)
    cursor_values = self._cursor_values(cursor)
    stmt = self._scoped_select(user_sub)
    if cursor_values is not None:
        columns = [
            getattr(self._contract.model, c) for c in self._contract.order_columns
        ]
        stmt = stmt.where(
            keyset_predicate(columns, self._domain.order_directions(), cursor_values)
        )
    stmt = self._ordered(stmt).limit(effective_limit + 1)
    async with self._sessions.get_session_scoped(user_context) as session:
        rows = (await session.execute(stmt)).scalars().all()
        snapshots = [self._snapshot(row) for row in rows]
    return self._page(snapshots, effective_limit)


class TestOrderColumnsNonVacuous:
    async def test_the_fixed_code_pages_correctly_on_every_read(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouOrderByLengthDomain(), harness.factory
        )
        assert store.domain.order_by == (("updated_at_ms", "desc"),), (
            "the fixture must actually be hostile post-construction"
        )
        await store.upsert(alice, {"kind": "tool", "name": "a"}, {"payload": "1"})
        await store.upsert(alice, {"kind": "tool", "name": "b"}, {"payload": "2"})
        _, cursor = await store.list(alice, limit=1)
        assert cursor is not None
        items2, _ = await store.list(alice, cursor=cursor, limit=1)
        assert [item["name"] for item in items2] == ["a"], (
            "a live re-read of order_columns would have raised pagination "
            "instead of returning the next page"
        )

    async def test_neutering_the_cached_order_columns_breaks_pagination(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        original = _postgres_module.PostgresConsoleStore.__dict__["list"]
        type.__setattr__(
            _postgres_module.PostgresConsoleStore,
            "list",
            _list_reading_order_columns_live,
        )
        try:
            hostile_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
                TocTouOrderByLengthDomain(), harness.factory
            )
            await hostile_store.upsert(
                alice, {"kind": "tool", "name": "a"}, {"payload": "1"}
            )
            await hostile_store.upsert(
                alice, {"kind": "tool", "name": "b"}, {"payload": "2"}
            )
            _, cursor = await hostile_store.list(alice, limit=1)
            assert cursor is not None
            with pytest.raises(ValueError, match="zip"):
                await hostile_store.list(alice, cursor=cursor, limit=1)
        finally:
            type.__setattr__(_postgres_module.PostgresConsoleStore, "list", original)
        assert _postgres_module.PostgresConsoleStore.__dict__["list"] is original

        fixed_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouOrderByLengthDomain(), harness.factory
        )
        await fixed_store.upsert(alice, {"kind": "tool", "name": "a"}, {"payload": "1"})
        await fixed_store.upsert(alice, {"kind": "tool", "name": "b"}, {"payload": "2"})
        _, cursor2 = await fixed_store.list(alice, limit=1)
        assert cursor2 is not None
        items2, _ = await fixed_store.list(alice, cursor=cursor2, limit=1)
        assert [item["name"] for item in items2] == ["a"]


class TestOrderDirectionsNonVacuous:
    async def test_the_fixed_code_pages_correctly_on_every_read(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouOrderByLengthDomain(), harness.factory
        )
        assert store.domain.order_by == (("updated_at_ms", "desc"),), (
            "the fixture must actually be hostile post-construction"
        )
        await store.upsert(alice, {"kind": "tool", "name": "a"}, {"payload": "1"})
        await store.upsert(alice, {"kind": "tool", "name": "b"}, {"payload": "2"})
        _, cursor = await store.list(alice, limit=1)
        assert cursor is not None
        items2, _ = await store.list(alice, cursor=cursor, limit=1)
        assert [item["name"] for item in items2] == ["a"], (
            "a live re-read of order_directions would have raised "
            "pagination instead of returning the next page"
        )

    async def test_neutering_the_cached_order_directions_breaks_pagination(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        original = _postgres_module.PostgresConsoleStore.__dict__["list"]
        type.__setattr__(
            _postgres_module.PostgresConsoleStore,
            "list",
            _list_reading_order_directions_live,
        )
        try:
            hostile_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
                TocTouOrderByLengthDomain(), harness.factory
            )
            await hostile_store.upsert(
                alice, {"kind": "tool", "name": "a"}, {"payload": "1"}
            )
            await hostile_store.upsert(
                alice, {"kind": "tool", "name": "b"}, {"payload": "2"}
            )
            _, cursor = await hostile_store.list(alice, limit=1)
            assert cursor is not None
            with pytest.raises(ValueError, match="zip"):
                await hostile_store.list(alice, cursor=cursor, limit=1)
        finally:
            type.__setattr__(_postgres_module.PostgresConsoleStore, "list", original)
        assert _postgres_module.PostgresConsoleStore.__dict__["list"] is original

        fixed_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouOrderByLengthDomain(), harness.factory
        )
        await fixed_store.upsert(alice, {"kind": "tool", "name": "a"}, {"payload": "1"})
        await fixed_store.upsert(alice, {"kind": "tool", "name": "b"}, {"payload": "2"})
        _, cursor2 = await fixed_store.list(alice, limit=1)
        assert cursor2 is not None
        items2, _ = await fixed_store.list(alice, cursor=cursor2, limit=1)
        assert [item["name"] for item in items2] == ["a"]


# ── max_list_limit ───────────────────────────────────────────────────────


class TocTouMaxListLimitDomain(WidgetDomain):
    """Safe ``50`` (matching ``WidgetDomain``) on the ONE
    ``validate_domain()`` read; hostile ``99999`` (effectively unbounded)
    on every read after."""

    name = "toctou_max_list_limit_widget"

    def __init__(self) -> None:
        object.__setattr__(self, "_n", 0)

    @property
    def max_list_limit(self) -> int:  # type: ignore[override]
        n = _bump_instance_counter(self)
        return 50 if n < 1 else 99999


def _clamp_limit_reading_domain_live(self: Any, limit: int | None) -> int:
    """``ConsoleStoreBase._clamp_limit`` with ``max_list_limit`` read live
    off ``self._domain`` instead of the cached
    ``self._contract.max_list_limit``."""
    if limit is None:
        return self._contract.default_list_limit
    return max(1, min(int(limit), self._domain.max_list_limit))


class TestMaxListLimitNonVacuous:
    async def test_the_fixed_code_clamps_on_every_call(
        self, harness: SqliteHarness
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouMaxListLimitDomain(), harness.factory
        )
        assert store.domain.max_list_limit == 99999, (
            "the fixture must actually be hostile post-construction"
        )
        assert store._clamp_limit(99999) == 50, (
            "a live re-read of max_list_limit would have let an unbounded "
            "page size through uncapped"
        )

    async def test_neutering_the_cached_max_list_limit_uncaps_the_page_size(
        self, harness: SqliteHarness
    ) -> None:
        original = _base_module.ConsoleStoreBase.__dict__["_clamp_limit"]
        type.__setattr__(
            _base_module.ConsoleStoreBase,
            "_clamp_limit",
            _clamp_limit_reading_domain_live,
        )
        try:
            hostile_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
                TocTouMaxListLimitDomain(), harness.factory
            )
            assert hostile_store._clamp_limit(99999) == 99999, (
                "neutering the cached max_list_limit should have let an "
                "unbounded page size through"
            )
        finally:
            type.__setattr__(_base_module.ConsoleStoreBase, "_clamp_limit", original)
        assert _base_module.ConsoleStoreBase.__dict__["_clamp_limit"] is original

        fixed_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouMaxListLimitDomain(), harness.factory
        )
        assert fixed_store._clamp_limit(99999) == 50
