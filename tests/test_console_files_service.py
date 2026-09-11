"""Tests for the console-files service (Files-metadata domain,
MongoDB-elimination EPIC).

Mirrors ``test_console_chat_projects_service.py``'s structure EXACTLY
(the spec's instruction), scaled to the files field set
(filename/type/bytes/object_key/width/height/context/usage/embedded/
temp_file_id instead of name/description), plus the extra
batch-get-by-ids read shape this domain adds. Mock-service interface
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
from audittrace.services.console_files import (
    ConsoleFilesService,
    MockConsoleFilesService,
    PostgresConsoleFilesService,
    _decode_cursor,
    _encode_cursor,
)

# ── cursor helpers ───────────────────────────────────────────────────────


class TestCursorCodec:
    def test_round_trips(self) -> None:
        cursor = _encode_cursor(updated_at_ms=12345, file_id="file-a")
        assert _decode_cursor(cursor) == (12345, "file-a")

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

        garbage = base64.urlsafe_b64encode(b"not-a-number:file-1").decode("ascii")
        with pytest.raises(ValueError, match="invalid cursor"):
            _decode_cursor(garbage)


# ── MockConsoleFilesService ────────────────────────────────────────────────


class TestMockConsoleFilesService:
    def test_abstract_interface(self) -> None:
        assert isinstance(MockConsoleFilesService(), ConsoleFilesService)

    async def test_get_missing_returns_none(self, user_context) -> None:
        service = MockConsoleFilesService()
        assert await service.get_file(user_context, "nope") is None

    async def test_upsert_then_get_round_trips(self, user_context) -> None:
        service = MockConsoleFilesService()
        created = await service.upsert_file(
            user_context,
            "file-1",
            filename="report.pdf",
            type="application/pdf",
            bytes=1024,
        )
        assert created["file_id"] == "file-1"
        assert created["filename"] == "report.pdf"
        assert created["type"] == "application/pdf"
        assert created["bytes"] == 1024
        assert created["deleted_at_ms"] is None

        fetched = await service.get_file(user_context, "file-1")
        assert fetched is not None
        assert fetched["filename"] == "report.pdf"

    async def test_upsert_defaults(self, user_context) -> None:
        service = MockConsoleFilesService()
        created = await service.upsert_file(
            user_context, "file-1", filename="bare.txt", type="text/plain"
        )
        assert created["bytes"] == 0
        assert created["object_key"] is None
        assert created["width"] is None
        assert created["height"] is None
        assert created["context"] is None
        assert created["usage"] == {}
        assert created["embedded"] is False
        assert created["temp_file_id"] is None
        assert created["metadata"] == {}

    async def test_upsert_is_idempotent_by_file_id(self, user_context) -> None:
        """Calling upsert twice with the same file_id updates the SAME
        row rather than creating a duplicate."""
        service = MockConsoleFilesService()
        first = await service.upsert_file(
            user_context, "file-1", filename="a.txt", type="text/plain"
        )
        second = await service.upsert_file(
            user_context, "file-1", filename="b.txt", type="text/plain"
        )
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["filename"] == "b.txt"

        items, _ = await service.list_files(user_context, limit=10)
        assert len(items) == 1

    async def test_upsert_updates_every_optional_field_on_existing_row(
        self, user_context
    ) -> None:
        """The upsert's update-existing branch updates EACH optional
        field independently — covers every ``if <field> is not None``
        branch on the second call."""
        service = MockConsoleFilesService()
        await service.upsert_file(
            user_context, "file-1", filename="a.txt", type="text/plain"
        )
        updated = await service.upsert_file(
            user_context,
            "file-1",
            filename="a.txt",
            type="text/plain",
            object_key="uploads/a.txt",
            width=10,
            height=20,
            context="avatar",
            usage={"k": "v"},
            embedded=True,
            temp_file_id="temp-1",
            metadata={"m": "n"},
        )
        assert updated["object_key"] == "uploads/a.txt"
        assert updated["width"] == 10
        assert updated["height"] == 20
        assert updated["context"] == "avatar"
        assert updated["usage"] == {"k": "v"}
        assert updated["embedded"] is True
        assert updated["temp_file_id"] == "temp-1"
        assert updated["metadata"] == {"m": "n"}

    async def test_delete_file(self, user_context) -> None:
        service = MockConsoleFilesService()
        await service.upsert_file(
            user_context, "file-1", filename="x.txt", type="text/plain"
        )
        assert await service.delete_file(user_context, "file-1") is True
        assert await service.get_file(user_context, "file-1") is None

    async def test_delete_missing_file_returns_false(self, user_context) -> None:
        service = MockConsoleFilesService()
        assert await service.delete_file(user_context, "nope") is False

    async def test_isolates_files_by_user(self, user_context) -> None:
        """Non-vacuity guard: neuter the ``user_sub`` filter in
        ``get_file``/``list_files`` and Bob starts seeing Alice's
        files."""
        service = MockConsoleFilesService()
        alice = replace(user_context, user_id="user-alice", is_admin=False)
        bob = replace(user_context, user_id="user-bob", is_admin=False)
        await service.upsert_file(
            alice, "file-1", filename="alice.txt", type="text/plain"
        )

        assert await service.get_file(bob, "file-1") is None
        bob_items, _ = await service.list_files(bob, limit=10)
        assert bob_items == [], (
            "user B's list_files included user A's file — the isolation "
            "wall is broken (missing/neutered user_sub filter)"
        )

        alice_read = await service.get_file(alice, "file-1")
        assert alice_read is not None
        assert alice_read["filename"] == "alice.txt"

    async def test_reset(self, user_context) -> None:
        service = MockConsoleFilesService()
        await service.upsert_file(
            user_context, "file-1", filename="x.txt", type="text/plain"
        )
        service.reset()
        assert await service.get_file(user_context, "file-1") is None


class TestMockConsoleFilesServiceListPagination:
    async def test_list_empty(self, user_context) -> None:
        service = MockConsoleFilesService()
        items, next_cursor = await service.list_files(user_context, limit=10)
        assert items == []
        assert next_cursor is None

    async def test_list_newest_first(self, user_context) -> None:
        service = MockConsoleFilesService()
        await service.upsert_file(
            user_context, "file-1", filename="f1", type="text/plain"
        )
        await service.upsert_file(
            user_context, "file-2", filename="f2", type="text/plain"
        )
        await service.upsert_file(
            user_context, "file-3", filename="f3", type="text/plain"
        )
        items, _ = await service.list_files(user_context, limit=10)
        assert [i["file_id"] for i in items] == ["file-3", "file-2", "file-1"]

    async def test_list_excludes_deleted(self, user_context) -> None:
        service = MockConsoleFilesService()
        await service.upsert_file(
            user_context, "file-1", filename="f1", type="text/plain"
        )
        await service.upsert_file(
            user_context, "file-2", filename="f2", type="text/plain"
        )
        await service.delete_file(user_context, "file-1")
        items, _ = await service.list_files(user_context, limit=10)
        assert [i["file_id"] for i in items] == ["file-2"]

    async def test_list_pagination_across_pages(self, user_context) -> None:
        service = MockConsoleFilesService()
        for i in range(5):
            await service.upsert_file(
                user_context, f"file-{i}", filename="p", type="text/plain"
            )

        page1, cursor1 = await service.list_files(user_context, limit=2)
        assert len(page1) == 2
        assert cursor1 is not None

        page2, cursor2 = await service.list_files(user_context, cursor=cursor1, limit=2)
        assert len(page2) == 2
        assert cursor2 is not None

        page3, cursor3 = await service.list_files(user_context, cursor=cursor2, limit=2)
        assert len(page3) == 1
        assert cursor3 is None

        all_ids = [i["file_id"] for i in page1 + page2 + page3]
        assert len(set(all_ids)) == 5, "pagination must not duplicate or skip rows"

    async def test_list_last_page_has_no_next_cursor(self, user_context) -> None:
        service = MockConsoleFilesService()
        await service.upsert_file(
            user_context, "file-1", filename="f1", type="text/plain"
        )
        items, next_cursor = await service.list_files(user_context, limit=10)
        assert len(items) == 1
        assert next_cursor is None

    async def test_invalid_cursor_raises_value_error(self, user_context) -> None:
        service = MockConsoleFilesService()
        with pytest.raises(ValueError, match="invalid cursor"):
            await service.list_files(user_context, cursor="garbage!!")


class TestMockConsoleFilesServiceBatchGet:
    async def test_batch_get_preserves_request_order(self, user_context) -> None:
        service = MockConsoleFilesService()
        for i in range(3):
            await service.upsert_file(
                user_context, f"file-{i}", filename="p", type="text/plain"
            )
        results = await service.batch_get_files(
            user_context, ["file-2", "file-0", "file-1"]
        )
        assert [r["file_id"] for r in results] == ["file-2", "file-0", "file-1"]

    async def test_batch_get_omits_missing_ids(self, user_context) -> None:
        service = MockConsoleFilesService()
        await service.upsert_file(
            user_context, "file-1", filename="p", type="text/plain"
        )
        results = await service.batch_get_files(
            user_context, ["file-1", "does-not-exist"]
        )
        assert [r["file_id"] for r in results] == ["file-1"]

    async def test_batch_get_empty_input_returns_empty(self, user_context) -> None:
        service = MockConsoleFilesService()
        assert await service.batch_get_files(user_context, []) == []

    async def test_batch_get_isolates_by_user(self, user_context) -> None:
        """Non-vacuity guard: neuter the ``user_sub`` comparison in
        ``_find`` and Bob's batch-get starts returning Alice's files."""
        service = MockConsoleFilesService()
        alice = replace(user_context, user_id="user-alice", is_admin=False)
        bob = replace(user_context, user_id="user-bob", is_admin=False)
        await service.upsert_file(
            alice, "alice-file", filename="secret", type="text/plain"
        )
        results = await service.batch_get_files(bob, ["alice-file"])
        assert results == [], (
            "bob's batch-get returned alice's file — the isolation wall "
            "is broken (missing/neutered user_sub filter)"
        )


# ── PostgresConsoleFilesService (aiosqlite) ────────────────────────────────


@pytest_asyncio.fixture
async def pg_factory():
    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    return factory


@pytest.fixture
def service(pg_factory) -> PostgresConsoleFilesService:
    return PostgresConsoleFilesService(
        session_factory=pg_factory.get_session_factory(),
    )


class TestPostgresConsoleFilesService:
    def test_abstract_interface(self, service) -> None:
        assert isinstance(service, ConsoleFilesService)

    async def test_get_missing_returns_none(self, service, user_context) -> None:
        assert await service.get_file(user_context, "nope") is None

    async def test_upsert_then_get_round_trips(self, service, user_context) -> None:
        created = await service.upsert_file(
            user_context,
            "file-1",
            filename="report.pdf",
            type="application/pdf",
            bytes=1024,
        )
        assert created["file_id"] == "file-1"
        assert created["filename"] == "report.pdf"
        assert created["bytes"] == 1024

        fetched = await service.get_file(user_context, "file-1")
        assert fetched is not None
        assert fetched["filename"] == "report.pdf"

    async def test_upsert_defaults(self, service, user_context) -> None:
        created = await service.upsert_file(
            user_context, "file-1", filename="bare.txt", type="text/plain"
        )
        assert created["bytes"] == 0
        assert created["embedded"] is False

    async def test_upsert_is_idempotent_by_file_id(self, service, user_context) -> None:
        first = await service.upsert_file(
            user_context, "file-1", filename="a.txt", type="text/plain"
        )
        second = await service.upsert_file(
            user_context, "file-1", filename="b.txt", type="text/plain"
        )
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["filename"] == "b.txt"

        items, _ = await service.list_files(user_context, limit=10)
        assert len(items) == 1

    async def test_upsert_updates_every_optional_field_on_existing_row(
        self, service, user_context
    ) -> None:
        await service.upsert_file(
            user_context, "file-1", filename="a.txt", type="text/plain"
        )
        updated = await service.upsert_file(
            user_context,
            "file-1",
            filename="a.txt",
            type="text/plain",
            object_key="uploads/a.txt",
            width=10,
            height=20,
            context="avatar",
            usage={"k": "v"},
            embedded=True,
            temp_file_id="temp-1",
            metadata={"m": "n"},
        )
        assert updated["object_key"] == "uploads/a.txt"
        assert updated["width"] == 10
        assert updated["height"] == 20
        assert updated["context"] == "avatar"
        assert updated["usage"] == {"k": "v"}
        assert updated["embedded"] is True
        assert updated["temp_file_id"] == "temp-1"
        assert updated["metadata"] == {"m": "n"}

    async def test_delete_missing_file_returns_false(
        self, service, user_context
    ) -> None:
        assert await service.delete_file(user_context, "nope") is False

    async def test_delete_file_removes_it(self, service, user_context) -> None:
        await service.upsert_file(
            user_context, "file-1", filename="x.txt", type="text/plain"
        )
        assert await service.delete_file(user_context, "file-1") is True
        assert await service.get_file(user_context, "file-1") is None

    async def test_cross_user_isolation_denies_read(
        self, service, user_context
    ) -> None:
        """Acceptance guard — RLS: user B cannot read user A's file.
        Neuter the explicit ``user_sub`` filter in
        ``PostgresConsoleFilesService.get_file`` and this test goes RED.

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
            await service.upsert_file(
                alice,
                "secret-file",
                filename="alice-secret.pdf",
                type="application/pdf",
            )
        finally:
            set_current_user_id(None)

        set_current_user_id(bob.user_id)
        try:
            bob_read = await service.get_file(bob, "secret-file")
            bob_list, _ = await service.list_files(bob, limit=10)
            bob_batch = await service.batch_get_files(bob, ["secret-file"])
        finally:
            set_current_user_id(None)

        assert bob_read is None, (
            "user B read user A's file — the isolation wall is broken "
            "(missing/neutered user_sub filter)"
        )
        assert bob_list == [], "user B's list_files included user A's file"
        assert bob_batch == [], "user B's batch_get_files included user A's file"

        set_current_user_id(alice.user_id)
        try:
            alice_read = await service.get_file(alice, "secret-file")
        finally:
            set_current_user_id(None)
        assert alice_read is not None
        assert alice_read["filename"] == "alice-secret.pdf"

    async def test_upsert_file_cannot_overwrite_another_users_row(
        self, service, user_context
    ) -> None:
        """Non-vacuity guard: even with an IDENTICAL file_id, bob's
        upsert must create/update HIS OWN row, never alice's — neuter
        the explicit ``user_sub`` filter in the upsert's existence check
        and this test goes RED (bob's filename would overwrite alice's
        row instead of creating a second, isolated one)."""
        alice = replace(user_context, user_id="user-alice-up", is_admin=False)
        bob = replace(user_context, user_id="user-bob-up", is_admin=False)

        await service.upsert_file(
            alice, "shared-id", filename="alice.txt", type="text/plain"
        )
        await service.upsert_file(
            bob, "shared-id", filename="bob.txt", type="text/plain"
        )

        alice_row = await service.get_file(alice, "shared-id")
        bob_row = await service.get_file(bob, "shared-id")
        assert alice_row is not None
        assert bob_row is not None
        assert alice_row["filename"] == "alice.txt", (
            "bob's upsert overwrote alice's file — the user_sub "
            "isolation filter in the upsert existence-check is missing/neutered"
        )
        assert bob_row["filename"] == "bob.txt"

    async def test_cross_user_delete_denied(self, service, user_context) -> None:
        alice = replace(user_context, user_id="user-alice-del", is_admin=False)
        bob = replace(user_context, user_id="user-bob-del", is_admin=False)
        await service.upsert_file(
            alice, "alice-file", filename="mine.txt", type="text/plain"
        )

        deleted = await service.delete_file(bob, "alice-file")
        assert deleted is False, "user B deleted user A's file — isolation broken"
        assert (await service.get_file(alice, "alice-file")) is not None

    async def test_write_failure_raises_runtime_error(
        self, service, user_context, monkeypatch
    ) -> None:
        async def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "sqlalchemy.ext.asyncio.AsyncSession.commit",
            _boom,
        )
        with pytest.raises(RuntimeError, match="upsert_file.*failed"):
            await service.upsert_file(
                user_context, "file-1", filename="x.txt", type="text/plain"
            )


