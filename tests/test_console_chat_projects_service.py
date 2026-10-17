"""Tests for the console-chat-projects service (Chat-Projects domain,
MongoDB-elimination EPIC).

Mirrors ``test_console_presets_service.py``'s structure EXACTLY (the
spec's instruction), scaled to the chat-projects field set
(name/description instead of title/data): Mock-service interface
tests, then a Postgres-backed suite via ``InMemoryPostgresFactory``
(aiosqlite, no real PostgreSQL required). The cross-user isolation
tests bracket their assertions with ``set_current_user_id`` per
``feedback_unit_tests_miss_rls`` — RLS itself is a no-op on SQLite, so
the guard under test is the service's own explicit
``.filter(user_sub == ...)`` clause, not the (here inert) Postgres GUC.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import pytest_asyncio

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.db.rls import set_current_user_id
from audittrace.services.console_chat_projects import (
    ConsoleChatProjectsService,
    MockConsoleChatProjectsService,
    PostgresConsoleChatProjectsService,
    _decode_cursor,
    _encode_cursor,
)

# ── cursor helpers ───────────────────────────────────────────────────────


class TestCursorCodec:
    def test_round_trips(self) -> None:
        cursor = _encode_cursor(updated_at_ms=12345, chat_project_id="project-a")
        assert _decode_cursor(cursor) == (12345, "project-a")

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

        garbage = base64.urlsafe_b64encode(b"not-a-number:project-1").decode("ascii")
        with pytest.raises(ValueError, match="invalid cursor"):
            _decode_cursor(garbage)


# ── MockConsoleChatProjectsService ────────────────────────────────────────


class TestMockConsoleChatProjectsService:
    def test_abstract_interface(self) -> None:
        assert isinstance(MockConsoleChatProjectsService(), ConsoleChatProjectsService)

    async def test_get_missing_returns_none(self, user_context) -> None:
        service = MockConsoleChatProjectsService()
        assert await service.get_chat_project(user_context, "nope") is None

    async def test_upsert_then_get_round_trips(self, user_context) -> None:
        service = MockConsoleChatProjectsService()
        created = await service.upsert_chat_project(
            user_context,
            "project-1",
            name="My Project",
            description="things to do",
        )
        assert created["chat_project_id"] == "project-1"
        assert created["name"] == "My Project"
        assert created["description"] == "things to do"
        assert created["deleted_at_ms"] is None

        fetched = await service.get_chat_project(user_context, "project-1")
        assert fetched is not None
        assert fetched["name"] == "My Project"

    async def test_upsert_defaults_description_to_empty_string(
        self, user_context
    ) -> None:
        service = MockConsoleChatProjectsService()
        created = await service.upsert_chat_project(
            user_context, "project-1", name="Bare"
        )
        assert created["description"] == ""
        assert created["metadata"] == {}

    async def test_upsert_is_idempotent_by_chat_project_id(self, user_context) -> None:
        """Calling upsert twice with the same chat_project_id updates
        the SAME row rather than creating a duplicate."""
        service = MockConsoleChatProjectsService()
        first = await service.upsert_chat_project(user_context, "project-1", name="A")
        second = await service.upsert_chat_project(user_context, "project-1", name="B")
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["name"] == "B"

        items, _ = await service.list_chat_projects(user_context, limit=10)
        assert len(items) == 1

    async def test_upsert_updates_every_optional_field_on_existing_row(
        self, user_context
    ) -> None:
        """The upsert's update-existing branch updates EACH optional
        field independently (name omitted/unchanged here; description/
        metadata both changed) — covers every ``if <field> is not None``
        branch on the second call."""
        service = MockConsoleChatProjectsService()
        await service.upsert_chat_project(
            user_context, "project-1", name="A", description="d1"
        )
        updated = await service.upsert_chat_project(
            user_context,
            "project-1",
            name="A",
            description="d2",
            metadata={"k": "v"},
        )
        assert updated["name"] == "A"
        assert updated["description"] == "d2"
        assert updated["metadata"] == {"k": "v"}

    async def test_delete_chat_project(self, user_context) -> None:
        service = MockConsoleChatProjectsService()
        await service.upsert_chat_project(user_context, "project-1", name="X")
        assert await service.delete_chat_project(user_context, "project-1") is True
        assert await service.get_chat_project(user_context, "project-1") is None

    async def test_delete_missing_chat_project_returns_false(
        self, user_context
    ) -> None:
        service = MockConsoleChatProjectsService()
        assert await service.delete_chat_project(user_context, "nope") is False

    async def test_isolates_chat_projects_by_user(self, user_context) -> None:
        """Non-vacuity guard: neuter the ``user_sub`` filter in
        ``get_chat_project``/``list_chat_projects`` and Bob starts
        seeing Alice's chat-projects."""
        service = MockConsoleChatProjectsService()
        alice = replace(user_context, user_id="user-alice", is_admin=False)
        bob = replace(user_context, user_id="user-bob", is_admin=False)
        await service.upsert_chat_project(alice, "project-1", name="Alice's project")

        assert await service.get_chat_project(bob, "project-1") is None
        bob_items, _ = await service.list_chat_projects(bob, limit=10)
        assert bob_items == [], (
            "user B's list_chat_projects included user A's chat-project — "
            "the isolation wall is broken (missing/neutered user_sub filter)"
        )

        alice_read = await service.get_chat_project(alice, "project-1")
        assert alice_read is not None
        assert alice_read["name"] == "Alice's project"

    async def test_reset(self, user_context) -> None:
        service = MockConsoleChatProjectsService()
        await service.upsert_chat_project(user_context, "project-1", name="X")
        service.reset()
        assert await service.get_chat_project(user_context, "project-1") is None


