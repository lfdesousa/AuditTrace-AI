"""Tests for routes/health.py — SPEC #387 P2b scan/index visibility.

Falsifiability anchor (SPEC Acceptance §4): each of the four additive
``/health`` fields is proven by seeding rows in every state the field's
predicate distinguishes, so dropping any one predicate makes a
non-matching row leak into the count — RED. The best-effort contract
(DB/AMQP failure degrades the AFFECTED field to "unknown", readiness
still "ok") is proven by forcing the DB lookup and the AMQP round-trip
to raise independently.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest_asyncio

from audittrace import dependencies
from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.routes import health as health_mod
from audittrace.routes.memory_upload import manifest as manifest_mod


def _settings(
    *, grace: int = 60, pipeline_enabled: bool = False, amqp_url: str = ""
) -> MagicMock:
    s = MagicMock()
    s.scan_janitor_grace_seconds = grace
    s.scan_pipeline_enabled = pipeline_enabled
    s.scan_amqp_url = amqp_url
    return s


async def _seed_item(
    factory,
    *,
    scan_id: str,
    age_seconds: int = 0,
    scan_status: str | None = "pending_scan",
    indexed_at_ms: int | None = None,
    index_failed_at_ms: int | None = None,
    deleted: bool = False,
) -> None:
    """Insert a ``memory_items`` row in an arbitrary scan/index state,
    mirroring ``test_index_janitor.py``'s ``_seed`` idiom: start from a
    real insert (so every non-overridden column is realistic), then
    mutate to the exact combination the test needs."""
    async with factory() as session:
        row = await manifest_mod.insert_pending_scan(
            session,
            scan_id=scan_id,
            user_id="alice",
            object_uri=f"s3://x/{scan_id}",
            object_sha256="0" * 64,
            size_bytes=1,
            title="x",
            trace_id="t",
        )
        row.created_at_ms = int(time.time() * 1000) - (age_seconds * 1000)
        row.scan_status = scan_status
        row.indexed_at_ms = indexed_at_ms
        row.index_failed_at_ms = index_failed_at_ms
        if deleted:
            row.deleted_at_ms = int(time.time() * 1000)
        await session.commit()


@pytest_asyncio.fixture
async def pg_factory():
    """Register a fresh in-memory Postgres factory as the DI container's
    ``postgres_factory`` — ``_scan_index_health_fields`` looks it up via
    ``get_postgres_factory()`` at call time."""
    factory = InMemoryPostgresFactory()
    dependencies.container._instances["postgres_factory"] = factory
    yield factory.get_session_factory()
    dependencies.container._instances.pop("postgres_factory", None)


class TestScanIndexHealthFieldsDb:
    async def test_no_postgres_factory_degrades_all_three_to_unknown(
        self, monkeypatch
    ) -> None:
        # No postgres_factory registered at all (container reset by the
        # autouse `_reset_global_container` fixture) — `get_postgres_
        # factory()` itself raises, so the DB lookup never happens.
        monkeypatch.setattr(health_mod, "get_settings", lambda: _settings())
        fields = await health_mod._scan_index_health_fields()
        assert fields["pending_scan_orphans"] == "unknown"
        assert fields["unindexed_count"] == "unknown"
        assert fields["dead_lettered_index_count"] == "unknown"
        assert fields["scan_dlq_depth"] == "unknown"

    async def test_pending_scan_orphans_counts_only_stuck_pending_rows(
        self, pg_factory, monkeypatch
    ) -> None:
        await _seed_item(
            pg_factory, scan_id="stuck-1", age_seconds=200, scan_status="pending_scan"
        )
        # Fresh (within grace) — must NOT count. Falsifiability: drop the
        # `created_at_ms < cutoff` predicate and this row leaks in — RED.
        await _seed_item(
            pg_factory, scan_id="fresh-1", age_seconds=5, scan_status="pending_scan"
        )
        # Soft-deleted — must NOT count. Falsifiability: drop the
        # `deleted_at_ms IS NULL` predicate and this row leaks in — RED.
        await _seed_item(
            pg_factory,
            scan_id="deleted-1",
            age_seconds=200,
            scan_status="pending_scan",
            deleted=True,
        )
        # Different scan_status — must NOT count. Falsifiability: drop
        # the `scan_status == "pending_scan"` predicate and this row
        # leaks in — RED.
        await _seed_item(
            pg_factory,
            scan_id="clean-1",
            age_seconds=200,
            scan_status="scanned_clean",
        )

        monkeypatch.setattr(health_mod, "get_settings", lambda: _settings(grace=60))
        fields = await health_mod._scan_index_health_fields()
        assert fields["pending_scan_orphans"] == "1"

    async def test_unindexed_count_counts_only_clean_undrained_rows(
        self, pg_factory, monkeypatch
    ) -> None:
        await _seed_item(
            pg_factory,
            scan_id="undrained-1",
            age_seconds=200,
            scan_status="scanned_clean",
        )
        # Already indexed — must NOT count. Falsifiability: drop the
        # `indexed_at_ms IS NULL` predicate and this row leaks in — RED.
        await _seed_item(
            pg_factory,
            scan_id="indexed-1",
            age_seconds=200,
            scan_status="scanned_clean",
            indexed_at_ms=1_700_000_000_000,
        )
        # Dead-lettered — must NOT count (it's a DIFFERENT field's
        # concern). Falsifiability: drop the
        # `index_failed_at_ms IS NULL` predicate and this row leaks
        # in — RED.
        await _seed_item(
            pg_factory,
            scan_id="dead-1",
            age_seconds=200,
            scan_status="scanned_clean",
            index_failed_at_ms=1_700_000_000_000,
        )
        # Not scanned_clean at all — must NOT count. Falsifiability:
        # drop the `scan_status == "scanned_clean"` predicate and this
        # row leaks in — RED.
        await _seed_item(
            pg_factory, scan_id="pending-1", age_seconds=200, scan_status="pending_scan"
        )
        # Soft-deleted — must NOT count. Falsifiability: drop the
        # `deleted_at_ms IS NULL` predicate and this row leaks in — RED.
        await _seed_item(
            pg_factory,
            scan_id="deleted-1",
            age_seconds=200,
            scan_status="scanned_clean",
            deleted=True,
        )

        monkeypatch.setattr(health_mod, "get_settings", lambda: _settings(grace=60))
        fields = await health_mod._scan_index_health_fields()
        assert fields["unindexed_count"] == "1"

    async def test_dead_lettered_index_count_counts_only_terminal_rows(
        self, pg_factory, monkeypatch
    ) -> None:
        await _seed_item(
            pg_factory,
            scan_id="dead-1",
            scan_status="scanned_clean",
            index_failed_at_ms=1_700_000_000_000,
        )
        # Not dead-lettered — must NOT count. Falsifiability: drop the
        # `index_failed_at_ms IS NOT NULL` predicate and this row leaks
        # in — RED.
        await _seed_item(pg_factory, scan_id="live-1", scan_status="scanned_clean")
        # Soft-deleted dead-letter — must NOT count. Falsifiability:
        # drop the `deleted_at_ms IS NULL` predicate and this row leaks
        # in — RED.
        await _seed_item(
            pg_factory,
            scan_id="deleted-dead-1",
            scan_status="scanned_clean",
            index_failed_at_ms=1_700_000_000_000,
            deleted=True,
        )

        monkeypatch.setattr(health_mod, "get_settings", lambda: _settings(grace=60))
        fields = await health_mod._scan_index_health_fields()
        assert fields["dead_lettered_index_count"] == "1"

    async def test_one_bad_field_query_degrades_only_that_field(
        self, pg_factory, monkeypatch
    ) -> None:
        """Best-effort per-field contract: force ONE of the three
        ``_count_memory_items`` calls to come back "unknown" and
        confirm the OTHER two still surface real values (i.e.
        ``_scan_index_health_fields`` makes three INDEPENDENT calls,
        not one big try/except around the whole session block — a
        single bad field can never take the other two down with it).
        """
        await _seed_item(
            pg_factory,
            scan_id="ok-1",
            age_seconds=200,
            scan_status="pending_scan",
        )

        calls: list[str] = []
        orig_count = health_mod._count_memory_items

        async def _flaky_count(session, *predicates, field_name):
            calls.append(field_name)
            if field_name == "unindexed_count":
                return "unknown"
            return await orig_count(session, *predicates, field_name=field_name)

        monkeypatch.setattr(health_mod, "_count_memory_items", _flaky_count)
        monkeypatch.setattr(health_mod, "get_settings", lambda: _settings(grace=60))
        fields = await health_mod._scan_index_health_fields()
        assert fields["pending_scan_orphans"] == "1"
        assert fields["unindexed_count"] == "unknown"
        assert fields["dead_lettered_index_count"] == "0"
        assert calls == [
            "pending_scan_orphans",
            "unindexed_count",
            "dead_lettered_index_count",
        ]


class TestCountMemoryItems:
    """Direct unit coverage of ``_count_memory_items``'s own best-effort
    contract — a query failure (not just a missing DB) degrades to
    "unknown" too."""

    async def test_query_failure_degrades_to_unknown(self) -> None:
        from audittrace.db.models import MemoryItem

        broken_session = MagicMock()

        async def _raise(*_a, **_kw):
            raise RuntimeError("boom")

        broken_session.execute = _raise
        result = await health_mod._count_memory_items(
            broken_session,
            MemoryItem.scan_status == "pending_scan",
            field_name="pending_scan_orphans",
        )
        assert result == "unknown"


class TestScanDlqDepth:
    async def test_disabled_pipeline_is_unknown(self) -> None:
        fields = await health_mod._scan_dlq_depth(_settings(pipeline_enabled=False))
        assert fields == "unknown"

    async def test_enabled_but_no_url_is_unknown(self) -> None:
        fields = await health_mod._scan_dlq_depth(
            _settings(pipeline_enabled=True, amqp_url="")
        )
        assert fields == "unknown"

    async def test_enabled_surfaces_message_count(self, monkeypatch) -> None:
        async def _fake_count(settings):
            return 7

        monkeypatch.setattr(health_mod, "_get_scan_dlq_message_count", _fake_count)
        depth = await health_mod._scan_dlq_depth(
            _settings(pipeline_enabled=True, amqp_url="amqp://x")
        )
        assert depth == "7"

    async def test_amqp_failure_degrades_to_unknown(self, monkeypatch) -> None:
        async def _raise(settings):
            raise RuntimeError("broker unreachable")

        monkeypatch.setattr(health_mod, "_get_scan_dlq_message_count", _raise)
        depth = await health_mod._scan_dlq_depth(
            _settings(pipeline_enabled=True, amqp_url="amqp://x")
        )
        assert depth == "unknown"

    async def test_get_scan_dlq_message_count_passive_declares_the_dlq(
        self, monkeypatch
    ) -> None:
        """Direct coverage of ``_get_scan_dlq_message_count``'s real
        aio_pika round-trip (not the ``_scan_dlq_depth`` wrapper): a
        fake ``aio_pika.connect`` proves the function passively
        declares the EXACT ADR-057 DLQ queue name and surfaces its
        ``message_count``, and that the connection is closed on exit.

        Falsifiability: change ``passive=True`` to omit the kwarg (or
        typo the queue name) and this test's assertions on
        ``fake_channel.declared`` go RED.
        """
        import aio_pika

        class _FakeDeclarationResult:
            def __init__(self, message_count: int) -> None:
                self.message_count = message_count

        class _FakeQueue:
            def __init__(self, message_count: int) -> None:
                self.declaration_result = _FakeDeclarationResult(message_count)

        class _FakeChannel:
            def __init__(self) -> None:
                self.declared: list[tuple[str, bool]] = []

            async def declare_queue(self, name: str, *, passive: bool) -> _FakeQueue:
                self.declared.append((name, passive))
                return _FakeQueue(message_count=4)

        fake_channel = _FakeChannel()

        class _FakeConnection:
            def __init__(self) -> None:
                self.closed = False

            async def channel(self) -> _FakeChannel:
                return fake_channel

            async def close(self) -> None:
                self.closed = True

            async def __aenter__(self) -> _FakeConnection:
                return self

            async def __aexit__(self, *exc_info: object) -> None:
                await self.close()

        fake_connection = _FakeConnection()

        async def _fake_connect(url: str, timeout: float) -> _FakeConnection:
            assert url == "amqp://x"
            return fake_connection

        monkeypatch.setattr(aio_pika, "connect", _fake_connect)
        settings = _settings(pipeline_enabled=True, amqp_url="amqp://x")
        count = await health_mod._get_scan_dlq_message_count(settings)
        assert count == 4
        assert fake_channel.declared == [(health_mod._SCAN_DLQ_QUEUE_NAME, True)]
        assert fake_connection.closed is True


class TestHealthEndpointIntegration:
    """/health stays ``status="ok"`` and additive-only (open string map)
    even when every scan/index-visibility backend is broken."""

    async def test_health_ok_when_scan_index_backends_all_fail(
        self, monkeypatch
    ) -> None:
        # No postgres_factory registered (container reset by the autouse
        # fixture) AND the scan pipeline is disabled by default settings
        # — both DB and AMQP paths degrade.
        response = await health_mod.health_check()
        assert response.status == "ok"
        for field in (
            "pending_scan_orphans",
            "unindexed_count",
            "dead_lettered_index_count",
            "scan_dlq_depth",
        ):
            assert field in response.components
            assert response.components[field] == "unknown"

    async def test_health_surfaces_real_counts_when_db_available(
        self, pg_factory
    ) -> None:
        await _seed_item(
            pg_factory,
            scan_id="dead-1",
            scan_status="scanned_clean",
            index_failed_at_ms=1_700_000_000_000,
        )
        response = await health_mod.health_check()
        assert response.status == "ok"
        assert response.components["dead_lettered_index_count"] == "1"
