"""Tests for the ``session`` memory layer's service (WU-1, Sovereign-Attach
EPIC).

Mirrors ``test_postgres_conversational.py``'s structure: Mock-service
interface tests, then a Postgres-backed suite via ``InMemoryPostgresFactory``
(no real PostgreSQL required — aiosqlite under the hood). The acceptance-
test-(d) isolation case (spec 2026-09-03-SPEC-wu1-session-layer-narrow-
ingest-scope.md) brackets its assertion with ``set_current_user_id`` per
``feedback_unit_tests_miss_rls`` — RLS itself is a no-op on SQLite, so the
guard under test is ``PostgresSessionMemoryService.read_own``'s explicit
``.filter(SessionMemoryItem.user_id == ...)`` clause, not the (here inert)
Postgres GUC.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import pytest_asyncio

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.db.rls import set_current_user_id
from audittrace.services.session_memory import (
    MockSessionMemoryService,
    PostgresSessionMemoryService,
    SessionMemoryService,
)

# ── MockSessionMemoryService ─────────────────────────────────────────────────


class TestMockSessionMemoryService:
    def test_abstract_interface(self) -> None:
        assert isinstance(MockSessionMemoryService(), SessionMemoryService)

    async def test_starts_empty(self, user_context) -> None:
        service = MockSessionMemoryService()
        assert await service.read_own(user_context, "note.txt") is None

    async def test_write_then_read_own(self, user_context) -> None:
        service = MockSessionMemoryService()
        doc = await service.write(user_context, "note.txt", "hello session")
        assert doc.page_content == "hello session"
        assert doc.metadata["layer"] == "session"
        assert doc.metadata["tier"] == "private"
        assert doc.metadata["filename"] == "note.txt"

        read_back = await service.read_own(user_context, "note.txt")
        assert read_back is not None
        assert read_back.page_content == "hello session"

    async def test_rejects_path_traversal_filename(self, user_context) -> None:
        service = MockSessionMemoryService()
        with pytest.raises(ValueError, match="invalid filename"):
            await service.write(user_context, "../../etc/passwd", "x")

    async def test_rejects_empty_filename(self, user_context) -> None:
        service = MockSessionMemoryService()
        with pytest.raises(ValueError, match="invalid filename"):
            await service.write(user_context, "", "x")

    async def test_isolates_by_user(self, user_context) -> None:
        service = MockSessionMemoryService()
        alice = replace(user_context, user_id="user-alice", is_admin=False)
        bob = replace(user_context, user_id="user-bob", is_admin=False)
        await service.write(alice, "note.txt", "alice's note")

        # Bob has never written note.txt — MUST see nothing, even though
        # alice's row for that exact filename exists.
        assert await service.read_own(bob, "note.txt") is None
        alice_read = await service.read_own(alice, "note.txt")
        assert alice_read is not None
        assert alice_read.page_content == "alice's note"

    async def test_reset(self, user_context) -> None:
        service = MockSessionMemoryService()
        await service.write(user_context, "note.txt", "x")
        service.reset()
        assert await service.read_own(user_context, "note.txt") is None


# ── PostgresSessionMemoryService ─────────────────────────────────────────────


@pytest_asyncio.fixture
async def pg_factory():
    """Fresh in-memory factory with tables created (async)."""
    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    return factory


@pytest.fixture
def service(pg_factory) -> PostgresSessionMemoryService:
    return PostgresSessionMemoryService(
        session_factory=pg_factory.get_session_factory(),
    )


class TestPostgresSessionMemoryService:
    def test_abstract_interface(self, service) -> None:
        assert isinstance(service, SessionMemoryService)

    async def test_read_own_missing_returns_none(self, service, user_context) -> None:
        assert await service.read_own(user_context, "note.txt") is None

    async def test_write_then_read_own_round_trips(self, service, user_context) -> None:
        written = await service.write(user_context, "note.txt", "hello session")
        assert written.page_content == "hello session"
        assert written.metadata["tier"] == "private"
        assert written.metadata["user_id"] == user_context.user_id

        read_back = await service.read_own(user_context, "note.txt")
        assert read_back is not None
        assert read_back.page_content == "hello session"
        assert read_back.metadata["filename"] == "note.txt"

    async def test_write_rejects_path_traversal_filename(
        self, service, user_context
    ) -> None:
        with pytest.raises(ValueError, match="invalid filename"):
            await service.write(user_context, "../secret.md", "x")

    async def test_write_rejects_slash_in_filename(self, service, user_context) -> None:
        with pytest.raises(ValueError, match="invalid filename"):
            await service.write(user_context, "sub/dir.txt", "x")

    async def test_read_own_returns_most_recent_write(
        self, service, user_context
    ) -> None:
        """Two writes with the same filename — read_own returns the LATEST
        one (append-only scratch space, not an upsert-by-filename store)."""
        await service.write(user_context, "note.txt", "first version")
        second = await service.write(user_context, "note.txt", "second version")
        read_back = await service.read_own(user_context, "note.txt")
        assert read_back is not None
        assert read_back.page_content == "second version"
        assert read_back.metadata["id"] == second.metadata["id"]

    async def test_cross_user_isolation_denies_read(
        self, service, user_context
    ) -> None:
        """Acceptance test (d) — RLS: user B cannot read user A's session
        upload. Neuter the explicit ``user_id`` filter in
        ``PostgresSessionMemoryService.read_own`` and this test goes RED
        (user B would see user A's row).

        Brackets the assertion with ``set_current_user_id`` per
        ``feedback_unit_tests_miss_rls`` — SQLite has no Postgres RLS GUC,
        so the ACTUAL guard under test is the service's own explicit
        ``.filter(SessionMemoryItem.user_id == ...)`` clause, exercised
        identically to how it would run inside a real request (where
        ``require_user`` sets this same ContextVar).
        """
        alice = replace(user_context, user_id="user-alice-rls", is_admin=False)
        bob = replace(user_context, user_id="user-bob-rls", is_admin=False)

        set_current_user_id(alice.user_id)
        try:
            await service.write(alice, "secret.txt", "alice's private upload")
        finally:
            set_current_user_id(None)

        set_current_user_id(bob.user_id)
        try:
            bob_read = await service.read_own(bob, "secret.txt")
        finally:
            set_current_user_id(None)

        assert bob_read is None, (
            "user B read user A's session upload — the isolation wall "
            "is broken (missing/neutered user_id filter)"
        )

        # Sanity: alice can still read her own upload (proves the None
        # above is isolation, not a broken write path).
        set_current_user_id(alice.user_id)
        try:
            alice_read = await service.read_own(alice, "secret.txt")
        finally:
            set_current_user_id(None)
        assert alice_read is not None
        assert alice_read.page_content == "alice's private upload"

    async def test_write_failure_raises_runtime_error(
        self, service, user_context, monkeypatch
    ) -> None:
        """A backend failure surfaces as ``RuntimeError`` (the contract
        ``_write_layer_private`` maps to a 502), not a raw SQLAlchemy
        exception leaking through the service boundary."""

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "sqlalchemy.ext.asyncio.AsyncSession.commit",
            _boom,
        )
        with pytest.raises(RuntimeError, match="write.*failed"):
            await service.write(user_context, "note.txt", "x")
