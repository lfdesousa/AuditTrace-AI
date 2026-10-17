"""Tests for the console-presets service (Mongo-repl WU-presets,
MongoDB-elimination EPIC).

Mirrors ``test_console_conversations_service.py``'s structure EXACTLY
(the spec's instruction), scaled down to a single table (no message
tree): Mock-service interface tests, then a Postgres-backed suite via
``InMemoryPostgresFactory`` (aiosqlite, no real PostgreSQL required). The
cross-user isolation tests bracket their assertions with
``set_current_user_id`` per ``feedback_unit_tests_miss_rls`` — RLS itself
is a no-op on SQLite, so the guard under test is the service's own
explicit ``.filter(user_sub == ...)`` clause, not the (here inert)
Postgres GUC.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import pytest_asyncio

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.db.rls import set_current_user_id
from audittrace.services.console_presets import (
    ConsolePresetsService,
    MockConsolePresetsService,
    PostgresConsolePresetsService,
    _decode_cursor,
    _encode_cursor,
)

# ── cursor helpers ───────────────────────────────────────────────────────


class TestCursorCodec:
    def test_round_trips(self) -> None:
        cursor = _encode_cursor(updated_at_ms=12345, preset_id="preset-a")
        assert _decode_cursor(cursor) == (12345, "preset-a")

    def test_decode_rejects_garbage(self) -> None:
        with pytest.raises(ValueError, match="invalid cursor"):
            _decode_cursor("not-a-valid-cursor!!!")

    def test_decode_rejects_missing_separator(self) -> None:
        import base64

        garbage = base64.urlsafe_b64encode(b"no-separator-here").decode("ascii")
        with pytest.raises(ValueError, match="invalid cursor"):
            _decode_cursor(garbage)

    def test_decode_rejects_non_integer_timestamp(self) -> None:
        import base64

        garbage = base64.urlsafe_b64encode(b"not-a-number:preset-1").decode("ascii")
        with pytest.raises(ValueError, match="invalid cursor"):
            _decode_cursor(garbage)


# ── MockConsolePresetsService ──────────────────────────────────────────────


class TestMockConsolePresetsService:
    def test_abstract_interface(self) -> None:
        assert isinstance(MockConsolePresetsService(), ConsolePresetsService)

    async def test_get_missing_returns_none(self, user_context) -> None:
        service = MockConsolePresetsService()
        assert await service.get_preset(user_context, "nope") is None

    async def test_upsert_then_get_round_trips(self, user_context) -> None:
        service = MockConsolePresetsService()
        created = await service.upsert_preset(
            user_context,
            "preset-1",
            title="My Preset",
            data={"endpoint": "openAI", "model": "gpt-x", "temperature": 0.7},
        )
        assert created["preset_id"] == "preset-1"
        assert created["title"] == "My Preset"
        assert created["data"] == {
            "endpoint": "openAI",
            "model": "gpt-x",
            "temperature": 0.7,
        }
        assert created["deleted_at_ms"] is None

        fetched = await service.get_preset(user_context, "preset-1")
        assert fetched is not None
        assert fetched["title"] == "My Preset"

    async def test_upsert_defaults_title_to_new_chat(self, user_context) -> None:
        service = MockConsolePresetsService()
        created = await service.upsert_preset(user_context, "preset-1")
        assert created["title"] == "New Chat"
        assert created["data"] == {}

    async def test_upsert_is_idempotent_by_preset_id(self, user_context) -> None:
        """Calling upsert twice with the same preset_id updates the SAME
        row rather than creating a duplicate."""
        service = MockConsolePresetsService()
        first = await service.upsert_preset(user_context, "preset-1", title="A")
        second = await service.upsert_preset(user_context, "preset-1", title="B")
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["title"] == "B"

        items, _ = await service.list_presets(user_context, limit=10)
        assert len(items) == 1

    async def test_upsert_updates_every_optional_field_on_existing_row(
        self, user_context
    ) -> None:
        """The upsert's update-existing branch updates EACH optional
        field independently (title omitted/unchanged here; data/metadata
        both changed) — covers every ``if <field> is not None`` branch
        on the second call."""
        service = MockConsolePresetsService()
        await service.upsert_preset(
            user_context, "preset-1", title="A", data={"model": "m1"}
        )
        updated = await service.upsert_preset(
            user_context,
            "preset-1",
            title=None,
            data={"model": "m2"},
            metadata={"k": "v"},
        )
        assert updated["title"] == "A", "title=None must leave the title unchanged"
        assert updated["data"] == {"model": "m2"}
        assert updated["metadata"] == {"k": "v"}

    async def test_delete_preset(self, user_context) -> None:
        service = MockConsolePresetsService()
        await service.upsert_preset(user_context, "preset-1")
        assert await service.delete_preset(user_context, "preset-1") is True
        assert await service.get_preset(user_context, "preset-1") is None

    async def test_delete_missing_preset_returns_false(self, user_context) -> None:
        service = MockConsolePresetsService()
        assert await service.delete_preset(user_context, "nope") is False

    async def test_isolates_presets_by_user(self, user_context) -> None:
        """Non-vacuity guard: neuter the ``user_sub`` filter in
        ``get_preset``/``list_presets`` and Bob starts seeing Alice's
        presets."""
        service = MockConsolePresetsService()
        alice = replace(user_context, user_id="user-alice", is_admin=False)
        bob = replace(user_context, user_id="user-bob", is_admin=False)
        await service.upsert_preset(alice, "preset-1", title="Alice's preset")

        assert await service.get_preset(bob, "preset-1") is None
        bob_items, _ = await service.list_presets(bob, limit=10)
        assert bob_items == [], (
            "user B's list_presets included user A's preset — the "
            "isolation wall is broken (missing/neutered user_sub filter)"
        )

        alice_read = await service.get_preset(alice, "preset-1")
        assert alice_read is not None
        assert alice_read["title"] == "Alice's preset"

    async def test_reset(self, user_context) -> None:
        service = MockConsolePresetsService()
        await service.upsert_preset(user_context, "preset-1")
        service.reset()
        assert await service.get_preset(user_context, "preset-1") is None


class TestMockConsolePresetsServiceListPagination:
    async def test_list_empty(self, user_context) -> None:
        service = MockConsolePresetsService()
        items, next_cursor = await service.list_presets(user_context, limit=10)
        assert items == []
        assert next_cursor is None

    async def test_list_newest_first(self, user_context) -> None:
        service = MockConsolePresetsService()
        await service.upsert_preset(user_context, "preset-1")
        await service.upsert_preset(user_context, "preset-2")
        await service.upsert_preset(user_context, "preset-3")
        items, _ = await service.list_presets(user_context, limit=10)
        assert [i["preset_id"] for i in items] == ["preset-3", "preset-2", "preset-1"]

    async def test_list_excludes_deleted(self, user_context) -> None:
        service = MockConsolePresetsService()
        await service.upsert_preset(user_context, "preset-1")
        await service.upsert_preset(user_context, "preset-2")
        await service.delete_preset(user_context, "preset-1")
        items, _ = await service.list_presets(user_context, limit=10)
        assert [i["preset_id"] for i in items] == ["preset-2"]

    async def test_list_pagination_across_pages(self, user_context) -> None:
        service = MockConsolePresetsService()
        for i in range(5):
            await service.upsert_preset(user_context, f"preset-{i}")

        page1, cursor1 = await service.list_presets(user_context, limit=2)
        assert len(page1) == 2
        assert cursor1 is not None

        page2, cursor2 = await service.list_presets(
            user_context, cursor=cursor1, limit=2
        )
        assert len(page2) == 2
        assert cursor2 is not None

        page3, cursor3 = await service.list_presets(
            user_context, cursor=cursor2, limit=2
        )
        assert len(page3) == 1
        assert cursor3 is None

        all_ids = [i["preset_id"] for i in page1 + page2 + page3]
        assert len(set(all_ids)) == 5, "pagination must not duplicate or skip rows"

    async def test_list_last_page_has_no_next_cursor(self, user_context) -> None:
        service = MockConsolePresetsService()
        await service.upsert_preset(user_context, "preset-1")
        items, next_cursor = await service.list_presets(user_context, limit=10)
        assert len(items) == 1
        assert next_cursor is None

    async def test_invalid_cursor_raises_value_error(self, user_context) -> None:
        service = MockConsolePresetsService()
        with pytest.raises(ValueError, match="invalid cursor"):
            await service.list_presets(user_context, cursor="garbage!!")


# ── PostgresConsolePresetsService (aiosqlite) ─────────────────────────────


@pytest_asyncio.fixture
async def pg_factory():
    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    return factory


@pytest.fixture
def service(pg_factory) -> PostgresConsolePresetsService:
    return PostgresConsolePresetsService(
        session_factory=pg_factory.get_session_factory(),
    )


class TestPostgresConsolePresetsService:
    def test_abstract_interface(self, service) -> None:
        assert isinstance(service, ConsolePresetsService)

    async def test_get_missing_returns_none(self, service, user_context) -> None:
        assert await service.get_preset(user_context, "nope") is None

    async def test_upsert_then_get_round_trips(self, service, user_context) -> None:
        created = await service.upsert_preset(
            user_context, "preset-1", title="Hello", data={"model": "gpt-x"}
        )
        assert created["preset_id"] == "preset-1"
        assert created["title"] == "Hello"
        assert created["data"] == {"model": "gpt-x"}

        fetched = await service.get_preset(user_context, "preset-1")
        assert fetched is not None
        assert fetched["title"] == "Hello"

    async def test_upsert_defaults_title_to_new_chat(
        self, service, user_context
    ) -> None:
        created = await service.upsert_preset(user_context, "preset-1")
        assert created["title"] == "New Chat"

    async def test_upsert_is_idempotent_by_preset_id(
        self, service, user_context
    ) -> None:
        first = await service.upsert_preset(user_context, "preset-1", title="A")
        second = await service.upsert_preset(user_context, "preset-1", title="B")
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["title"] == "B"

        items, _ = await service.list_presets(user_context, limit=10)
        assert len(items) == 1

    async def test_upsert_updates_every_optional_field_on_existing_row(
        self, service, user_context
    ) -> None:
        await service.upsert_preset(
            user_context, "preset-1", title="A", data={"model": "m1"}
        )
        updated = await service.upsert_preset(
            user_context,
            "preset-1",
            title=None,
            data={"model": "m2"},
            metadata={"k": "v"},
        )
        assert updated["title"] == "A", "title=None must leave the title unchanged"
        assert updated["data"] == {"model": "m2"}
        assert updated["metadata"] == {"k": "v"}

    async def test_delete_missing_preset_returns_false(
        self, service, user_context
    ) -> None:
        assert await service.delete_preset(user_context, "nope") is False

    async def test_delete_preset_removes_it(self, service, user_context) -> None:
        await service.upsert_preset(user_context, "preset-1")
        assert await service.delete_preset(user_context, "preset-1") is True
        assert await service.get_preset(user_context, "preset-1") is None

    async def test_cross_user_isolation_denies_read(
        self, service, user_context
    ) -> None:
        """Acceptance guard — RLS: user B cannot read user A's preset.
        Neuter the explicit ``user_sub`` filter in
        ``PostgresConsolePresetsService.get_preset`` and this test goes
        RED.

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
            await service.upsert_preset(
                alice, "secret-preset", title="alice's private preset"
            )
        finally:
            set_current_user_id(None)

        set_current_user_id(bob.user_id)
        try:
            bob_read = await service.get_preset(bob, "secret-preset")
            bob_list, _ = await service.list_presets(bob, limit=10)
        finally:
            set_current_user_id(None)

        assert bob_read is None, (
            "user B read user A's preset — the isolation wall is broken "
            "(missing/neutered user_sub filter)"
        )
        assert bob_list == [], "user B's list_presets included user A's preset"

        set_current_user_id(alice.user_id)
        try:
            alice_read = await service.get_preset(alice, "secret-preset")
        finally:
            set_current_user_id(None)
        assert alice_read is not None
        assert alice_read["title"] == "alice's private preset"

    async def test_upsert_preset_cannot_overwrite_another_users_row(
        self, service, user_context
    ) -> None:
        """Non-vacuity guard: even with an IDENTICAL preset_id, bob's
        upsert must create/update HIS OWN row, never alice's — neuter
        the explicit ``user_sub`` filter in the upsert's existence check
        and this test goes RED (bob's title would overwrite alice's row
        instead of creating a second, isolated one)."""
        alice = replace(user_context, user_id="user-alice-up", is_admin=False)
        bob = replace(user_context, user_id="user-bob-up", is_admin=False)

        await service.upsert_preset(alice, "shared-id", title="alice's title")
        await service.upsert_preset(bob, "shared-id", title="bob's title")

        alice_row = await service.get_preset(alice, "shared-id")
        bob_row = await service.get_preset(bob, "shared-id")
        assert alice_row is not None
        assert bob_row is not None
        assert alice_row["title"] == "alice's title", (
            "bob's upsert overwrote alice's preset — the user_sub "
            "isolation filter in the upsert existence-check is missing/neutered"
        )
        assert bob_row["title"] == "bob's title"

    async def test_cross_user_delete_denied(self, service, user_context) -> None:
        alice = replace(user_context, user_id="user-alice-del", is_admin=False)
        bob = replace(user_context, user_id="user-bob-del", is_admin=False)
        await service.upsert_preset(alice, "alice-preset", title="mine")

        deleted = await service.delete_preset(bob, "alice-preset")
        assert deleted is False, "user B deleted user A's preset — isolation broken"
        assert (await service.get_preset(alice, "alice-preset")) is not None

    async def test_write_failure_raises_runtime_error(
        self, service, user_context, monkeypatch
    ) -> None:
        async def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "sqlalchemy.ext.asyncio.AsyncSession.commit",
            _boom,
        )
        with pytest.raises(RuntimeError, match="upsert_preset.*failed"):
            await service.upsert_preset(user_context, "preset-1")


