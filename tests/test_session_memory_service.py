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

WU-5 (2026-09-06-SPEC-wu5-same-turn-session-recall.md) adds ``list_own``
test classes below (``TestMockSessionMemoryServiceListOwn`` /
``TestPostgresSessionMemoryServiceListOwn``), covering the same three
non-vacuity guards named in the spec: the ``user_id`` isolation filter,
recency ordering, and the "+1 probe" window that makes ``has_more``
correct.
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


class TestMockSessionMemoryServiceListOwn:
    """WU-5 — ``list_own``, the recency-ordered window ``recall_attachments``
    is built on."""

    async def test_list_own_empty(self, user_context) -> None:
        service = MockSessionMemoryService()
        assert await service.list_own(user_context, limit=5) == []

    async def test_list_own_recency_order(self, user_context) -> None:
        service = MockSessionMemoryService()
        await service.write(user_context, "first.txt", "one")
        await service.write(user_context, "second.txt", "two")
        await service.write(user_context, "third.txt", "three")
        window = await service.list_own(user_context, limit=10)
        assert [d.metadata["filename"] for d in window] == [
            "third.txt",
            "second.txt",
            "first.txt",
        ]

    async def test_list_own_isolates_by_user(self, user_context) -> None:
        """Non-vacuity guard (spec §6.1): neuter the ``user_id`` filter in
        ``list_own`` and Bob's window starts including Alice's uploads."""
        service = MockSessionMemoryService()
        alice = replace(user_context, user_id="user-alice-list", is_admin=False)
        bob = replace(user_context, user_id="user-bob-list", is_admin=False)
        await service.write(alice, "alice-note.txt", "alice's content")

        bob_window = await service.list_own(bob, limit=10)
        assert bob_window == [], (
            "user B's list_own window included user A's upload — the "
            "isolation wall is broken (missing/neutered user_id filter)"
        )
        alice_window = await service.list_own(alice, limit=10)
        assert len(alice_window) == 1
        assert alice_window[0].page_content == "alice's content"

    async def test_list_own_plus_one_probe_window_size(self, user_context) -> None:
        """Non-vacuity guard (spec §6.4): the window fetched is
        ``min(offset + limit + 1, MAX_RECALL_WINDOW)`` — one MORE than the
        page needs, so the caller (``recall_attachments``) can tell
        ``has_more`` apart from "exactly the page, no more"."""
        service = MockSessionMemoryService()
        for i in range(4):
            await service.write(user_context, f"note-{i}.txt", f"content {i}")
        # limit=3, offset=0 -> window should be min(0+3+1, 500) = 4 rows,
        # even though only 4 exist. len(window) == 4 proves the +1 probe
        # was applied (a plain LIMIT 3 would have returned only 3 rows,
        # making it impossible for the caller to detect the 4th exists).
        window = await service.list_own(user_context, limit=3, offset=0)
        assert len(window) == 4

    async def test_list_own_window_bounded_when_fewer_rows_exist(
        self, user_context
    ) -> None:
        service = MockSessionMemoryService()
        await service.write(user_context, "only.txt", "solo")
        window = await service.list_own(user_context, limit=10, offset=0)
        assert len(window) == 1


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


class TestPostgresSessionMemoryServiceListOwn:
    """WU-5 — ``list_own`` against the real (aiosqlite) SQLAlchemy path."""

    async def test_list_own_empty(self, service, user_context) -> None:
        assert await service.list_own(user_context, limit=5) == []

    async def test_list_own_recency_order(self, service, user_context) -> None:
        await service.write(user_context, "first.txt", "one")
        await service.write(user_context, "second.txt", "two")
        await service.write(user_context, "third.txt", "three")
        window = await service.list_own(user_context, limit=10)
        assert [d.metadata["filename"] for d in window] == [
            "third.txt",
            "second.txt",
            "first.txt",
        ]

    async def test_list_own_cross_user_isolation(self, service, user_context) -> None:
        """Non-vacuity guard (spec §6.1): the same isolation wall as
        ``read_own``'s acceptance test — neuter the explicit ``user_id``
        filter in ``PostgresSessionMemoryService.list_own`` and Bob's
        window starts including Alice's uploads. Bracketed with
        ``set_current_user_id`` per ``feedback_unit_tests_miss_rls`` (RLS
        is a no-op on SQLite; the guard under test is the service's own
        explicit ``.filter(...)`` clause)."""
        alice = replace(user_context, user_id="user-alice-list-rls", is_admin=False)
        bob = replace(user_context, user_id="user-bob-list-rls", is_admin=False)

        set_current_user_id(alice.user_id)
        try:
            await service.write(alice, "secret.txt", "alice's private upload")
        finally:
            set_current_user_id(None)

        set_current_user_id(bob.user_id)
        try:
            bob_window = await service.list_own(bob, limit=10)
        finally:
            set_current_user_id(None)

        assert bob_window == [], (
            "user B's list_own window included user A's upload — the "
            "isolation wall is broken (missing/neutered user_id filter)"
        )

        set_current_user_id(alice.user_id)
        try:
            alice_window = await service.list_own(alice, limit=10)
        finally:
            set_current_user_id(None)
        assert len(alice_window) == 1
        assert alice_window[0].page_content == "alice's private upload"

    async def test_list_own_plus_one_probe_window_size(
        self, service, user_context
    ) -> None:
        """Non-vacuity guard (spec §6.4): fetches
        ``min(offset + limit + 1, MAX_RECALL_WINDOW)`` rows — a plain
        ``LIMIT limit`` would return only 3 rows here, hiding the 4th
        from the caller's ``has_more`` computation."""
        for i in range(4):
            await service.write(user_context, f"note-{i}.txt", f"content {i}")
        window = await service.list_own(user_context, limit=3, offset=0)
        assert len(window) == 4

    async def test_list_own_window_bounded_when_fewer_rows_exist(
        self, service, user_context
    ) -> None:
        await service.write(user_context, "only.txt", "solo")
        window = await service.list_own(user_context, limit=10, offset=0)
        assert len(window) == 1