class TestMockConsoleChatProjectsServiceListPagination:
    async def test_list_empty(self, user_context) -> None:
        service = MockConsoleChatProjectsService()
        items, next_cursor = await service.list_chat_projects(user_context, limit=10)
        assert items == []
        assert next_cursor is None

    async def test_list_newest_first(self, user_context) -> None:
        service = MockConsoleChatProjectsService()
        await service.upsert_chat_project(user_context, "project-1", name="p1")
        await service.upsert_chat_project(user_context, "project-2", name="p2")
        await service.upsert_chat_project(user_context, "project-3", name="p3")
        items, _ = await service.list_chat_projects(user_context, limit=10)
        assert [i["chat_project_id"] for i in items] == [
            "project-3",
            "project-2",
            "project-1",
        ]

    async def test_list_excludes_deleted(self, user_context) -> None:
        service = MockConsoleChatProjectsService()
        await service.upsert_chat_project(user_context, "project-1", name="p1")
        await service.upsert_chat_project(user_context, "project-2", name="p2")
        await service.delete_chat_project(user_context, "project-1")
        items, _ = await service.list_chat_projects(user_context, limit=10)
        assert [i["chat_project_id"] for i in items] == ["project-2"]

    async def test_list_pagination_across_pages(self, user_context) -> None:
        service = MockConsoleChatProjectsService()
        for i in range(5):
            await service.upsert_chat_project(user_context, f"project-{i}", name="p")

        page1, cursor1 = await service.list_chat_projects(user_context, limit=2)
        assert len(page1) == 2
        assert cursor1 is not None

        page2, cursor2 = await service.list_chat_projects(
            user_context, cursor=cursor1, limit=2
        )
        assert len(page2) == 2
        assert cursor2 is not None

        page3, cursor3 = await service.list_chat_projects(
            user_context, cursor=cursor2, limit=2
        )
        assert len(page3) == 1
        assert cursor3 is None

        all_ids = [i["chat_project_id"] for i in page1 + page2 + page3]
        assert len(set(all_ids)) == 5, "pagination must not duplicate or skip rows"

    async def test_list_last_page_has_no_next_cursor(self, user_context) -> None:
        service = MockConsoleChatProjectsService()
        await service.upsert_chat_project(user_context, "project-1", name="p1")
        items, next_cursor = await service.list_chat_projects(user_context, limit=10)
        assert len(items) == 1
        assert next_cursor is None

    async def test_invalid_cursor_raises_value_error(self, user_context) -> None:
        service = MockConsoleChatProjectsService()
        with pytest.raises(ValueError, match="invalid cursor"):
            await service.list_chat_projects(user_context, cursor="garbage!!")


