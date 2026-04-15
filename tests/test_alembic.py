"""Tests for Alembic migrations — ADR-020.

Validates that migrations apply cleanly, rollback works, and the
resulting schema matches our SQLAlchemy models.
"""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


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
            # (ADR-030 Part 2 background summariser) are later additions.
            assert columns == {
                "id",
                "project",
                "date",
                "summary",
                "key_points",
                "model",
                "user_id",
                "summarized_at",
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
