"""Tests for the console-conversation-tags service (Conversation-Tags
domain, MongoDB-elimination EPIC).

Mirrors ``test_console_chat_projects_service.py``'s structure EXACTLY
(the spec's instruction), scaled to the conversation-tags field set
(tag/description/count/position instead of chat_project_id/name/
description): Mock-service interface tests, then a Postgres-backed
suite via ``InMemoryPostgresFactory`` (aiosqlite, no real PostgreSQL
required). The cross-user isolation tests bracket their assertions with
``set_current_user_id`` per ``feedback_unit_tests_miss_rls`` — RLS
itself is a no-op on SQLite, so the guard under test is the service's
own explicit ``.filter(user_sub == ...)`` clause, not the (here inert)
Postgres GUC.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import pytest_asyncio

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.db.rls import set_current_user_id
from audittrace.services.console_conversation_tags import (
    ConsoleConversationTagsService,
    MockConsoleConversationTagsService,
    PostgresConsoleConversationTagsService,
    _decode_cursor,
    _encode_cursor,
)

# ── cursor helpers ───────────────────────────────────────────────────────


class TestCursorCodec:
    def test_round_trips(self) -> None:
        cursor = _encode_cursor(updated_at_ms=12345, tag="work")
        assert _decode_cursor(cursor) == (12345, "work")

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

        garbage = base64.urlsafe_b64encode(b"not-a-number:work").decode("ascii")
        with pytest.raises(ValueError, match="invalid cursor"):
            _decode_cursor(garbage)


# ── MockConsoleConversationTagsService ────────────────────────────────────


class TestMockConsoleConversationTagsService:
    def test_abstract_interface(self) -> None:
        assert isinstance(
            MockConsoleConversationTagsService(), ConsoleConversationTagsService
        )

    async def test_get_missing_returns_none(self, user_context) -> None:
        service = MockConsoleConversationTagsService()
        assert await service.get_conversation_tag(user_context, "nope") is None

    async def test_upsert_then_get_round_trips(self, user_context) -> None:
        service = MockConsoleConversationTagsService()
        created = await service.upsert_conversation_tag(
            user_context,
            "work",
            description="work chats",
            count=3,
            position=1,
        )
        assert created["tag"] == "work"
        assert created["description"] == "work chats"
        assert created["count"] == 3
        assert created["position"] == 1
        assert created["deleted_at_ms"] is None

        fetched = await service.get_conversation_tag(user_context, "work")
        assert fetched is not None
        assert fetched["description"] == "work chats"

    async def test_upsert_defaults_description_count_position(
        self, user_context
    ) -> None:
        service = MockConsoleConversationTagsService()
        created = await service.upsert_conversation_tag(user_context, "bare")
        assert created["description"] == ""
        assert created["count"] == 0
        assert created["position"] == 0
        assert created["metadata"] == {}

    async def test_upsert_is_idempotent_by_tag(self, user_context) -> None:
        """Calling upsert twice with the same tag updates the SAME row
        rather than creating a duplicate."""
        service = MockConsoleConversationTagsService()
        first = await service.upsert_conversation_tag(user_context, "work")
        second = await service.upsert_conversation_tag(
            user_context, "work", description="B"
        )
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["description"] == "B"

        items, _ = await service.list_conversation_tags(user_context, limit=10)
        assert len(items) == 1

    async def test_upsert_updates_every_optional_field_on_existing_row(
        self, user_context
    ) -> None:
        """The upsert's update-existing branch updates EACH optional
        field independently — covers every ``if <field> is not None``
        branch on the second call, plus count/position (always
        overwritten, no ``is not None`` guard)."""
        service = MockConsoleConversationTagsService()
        await service.upsert_conversation_tag(
            user_context, "work", description="d1", count=1, position=1
        )
        updated = await service.upsert_conversation_tag(
            user_context,
            "work",
            description="d2",
            count=2,
            position=2,
            metadata={"k": "v"},
        )
        assert updated["description"] == "d2"
        assert updated["count"] == 2
        assert updated["position"] == 2
        assert updated["metadata"] == {"k": "v"}

    async def test_upsert_preserves_description_when_omitted_on_update(
        self, user_context
    ) -> None:
        """The ``description=None`` (omitted) branch on an UPDATE must
        preserve the existing description, never blank it out — the
        false side of ``if description is not None``."""
        service = MockConsoleConversationTagsService()
        await service.upsert_conversation_tag(
            user_context, "work", description="keep-me"
        )
        updated = await service.upsert_conversation_tag(user_context, "work", count=5)
        assert updated["description"] == "keep-me"
        assert updated["count"] == 5

    async def test_delete_conversation_tag(self, user_context) -> None:
        service = MockConsoleConversationTagsService()
        await service.upsert_conversation_tag(user_context, "work")
        assert await service.delete_conversation_tag(user_context, "work") is True
        assert await service.get_conversation_tag(user_context, "work") is None

    async def test_delete_missing_conversation_tag_returns_false(
        self, user_context
    ) -> None:
        service = MockConsoleConversationTagsService()
        assert await service.delete_conversation_tag(user_context, "nope") is False

    async def test_isolates_conversation_tags_by_user(self, user_context) -> None:
        """Non-vacuity guard: neuter the ``user_sub`` filter in
        ``get_conversation_tag``/``list_conversation_tags`` and Bob
        starts seeing Alice's conversation-tags."""
        service = MockConsoleConversationTagsService()
        alice = replace(user_context, user_id="user-alice", is_admin=False)
        bob = replace(user_context, user_id="user-bob", is_admin=False)
        await service.upsert_conversation_tag(alice, "work", description="Alice's tag")

        assert await service.get_conversation_tag(bob, "work") is None
        bob_items, _ = await service.list_conversation_tags(bob, limit=10)
        assert bob_items == [], (
            "user B's list_conversation_tags included user A's tag — "
            "the isolation wall is broken (missing/neutered user_sub filter)"
        )

        alice_read = await service.get_conversation_tag(alice, "work")
        assert alice_read is not None
        assert alice_read["description"] == "Alice's tag"

    async def test_reset(self, user_context) -> None:
        service = MockConsoleConversationTagsService()
        await service.upsert_conversation_tag(user_context, "work")
        service.reset()
        assert await service.get_conversation_tag(user_context, "work") is None


