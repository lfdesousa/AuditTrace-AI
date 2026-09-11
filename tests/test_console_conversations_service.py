"""Tests for the console-conversations service (WU-1, MongoDB-elimination
EPIC).

Mirrors ``test_session_memory_service.py``'s structure: Mock-service
interface tests, then a Postgres-backed suite via
``InMemoryPostgresFactory`` (aiosqlite, no real PostgreSQL required). The
cross-user isolation tests bracket their assertions with
``set_current_user_id`` per ``feedback_unit_tests_miss_rls`` — RLS itself
is a no-op on SQLite, so the guard under test is the service's own
explicit ``.filter(user_sub == ...)`` clause, not the (here inert)
Postgres GUC.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest
import pytest_asyncio

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.db.rls import set_current_user_id
from audittrace.services.console_conversations import (
    ConsoleConversationsService,
    MockConsoleConversationsService,
    PostgresConsoleConversationsService,
    _decode_cursor,
    _encode_cursor,
)

# ── cursor helpers ───────────────────────────────────────────────────────


class TestCursorCodec:
    def test_round_trips(self) -> None:
        cursor = _encode_cursor(updated_at_ms=12345, conversation_id="conv-a")
        assert _decode_cursor(cursor) == (12345, "conv-a")

    def test_decode_rejects_garbage(self) -> None:
        with pytest.raises(ValueError, match="invalid cursor"):
            _decode_cursor("not-a-valid-cursor!!!")

    def test_decode_rejects_missing_separator(self) -> None:
        import base64

        garbage = base64.urlsafe_b64encode(b"no-separator-here").decode("ascii")
        with pytest.raises(ValueError, match="invalid cursor"):
            _decode_cursor(garbage)


# ── MockConsoleConversationsService ───────────────────────────────────────


class TestMockConsoleConversationsServiceConversations:
    def test_abstract_interface(self) -> None:
        assert isinstance(
            MockConsoleConversationsService(), ConsoleConversationsService
        )

    async def test_get_missing_returns_none(self, user_context) -> None:
        service = MockConsoleConversationsService()
        assert await service.get_conversation(user_context, "nope") is None

    async def test_upsert_then_get_round_trips(self, user_context) -> None:
        service = MockConsoleConversationsService()
        created = await service.upsert_conversation(
            user_context, "conv-1", title="Hello", endpoint="openAI", model="gpt-x"
        )
        assert created["conversation_id"] == "conv-1"
        assert created["title"] == "Hello"
        assert created["endpoint"] == "openAI"
        assert created["model"] == "gpt-x"
        assert created["is_temporary"] is False
        assert created["deleted_at_ms"] is None

        fetched = await service.get_conversation(user_context, "conv-1")
        assert fetched is not None
        assert fetched["title"] == "Hello"

    async def test_upsert_defaults_title_to_new_chat(self, user_context) -> None:
        service = MockConsoleConversationsService()
        created = await service.upsert_conversation(user_context, "conv-1")
        assert created["title"] == "New Chat"

    async def test_upsert_is_idempotent_by_conversation_id(self, user_context) -> None:
        """Calling upsert twice with the same conversation_id updates the
        SAME row rather than creating a duplicate."""
        service = MockConsoleConversationsService()
        first = await service.upsert_conversation(user_context, "conv-1", title="A")
        second = await service.upsert_conversation(user_context, "conv-1", title="B")
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["title"] == "B"

        items, _ = await service.list_conversations(user_context, limit=10)
        assert len(items) == 1

    async def test_update_title(self, user_context) -> None:
        service = MockConsoleConversationsService()
        await service.upsert_conversation(user_context, "conv-1", title="Old")
        updated = await service.update_conversation_title(user_context, "conv-1", "New")
        assert updated is not None
        assert updated["title"] == "New"

    async def test_update_title_missing_returns_none(self, user_context) -> None:
        service = MockConsoleConversationsService()
        assert (
            await service.update_conversation_title(user_context, "nope", "x") is None
        )

    async def test_delete_conversation(self, user_context) -> None:
        service = MockConsoleConversationsService()
        await service.upsert_conversation(user_context, "conv-1")
        assert await service.delete_conversation(user_context, "conv-1") is True
        assert await service.get_conversation(user_context, "conv-1") is None

    async def test_delete_missing_conversation_returns_false(
        self, user_context
    ) -> None:
        service = MockConsoleConversationsService()
        assert await service.delete_conversation(user_context, "nope") is False

    async def test_delete_conversation_also_deletes_its_messages(
        self, user_context
    ) -> None:
        service = MockConsoleConversationsService()
        await service.upsert_conversation(user_context, "conv-1")
        await service.upsert_message(
            user_context,
            "conv-1",
            "msg-1",
            sender="user",
            text="hi",
            is_created_by_user=True,
        )
        await service.delete_conversation(user_context, "conv-1")
        assert await service.get_messages(user_context, "conv-1") == []

    async def test_isolates_conversations_by_user(self, user_context) -> None:
        """Non-vacuity guard: neuter the ``user_sub`` filter in
        ``get_conversation``/``list_conversations`` and Bob starts seeing
        Alice's conversations."""
        service = MockConsoleConversationsService()
        alice = replace(user_context, user_id="user-alice", is_admin=False)
        bob = replace(user_context, user_id="user-bob", is_admin=False)
        await service.upsert_conversation(alice, "conv-1", title="Alice's chat")

        assert await service.get_conversation(bob, "conv-1") is None
        bob_items, _ = await service.list_conversations(bob, limit=10)
        assert bob_items == [], (
            "user B's list_conversations included user A's conversation — "
            "the isolation wall is broken (missing/neutered user_sub filter)"
        )

        alice_read = await service.get_conversation(alice, "conv-1")
        assert alice_read is not None
        assert alice_read["title"] == "Alice's chat"

    async def test_reset(self, user_context) -> None:
        service = MockConsoleConversationsService()
        await service.upsert_conversation(user_context, "conv-1")
        service.reset()
        assert await service.get_conversation(user_context, "conv-1") is None


