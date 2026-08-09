"""Tests for Alembic migrations — ADR-020.

Validates that migrations apply cleanly, rollback works, and the
resulting schema matches our SQLAlchemy models.
"""

import pathlib

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

import audittrace
from audittrace.config import Settings


@pytest.fixture
def alembic_cfg():
    """Alembic config pointing at an in-memory SQLite database."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", "sqlite://")
    return cfg


@pytest.fixture
def engine():
    """In-memory SQLite engine for migration testing."""
    return create_engine("sqlite://", echo=False)


class TestAlembicMigrations:
    def test_upgrade_head_applies_cleanly(self, alembic_cfg, engine):
        """Upgrade to head creates the sessions table with correct schema."""
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")

            inspector = inspect(conn)
            tables = inspector.get_table_names()
            assert "sessions" in tables
            assert "alembic_version" in tables

    def test_upgrade_creates_correct_columns(self, alembic_cfg, engine):
        """Verify columns after migration match our model."""
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")

            inspector = inspect(conn)
            columns = {col["name"] for col in inspector.get_columns("sessions")}
            # user_id (Phase 0 multi-user identity) and summarized_at
            # (ADR-030 Part 2 background summariser) are later additions;
            # trace_id (#344, migration 014) links a summary row to the
            # summariser run's trace.
            assert columns == {
                "id",
                "project",
                "date",
                "summary",
                "key_points",
                "model",
                "user_id",
                "summarized_at",
                "trace_id",
            }

    def test_upgrade_creates_project_index(self, alembic_cfg, engine):
        """Verify project index is created by migration."""
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")

            inspector = inspect(conn)
            indexes = inspector.get_indexes("sessions")
            indexed_columns = {col for idx in indexes for col in idx["column_names"]}
            assert "project" in indexed_columns

    def test_downgrade_removes_table(self, alembic_cfg, engine):
        """Downgrade to base removes the sessions table."""
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")

            # Verify table exists
            inspector = inspect(conn)
            assert "sessions" in inspector.get_table_names()

            # Downgrade
            command.downgrade(alembic_cfg, "base")

            # Refresh inspector after schema change
            inspector = inspect(conn)
            assert "sessions" not in inspector.get_table_names()


class TestAlembicUsesSyncDriver:
    """Regression guard — kind integration crashloop 2026-05-31.

    The async rewrite (#263) made ``Settings.database_url`` return the
    ``postgresql+asyncpg://`` URL (the runtime engine driver). Alembic, however,
    runs SYNCHRONOUSLY (``engine_from_config`` + ``connectable.connect()`` in
    ``migrations/env.py``). Handing it the asyncpg URL makes SQLAlchemy attempt
    async I/O on a plain sync connection → ``sqlalchemy.exc.MissingGreenlet`` at
    migration time, which crashes the memory-server entrypoint before the app
    starts (CrashLoopBackOff → ``helm install --wait`` deadline exceeded). The
    unit suite missed it because it runs migrations on SQLite, never asyncpg.
    ``env.py`` MUST push ``database_url_sync`` (psycopg2) into ``sqlalchemy.url``.
    """

    def test_env_py_selects_the_sync_url(self) -> None:
        env_src = (
            pathlib.Path(audittrace.__file__).parent / "migrations" / "env.py"
        ).read_text(encoding="utf-8")
        # Must use the sync helper for the Alembic override...
        assert "database_url_sync" in env_src
        # ...and must NOT push the bare async ``database_url`` (the regression).
        assert "settings.database_url)" not in env_src

    def test_sync_url_builds_a_non_async_engine(self) -> None:
        """A sync engine from ``database_url_sync`` must not be an async dialect
        — that is exactly the ``MissingGreenlet`` precondition."""
        settings = Settings(postgres_url="postgresql+asyncpg://u:p@h:5432/db")
        # The runtime engine stays async (the whole point of #263)...
        assert settings.database_url is not None
        assert settings.database_url.startswith("postgresql+asyncpg://")
        # ...but Alembic's URL must be a synchronous psycopg2 dialect.
        sync_url = settings.database_url_sync
        assert sync_url is not None
        assert sync_url.startswith("postgresql+psycopg2://")
        engine = create_engine(sync_url)
        try:
            assert engine.dialect.is_async is False
        finally:
            engine.dispose()


class TestForwardMigration004KeycloakDelegated:
    """ADR-026 §15 — migration 004 retires the
    local users tables in favour of Keycloak-delegated identity.

    These tests assert the FINAL state of the schema after running every
    migration up to head: users / user_roles / pat_tokens are absent;
    interactions / sessions / tool_calls remain with their user_id
    columns intact (now Keycloak sub strings, no FK)."""

    def test_users_table_dropped(self, alembic_cfg, engine):
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")
            inspector = inspect(conn)
            assert "users" not in inspector.get_table_names()

    def test_user_roles_table_dropped(self, alembic_cfg, engine):
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")
            inspector = inspect(conn)
            assert "user_roles" not in inspector.get_table_names()

    def test_pat_tokens_table_dropped(self, alembic_cfg, engine):
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")
            inspector = inspect(conn)
            assert "pat_tokens" not in inspector.get_table_names()

    def test_tool_calls_table_present(self, alembic_cfg, engine):
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")
            inspector = inspect(conn)
            assert "tool_calls" in inspector.get_table_names()

    def test_tool_calls_user_id_no_fk_to_users(self, alembic_cfg, engine):
        """user_id is now a Keycloak sub string — no FK to a local users table."""
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")
            inspector = inspect(conn)
            for fk in inspector.get_foreign_keys("tool_calls"):
                assert fk["referred_table"] != "users"

    def test_interactions_user_id_present_no_fk(self, alembic_cfg, engine):
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")
            inspector = inspect(conn)
            columns = {c["name"] for c in inspector.get_columns("interactions")}
            assert "user_id" in columns
            for fk in inspector.get_foreign_keys("interactions"):
                assert fk["referred_table"] != "users"

    def test_sessions_user_id_present_no_fk(self, alembic_cfg, engine):
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")
            inspector = inspect(conn)
            columns = {c["name"] for c in inspector.get_columns("sessions")}
            assert "user_id" in columns
            for fk in inspector.get_foreign_keys("sessions"):
                assert fk["referred_table"] != "users"

    def test_downgrade_to_004_minus_recreates_users(self, alembic_cfg, engine):
        """Downgrading 004 → 003 must recreate the dropped tables.

        This tests the downgrade() function in migration 004 — recreating
        users, user_roles, and pat_tokens tables.
        """
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")
            inspector = inspect(conn)
            assert "users" not in inspector.get_table_names()

            command.downgrade(alembic_cfg, "c4e6f8a0b2d4")  # back to migration 003
            inspector = inspect(conn)
            assert "users" in inspector.get_table_names()
            assert "user_roles" in inspector.get_table_names()
            assert "pat_tokens" in inspector.get_table_names()


class TestMigration018AddTierToMemoryItems:
    """ADR-062 Phase B (WU-B4) — migration 018 adds ``memory_items.tier``.

    D2 (ratified 2026-08-04): existing rows (and any INSERT that doesn't
    set the column explicitly) read ``tier="corpus"`` — today's shared
    knowledge base stays visible to everyone unchanged, zero backfill
    required for this migration alone."""

    def test_upgrade_adds_tier_column_not_null(self, alembic_cfg, engine):
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")
            inspector = inspect(conn)
            columns = {c["name"]: c for c in inspector.get_columns("memory_items")}
            assert "tier" in columns
            assert columns["tier"]["nullable"] is False

    def test_pre_migration_018_row_reads_tier_corpus(self, alembic_cfg, engine):
        """D2's falsifiable core claim: a row inserted WITHOUT ``tier``
        (simulating every pre-migration-018 row, and any INSERT path
        this PR didn't touch) reads back ``tier="corpus"`` via the
        column's ``server_default`` — never NULL, never silently
        ``"private"``."""
        import sqlalchemy as sa

        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")

            memory_items = sa.Table("memory_items", sa.MetaData(), autoload_with=conn)
            conn.execute(
                memory_items.insert().values(
                    id="legacy-row-1",
                    layer="episodic",
                    key="ADR-legacy.md",
                    created_at_ms=1,
                    modified_at_ms=1,
                    created_by_user_id="legacy-user",
                    modified_by_user_id="legacy-user",
                    # tier intentionally omitted — server_default must fire.
                )
            )
            row = conn.execute(
                sa.select(memory_items.c.tier).where(
                    memory_items.c.id == "legacy-row-1"
                )
            ).scalar_one()
            assert row == "corpus"

    def test_downgrade_removes_tier_column(self, alembic_cfg, engine):
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")
            inspector = inspect(conn)
            assert "tier" in {c["name"] for c in inspector.get_columns("memory_items")}

            command.downgrade(alembic_cfg, "d8a0c2e4f637")  # migration 017 (pre-018)
            inspector = inspect(conn)
            assert "tier" not in {
                c["name"] for c in inspector.get_columns("memory_items")
            }


class TestMigration019AddIndexedAtMsToMemoryItems:
    """SPEC #387 Phase 1 (WU-1) — migration 019 adds
    ``memory_items.indexed_at_ms``, the place→index outbox marker one hop
    after ``published_at_ms`` (migration 013)."""

    def test_upgrade_adds_indexed_at_ms_column_nullable(self, alembic_cfg, engine):
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")
            inspector = inspect(conn)
            columns = {c["name"]: c for c in inspector.get_columns("memory_items")}
            assert "indexed_at_ms" in columns
            assert columns["indexed_at_ms"]["nullable"] is True

    def test_upgrade_creates_index_pending_composite_index(self, alembic_cfg, engine):
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")
            inspector = inspect(conn)
            indexes = inspector.get_indexes("memory_items")
            names = {idx["name"] for idx in indexes}
            assert "ix_memory_items_index_pending" in names

    def test_pre_migration_019_row_reads_indexed_at_ms_null(self, alembic_cfg, engine):
        """A row inserted WITHOUT ``indexed_at_ms`` (every pre-migration
        row, and every row outside the scan pipeline) reads back NULL —
        never a stray default that would make it look already-indexed."""
        import sqlalchemy as sa

        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")

            memory_items = sa.Table("memory_items", sa.MetaData(), autoload_with=conn)
            conn.execute(
                memory_items.insert().values(
                    id="legacy-row-2",
                    layer="episodic",
                    key="ADR-legacy-2.md",
                    created_at_ms=1,
                    modified_at_ms=1,
                    created_by_user_id="legacy-user",
                    modified_by_user_id="legacy-user",
                    # indexed_at_ms intentionally omitted.
                )
            )
            row = conn.execute(
                sa.select(memory_items.c.indexed_at_ms).where(
                    memory_items.c.id == "legacy-row-2"
                )
            ).scalar_one()
            assert row is None

    def test_downgrade_removes_indexed_at_ms_column(self, alembic_cfg, engine):
        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")
            inspector = inspect(conn)
            assert "indexed_at_ms" in {
                c["name"] for c in inspector.get_columns("memory_items")
            }

            command.downgrade(alembic_cfg, "e1c2b4d6a859")  # migration 018 (pre-019)
            inspector = inspect(conn)
            assert "indexed_at_ms" not in {
                c["name"] for c in inspector.get_columns("memory_items")
            }


class TestEnvPyDoesNotDisableOtherLoggers:
    """Regression guard for a session-wide footgun found building #411 v2.

    ``src/audittrace/migrations/env.py`` calls ``fileConfig(alembic.ini)`` on
    EVERY ``command.upgrade``/``command.downgrade``. ``fileConfig``'s DEFAULT
    (``disable_existing_loggers=True``) silently sets ``.disabled = True`` on
    every Python logger that already exists in the process — and under
    pytest ALL test modules (so every module-scoped
    ``logging.getLogger(__name__)``) are imported at collection time, before
    any test body runs. So the FIRST alembic upgrade anywhere in the session
    used to permanently mute every OTHER module's logger for the rest of the
    run: ``scripts.deploy.mesh``'s ``logger.error(...)`` in the #411 v2 G3
    tests went silently no-op whenever ``tests/test_alembic.py`` happened to
    collect/run first (alphabetically before ``test_deploy_mesh.py``) — never
    reproducible in isolation, only in the full suite. Falsifiable: reverting
    ``env.py`` to a bare ``fileConfig(config.config_file_name)`` call turns
    this RED.
    """

    def test_upgrade_does_not_disable_a_pre_existing_module_logger(
        self, alembic_cfg, engine
    ):
        import logging

        canary = logging.getLogger("audittrace.tests.canary_for_env_py_disable")
        assert canary.disabled is False  # sanity: freshly created, not disabled

        with engine.begin() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.upgrade(alembic_cfg, "head")

        assert canary.disabled is False