class TestMockConsoleConversationTagsServiceListPagination:
    async def test_list_empty(self, user_context) -> None:
        service = MockConsoleConversationTagsService()
        items, next_cursor = await service.list_conversation_tags(
            user_context, limit=10
        )
        assert items == []
        assert next_cursor is None

    async def test_list_newest_first(self, user_context) -> None:
        service = MockConsoleConversationTagsService()
        await service.upsert_conversation_tag(user_context, "tag-1")
        await service.upsert_conversation_tag(user_context, "tag-2")
        await service.upsert_conversation_tag(user_context, "tag-3")
        items, _ = await service.list_conversation_tags(user_context, limit=10)
        assert [i["tag"] for i in items] == ["tag-3", "tag-2", "tag-1"]

    async def test_list_excludes_deleted(self, user_context) -> None:
        service = MockConsoleConversationTagsService()
        await service.upsert_conversation_tag(user_context, "tag-1")
        await service.upsert_conversation_tag(user_context, "tag-2")
        await service.delete_conversation_tag(user_context, "tag-1")
        items, _ = await service.list_conversation_tags(user_context, limit=10)
        assert [i["tag"] for i in items] == ["tag-2"]

    async def test_list_pagination_across_pages(self, user_context) -> None:
        service = MockConsoleConversationTagsService()
        for i in range(5):
            await service.upsert_conversation_tag(user_context, f"tag-{i}")

        page1, cursor1 = await service.list_conversation_tags(user_context, limit=2)
        assert len(page1) == 2
        assert cursor1 is not None

        page2, cursor2 = await service.list_conversation_tags(
            user_context, cursor=cursor1, limit=2
        )
        assert len(page2) == 2
        assert cursor2 is not None

        page3, cursor3 = await service.list_conversation_tags(
            user_context, cursor=cursor2, limit=2
        )
        assert len(page3) == 1
        assert cursor3 is None

        all_tags = [i["tag"] for i in page1 + page2 + page3]
        assert len(set(all_tags)) == 5, "pagination must not duplicate or skip rows"

    async def test_list_last_page_has_no_next_cursor(self, user_context) -> None:
        service = MockConsoleConversationTagsService()
        await service.upsert_conversation_tag(user_context, "tag-1")
        items, next_cursor = await service.list_conversation_tags(
            user_context, limit=10
        )
        assert len(items) == 1
        assert next_cursor is None

    async def test_invalid_cursor_raises_value_error(self, user_context) -> None:
        service = MockConsoleConversationTagsService()
        with pytest.raises(ValueError, match="invalid cursor"):
            await service.list_conversation_tags(user_context, cursor="garbage!!")