class TestMockConsoleConversationsServiceListPagination:
    async def test_list_empty(self, user_context) -> None:
        service = MockConsoleConversationsService()
        items, next_cursor = await service.list_conversations(user_context, limit=10)
        assert items == []
        assert next_cursor is None

    async def test_list_newest_first(self, user_context) -> None:
        service = MockConsoleConversationsService()
        await service.upsert_conversation(user_context, "conv-1")
        await service.upsert_conversation(user_context, "conv-2")
        await service.upsert_conversation(user_context, "conv-3")
        items, _ = await service.list_conversations(user_context, limit=10)
        assert [i["conversation_id"] for i in items] == ["conv-3", "conv-2", "conv-1"]

    async def test_list_excludes_deleted(self, user_context) -> None:
        service = MockConsoleConversationsService()
        await service.upsert_conversation(user_context, "conv-1")
        await service.upsert_conversation(user_context, "conv-2")
        await service.delete_conversation(user_context, "conv-1")
        items, _ = await service.list_conversations(user_context, limit=10)
        assert [i["conversation_id"] for i in items] == ["conv-2"]

    async def test_list_pagination_across_pages(self, user_context) -> None:
        service = MockConsoleConversationsService()
        for i in range(5):
            await service.upsert_conversation(user_context, f"conv-{i}")

        page1, cursor1 = await service.list_conversations(user_context, limit=2)
        assert len(page1) == 2
        assert cursor1 is not None

        page2, cursor2 = await service.list_conversations(
            user_context, cursor=cursor1, limit=2
        )
        assert len(page2) == 2
        assert cursor2 is not None

        page3, cursor3 = await service.list_conversations(
            user_context, cursor=cursor2, limit=2
        )
        assert len(page3) == 1
        assert cursor3 is None

        all_ids = [i["conversation_id"] for i in page1 + page2 + page3]
        assert len(set(all_ids)) == 5, "pagination must not duplicate or skip rows"

    async def test_list_last_page_has_no_next_cursor(self, user_context) -> None:
        service = MockConsoleConversationsService()
        await service.upsert_conversation(user_context, "conv-1")
        items, next_cursor = await service.list_conversations(user_context, limit=10)
        assert len(items) == 1
        assert next_cursor is None


