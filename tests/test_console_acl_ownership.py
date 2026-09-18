"""Unit tests for ``services/console_acl/_ownership.py`` — ACL WU-2a
(``2026-09-18-SPEC-acl-wu2a-resource-ownership-verification.md``).

Exercised against the ``test_container``'s REAL ``Postgres*Service``
instances (backed by ``InMemoryPostgresFactory`` / aiosqlite) — the
same service-layer isolation logic (explicit ``user_sub`` filter) that
production runs, just without a live Postgres RLS enforcement layer
underneath it (``feedback_unit_tests_miss_rls`` — the REAL-Postgres
proof for the DB-level barrier this WU also ships, migration 032, is
``tests/test_acl_ownership_rls.py``).

**Per-resource_type, individually** (WU-2a §4 neuter 5): each resolved
resource_type gets its OWN owner/non-owner/missing test class, never a
batched "ownership works" assertion — the exact defect that made a
five-trap proof vacuous on the adapter branch.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from audittrace import dependencies
from audittrace.identity import UserContext
from audittrace.services.console_acl import RESOURCE_TYPES
from audittrace.services.console_acl._ownership import (
    RESOLVED_RESOURCE_TYPES,
    UNRESOLVED_RESOURCE_TYPES,
    UnknownResourceTypeError,
    owns,
)


@pytest.fixture(autouse=True)
def _wire_test_container(test_container):
    """``owns()`` resolves its per-domain services via
    ``audittrace.dependencies`` at call time — wire the populated
    ``test_container`` so those lookups succeed."""
    dependencies.container = test_container
    yield


@pytest.fixture
def attacker(user_context: UserContext) -> UserContext:
    """A second, distinct identity — never the resource owner."""
    return replace(user_context, user_id="attacker-sub-0002")


# ─────────────────── Committed enumeration (WU-2a §3.1) ────────────────────


class TestEnumerationIsCommitted:
    """``RESOURCE_TYPES`` (WU-1) partitions cleanly into resolved vs.
    unresolved — pinned here so a resolver silently added/removed (or a
    resource_type WU-1 adds without a matching resolver) is caught."""

    def test_resolved_and_unresolved_partition_all_resource_types(self) -> None:
        assert RESOLVED_RESOURCE_TYPES | UNRESOLVED_RESOURCE_TYPES == set(
            RESOURCE_TYPES
        )
        assert RESOLVED_RESOURCE_TYPES & UNRESOLVED_RESOURCE_TYPES == set()

    def test_resolved_set_is_exactly_the_two_migrated_stores(self) -> None:
        assert RESOLVED_RESOURCE_TYPES == {"agent", "promptGroup"}

    def test_unresolved_set_is_the_four_unmigrated_types(self) -> None:
        assert UNRESOLVED_RESOURCE_TYPES == {
            "mcpServer",
            "remoteAgent",
            "skill",
            "sharedLink",
        }


# ───────────────────────── resource_type='agent' ───────────────────────────


class TestOwnsAgent:
    async def test_owner_owns_their_own_agent(self, user_context: UserContext) -> None:
        agents = dependencies.get_console_agents_service()
        await agents.upsert_agent(user_context, "agent-1", name="Agent One")
        assert await owns(user_context, "agent", "agent-1") is True

    async def test_non_owner_does_not_own_the_agent(
        self, user_context: UserContext, attacker: UserContext
    ) -> None:
        agents = dependencies.get_console_agents_service()
        await agents.upsert_agent(user_context, "agent-1", name="Agent One")
        assert await owns(attacker, "agent", "agent-1") is False

    async def test_nonexistent_agent_id_is_not_owned_by_anyone(
        self, user_context: UserContext
    ) -> None:
        assert await owns(user_context, "agent", "does-not-exist") is False


# ─────────────────────── resource_type='promptGroup' ────────────────────────


class TestOwnsPromptGroup:
    async def test_owner_owns_their_own_group(self, user_context: UserContext) -> None:
        prompts = dependencies.get_console_prompts_service()
        await prompts.upsert_group(user_context, "group-1", name="Group One")
        assert await owns(user_context, "promptGroup", "group-1") is True

    async def test_non_owner_does_not_own_the_group(
        self, user_context: UserContext, attacker: UserContext
    ) -> None:
        prompts = dependencies.get_console_prompts_service()
        await prompts.upsert_group(user_context, "group-1", name="Group One")
        assert await owns(attacker, "promptGroup", "group-1") is False

    async def test_nonexistent_group_id_is_not_owned_by_anyone(
        self, user_context: UserContext
    ) -> None:
        assert await owns(user_context, "promptGroup", "does-not-exist") is False


# ───────────────── unresolved resource_type — fail CLOSED ──────────────────


class TestUnknownResourceTypeFailsClosed:
    """An unresolved resource_type must raise, by name, never return
    ``False`` — a gap must never look identical to a correct denial."""

    @pytest.mark.parametrize("resource_type", sorted(UNRESOLVED_RESOURCE_TYPES))
    async def test_unmigrated_resource_type_raises_named_error(
        self, user_context: UserContext, resource_type: str
    ) -> None:
        with pytest.raises(UnknownResourceTypeError) as exc_info:
            await owns(user_context, resource_type, "whatever-id")
        assert exc_info.value.resource_type == resource_type

    async def test_a_resource_type_absent_from_resource_types_raises_the_same_way(
        self, user_context: UserContext
    ) -> None:
        """Not merely 'unmigrated' but not a recognised resource_type at
        all — the fail-closed path is identical, since the dispatch
        table (not membership in ``RESOURCE_TYPES``) is the only
        source of truth :func:`owns` consults."""
        with pytest.raises(UnknownResourceTypeError) as exc_info:
            await owns(user_context, "not-a-real-resource-type", "whatever-id")
        assert exc_info.value.resource_type == "not-a-real-resource-type"

    async def test_error_message_names_the_resource_type(
        self, user_context: UserContext
    ) -> None:
        with pytest.raises(UnknownResourceTypeError, match="mcpServer"):
            await owns(user_context, "mcpServer", "some-id")