# ── PostgresConsoleConversationTagsService (aiosqlite) ────────────────────


@pytest_asyncio.fixture
async def pg_factory():
    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    return factory


@pytest.fixture
def service(pg_factory) -> PostgresConsoleConversationTagsService:
    return PostgresConsoleConversationTagsService(
        session_factory=pg_factory.get_session_factory(),
    )


class TestPostgresConsoleConversationTagsService:
    def test_abstract_interface(self, service) -> None:
        assert isinstance(service, ConsoleConversationTagsService)

    async def test_get_missing_returns_none(self, service, user_context) -> None:
        assert await service.get_conversation_tag(user_context, "nope") is None

    async def test_upsert_then_get_round_trips(self, service, user_context) -> None:
        created = await service.upsert_conversation_tag(
            user_context, "work", description="work chats", count=3
        )
        assert created["tag"] == "work"
        assert created["description"] == "work chats"
        assert created["count"] == 3

        fetched = await service.get_conversation_tag(user_context, "work")
        assert fetched is not None
        assert fetched["description"] == "work chats"

    async def test_upsert_defaults_description_count_position(
        self, service, user_context
    ) -> None:
        created = await service.upsert_conversation_tag(user_context, "bare")
        assert created["description"] == ""
        assert created["count"] == 0
        assert created["position"] == 0

    async def test_upsert_is_idempotent_by_tag(self, service, user_context) -> None:
        first = await service.upsert_conversation_tag(user_context, "work")
        second = await service.upsert_conversation_tag(
            user_context, "work", description="B"
        )
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["description"] == "B"

        items, _ = await service.list_conversation_tags(user_context, limit=10)
        assert len(items) == 1

    async def test_upsert_updates_every_optional_field_on_existing_row(
        self, service, user_context
    ) -> None:
        await service.upsert_conversation_tag(
            user_context, "work", description="d1", count=1
        )
        updated = await service.upsert_conversation_tag(
            user_context,
            "work",
            description="d2",
            count=2,
            position=2,
            metadata={"k": "v"},
        )
        assert updated["description"] == "d2"
        assert updated["count"] == 2
        assert updated["position"] == 2
        assert updated["metadata"] == {"k": "v"}

    async def test_upsert_preserves_description_when_omitted_on_update(
        self, service, user_context
    ) -> None:
        await service.upsert_conversation_tag(
            user_context, "work", description="keep-me"
        )
        updated = await service.upsert_conversation_tag(user_context, "work", count=5)
        assert updated["description"] == "keep-me"
        assert updated["count"] == 5

    async def test_delete_missing_conversation_tag_returns_false(
        self, service, user_context
    ) -> None:
        assert await service.delete_conversation_tag(user_context, "nope") is False

    async def test_delete_conversation_tag_removes_it(
        self, service, user_context
    ) -> None:
        await service.upsert_conversation_tag(user_context, "work")
        assert await service.delete_conversation_tag(user_context, "work") is True
        assert await service.get_conversation_tag(user_context, "work") is None

    async def test_cross_user_isolation_denies_read(
        self, service, user_context
    ) -> None:
        """Acceptance guard — RLS: user B cannot read user A's
        conversation-tag. Neuter the explicit ``user_sub`` filter in
        ``PostgresConsoleConversationTagsService.get_conversation_tag``
        and this test goes RED.

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
            await service.upsert_conversation_tag(
                alice, "secret-tag", description="alice's private tag"
            )
        finally:
            set_current_user_id(None)

        set_current_user_id(bob.user_id)
        try:
            bob_read = await service.get_conversation_tag(bob, "secret-tag")
            bob_list, _ = await service.list_conversation_tags(bob, limit=10)
        finally:
            set_current_user_id(None)

        assert bob_read is None, (
            "user B read user A's conversation-tag — the isolation wall "
            "is broken (missing/neutered user_sub filter)"
        )
        assert bob_list == [], "user B's list_conversation_tags included user A's tag"

        set_current_user_id(alice.user_id)
        try:
            alice_read = await service.get_conversation_tag(alice, "secret-tag")
        finally:
            set_current_user_id(None)
        assert alice_read is not None
        assert alice_read["description"] == "alice's private tag"

    async def test_upsert_conversation_tag_cannot_overwrite_another_users_row(
        self, service, user_context
    ) -> None:
        """Non-vacuity guard: even with an IDENTICAL tag, bob's upsert
        must create/update HIS OWN row, never alice's — neuter the
        explicit ``user_sub`` filter in the upsert's existence check
        and this test goes RED (bob's description would overwrite
        alice's row instead of creating a second, isolated one)."""
        alice = replace(user_context, user_id="user-alice-up", is_admin=False)
        bob = replace(user_context, user_id="user-bob-up", is_admin=False)

        await service.upsert_conversation_tag(
            alice, "shared-tag", description="alice's tag"
        )
        await service.upsert_conversation_tag(
            bob, "shared-tag", description="bob's tag"
        )

        alice_row = await service.get_conversation_tag(alice, "shared-tag")
        bob_row = await service.get_conversation_tag(bob, "shared-tag")
        assert alice_row is not None
        assert bob_row is not None
        assert alice_row["description"] == "alice's tag", (
            "bob's upsert overwrote alice's conversation-tag — the "
            "user_sub isolation filter in the upsert existence-check is "
            "missing/neutered"
        )
        assert bob_row["description"] == "bob's tag"

    async def test_cross_user_delete_denied(self, service, user_context) -> None:
        alice = replace(user_context, user_id="user-alice-del", is_admin=False)
        bob = replace(user_context, user_id="user-bob-del", is_admin=False)
        await service.upsert_conversation_tag(alice, "alice-tag", description="mine")

        deleted = await service.delete_conversation_tag(bob, "alice-tag")
        assert deleted is False, (
            "user B deleted user A's conversation-tag — isolation broken"
        )
        assert (await service.get_conversation_tag(alice, "alice-tag")) is not None

    async def test_write_failure_raises_runtime_error(
        self, service, user_context, monkeypatch
    ) -> None:
        async def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "sqlalchemy.ext.asyncio.AsyncSession.commit",
            _boom,
        )
        with pytest.raises(RuntimeError, match="upsert_conversation_tag.*failed"):
            await service.upsert_conversation_tag(user_context, "work")