class TestMockConsoleConversationsServiceMessages:
    async def test_get_messages_empty(self, user_context) -> None:
        service = MockConsoleConversationsService()
        assert await service.get_messages(user_context, "conv-1") == []

    async def test_upsert_then_get_messages_round_trips(self, user_context) -> None:
        service = MockConsoleConversationsService()
        created = await service.upsert_message(
            user_context,
            "conv-1",
            "msg-1",
            sender="user",
            text="hello",
            is_created_by_user=True,
        )
        assert created["message_id"] == "msg-1"
        assert created["text"] == "hello"
        assert created["parent_message_id"] is None

        rows = await service.get_messages(user_context, "conv-1")
        assert len(rows) == 1
        assert rows[0]["text"] == "hello"

    async def test_upsert_message_is_idempotent(self, user_context) -> None:
        service = MockConsoleConversationsService()
        await service.upsert_message(
            user_context,
            "conv-1",
            "msg-1",
            sender="user",
            text="v1",
            is_created_by_user=True,
        )
        await service.upsert_message(
            user_context,
            "conv-1",
            "msg-1",
            sender="user",
            text="v2",
            is_created_by_user=True,
        )
        rows = await service.get_messages(user_context, "conv-1")
        assert len(rows) == 1
        assert rows[0]["text"] == "v2"

    async def test_message_tree_ordering_is_chronological(self, user_context) -> None:
        service = MockConsoleConversationsService()
        root = await service.upsert_message(
            user_context,
            "conv-1",
            "msg-root",
            sender="user",
            text="root",
            is_created_by_user=True,
        )
        await service.upsert_message(
            user_context,
            "conv-1",
            "msg-child",
            sender="assistant",
            text="child",
            is_created_by_user=False,
            parent_message_id=root["message_id"],
        )
        rows = await service.get_messages(user_context, "conv-1")
        assert [r["message_id"] for r in rows] == ["msg-root", "msg-child"]
        assert rows[1]["parent_message_id"] == "msg-root"

    async def test_upsert_message_bumps_conversation_updated_at(
        self, user_context
    ) -> None:
        service = MockConsoleConversationsService()
        convo = await service.upsert_conversation(user_context, "conv-1")
        # Force a distinct timestamp for the message write.
        time.sleep(0.001)
        await service.upsert_message(
            user_context,
            "conv-1",
            "msg-1",
            sender="user",
            text="hi",
            is_created_by_user=True,
        )
        refreshed = await service.get_conversation(user_context, "conv-1")
        assert refreshed is not None
        assert refreshed["updated_at_ms"] >= convo["updated_at_ms"]

    async def test_edit_message(self, user_context) -> None:
        service = MockConsoleConversationsService()
        await service.upsert_message(
            user_context,
            "conv-1",
            "msg-1",
            sender="user",
            text="original",
            is_created_by_user=True,
        )
        edited = await service.edit_message(
            user_context, "conv-1", "msg-1", text="edited"
        )
        assert edited is not None
        assert edited["text"] == "edited"

    async def test_edit_message_missing_returns_none(self, user_context) -> None:
        service = MockConsoleConversationsService()
        assert (
            await service.edit_message(user_context, "conv-1", "nope", text="x") is None
        )

    async def test_delete_message(self, user_context) -> None:
        service = MockConsoleConversationsService()
        await service.upsert_message(
            user_context,
            "conv-1",
            "msg-1",
            sender="user",
            text="hi",
            is_created_by_user=True,
        )
        assert await service.delete_message(user_context, "conv-1", "msg-1") is True
        assert await service.get_messages(user_context, "conv-1") == []

    async def test_delete_missing_message_returns_false(self, user_context) -> None:
        service = MockConsoleConversationsService()
        assert await service.delete_message(user_context, "conv-1", "nope") is False

    async def test_isolates_messages_by_user(self, user_context) -> None:
        """Non-vacuity guard: neuter the ``user_sub`` filter in
        ``get_messages`` and Bob starts seeing Alice's messages."""
        service = MockConsoleConversationsService()
        alice = replace(user_context, user_id="user-alice-msg", is_admin=False)
        bob = replace(user_context, user_id="user-bob-msg", is_admin=False)
        await service.upsert_message(
            alice,
            "conv-1",
            "msg-1",
            sender="user",
            text="alice's secret",
            is_created_by_user=True,
        )

        bob_rows = await service.get_messages(bob, "conv-1")
        assert bob_rows == [], (
            "user B's get_messages included user A's message — the "
            "isolation wall is broken (missing/neutered user_sub filter)"
        )
        alice_rows = await service.get_messages(alice, "conv-1")
        assert len(alice_rows) == 1
        assert alice_rows[0]["text"] == "alice's secret"

    async def test_cannot_edit_another_users_message(self, user_context) -> None:
        service = MockConsoleConversationsService()
        alice = replace(user_context, user_id="user-alice-edit", is_admin=False)
        bob = replace(user_context, user_id="user-bob-edit", is_admin=False)
        await service.upsert_message(
            alice,
            "conv-1",
            "msg-1",
            sender="user",
            text="alice's message",
            is_created_by_user=True,
        )
        result = await service.edit_message(
            bob, "conv-1", "msg-1", text="hijacked by bob"
        )
        assert result is None, "user B edited user A's message — isolation broken"
        alice_rows = await service.get_messages(alice, "conv-1")
        assert alice_rows[0]["text"] == "alice's message"

    async def test_cannot_delete_another_users_message(self, user_context) -> None:
        service = MockConsoleConversationsService()
        alice = replace(user_context, user_id="user-alice-del", is_admin=False)
        bob = replace(user_context, user_id="user-bob-del", is_admin=False)
        await service.upsert_message(
            alice,
            "conv-1",
            "msg-1",
            sender="user",
            text="alice's message",
            is_created_by_user=True,
        )
        deleted = await service.delete_message(bob, "conv-1", "msg-1")
        assert deleted is False, "user B deleted user A's message — isolation broken"
        assert len(await service.get_messages(alice, "conv-1")) == 1


