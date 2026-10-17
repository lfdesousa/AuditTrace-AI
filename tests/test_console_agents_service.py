"""Tests for the console-agents service (Agents domain,
MongoDB-elimination EPIC).

Mirrors ``test_console_files_service.py``'s structure EXACTLY (the
spec's instruction), scaled to the agents field set (name/description/
instructions/provider/model/model_parameters/tools/artifacts/
end_after_tools/project_ids instead of filename/type/bytes/...), plus
the same extra batch-get-by-ids shape the files domain adds: Mock-
service interface tests, then a Postgres-backed suite via
``InMemoryPostgresFactory`` (aiosqlite, no real PostgreSQL required).
The cross-user isolation tests bracket their assertions with
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
from audittrace.services.console_agents import (
    ConsoleAgentsService,
    MockConsoleAgentsService,
    PostgresConsoleAgentsService,
    _decode_cursor,
    _encode_cursor,
)

# ── cursor helpers ───────────────────────────────────────────────────────


class TestCursorCodec:
    def test_round_trips(self) -> None:
        cursor = _encode_cursor(updated_at_ms=12345, agent_id="agent-a")
        assert _decode_cursor(cursor) == (12345, "agent-a")

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

        garbage = base64.urlsafe_b64encode(b"not-a-number:agent-1").decode("ascii")
        with pytest.raises(ValueError, match="invalid cursor"):
            _decode_cursor(garbage)


# ── MockConsoleAgentsService ────────────────────────────────────────────


class TestMockConsoleAgentsService:
    def test_abstract_interface(self) -> None:
        assert isinstance(MockConsoleAgentsService(), ConsoleAgentsService)

    async def test_get_missing_returns_none(self, user_context) -> None:
        service = MockConsoleAgentsService()
        assert await service.get_agent(user_context, "nope") is None

    async def test_upsert_then_get_round_trips(self, user_context) -> None:
        service = MockConsoleAgentsService()
        created = await service.upsert_agent(
            user_context,
            "agent-1",
            name="My Agent",
            description="does things",
            instructions="be helpful",
            provider="openAI",
            model="gpt-4",
        )
        assert created["agent_id"] == "agent-1"
        assert created["name"] == "My Agent"
        assert created["description"] == "does things"
        assert created["instructions"] == "be helpful"
        assert created["provider"] == "openAI"
        assert created["model"] == "gpt-4"
        assert created["deleted_at_ms"] is None

        fetched = await service.get_agent(user_context, "agent-1")
        assert fetched is not None
        assert fetched["name"] == "My Agent"

    async def test_upsert_defaults(self, user_context) -> None:
        service = MockConsoleAgentsService()
        created = await service.upsert_agent(user_context, "agent-1", name="Bare")
        assert created["description"] == ""
        assert created["instructions"] is None
        assert created["provider"] is None
        assert created["model"] is None
        assert created["model_parameters"] == {}
        assert created["tools"] == []
        assert created["artifacts"] == {}
        assert created["end_after_tools"] is False
        assert created["project_ids"] == []
        assert created["metadata"] == {}

    async def test_upsert_full_field_set(self, user_context) -> None:
        service = MockConsoleAgentsService()
        created = await service.upsert_agent(
            user_context,
            "agent-1",
            name="Full",
            description="d",
            instructions="i",
            provider="anthropic",
            model="claude",
            model_parameters={"temperature": 0.7},
            tools=["web_search", "code_interpreter"],
            artifacts={"kind": "shell"},
            end_after_tools=True,
            project_ids=["project-1", "project-2"],
            metadata={"k": "v"},
        )
        assert created["model_parameters"] == {"temperature": 0.7}
        assert created["tools"] == ["web_search", "code_interpreter"]
        assert created["artifacts"] == {"kind": "shell"}
        assert created["end_after_tools"] is True
        assert created["project_ids"] == ["project-1", "project-2"]
        assert created["metadata"] == {"k": "v"}

    async def test_upsert_is_idempotent_by_agent_id(self, user_context) -> None:
        """Calling upsert twice with the same agent_id updates the SAME
        row rather than creating a duplicate."""
        service = MockConsoleAgentsService()
        first = await service.upsert_agent(user_context, "agent-1", name="A")
        second = await service.upsert_agent(user_context, "agent-1", name="B")
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["name"] == "B"

        items, _ = await service.list_agents(user_context, limit=10)
        assert len(items) == 1

    async def test_upsert_updates_every_optional_field_on_existing_row(
        self, user_context
    ) -> None:
        """The upsert's update-existing branch updates EACH optional
        field independently — covers every ``if <field> is not None``
        branch on the second call."""
        service = MockConsoleAgentsService()
        await service.upsert_agent(user_context, "agent-1", name="A", description="d1")
        updated = await service.upsert_agent(
            user_context,
            "agent-1",
            name="A",
            description="d2",
            instructions="new instructions",
            provider="openAI",
            model="gpt-4o",
            model_parameters={"temperature": 1},
            tools=["tool-a"],
            artifacts={"kind": "default"},
            end_after_tools=True,
            project_ids=["p1"],
            metadata={"k": "v"},
        )
        assert updated["description"] == "d2"
        assert updated["instructions"] == "new instructions"
        assert updated["provider"] == "openAI"
        assert updated["model"] == "gpt-4o"
        assert updated["model_parameters"] == {"temperature": 1}
        assert updated["tools"] == ["tool-a"]
        assert updated["artifacts"] == {"kind": "default"}
        assert updated["end_after_tools"] is True
        assert updated["project_ids"] == ["p1"]
        assert updated["metadata"] == {"k": "v"}

    async def test_delete_agent(self, user_context) -> None:
        service = MockConsoleAgentsService()
        await service.upsert_agent(user_context, "agent-1", name="X")
        assert await service.delete_agent(user_context, "agent-1") is True
        assert await service.get_agent(user_context, "agent-1") is None

    async def test_delete_missing_agent_returns_false(self, user_context) -> None:
        service = MockConsoleAgentsService()
        assert await service.delete_agent(user_context, "nope") is False

    async def test_isolates_agents_by_user(self, user_context) -> None:
        """Non-vacuity guard: neuter the ``user_sub`` filter in
        ``get_agent``/``list_agents`` and Bob starts seeing Alice's
        agents."""
        service = MockConsoleAgentsService()
        alice = replace(user_context, user_id="user-alice", is_admin=False)
        bob = replace(user_context, user_id="user-bob", is_admin=False)
        await service.upsert_agent(alice, "agent-1", name="Alice's agent")

        assert await service.get_agent(bob, "agent-1") is None
        bob_items, _ = await service.list_agents(bob, limit=10)
        assert bob_items == [], (
            "user B's list_agents included user A's agent — the "
            "isolation wall is broken (missing/neutered user_sub filter)"
        )

        alice_read = await service.get_agent(alice, "agent-1")
        assert alice_read is not None
        assert alice_read["name"] == "Alice's agent"

    async def test_reset(self, user_context) -> None:
        service = MockConsoleAgentsService()
        await service.upsert_agent(user_context, "agent-1", name="X")
        service.reset()
        assert await service.get_agent(user_context, "agent-1") is None


class TestMockConsoleAgentsServiceListPagination:
    async def test_list_empty(self, user_context) -> None:
        service = MockConsoleAgentsService()
        items, next_cursor = await service.list_agents(user_context, limit=10)
        assert items == []
        assert next_cursor is None

    async def test_list_newest_first(self, user_context) -> None:
        service = MockConsoleAgentsService()
        await service.upsert_agent(user_context, "agent-1", name="a1")
        await service.upsert_agent(user_context, "agent-2", name="a2")
        await service.upsert_agent(user_context, "agent-3", name="a3")
        items, _ = await service.list_agents(user_context, limit=10)
        assert [i["agent_id"] for i in items] == ["agent-3", "agent-2", "agent-1"]

    async def test_list_excludes_deleted(self, user_context) -> None:
        service = MockConsoleAgentsService()
        await service.upsert_agent(user_context, "agent-1", name="a1")
        await service.upsert_agent(user_context, "agent-2", name="a2")
        await service.delete_agent(user_context, "agent-1")
        items, _ = await service.list_agents(user_context, limit=10)
        assert [i["agent_id"] for i in items] == ["agent-2"]

    async def test_list_pagination_across_pages(self, user_context) -> None:
        service = MockConsoleAgentsService()
        for i in range(5):
            await service.upsert_agent(user_context, f"agent-{i}", name="p")

        page1, cursor1 = await service.list_agents(user_context, limit=2)
        assert len(page1) == 2
        assert cursor1 is not None

        page2, cursor2 = await service.list_agents(
            user_context, cursor=cursor1, limit=2
        )
        assert len(page2) == 2
        assert cursor2 is not None

        page3, cursor3 = await service.list_agents(
            user_context, cursor=cursor2, limit=2
        )
        assert len(page3) == 1
        assert cursor3 is None

        all_ids = [i["agent_id"] for i in page1 + page2 + page3]
        assert len(set(all_ids)) == 5, "pagination must not duplicate or skip rows"

    async def test_list_last_page_has_no_next_cursor(self, user_context) -> None:
        service = MockConsoleAgentsService()
        await service.upsert_agent(user_context, "agent-1", name="a1")
        items, next_cursor = await service.list_agents(user_context, limit=10)
        assert len(items) == 1
        assert next_cursor is None

    async def test_invalid_cursor_raises_value_error(self, user_context) -> None:
        service = MockConsoleAgentsService()
        with pytest.raises(ValueError, match="invalid cursor"):
            await service.list_agents(user_context, cursor="garbage!!")


class TestMockConsoleAgentsServiceBatchGet:
    async def test_batch_get_preserves_request_order(self, user_context) -> None:
        service = MockConsoleAgentsService()
        for i in range(3):
            await service.upsert_agent(user_context, f"agent-{i}", name="p")
        results = await service.batch_get_agents(
            user_context, ["agent-2", "agent-0", "agent-1"]
        )
        assert [r["agent_id"] for r in results] == ["agent-2", "agent-0", "agent-1"]

    async def test_batch_get_omits_missing_ids(self, user_context) -> None:
        service = MockConsoleAgentsService()
        await service.upsert_agent(user_context, "agent-1", name="p")
        results = await service.batch_get_agents(
            user_context, ["agent-1", "does-not-exist"]
        )
        assert [r["agent_id"] for r in results] == ["agent-1"]

    async def test_batch_get_empty_input_returns_empty(self, user_context) -> None:
        service = MockConsoleAgentsService()
        assert await service.batch_get_agents(user_context, []) == []

    async def test_batch_get_isolates_by_user(self, user_context) -> None:
        """Non-vacuity guard: neuter the ``user_sub`` comparison in
        ``_find`` and Bob's batch-get starts returning Alice's agents."""
        service = MockConsoleAgentsService()
        alice = replace(user_context, user_id="user-alice", is_admin=False)
        bob = replace(user_context, user_id="user-bob", is_admin=False)
        await service.upsert_agent(alice, "alice-agent", name="secret")
        results = await service.batch_get_agents(bob, ["alice-agent"])
        assert results == [], (
            "bob's batch-get returned alice's agent — the isolation "
            "wall is broken (missing/neutered user_sub filter)"
        )


# ── PostgresConsoleAgentsService (aiosqlite) ─────────────────────────────


@pytest_asyncio.fixture
async def pg_factory():
    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    return factory


@pytest.fixture
def service(pg_factory) -> PostgresConsoleAgentsService:
    return PostgresConsoleAgentsService(
        session_factory=pg_factory.get_session_factory(),
    )


class TestPostgresConsoleAgentsService:
    def test_abstract_interface(self, service) -> None:
        assert isinstance(service, ConsoleAgentsService)

    async def test_get_missing_returns_none(self, service, user_context) -> None:
        assert await service.get_agent(user_context, "nope") is None

    async def test_upsert_then_get_round_trips(self, service, user_context) -> None:
        created = await service.upsert_agent(
            user_context, "agent-1", name="Hello", description="my agent"
        )
        assert created["agent_id"] == "agent-1"
        assert created["name"] == "Hello"
        assert created["description"] == "my agent"

        fetched = await service.get_agent(user_context, "agent-1")
        assert fetched is not None
        assert fetched["name"] == "Hello"

    async def test_upsert_defaults(self, service, user_context) -> None:
        created = await service.upsert_agent(user_context, "agent-1", name="Bare")
        assert created["description"] == ""
        assert created["model_parameters"] == {}
        assert created["tools"] == []
        assert created["artifacts"] == {}
        assert created["end_after_tools"] is False
        assert created["project_ids"] == []

    async def test_upsert_is_idempotent_by_agent_id(
        self, service, user_context
    ) -> None:
        first = await service.upsert_agent(user_context, "agent-1", name="A")
        second = await service.upsert_agent(user_context, "agent-1", name="B")
        assert first["created_at_ms"] == second["created_at_ms"]
        assert second["name"] == "B"

        items, _ = await service.list_agents(user_context, limit=10)
        assert len(items) == 1

    async def test_upsert_updates_every_optional_field_on_existing_row(
        self, service, user_context
    ) -> None:
        await service.upsert_agent(user_context, "agent-1", name="A", description="d1")
        updated = await service.upsert_agent(
            user_context,
            "agent-1",
            name="A",
            description="d2",
            instructions="i2",
            provider="openAI",
            model="gpt-4",
            model_parameters={"k": "v"},
            tools=["t"],
            artifacts={"a": "b"},
            end_after_tools=True,
            project_ids=["p"],
            metadata={"k": "v"},
        )
        assert updated["description"] == "d2"
        assert updated["instructions"] == "i2"
        assert updated["provider"] == "openAI"
        assert updated["model"] == "gpt-4"
        assert updated["model_parameters"] == {"k": "v"}
        assert updated["tools"] == ["t"]
        assert updated["artifacts"] == {"a": "b"}
        assert updated["end_after_tools"] is True
        assert updated["project_ids"] == ["p"]
        assert updated["metadata"] == {"k": "v"}

    async def test_delete_missing_agent_returns_false(
        self, service, user_context
    ) -> None:
        assert await service.delete_agent(user_context, "nope") is False

    async def test_delete_agent_removes_it(self, service, user_context) -> None:
        await service.upsert_agent(user_context, "agent-1", name="X")
        assert await service.delete_agent(user_context, "agent-1") is True
        assert await service.get_agent(user_context, "agent-1") is None

    async def test_cross_user_isolation_denies_read(
        self, service, user_context
    ) -> None:
        """Acceptance guard — RLS: user B cannot read user A's agent.
        Neuter the explicit ``user_sub`` filter in
        ``PostgresConsoleAgentsService.get_agent`` and this test goes
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
            await service.upsert_agent(
                alice, "secret-agent", name="alice's private agent"
            )
        finally:
            set_current_user_id(None)

        set_current_user_id(bob.user_id)
        try:
            bob_read = await service.get_agent(bob, "secret-agent")
            bob_list, _ = await service.list_agents(bob, limit=10)
            bob_batch = await service.batch_get_agents(bob, ["secret-agent"])
        finally:
            set_current_user_id(None)

        assert bob_read is None, (
            "user B read user A's agent — the isolation wall is broken "
            "(missing/neutered user_sub filter)"
        )
        assert bob_list == [], "user B's list_agents included user A's agent"
        assert bob_batch == [], "user B's batch_get_agents included user A's agent"

        set_current_user_id(alice.user_id)
        try:
            alice_read = await service.get_agent(alice, "secret-agent")
        finally:
            set_current_user_id(None)
        assert alice_read is not None
        assert alice_read["name"] == "alice's private agent"

    async def test_upsert_agent_cannot_overwrite_another_users_row(
        self, service, user_context
    ) -> None:
        """Non-vacuity guard: even with an IDENTICAL agent_id, bob's
        upsert must create/update HIS OWN row, never alice's — neuter
        the explicit ``user_sub`` filter in the upsert's existence
        check and this test goes RED (bob's name would overwrite
        alice's row instead of creating a second, isolated one)."""
        alice = replace(user_context, user_id="user-alice-up", is_admin=False)
        bob = replace(user_context, user_id="user-bob-up", is_admin=False)

        await service.upsert_agent(alice, "shared-id", name="alice's name")
        await service.upsert_agent(bob, "shared-id", name="bob's name")

        alice_row = await service.get_agent(alice, "shared-id")
        bob_row = await service.get_agent(bob, "shared-id")
        assert alice_row is not None
        assert bob_row is not None
        assert alice_row["name"] == "alice's name", (
            "bob's upsert overwrote alice's agent — the user_sub "
            "isolation filter in the upsert existence-check is missing/neutered"
        )
        assert bob_row["name"] == "bob's name"

    async def test_cross_user_delete_denied(self, service, user_context) -> None:
        alice = replace(user_context, user_id="user-alice-del", is_admin=False)
        bob = replace(user_context, user_id="user-bob-del", is_admin=False)
        await service.upsert_agent(alice, "alice-agent", name="mine")

        deleted = await service.delete_agent(bob, "alice-agent")
        assert deleted is False, "user B deleted user A's agent — isolation broken"
        assert (await service.get_agent(alice, "alice-agent")) is not None

    async def test_write_failure_raises_runtime_error(
        self, service, user_context, monkeypatch
    ) -> None:
        async def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "sqlalchemy.ext.asyncio.AsyncSession.commit",
            _boom,
        )
        with pytest.raises(RuntimeError, match="upsert_agent.*failed"):
            await service.upsert_agent(user_context, "agent-1", name="X")


