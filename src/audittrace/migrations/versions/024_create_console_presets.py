"""create console_presets table + RLS policy

Revision ID: f1c3a9d7b2e4
Revises: dbce4d563e86
Create Date: 2026-09-11 00:00:00.000000

WU-presets of the MongoDB-elimination EPIC
(2026-09-11-SPEC-mongo-repl-wu-presets-store.md) — mirrors WU-1's
migration 023 shape exactly, for a different domain (LibreChat's saved
model/endpoint presets, ``packages/data-schemas/src/schema/preset.ts``,
Mongo ``Preset`` collection). Adds ``console_presets`` (one row per
caller-minted ``preset_id``, config carried as a ``data`` jsonb blob).

RLS mirrors migrations 022/023 verbatim in shape: ENABLE + FORCE ROW
LEVEL SECURITY, one ``FOR ALL`` policy comparing ``user_sub`` against
``current_setting('app.current_user_id', true)`` in both USING and WITH
CHECK. Guarded by ``_is_postgres()`` so SQLite (the unit-test factory,
``InMemoryPostgresFactory``) creates the plain table with no RLS — the
service layer (``PostgresConsolePresetsService``) additionally filters
every query by ``user_sub`` explicitly so cross-user isolation is still
caught by the SQLite unit suite, not only a live-Postgres integration
run (feedback_unit_tests_miss_rls).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "f1c3a9d7b2e4"
down_revision: str | Sequence[str] | None = "dbce4d563e86"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JsonType = sa.JSON().with_variant(JSONB(), "postgresql")


def _is_postgres() -> bool:
    """Return True when Alembic is running against PostgreSQL.

    SQLite (the in-memory test factory) has no RLS concept; the
    upgrade/downgrade RLS statements below are skipped on that path —
    same guard shape as migrations 005, 022, and 023.
    """
    bind = op.get_bind()
    return bool(bind.dialect.name == "postgresql")


def upgrade() -> None:
    """Create console_presets + indexes, then (Postgres only) enable +
    force RLS with a per-user policy."""
    op.create_table(
        "console_presets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("preset_id", sa.String(length=255), nullable=False),
        sa.Column("user_sub", sa.String(length=36), nullable=False),
        sa.Column(
            "title",
            sa.String(length=512),
            nullable=False,
            server_default="New Chat",
        ),
        sa.Column("data", _JsonType, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at_ms", sa.BigInteger(), nullable=True),
        sa.Column(
            "metadata", _JsonType, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.UniqueConstraint(
            "user_sub", "preset_id", name="uq_console_presets_user_preset"
        ),
    )
    op.create_index(
        "ix_console_presets_user_sub",
        "console_presets",
        ["user_sub"],
        unique=False,
    )
    # Cursor-pagination shape (list_presets orders newest-first by
    # updated_at_ms within the caller's own rows) — same rationale as
    # migration 023's console_conversations index.
    op.create_index(
        "ix_console_presets_user_sub_updated_at_ms",
        "console_presets",
        ["user_sub", "updated_at_ms"],
        unique=False,
    )

    if not _is_postgres():
        return

    op.execute("ALTER TABLE console_presets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE console_presets FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_console_presets ON console_presets
            FOR ALL
            USING (user_sub = current_setting('app.current_user_id', true))
            WITH CHECK (user_sub = current_setting('app.current_user_id', true))
        """
    )


def downgrade() -> None:
    """Reverse: drop the RLS policy (Postgres only), then the indexes +
    table."""
    if _is_postgres():
        op.execute(
            "DROP POLICY IF EXISTS tenant_isolation_console_presets ON console_presets"
        )
        op.execute("ALTER TABLE console_presets NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE console_presets DISABLE ROW LEVEL SECURITY")

    op.drop_index(
        "ix_console_presets_user_sub_updated_at_ms", table_name="console_presets"
    )
    op.drop_index("ix_console_presets_user_sub", table_name="console_presets")
    op.drop_table("console_presets")