# ── PostgresConsoleChatProjectsService (aiosqlite) ────────────────────────


@pytest_asyncio.fixture
async def pg_factory():
    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    return factory


@pytest.fixture
def service(pg_factory) -> PostgresConsoleChatProjectsService:
    return PostgresConsoleChatProjectsService(
        session_factory=pg_factory.get_session_factory(),
    )


class TestPostgresConsoleChatProjectsService:
    def test_abstract_interface(self, service) -> None:
        assert isinstance(service, ConsoleChatProjectsService)

    async def test_get_missing_returns_none(self, service, user_context) -> None:
        assert await service.get_chat_project(user_context, "nope") is None

    async def test_upsert_then_get_round_trips(self, service, user_context) -> None:
        created = await service.upsert_chat_project(
            user_context, "project-1", name="Hello", description="my todos"
        )
        assert created["chat_project_id"] == "project-1"
        assert created["name"] == "Hello"
        assert created["description"] == "my todos"

        fetched = await service.get_chat_project(user_context, "project-1")
        assert fetched is not None
        assert fetched["name"] == "Hello"

    async def test_upsert_defaults_description_to_empty_string(
        self, service, user_context
    ) -> None:
        created = await service.upsert_chat_project(
            user_context, "project-1", name="Bare"
        )
        assert created["description"] == ""

    async def test_upsert_is_idempotent_by_chat_project_id(
        self, service, user_context
    ) -> None:
        first = await service.upsert_chat_project(user_context, "project-1", name="A")
        second = await service.upsert_chat_project(user_context, "project-1", name="B")
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["name"] == "B"

        items, _ = await service.list_chat_projects(user_context, limit=10)
        assert len(items) == 1

    async def test_upsert_updates_every_optional_field_on_existing_row(
        self, service, user_context
    ) -> None:
        await service.upsert_chat_project(
            user_context, "project-1", name="A", description="d1"
        )
        updated = await service.upsert_chat_project(
            user_context,
            "project-1",
            name="A",
            description="d2",
            metadata={"k": "v"},
        )
        assert updated["description"] == "d2"
        assert updated["metadata"] == {"k": "v"}

    async def test_delete_missing_chat_project_returns_false(
        self, service, user_context
    ) -> None:
        assert await service.delete_chat_project(user_context, "nope") is False

    async def test_delete_chat_project_removes_it(self, service, user_context) -> None:
        await service.upsert_chat_project(user_context, "project-1", name="X")
        assert await service.delete_chat_project(user_context, "project-1") is True
        assert await service.get_chat_project(user_context, "project-1") is None

    async def test_cross_user_isolation_denies_read(
        self, service, user_context
    ) -> None:
        """Acceptance guard — RLS: user B cannot read user A's
        chat-project. Neuter the explicit ``user_sub`` filter in
        ``PostgresConsoleChatProjectsService.get_chat_project`` and this
        test goes RED.

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
            await service.upsert_chat_project(
                alice, "secret-project", name="alice's private project"
            )
        finally:
            set_current_user_id(None)

        set_current_user_id(bob.user_id)
        try:
            bob_read = await service.get_chat_project(bob, "secret-project")
            bob_list, _ = await service.list_chat_projects(bob, limit=10)
        finally:
            set_current_user_id(None)

        assert bob_read is None, (
            "user B read user A's chat-project — the isolation wall is "
            "broken (missing/neutered user_sub filter)"
        )
        assert bob_list == [], (
            "user B's list_chat_projects included user A's chat-project"
        )

        set_current_user_id(alice.user_id)
        try:
            alice_read = await service.get_chat_project(alice, "secret-project")
        finally:
            set_current_user_id(None)
        assert alice_read is not None
        assert alice_read["name"] == "alice's private project"

    async def test_upsert_chat_project_cannot_overwrite_another_users_row(
        self, service, user_context
    ) -> None:
        """Non-vacuity guard: even with an IDENTICAL chat_project_id,
        bob's upsert must create/update HIS OWN row, never alice's —
        neuter the explicit ``user_sub`` filter in the upsert's
        existence check and this test goes RED (bob's name would
        overwrite alice's row instead of creating a second, isolated
        one)."""
        alice = replace(user_context, user_id="user-alice-up", is_admin=False)
        bob = replace(user_context, user_id="user-bob-up", is_admin=False)

        await service.upsert_chat_project(alice, "shared-id", name="alice's name")
        await service.upsert_chat_project(bob, "shared-id", name="bob's name")

        alice_row = await service.get_chat_project(alice, "shared-id")
        bob_row = await service.get_chat_project(bob, "shared-id")
        assert alice_row is not None
        assert bob_row is not None
        assert alice_row["name"] == "alice's name", (
            "bob's upsert overwrote alice's chat-project — the user_sub "
            "isolation filter in the upsert existence-check is missing/neutered"
        )
        assert bob_row["name"] == "bob's name"

    async def test_cross_user_delete_denied(self, service, user_context) -> None:
        alice = replace(user_context, user_id="user-alice-del", is_admin=False)
        bob = replace(user_context, user_id="user-bob-del", is_admin=False)
        await service.upsert_chat_project(alice, "alice-project", name="mine")

        deleted = await service.delete_chat_project(bob, "alice-project")
        assert deleted is False, (
            "user B deleted user A's chat-project — isolation broken"
        )
        assert (await service.get_chat_project(alice, "alice-project")) is not None

    async def test_write_failure_raises_runtime_error(
        self, service, user_context, monkeypatch
    ) -> None:
        async def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "sqlalchemy.ext.asyncio.AsyncSession.commit",
            _boom,
        )
        with pytest.raises(RuntimeError, match="upsert_chat_project.*failed"):
            await service.upsert_chat_project(user_context, "project-1", name="X")


class TestPostgresConsoleChatProjectsServiceListPagination:
    async def test_list_newest_first(self, service, user_context) -> None:
        await service.upsert_chat_project(user_context, "project-1", name="p1")
        await service.upsert_chat_project(user_context, "project-2", name="p2")
        await service.upsert_chat_project(user_context, "project-3", name="p3")
        items, _ = await service.list_chat_projects(user_context, limit=10)
        assert [i["chat_project_id"] for i in items] == [
            "project-3",
            "project-2",
            "project-1",
        ]

    async def test_list_pagination_across_pages(self, service, user_context) -> None:
        for i in range(5):
            await service.upsert_chat_project(user_context, f"project-{i}", name="p")

        page1, cursor1 = await service.list_chat_projects(user_context, limit=2)
        assert len(page1) == 2
        page2, cursor2 = await service.list_chat_projects(
            user_context, cursor=cursor1, limit=2
        )
        assert len(page2) == 2
        page3, cursor3 = await service.list_chat_projects(
            user_context, cursor=cursor2, limit=2
        )
        assert len(page3) == 1
        assert cursor3 is None

        all_ids = [i["chat_project_id"] for i in page1 + page2 + page3]
        assert len(set(all_ids)) == 5

    async def test_invalid_cursor_raises_value_error(
        self, service, user_context
    ) -> None:
        with pytest.raises(ValueError, match="invalid cursor"):
            await service.list_chat_projects(user_context, cursor="garbage!!")