class TestPostgresConsoleConversationTagsServiceListPagination:
    async def test_list_newest_first(self, service, user_context) -> None:
        await service.upsert_conversation_tag(user_context, "tag-1")
        await service.upsert_conversation_tag(user_context, "tag-2")
        await service.upsert_conversation_tag(user_context, "tag-3")
        items, _ = await service.list_conversation_tags(user_context, limit=10)
        assert [i["tag"] for i in items] == ["tag-3", "tag-2", "tag-1"]

    async def test_list_pagination_across_pages(self, service, user_context) -> None:
        for i in range(5):
            await service.upsert_conversation_tag(user_context, f"tag-{i}")

        page1, cursor1 = await service.list_conversation_tags(user_context, limit=2)
        assert len(page1) == 2
        page2, cursor2 = await service.list_conversation_tags(
            user_context, cursor=cursor1, limit=2
        )
        assert len(page2) == 2
        page3, cursor3 = await service.list_conversation_tags(
            user_context, cursor=cursor2, limit=2
        )
        assert len(page3) == 1
        assert cursor3 is None

        all_tags = [i["tag"] for i in page1 + page2 + page3]
        assert len(set(all_tags)) == 5

    async def test_invalid_cursor_raises_value_error(
        self, service, user_context
    ) -> None:
        with pytest.raises(ValueError, match="invalid cursor"):
            await service.list_conversation_tags(user_context, cursor="garbage!!")