class TestPostgresConsoleFilesServiceListPagination:
    async def test_list_newest_first(self, service, user_context) -> None:
        await service.upsert_file(
            user_context, "file-1", filename="f1", type="text/plain"
        )
        await service.upsert_file(
            user_context, "file-2", filename="f2", type="text/plain"
        )
        await service.upsert_file(
            user_context, "file-3", filename="f3", type="text/plain"
        )
        items, _ = await service.list_files(user_context, limit=10)
        assert [i["file_id"] for i in items] == ["file-3", "file-2", "file-1"]

    async def test_list_pagination_across_pages(self, service, user_context) -> None:
        for i in range(5):
            await service.upsert_file(
                user_context, f"file-{i}", filename="p", type="text/plain"
            )

        page1, cursor1 = await service.list_files(user_context, limit=2)
        assert len(page1) == 2
        page2, cursor2 = await service.list_files(user_context, cursor=cursor1, limit=2)
        assert len(page2) == 2
        page3, cursor3 = await service.list_files(user_context, cursor=cursor2, limit=2)
        assert len(page3) == 1
        assert cursor3 is None

        all_ids = [i["file_id"] for i in page1 + page2 + page3]
        assert len(set(all_ids)) == 5

    async def test_invalid_cursor_raises_value_error(
        self, service, user_context
    ) -> None:
        with pytest.raises(ValueError, match="invalid cursor"):
            await service.list_files(user_context, cursor="garbage!!")


class TestPostgresConsoleFilesServiceBatchGet:
    async def test_batch_get_preserves_request_order(
        self, service, user_context
    ) -> None:
        for i in range(3):
            await service.upsert_file(
                user_context, f"file-{i}", filename="p", type="text/plain"
            )
        results = await service.batch_get_files(
            user_context, ["file-2", "file-0", "file-1"]
        )
        assert [r["file_id"] for r in results] == ["file-2", "file-0", "file-1"]

    async def test_batch_get_omits_missing_ids(self, service, user_context) -> None:
        await service.upsert_file(
            user_context, "file-1", filename="p", type="text/plain"
        )
        results = await service.batch_get_files(
            user_context, ["file-1", "does-not-exist"]
        )
        assert [r["file_id"] for r in results] == ["file-1"]

    async def test_batch_get_empty_input_returns_empty(
        self, service, user_context
    ) -> None:
        assert await service.batch_get_files(user_context, []) == []

    async def test_batch_get_excludes_deleted(self, service, user_context) -> None:
        await service.upsert_file(
            user_context, "file-1", filename="p", type="text/plain"
        )
        await service.delete_file(user_context, "file-1")
        results = await service.batch_get_files(user_context, ["file-1"])
        assert results == []
