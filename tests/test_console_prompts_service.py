"""Tests for the console-prompts service (Mongo-repl WU-prompts,
MongoDB-elimination EPIC).

Mirrors ``test_console_conversations_service.py``'s structure (the
spec's instruction), scaled to the group+versions shape: Mock-service
interface tests, then a Postgres-backed suite via
``InMemoryPostgresFactory`` (aiosqlite, no real PostgreSQL required).
The cross-user isolation tests bracket their assertions with
``set_current_user_id`` per ``feedback_unit_tests_miss_rls`` — RLS
itself is a no-op on SQLite, so the guard under test is the service's
own explicit ``.filter(user_sub == ...)`` clause, not the (here inert)
Postgres GUC. Additional COMPLETENESS coverage (the WU-2 lesson):
``upsert_version``/``set_production`` cross-GROUP and cross-USER
hijack attempts must both be denied.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import pytest_asyncio

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.db.rls import set_current_user_id
from audittrace.services.console_prompts import (
    ConsolePromptsService,
    MockConsolePromptsService,
    PostgresConsolePromptsService,
    _decode_cursor,
    _encode_cursor,
)

# ── cursor helpers ───────────────────────────────────────────────────────


class TestCursorCodec:
    def test_round_trips(self) -> None:
        cursor = _encode_cursor(updated_at_ms=12345, group_id="group-a")
        assert _decode_cursor(cursor) == (12345, "group-a")

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

        garbage = base64.urlsafe_b64encode(b"not-a-number:group-1").decode("ascii")
        with pytest.raises(ValueError, match="invalid cursor"):
            _decode_cursor(garbage)


# ── MockConsolePromptsService ──────────────────────────────────────────────


class TestMockConsolePromptsService:
    def test_abstract_interface(self) -> None:
        assert isinstance(MockConsolePromptsService(), ConsolePromptsService)

    async def test_get_missing_returns_none(self, user_context) -> None:
        service = MockConsolePromptsService()
        assert await service.get_group(user_context, "nope") is None

    async def test_upsert_then_get_round_trips(self, user_context) -> None:
        service = MockConsolePromptsService()
        created = await service.upsert_group(
            user_context,
            "group-1",
            name="My Prompt",
            category="writing",
            oneliner="a one liner",
        )
        assert created["group_id"] == "group-1"
        assert created["name"] == "My Prompt"
        assert created["category"] == "writing"
        assert created["oneliner"] == "a one liner"
        assert created["production_prompt_id"] is None
        assert created["deleted_at_ms"] is None

        fetched = await service.get_group(user_context, "group-1")
        assert fetched is not None
        assert fetched["name"] == "My Prompt"
        assert fetched["versions"] == []

    async def test_upsert_group_defaults(self, user_context) -> None:
        service = MockConsolePromptsService()
        created = await service.upsert_group(user_context, "group-1", name="Bare")
        assert created["category"] == ""
        assert created["oneliner"] == ""
        assert created["command"] is None
        assert created["metadata"] == {}

    async def test_upsert_group_is_idempotent(self, user_context) -> None:
        service = MockConsolePromptsService()
        first = await service.upsert_group(user_context, "group-1", name="A")
        second = await service.upsert_group(user_context, "group-1", name="B")
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["name"] == "B"

        items, _ = await service.list_groups(user_context, limit=10)
        assert len(items) == 1

    async def test_upsert_group_updates_every_optional_field(
        self, user_context
    ) -> None:
        service = MockConsolePromptsService()
        await service.upsert_group(
            user_context, "group-1", name="A", category="c1", oneliner="o1"
        )
        updated = await service.upsert_group(
            user_context,
            "group-1",
            name="A2",
            category="c2",
            oneliner="o2",
            command="cmd",
            metadata={"k": "v"},
        )
        assert updated["name"] == "A2"
        assert updated["category"] == "c2"
        assert updated["oneliner"] == "o2"
        assert updated["command"] == "cmd"
        assert updated["metadata"] == {"k": "v"}

    async def test_delete_group(self, user_context) -> None:
        service = MockConsolePromptsService()
        await service.upsert_group(user_context, "group-1", name="A")
        assert await service.delete_group(user_context, "group-1") is True
        assert await service.get_group(user_context, "group-1") is None

    async def test_delete_missing_group_returns_false(self, user_context) -> None:
        service = MockConsolePromptsService()
        assert await service.delete_group(user_context, "nope") is False

    async def test_delete_group_removes_its_versions(self, user_context) -> None:
        service = MockConsolePromptsService()
        await service.upsert_group(user_context, "group-1", name="A")
        await service.upsert_version(user_context, "group-1", "prompt-1", text="hello")
        await service.delete_group(user_context, "group-1")
        # White-box: the version was HARD-deleted, not merely orphaned
        # under the now-soft-deleted group.
        assert service._versions == []

    async def test_isolates_groups_by_user(self, user_context) -> None:
        """Non-vacuity guard: neuter the ``user_sub`` filter in
        ``get_group``/``list_groups`` and Bob starts seeing Alice's
        prompt groups."""
        service = MockConsolePromptsService()
        alice = replace(user_context, user_id="user-alice", is_admin=False)
        bob = replace(user_context, user_id="user-bob", is_admin=False)
        await service.upsert_group(alice, "group-1", name="Alice's group")

        assert await service.get_group(bob, "group-1") is None
        bob_items, _ = await service.list_groups(bob, limit=10)
        assert bob_items == [], (
            "user B's list_groups included user A's group — the "
            "isolation wall is broken (missing/neutered user_sub filter)"
        )

        alice_read = await service.get_group(alice, "group-1")
        assert alice_read is not None
        assert alice_read["name"] == "Alice's group"

    async def test_reset(self, user_context) -> None:
        service = MockConsolePromptsService()
        await service.upsert_group(user_context, "group-1", name="A")
        service.reset()
        assert await service.get_group(user_context, "group-1") is None


class TestMockConsolePromptsServiceListPagination:
    async def test_list_empty(self, user_context) -> None:
        service = MockConsolePromptsService()
        items, next_cursor = await service.list_groups(user_context, limit=10)
        assert items == []
        assert next_cursor is None

    async def test_list_newest_first(self, user_context) -> None:
        service = MockConsolePromptsService()
        await service.upsert_group(user_context, "group-1", name="A")
        await service.upsert_group(user_context, "group-2", name="B")
        await service.upsert_group(user_context, "group-3", name="C")
        items, _ = await service.list_groups(user_context, limit=10)
        assert [i["group_id"] for i in items] == ["group-3", "group-2", "group-1"]

    async def test_list_excludes_deleted(self, user_context) -> None:
        service = MockConsolePromptsService()
        await service.upsert_group(user_context, "group-1", name="A")
        await service.upsert_group(user_context, "group-2", name="B")
        await service.delete_group(user_context, "group-1")
        items, _ = await service.list_groups(user_context, limit=10)
        assert [i["group_id"] for i in items] == ["group-2"]

    async def test_list_pagination_across_pages(self, user_context) -> None:
        service = MockConsolePromptsService()
        for i in range(5):
            await service.upsert_group(user_context, f"group-{i}", name=f"g{i}")

        page1, cursor1 = await service.list_groups(user_context, limit=2)
        assert len(page1) == 2
        assert cursor1 is not None

        page2, cursor2 = await service.list_groups(
            user_context, cursor=cursor1, limit=2
        )
        assert len(page2) == 2
        assert cursor2 is not None

        page3, cursor3 = await service.list_groups(
            user_context, cursor=cursor2, limit=2
        )
        assert len(page3) == 1
        assert cursor3 is None

        all_ids = [i["group_id"] for i in page1 + page2 + page3]
        assert len(set(all_ids)) == 5, "pagination must not duplicate or skip rows"

    async def test_invalid_cursor_raises_value_error(self, user_context) -> None:
        service = MockConsolePromptsService()
        with pytest.raises(ValueError, match="invalid cursor"):
            await service.list_groups(user_context, cursor="garbage!!")


class TestMockConsolePromptsServiceVersions:
    async def test_upsert_version_requires_owned_group(self, user_context) -> None:
        """Completeness guard (WU-2 lesson): a version can never be
        attached to a group the caller doesn't own — even if the
        group_id string exists (owned by someone else)."""
        service = MockConsolePromptsService()
        alice = replace(user_context, user_id="user-alice-v", is_admin=False)
        bob = replace(user_context, user_id="user-bob-v", is_admin=False)
        await service.upsert_group(alice, "alice-group", name="Alice")

        result = await service.upsert_version(
            bob, "alice-group", "prompt-1", text="hijack attempt"
        )
        assert result is None, (
            "bob attached a version to alice's group — completeness/"
            "isolation guard is broken"
        )

    async def test_upsert_version_missing_group_returns_none(
        self, user_context
    ) -> None:
        service = MockConsolePromptsService()
        result = await service.upsert_version(
            user_context, "no-such-group", "prompt-1", text="hi"
        )
        assert result is None

    async def test_upsert_version_assigns_monotonic_version_numbers(
        self, user_context
    ) -> None:
        service = MockConsolePromptsService()
        await service.upsert_group(user_context, "group-1", name="G")
        v1 = await service.upsert_version(
            user_context, "group-1", "prompt-1", text="v1 text"
        )
        v2 = await service.upsert_version(
            user_context, "group-1", "prompt-2", text="v2 text"
        )
        assert v1["version"] == 1
        assert v2["version"] == 2

    async def test_upsert_version_defaults_type_to_text(self, user_context) -> None:
        service = MockConsolePromptsService()
        await service.upsert_group(user_context, "group-1", name="G")
        v1 = await service.upsert_version(
            user_context, "group-1", "prompt-1", text="v1"
        )
        assert v1["type"] == "text"

    async def test_upsert_version_is_idempotent_by_prompt_id(
        self, user_context
    ) -> None:
        service = MockConsolePromptsService()
        await service.upsert_group(user_context, "group-1", name="G")
        first = await service.upsert_version(
            user_context, "group-1", "prompt-1", text="v1"
        )
        second = await service.upsert_version(
            user_context,
            "group-1",
            "prompt-1",
            text="v1-edited",
            type="chat",
            metadata={"k": "v"},
        )
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["text"] == "v1-edited"
        assert second["type"] == "chat"
        assert second["metadata"] == {"k": "v"}
        assert second["version"] == 1, (
            "editing an existing prompt_id must NOT mint a new version"
        )
        # A third edit with metadata=None must leave the metadata set by
        # the second edit untouched (covers the "existing row,
        # metadata omitted" branch).
        third = await service.upsert_version(
            user_context, "group-1", "prompt-1", text="v1-edited-again"
        )
        assert third["text"] == "v1-edited-again"
        assert third["metadata"] == {"k": "v"}, (
            "metadata=None on an update must leave existing metadata unchanged"
        )

    async def test_get_group_includes_versions_oldest_first(self, user_context) -> None:
        service = MockConsolePromptsService()
        await service.upsert_group(user_context, "group-1", name="G")
        await service.upsert_version(user_context, "group-1", "p1", text="one")
        await service.upsert_version(user_context, "group-1", "p2", text="two")
        fetched = await service.get_group(user_context, "group-1")
        assert fetched is not None
        assert [v["prompt_id"] for v in fetched["versions"]] == ["p1", "p2"]

    async def test_get_group_never_leaks_another_users_versions(
        self, user_context
    ) -> None:
        """Non-vacuity guard: two users independently own a group with
        the SAME group_id string; Bob's fetch must never include
        Alice's versions."""
        service = MockConsolePromptsService()
        alice = replace(user_context, user_id="user-alice-g", is_admin=False)
        bob = replace(user_context, user_id="user-bob-g", is_admin=False)
        await service.upsert_group(alice, "shared-group-id", name="Alice's")
        await service.upsert_group(bob, "shared-group-id", name="Bob's")
        await service.upsert_version(
            alice, "shared-group-id", "alice-p1", text="alice secret"
        )

        bob_fetch = await service.get_group(bob, "shared-group-id")
        assert bob_fetch is not None
        assert bob_fetch["versions"] == [], (
            "bob's group fetch included alice's version — isolation wall is broken"
        )