class TestPostgresConsolePresetsServiceListPagination:
    async def test_list_newest_first(self, service, user_context) -> None:
        await service.upsert_preset(user_context, "preset-1")
        await service.upsert_preset(user_context, "preset-2")
        await service.upsert_preset(user_context, "preset-3")
        items, _ = await service.list_presets(user_context, limit=10)
        assert [i["preset_id"] for i in items] == ["preset-3", "preset-2", "preset-1"]

    async def test_list_pagination_across_pages(self, service, user_context) -> None:
        for i in range(5):
            await service.upsert_preset(user_context, f"preset-{i}")

        page1, cursor1 = await service.list_presets(user_context, limit=2)
        assert len(page1) == 2
        page2, cursor2 = await service.list_presets(
            user_context, cursor=cursor1, limit=2
        )
        assert len(page2) == 2
        page3, cursor3 = await service.list_presets(
            user_context, cursor=cursor2, limit=2
        )
        assert len(page3) == 1
        assert cursor3 is None

        all_ids = [i["preset_id"] for i in page1 + page2 + page3]
        assert len(set(all_ids)) == 5

    async def test_invalid_cursor_raises_value_error(
        self, service, user_context
    ) -> None:
        with pytest.raises(ValueError, match="invalid cursor"):
            await service.list_presets(user_context, cursor="garbage!!")