# ── PostgresConsoleConversationsService (aiosqlite) ───────────────────────


@pytest_asyncio.fixture
async def pg_factory():
    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    return factory


@pytest.fixture
def service(pg_factory) -> PostgresConsoleConversationsService:
    return PostgresConsoleConversationsService(
        session_factory=pg_factory.get_session_factory(),
    )


class TestPostgresConsoleConversationsServiceConversations:
    def test_abstract_interface(self, service) -> None:
        assert isinstance(service, ConsoleConversationsService)

    async def test_get_missing_returns_none(self, service, user_context) -> None:
        assert await service.get_conversation(user_context, "nope") is None

    async def test_upsert_then_get_round_trips(self, service, user_context) -> None:
        created = await service.upsert_conversation(
            user_context, "conv-1", title="Hello", model="gpt-x"
        )
        assert created["title"] == "Hello"
        fetched = await service.get_conversation(user_context, "conv-1")
        assert fetched is not None
        assert fetched["model"] == "gpt-x"

    async def test_upsert_is_idempotent(self, service, user_context) -> None:
        first = await service.upsert_conversation(user_context, "conv-1", title="A")
        second = await service.upsert_conversation(user_context, "conv-1", title="B")
        assert first["created_at_ms"] == second["created_at_ms"]
        items, _ = await service.list_conversations(user_context, limit=10)
        assert len(items) == 1
        assert items[0]["title"] == "B"

    async def test_update_title_missing_returns_none(
        self, service, user_context
    ) -> None:
        assert (
            await service.update_conversation_title(user_context, "nope", "x") is None
        )

    async def test_delete_conversation_removes_it_and_its_messages(
        self, service, user_context
    ) -> None:
        await service.upsert_conversation(user_context, "conv-1")
        await service.upsert_message(
            user_context,
            "conv-1",
            "msg-1",
            sender="user",
            text="hi",
            is_created_by_user=True,
        )
        assert await service.delete_conversation(user_context, "conv-1") is True
        assert await service.get_conversation(user_context, "conv-1") is None
        assert await service.get_messages(user_context, "conv-1") == []

    async def test_delete_missing_conversation_returns_false(
        self, service, user_context
    ) -> None:
        assert await service.delete_conversation(user_context, "nope") is False

    async def test_cross_user_isolation_denies_read(
        self, service, user_context
    ) -> None:
        """Acceptance guard — RLS: user B cannot read user A's
        conversation. Neuter the explicit ``user_sub`` filter in
        ``PostgresConsoleConversationsService.get_conversation`` and this
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
            await service.upsert_conversation(
                alice, "secret-conv", title="alice's private chat"
            )
        finally:
            set_current_user_id(None)

        set_current_user_id(bob.user_id)
        try:
            bob_read = await service.get_conversation(bob, "secret-conv")
            bob_list, _ = await service.list_conversations(bob, limit=10)
        finally:
            set_current_user_id(None)

        assert bob_read is None, (
            "user B read user A's conversation — the isolation wall is "
            "broken (missing/neutered user_sub filter)"
        )
        assert bob_list == [], (
            "user B's list_conversations included user A's conversation"
        )

        set_current_user_id(alice.user_id)
        try:
            alice_read = await service.get_conversation(alice, "secret-conv")
        finally:
            set_current_user_id(None)
        assert alice_read is not None
        assert alice_read["title"] == "alice's private chat"

    async def test_upsert_conversation_cannot_overwrite_another_users_row(
        self, service, user_context
    ) -> None:
        """Non-vacuity guard: even with an IDENTICAL conversation_id,
        bob's upsert must create/update HIS OWN row, never alice's —
        neuter the explicit ``user_sub`` filter in the upsert's existence
        check and this test goes RED (bob's title would overwrite
        alice's row instead of creating a second, isolated one)."""
        alice = replace(user_context, user_id="user-alice-up", is_admin=False)
        bob = replace(user_context, user_id="user-bob-up", is_admin=False)

        await service.upsert_conversation(alice, "shared-id", title="alice's title")
        await service.upsert_conversation(bob, "shared-id", title="bob's title")

        alice_row = await service.get_conversation(alice, "shared-id")
        bob_row = await service.get_conversation(bob, "shared-id")
        assert alice_row is not None
        assert bob_row is not None
        assert alice_row["title"] == "alice's title", (
            "bob's upsert overwrote alice's conversation — the user_sub "
            "isolation filter in the upsert existence-check is missing/neutered"
        )
        assert bob_row["title"] == "bob's title"

    async def test_write_failure_raises_runtime_error(
        self, service, user_context, monkeypatch
    ) -> None:
        async def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "sqlalchemy.ext.asyncio.AsyncSession.commit",
            _boom,
        )
        with pytest.raises(RuntimeError, match="upsert_conversation.*failed"):
            await service.upsert_conversation(user_context, "conv-1")