class TestMockConsolePromptsServiceSetProduction:
    async def test_set_production_happy_path(self, user_context) -> None:
        service = MockConsolePromptsService()
        await service.upsert_group(user_context, "group-1", name="G")
        await service.upsert_version(user_context, "group-1", "p1", text="v1")
        result = await service.set_production(user_context, "group-1", "p1")
        assert result is not None
        assert result["production_prompt_id"] == "p1"

    async def test_set_production_missing_group_returns_none(
        self, user_context
    ) -> None:
        service = MockConsolePromptsService()
        result = await service.set_production(user_context, "no-such-group", "p1")
        assert result is None

    async def test_set_production_missing_version_returns_none(
        self, user_context
    ) -> None:
        service = MockConsolePromptsService()
        await service.upsert_group(user_context, "group-1", name="G")
        result = await service.set_production(user_context, "group-1", "no-such-prompt")
        assert result is None

    async def test_set_production_denies_cross_user_version(self, user_context) -> None:
        """Completeness/isolation guard: bob can never promote alice's
        version, even inside a group bob happens to also own with the
        same group_id."""
        service = MockConsolePromptsService()
        alice = replace(user_context, user_id="user-alice-sp", is_admin=False)
        bob = replace(user_context, user_id="user-bob-sp", is_admin=False)
        await service.upsert_group(alice, "shared-id", name="Alice's")
        await service.upsert_group(bob, "shared-id", name="Bob's")
        await service.upsert_version(alice, "shared-id", "alice-p1", text="alice")

        result = await service.set_production(bob, "shared-id", "alice-p1")
        assert result is None, (
            "bob promoted alice's version to production — completeness/"
            "isolation guard is broken"
        )

    async def test_set_production_denies_cross_group_version(
        self, user_context
    ) -> None:
        """Completeness guard: a version from a DIFFERENT group (same
        owner) can never be promoted into this group's production
        slot."""
        service = MockConsolePromptsService()
        await service.upsert_group(user_context, "group-a", name="A")
        await service.upsert_group(user_context, "group-b", name="B")
        await service.upsert_version(user_context, "group-b", "p-in-b", text="b")

        result = await service.set_production(user_context, "group-a", "p-in-b")
        assert result is None, (
            "a version belonging to a different group was promoted — "
            "completeness guard is broken"
        )


