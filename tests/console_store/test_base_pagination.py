"""Pagination, domain equality filters, session-scope discipline, descriptor
validation and cursor-helper invariants for ``services/console_store``
(split from ``test_console_store_base.py``, fix round 1 A3 — that file
exceeded the PYTHON-ENGINEERING §11 500-LOC trigger).
"""

from __future__ import annotations

from typing import Any

import pytest

from audittrace.identity import UserContext
from audittrace.services.console_store import (
    ConsoleDomain,
    ConsoleStoreDomainError,
    MockConsoleStore,
    PostgresConsoleStore,
    decode_cursor,
    encode_cursor,
)
from audittrace.services.console_store._cursor import coercer_for, row_after_cursor
from tests.console_store.support import KEY_A, Sut
from tests.console_store_fixture_domain import (
    PinnedOnlyWidgetDomain,
    SqliteHarness,
    WidgetDomain,
    WidgetRow,
)


class TestPagination:
    async def test_pages_walk_every_row_once(
        self, sut: Sut, alice: UserContext
    ) -> None:
        for i in range(5):
            await sut.store.upsert(alice, {"kind": "tool", "name": f"n{i}"}, {})
        seen: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            items, cursor = await sut.store.list(alice, cursor=cursor, limit=2)
            seen += [i["name"] for i in items]
            pages += 1
            if cursor is None:
                break
        assert pages == 3 and sorted(seen) == [f"n{i}" for i in range(5)]
        assert len(seen) == len(set(seen))

    async def test_order_is_newest_first_with_key_tiebreak(
        self, sut: Sut, alice: UserContext
    ) -> None:
        for name in ("a", "b", "c"):
            await sut.store.upsert(alice, {"kind": "tool", "name": name}, {})
        items, _ = await sut.store.list(alice)
        stamps = [(i["updated_at_ms"], i["kind"], i["name"]) for i in items]
        assert stamps == sorted(stamps, reverse=True)

    @pytest.mark.parametrize(
        "cursor",
        [
            "garbage!!",
            encode_cursor([1]),
            encode_cursor([1, "tool"]),
            encode_cursor(["1", "tool", "n"]),
            encode_cursor([None, "tool", "n"]),
            encode_cursor([True, "tool", "n"]),
            encode_cursor([1, 2, "n"]),
        ],
    )
    async def test_invalid_cursor_raises_value_error(
        self, sut: Sut, alice: UserContext, cursor: str
    ) -> None:
        with pytest.raises(ValueError, match="invalid cursor"):
            await sut.store.list(alice, cursor=cursor)

    async def test_limit_is_clamped(self, sut: Sut, alice: UserContext) -> None:
        for i in range(3):
            await sut.store.upsert(alice, {"kind": "tool", "name": f"n{i}"}, {})
        items, cursor = await sut.store.list(alice, limit=0)
        assert len(items) == 1 and cursor is not None
        items, cursor = await sut.store.list(alice, limit=9999)
        assert len(items) == 3 and cursor is None


class TestEqualityFilters:
    async def test_domain_filter_narrows_every_read(
        self, sut: Sut, alice: UserContext
    ) -> None:
        pinned = sut.make(PinnedOnlyWidgetDomain())
        await pinned.store.upsert(alice, {"kind": "pinned", "name": "p"}, {})
        await pinned.store.upsert(alice, {"kind": "tool", "name": "t"}, {})
        items, _ = await pinned.store.list(alice)
        assert [i["kind"] for i in items] == ["pinned"]
        assert await pinned.store.count(alice) == 1
        assert await pinned.store.get(alice, {"kind": "tool", "name": "t"}) is None
        assert len(await pinned.raw_rows()) == 2