class TestPostgresConsoleConversationsServiceListPagination:
    async def test_list_newest_first(self, service, user_context) -> None:
        await service.upsert_conversation(user_context, "conv-1")
        await service.upsert_conversation(user_context, "conv-2")
        await service.upsert_conversation(user_context, "conv-3")
        items, _ = await service.list_conversations(user_context, limit=10)
        assert [i["conversation_id"] for i in items] == ["conv-3", "conv-2", "conv-1"]

    async def test_list_pagination_across_pages(self, service, user_context) -> None:
        for i in range(5):
            await service.upsert_conversation(user_context, f"conv-{i}")

        page1, cursor1 = await service.list_conversations(user_context, limit=2)
        assert len(page1) == 2
        page2, cursor2 = await service.list_conversations(
            user_context, cursor=cursor1, limit=2
        )
        assert len(page2) == 2
        page3, cursor3 = await service.list_conversations(
            user_context, cursor=cursor2, limit=2
        )
        assert len(page3) == 1
        assert cursor3 is None

        all_ids = [i["conversation_id"] for i in page1 + page2 + page3]
        assert len(set(all_ids)) == 5

    async def test_invalid_cursor_raises_value_error(
        self, service, user_context
    ) -> None:
        with pytest.raises(ValueError, match="invalid cursor"):
            await service.list_conversations(user_context, cursor="garbage!!")