class TestPostgresConsoleAgentsServiceListPagination:
    async def test_list_newest_first(self, service, user_context) -> None:
        await service.upsert_agent(user_context, "agent-1", name="a1")
        await service.upsert_agent(user_context, "agent-2", name="a2")
        await service.upsert_agent(user_context, "agent-3", name="a3")
        items, _ = await service.list_agents(user_context, limit=10)
        assert [i["agent_id"] for i in items] == ["agent-3", "agent-2", "agent-1"]

    async def test_list_pagination_across_pages(self, service, user_context) -> None:
        for i in range(5):
            await service.upsert_agent(user_context, f"agent-{i}", name="p")

        page1, cursor1 = await service.list_agents(user_context, limit=2)
        assert len(page1) == 2
        page2, cursor2 = await service.list_agents(
            user_context, cursor=cursor1, limit=2
        )
        assert len(page2) == 2
        page3, cursor3 = await service.list_agents(
            user_context, cursor=cursor2, limit=2
        )
        assert len(page3) == 1
        assert cursor3 is None

        all_ids = [i["agent_id"] for i in page1 + page2 + page3]
        assert len(set(all_ids)) == 5

    async def test_invalid_cursor_raises_value_error(
        self, service, user_context
    ) -> None:
        with pytest.raises(ValueError, match="invalid cursor"):
            await service.list_agents(user_context, cursor="garbage!!")


class TestPostgresConsoleAgentsServiceBatchGet:
    async def test_batch_get_preserves_request_order(
        self, service, user_context
    ) -> None:
        for i in range(3):
            await service.upsert_agent(user_context, f"agent-{i}", name="p")
        results = await service.batch_get_agents(
            user_context, ["agent-2", "agent-0", "agent-1"]
        )
        assert [r["agent_id"] for r in results] == ["agent-2", "agent-0", "agent-1"]

    async def test_batch_get_omits_missing_ids(self, service, user_context) -> None:
        await service.upsert_agent(user_context, "agent-1", name="p")
        results = await service.batch_get_agents(
            user_context, ["agent-1", "does-not-exist"]
        )
        assert [r["agent_id"] for r in results] == ["agent-1"]

    async def test_batch_get_empty_input_returns_empty(
        self, service, user_context
    ) -> None:
        assert await service.batch_get_agents(user_context, []) == []

    async def test_batch_get_excludes_deleted(self, service, user_context) -> None:
        await service.upsert_agent(user_context, "agent-1", name="p")
        await service.delete_agent(user_context, "agent-1")
        results = await service.batch_get_agents(user_context, ["agent-1"])
        assert results == []