# ── PostgresConsolePromptsService (aiosqlite) ─────────────────────────────


@pytest_asyncio.fixture
async def pg_factory():
    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    return factory


@pytest.fixture
def service(pg_factory) -> PostgresConsolePromptsService:
    return PostgresConsolePromptsService(
        session_factory=pg_factory.get_session_factory(),
    )


class TestPostgresConsolePromptsService:
    def test_abstract_interface(self, service) -> None:
        assert isinstance(service, ConsolePromptsService)

    async def test_get_missing_returns_none(self, service, user_context) -> None:
        assert await service.get_group(user_context, "nope") is None

    async def test_upsert_then_get_round_trips(self, service, user_context) -> None:
        created = await service.upsert_group(
            user_context, "group-1", name="Hello", category="c", oneliner="o"
        )
        assert created["group_id"] == "group-1"
        assert created["name"] == "Hello"

        fetched = await service.get_group(user_context, "group-1")
        assert fetched is not None
        assert fetched["name"] == "Hello"
        assert fetched["versions"] == []

    async def test_upsert_group_is_idempotent(self, service, user_context) -> None:
        first = await service.upsert_group(user_context, "group-1", name="A")
        second = await service.upsert_group(user_context, "group-1", name="B")
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["name"] == "B"

        items, _ = await service.list_groups(user_context, limit=10)
        assert len(items) == 1

    async def test_upsert_group_updates_every_optional_field(
        self, service, user_context
    ) -> None:
        await service.upsert_group(
            user_context, "group-1", name="A", category="c1", oneliner="o1"
        )
        updated = await service.upsert_group(
            user_context,
            "group-1",
            name="A2",
            category="c2",
            oneliner="o2",
            command="cmd",
            metadata={"k": "v"},
        )
        assert updated["name"] == "A2"
        assert updated["category"] == "c2"
        assert updated["oneliner"] == "o2"
        assert updated["command"] == "cmd"
        assert updated["metadata"] == {"k": "v"}

    async def test_delete_missing_group_returns_false(
        self, service, user_context
    ) -> None:
        assert await service.delete_group(user_context, "nope") is False

    async def test_delete_group_removes_it_and_its_versions(
        self, service, user_context, pg_factory
    ) -> None:
        from sqlalchemy import select

        from audittrace.db.models import ConsolePromptVersion

        await service.upsert_group(user_context, "group-1", name="A")
        await service.upsert_version(user_context, "group-1", "p1", text="v1")
        assert await service.delete_group(user_context, "group-1") is True
        assert await service.get_group(user_context, "group-1") is None

        # Direct row-level check: the version was HARD-deleted, not
        # merely orphaned under the now-soft-deleted group.
        async with pg_factory.get_session_factory()() as session:
            rows = (
                (
                    await session.execute(
                        select(ConsolePromptVersion).filter(
                            ConsolePromptVersion.group_id == "group-1"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert rows == []

    async def test_cross_user_isolation_denies_read(
        self, service, user_context
    ) -> None:
        """Acceptance guard — RLS: user B cannot read user A's group.
        Neuter the explicit ``user_sub`` filter in
        ``PostgresConsolePromptsService.get_group`` and this test goes
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
            await service.upsert_group(
                alice, "secret-group", name="alice's private group"
            )
        finally:
            set_current_user_id(None)

        set_current_user_id(bob.user_id)
        try:
            bob_read = await service.get_group(bob, "secret-group")
            bob_list, _ = await service.list_groups(bob, limit=10)
        finally:
            set_current_user_id(None)

        assert bob_read is None, (
            "user B read user A's prompt group — the isolation wall is "
            "broken (missing/neutered user_sub filter)"
        )
        assert bob_list == [], "user B's list_groups included user A's group"

        set_current_user_id(alice.user_id)
        try:
            alice_read = await service.get_group(alice, "secret-group")
        finally:
            set_current_user_id(None)
        assert alice_read is not None
        assert alice_read["name"] == "alice's private group"

    async def test_upsert_group_cannot_overwrite_another_users_row(
        self, service, user_context
    ) -> None:
        """Non-vacuity guard: even with an IDENTICAL group_id, bob's
        upsert must create/update HIS OWN row, never alice's — neuter
        the explicit ``user_sub`` filter in the upsert's existence
        check and this test goes RED (bob's name would overwrite
        alice's row instead of creating a second, isolated one)."""
        alice = replace(user_context, user_id="user-alice-up", is_admin=False)
        bob = replace(user_context, user_id="user-bob-up", is_admin=False)

        await service.upsert_group(alice, "shared-id", name="alice's name")
        await service.upsert_group(bob, "shared-id", name="bob's name")

        alice_row = await service.get_group(alice, "shared-id")
        bob_row = await service.get_group(bob, "shared-id")
        assert alice_row is not None
        assert bob_row is not None
        assert alice_row["name"] == "alice's name", (
            "bob's upsert overwrote alice's group — the user_sub "
            "isolation filter in the upsert existence-check is missing/neutered"
        )
        assert bob_row["name"] == "bob's name"

    async def test_cross_user_delete_denied(self, service, user_context) -> None:
        alice = replace(user_context, user_id="user-alice-del", is_admin=False)
        bob = replace(user_context, user_id="user-bob-del", is_admin=False)
        await service.upsert_group(alice, "alice-group", name="mine")

        deleted = await service.delete_group(bob, "alice-group")
        assert deleted is False, "user B deleted user A's group — isolation broken"
        assert (await service.get_group(alice, "alice-group")) is not None

    async def test_write_failure_raises_runtime_error(
        self, service, user_context, monkeypatch
    ) -> None:
        async def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "sqlalchemy.ext.asyncio.AsyncSession.commit",
            _boom,
        )
        with pytest.raises(RuntimeError, match="upsert_group.*failed"):
            await service.upsert_group(user_context, "group-1", name="A")


class TestPostgresConsolePromptsServiceListPagination:
    async def test_list_newest_first(self, service, user_context) -> None:
        await service.upsert_group(user_context, "group-1", name="A")
        await service.upsert_group(user_context, "group-2", name="B")
        await service.upsert_group(user_context, "group-3", name="C")
        items, _ = await service.list_groups(user_context, limit=10)
        assert [i["group_id"] for i in items] == ["group-3", "group-2", "group-1"]

    async def test_list_pagination_across_pages(self, service, user_context) -> None:
        for i in range(5):
            await service.upsert_group(user_context, f"group-{i}", name=f"g{i}")

        page1, cursor1 = await service.list_groups(user_context, limit=2)
        assert len(page1) == 2
        page2, cursor2 = await service.list_groups(
            user_context, cursor=cursor1, limit=2
        )
        assert len(page2) == 2
        page3, cursor3 = await service.list_groups(
            user_context, cursor=cursor2, limit=2
        )
        assert len(page3) == 1
        assert cursor3 is None

        all_ids = [i["group_id"] for i in page1 + page2 + page3]
        assert len(set(all_ids)) == 5

    async def test_invalid_cursor_raises_value_error(
        self, service, user_context
    ) -> None:
        with pytest.raises(ValueError, match="invalid cursor"):
            await service.list_groups(user_context, cursor="garbage!!")


class TestPostgresConsolePromptsServiceVersions:
    async def test_upsert_version_requires_owned_group(
        self, service, user_context
    ) -> None:
        """Completeness guard (WU-2 lesson), Postgres-backed path:
        neuter the group-ownership check in
        ``PostgresConsolePromptsService.upsert_version`` and this test
        goes RED."""
        alice = replace(user_context, user_id="user-alice-v", is_admin=False)
        bob = replace(user_context, user_id="user-bob-v", is_admin=False)
        await service.upsert_group(alice, "alice-group", name="Alice")

        result = await service.upsert_version(
            bob, "alice-group", "prompt-1", text="hijack attempt"
        )
        assert result is None, (
            "bob attached a version to alice's group — completeness/"
            "isolation guard is broken"
        )

    async def test_upsert_version_missing_group_returns_none(
        self, service, user_context
    ) -> None:
        result = await service.upsert_version(
            user_context, "no-such-group", "prompt-1", text="hi"
        )
        assert result is None

    async def test_upsert_version_assigns_monotonic_version_numbers(
        self, service, user_context
    ) -> None:
        await service.upsert_group(user_context, "group-1", name="G")
        v1 = await service.upsert_version(
            user_context, "group-1", "prompt-1", text="v1 text"
        )
        v2 = await service.upsert_version(
            user_context, "group-1", "prompt-2", text="v2 text"
        )
        assert v1["version"] == 1
        assert v2["version"] == 2

    async def test_upsert_version_is_idempotent_by_prompt_id(
        self, service, user_context
    ) -> None:
        await service.upsert_group(user_context, "group-1", name="G")
        first = await service.upsert_version(
            user_context, "group-1", "prompt-1", text="v1"
        )
        second = await service.upsert_version(
            user_context,
            "group-1",
            "prompt-1",
            text="v1-edited",
            type="chat",
            metadata={"k": "v"},
        )
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["text"] == "v1-edited"
        assert second["type"] == "chat"
        assert second["metadata"] == {"k": "v"}
        assert second["version"] == 1
        # A third edit with metadata=None must leave the metadata set by
        # the second edit untouched (covers the "existing row,
        # metadata omitted" branch).
        third = await service.upsert_version(
            user_context, "group-1", "prompt-1", text="v1-edited-again"
        )
        assert third["text"] == "v1-edited-again"
        assert third["metadata"] == {"k": "v"}, (
            "metadata=None on an update must leave existing metadata unchanged"
        )

    async def test_upsert_version_bumps_group_updated_at(
        self, service, user_context
    ) -> None:
        group = await service.upsert_group(user_context, "group-1", name="G")
        await service.upsert_version(user_context, "group-1", "p1", text="v1")
        refetched = await service.get_group(user_context, "group-1")
        assert refetched is not None
        assert refetched["updated_at_ms"] >= group["updated_at_ms"]

    async def test_get_group_includes_versions_oldest_first(
        self, service, user_context
    ) -> None:
        await service.upsert_group(user_context, "group-1", name="G")
        await service.upsert_version(user_context, "group-1", "p1", text="one")
        await service.upsert_version(user_context, "group-1", "p2", text="two")
        fetched = await service.get_group(user_context, "group-1")
        assert fetched is not None
        assert [v["prompt_id"] for v in fetched["versions"]] == ["p1", "p2"]

    async def test_get_group_never_leaks_another_users_versions(
        self, service, user_context
    ) -> None:
        alice = replace(user_context, user_id="user-alice-g", is_admin=False)
        bob = replace(user_context, user_id="user-bob-g", is_admin=False)
        await service.upsert_group(alice, "shared-group-id", name="Alice's")
        await service.upsert_group(bob, "shared-group-id", name="Bob's")
        await service.upsert_version(
            alice, "shared-group-id", "alice-p1", text="alice secret"
        )

        bob_fetch = await service.get_group(bob, "shared-group-id")
        assert bob_fetch is not None
        assert bob_fetch["versions"] == [], (
            "bob's group fetch included alice's version — isolation wall is broken"
        )

    async def test_upsert_version_write_failure_raises_runtime_error(
        self, service, user_context, monkeypatch
    ) -> None:
        await service.upsert_group(user_context, "group-1", name="G")

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "sqlalchemy.ext.asyncio.AsyncSession.commit",
            _boom,
        )
        with pytest.raises(RuntimeError, match="upsert_version.*failed"):
            await service.upsert_version(user_context, "group-1", "prompt-1", text="v1")


class TestPostgresConsolePromptsServiceSetProduction:
    async def test_set_production_happy_path(self, service, user_context) -> None:
        await service.upsert_group(user_context, "group-1", name="G")
        await service.upsert_version(user_context, "group-1", "p1", text="v1")
        result = await service.set_production(user_context, "group-1", "p1")
        assert result is not None
        assert result["production_prompt_id"] == "p1"
        assert [v["prompt_id"] for v in result["versions"]] == ["p1"]

    async def test_set_production_missing_group_returns_none(
        self, service, user_context
    ) -> None:
        result = await service.set_production(user_context, "no-such-group", "p1")
        assert result is None

    async def test_set_production_missing_version_returns_none(
        self, service, user_context
    ) -> None:
        await service.upsert_group(user_context, "group-1", name="G")
        result = await service.set_production(user_context, "group-1", "no-such-prompt")
        assert result is None

    async def test_set_production_denies_cross_user_version(
        self, service, user_context
    ) -> None:
        """Completeness/isolation guard, Postgres-backed path: neuter
        the ``user_sub`` half of the version-ownership filter in
        ``PostgresConsolePromptsService.set_production`` and this test
        goes RED."""
        alice = replace(user_context, user_id="user-alice-sp", is_admin=False)
        bob = replace(user_context, user_id="user-bob-sp", is_admin=False)
        await service.upsert_group(alice, "shared-id", name="Alice's")
        await service.upsert_group(bob, "shared-id", name="Bob's")
        await service.upsert_version(alice, "shared-id", "alice-p1", text="alice")

        result = await service.set_production(bob, "shared-id", "alice-p1")
        assert result is None, (
            "bob promoted alice's version to production — completeness/"
            "isolation guard is broken"
        )

    async def test_set_production_denies_cross_group_version(
        self, service, user_context
    ) -> None:
        """Completeness guard, Postgres-backed path: neuter the
        ``group_id`` half of the version-ownership filter in
        ``PostgresConsolePromptsService.set_production`` and this test
        goes RED."""
        await service.upsert_group(user_context, "group-a", name="A")
        await service.upsert_group(user_context, "group-b", name="B")
        await service.upsert_version(user_context, "group-b", "p-in-b", text="b")

        result = await service.set_production(user_context, "group-a", "p-in-b")
        assert result is None, (
            "a version belonging to a different group was promoted — "
            "completeness guard is broken"
        )