class TestPostgresConsoleConversationsServiceMessages:
    async def test_upsert_then_get_messages_round_trips(
        self, service, user_context
    ) -> None:
        await service.upsert_message(
            user_context,
            "conv-1",
            "msg-1",
            sender="user",
            text="hello",
            is_created_by_user=True,
        )
        rows = await service.get_messages(user_context, "conv-1")
        assert len(rows) == 1
        assert rows[0]["text"] == "hello"

    async def test_message_tree_ordering_is_chronological(
        self, service, user_context
    ) -> None:
        await service.upsert_message(
            user_context,
            "conv-1",
            "msg-root",
            sender="user",
            text="root",
            is_created_by_user=True,
        )
        await service.upsert_message(
            user_context,
            "conv-1",
            "msg-child",
            sender="assistant",
            text="child",
            is_created_by_user=False,
            parent_message_id="msg-root",
        )
        rows = await service.get_messages(user_context, "conv-1")
        assert [r["message_id"] for r in rows] == ["msg-root", "msg-child"]

    async def test_edit_message_missing_returns_none(
        self, service, user_context
    ) -> None:
        assert (
            await service.edit_message(user_context, "conv-1", "nope", text="x") is None
        )

    async def test_delete_missing_message_returns_false(
        self, service, user_context
    ) -> None:
        assert await service.delete_message(user_context, "conv-1", "nope") is False

    async def test_cross_user_isolation_denies_message_read(
        self, service, user_context
    ) -> None:
        """Acceptance guard — RLS: user B cannot read user A's messages.
        Neuter the explicit ``user_sub`` filter in
        ``PostgresConsoleConversationsService.get_messages`` and this
        test goes RED."""
        alice = replace(user_context, user_id="user-alice-msg-rls", is_admin=False)
        bob = replace(user_context, user_id="user-bob-msg-rls", is_admin=False)

        set_current_user_id(alice.user_id)
        try:
            await service.upsert_message(
                alice,
                "conv-1",
                "secret-msg",
                sender="user",
                text="alice's secret message",
                is_created_by_user=True,
            )
        finally:
            set_current_user_id(None)

        set_current_user_id(bob.user_id)
        try:
            bob_rows = await service.get_messages(bob, "conv-1")
        finally:
            set_current_user_id(None)

        assert bob_rows == [], (
            "user B read user A's messages — the isolation wall is "
            "broken (missing/neutered user_sub filter)"
        )

        set_current_user_id(alice.user_id)
        try:
            alice_rows = await service.get_messages(alice, "conv-1")
        finally:
            set_current_user_id(None)
        assert len(alice_rows) == 1
        assert alice_rows[0]["text"] == "alice's secret message"

    async def test_cross_user_cannot_edit_or_delete_message(
        self, service, user_context
    ) -> None:
        """Non-vacuity guard: neuter the explicit ``user_sub`` filter in
        ``edit_message``/``delete_message`` and bob's calls would
        succeed against alice's message."""
        alice = replace(user_context, user_id="user-alice-med", is_admin=False)
        bob = replace(user_context, user_id="user-bob-med", is_admin=False)

        set_current_user_id(alice.user_id)
        try:
            await service.upsert_message(
                alice,
                "conv-1",
                "msg-1",
                sender="user",
                text="alice's message",
                is_created_by_user=True,
            )
        finally:
            set_current_user_id(None)

        set_current_user_id(bob.user_id)
        try:
            edit_result = await service.edit_message(
                bob, "conv-1", "msg-1", text="hijacked"
            )
            delete_result = await service.delete_message(bob, "conv-1", "msg-1")
        finally:
            set_current_user_id(None)

        assert edit_result is None, "user B edited user A's message — isolation broken"
        assert delete_result is False, (
            "user B deleted user A's message — isolation broken"
        )

        set_current_user_id(alice.user_id)
        try:
            alice_rows = await service.get_messages(alice, "conv-1")
        finally:
            set_current_user_id(None)
        assert len(alice_rows) == 1
        assert alice_rows[0]["text"] == "alice's message"