class TestSessionScopeDiscipline:
    async def test_store_survives_attribute_expiry_on_commit(
        self, alice: UserContext
    ) -> None:
        """With ``expire_on_commit=True`` every ORM attribute is expired after
        ``commit()``; reading one outside the session (or after commit in an
        async session) fails. The base serializes INSIDE the session and
        BEFORE commit, so it does not depend on ``expire_on_commit=False``."""
        harness = SqliteHarness(expire_on_commit=True)
        await harness.create()
        try:
            store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
                WidgetDomain(), harness.factory
            )
            created = await store.upsert(alice, KEY_A, {"payload": "x"})
            assert created["payload"] == "x"
            updated = await store.upsert(alice, KEY_A, {"priority": 4})
            assert updated["priority"] == 4
            assert (await store.get(alice, KEY_A)) == updated
            assert (await store.list(alice))[0] == [updated]
            assert await store.delete(alice, KEY_A) is True
        finally:
            await harness.dispose()

    async def test_upsert_failure_rolls_back_and_wraps(
        self, alice: UserContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = SqliteHarness()
        await harness.create()
        try:
            store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
                WidgetDomain(), harness.factory
            )

            async def _boom(*_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("boom")

            monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncSession.commit", _boom)
            with pytest.raises(
                RuntimeError, match=r"console_store_test_widgets\.upsert\(.*failed"
            ):
                await store.upsert(alice, KEY_A, {})
            monkeypatch.undo()
            assert await harness.raw_rows() == []
        finally:
            await harness.dispose()


def _domain(**overrides: Any) -> ConsoleDomain[dict[str, Any]]:
    attrs: dict[str, Any] = {
        "name": "d",
        "model": WidgetRow,
        "key_columns": ("kind", "name"),
        "value_columns": ("payload", "priority"),
        "order_by": (("created_at_ms", "asc"), ("id", "asc")),
        "to_item": lambda self, row: dict(row),
    }
    attrs.update(overrides)
    cls = type("D", (ConsoleDomain,), attrs)
    return cls()  # type: ignore[no-any-return]


class TestDomainValidation:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"name": ""},
            {"model": "not-a-class"},
            {"key_columns": ()},
            {"key_columns": ("kind", "")},
            {"key_columns": ("kind", "user_sub")},
            {"value_columns": ("payload", "trace_id")},
            {"value_columns": ("kind",)},
            {"key_columns": ("kind", "kind")},
            {"value_columns": ("payload", "payload")},
            {"value_columns": ("nope",)},
            {"order_by": ()},
            {"order_by": ("created_at_ms",)},
            {"order_by": (("payload", "asc"), ("id", "asc"))},
            {"order_by": (("id", "sideways"),)},
            {"order_by": (("created_at_ms", "asc"),)},
            {"default_list_limit": 0},
            {"default_list_limit": "25"},
            {"max_list_limit": 10, "default_list_limit": 25},
            {"cap": lambda self: 0},
            {"cap": lambda self: True},
            {"cap": lambda self: "3"},
            {"equality_filters": lambda self: {"user_sub": "x"}},
            {"equality_filters": lambda self: ["kind"]},
        ],
    )
    def test_invalid_descriptor_is_refused_at_construction(
        self, overrides: dict[str, Any]
    ) -> None:
        with pytest.raises(ConsoleStoreDomainError):
            MockConsoleStore(_domain(**overrides))

    async def test_default_hooks_apply_when_a_domain_overrides_none(
        self, alice: UserContext
    ) -> None:
        store: MockConsoleStore[dict[str, Any]] = MockConsoleStore(_domain())
        created = await store.upsert(alice, KEY_A, {"payload": "p"})
        assert created["payload"] == "p" and created["priority"] is None
        assert await store.count(alice) == 1

    def test_non_domain_is_refused(self) -> None:
        with pytest.raises(ConsoleStoreDomainError):
            MockConsoleStore(object())  # type: ignore[arg-type]

    def test_unsupported_ordering_type_is_refused(self) -> None:
        class Odd:
            id = WidgetRow.id
            user_sub = WidgetRow.user_sub
            created_at_ms = WidgetRow.created_at_ms
            updated_at_ms = WidgetRow.updated_at_ms
            deleted_at_ms = WidgetRow.deleted_at_ms
            trace_id = WidgetRow.trace_id
            kind = WidgetRow.kind
            name = WidgetRow.name
            payload = WidgetRow.payload
            priority = WidgetRow.priority
            when = object()

        with pytest.raises(ConsoleStoreDomainError, match="unsupported type"):
            MockConsoleStore(
                _domain(
                    model=Odd, key_columns=("kind", "when"), order_by=(("when", "asc"),)
                )
            )


class TestCursorHelpers:
    def test_round_trip(self) -> None:
        values = [1700000000000, "tool", "web-search"]
        assert decode_cursor(encode_cursor(values), [int, str, str]) == values

    def test_row_after_cursor_honours_directions(self) -> None:
        assert row_after_cursor([5, "b"], [5, "a"], ["desc", "desc"]) is False
        assert row_after_cursor([5, "a"], [5, "b"], ["desc", "desc"]) is True
        assert row_after_cursor([4, "z"], [5, "a"], ["desc", "asc"]) is True
        assert row_after_cursor([5, "a"], [5, "a"], ["asc", "asc"]) is False
        assert row_after_cursor([6], [5], ["asc"]) is True

    def test_coercer_for_rejects_other_types(self) -> None:
        with pytest.raises(TypeError):
            coercer_for(float)
