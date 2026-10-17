"""create console_chat_projects table + RLS policy

Revision ID: a3f7c92e1d5b
Revises: c4a8f16e9d72
Create Date: 2026-09-11 00:00:00.000000

Chat-Projects domain of the MongoDB-elimination EPIC
(2026-09-11-SPEC-mongo-repl-wu-chatprojects-store.md) — mirrors WU-1's
migration 023 (and WU-presets' migration 024) shape exactly, for a
different domain (LibreChat's first-class chat-projects,
``packages/data-schemas/src/schema/chatProject.ts``, Mongo
``ChatProject`` collection). Adds ``console_chat_projects`` (one row per
caller-minted ``chat_project_id``): name/description as first-class
columns (unlike WU-presets' single ``data`` blob — a chat-project's
shape is a plain name+description+metadata record, not an open
index-signature bag), plus a ``metadata`` jsonb column for forward
compatibility.

RLS mirrors migrations 022/023/024/025 verbatim in shape: ENABLE +
FORCE ROW LEVEL SECURITY, one ``FOR ALL`` policy comparing ``user_sub``
against ``current_setting('app.current_user_id', true)`` in both USING
and WITH CHECK. Guarded by ``_is_postgres()`` so SQLite (the unit-test
factory, ``InMemoryPostgresFactory``) creates the plain table with no
RLS — the service layer (``PostgresConsoleChatProjectsService``)
additionally filters every query by ``user_sub`` explicitly so
cross-user isolation is still caught by the SQLite unit suite, not only
a live-Postgres integration run (feedback_unit_tests_miss_rls).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "a3f7c92e1d5b"
down_revision: str | Sequence[str] | None = "c4a8f16e9d72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JsonType = sa.JSON().with_variant(JSONB(), "postgresql")


def _is_postgres() -> bool:
    """Return True when Alembic is running against PostgreSQL.

    SQLite (the in-memory test factory) has no RLS concept; the
    upgrade/downgrade RLS statements below are skipped on that path —
    same guard shape as migrations 005, 022, 023, 024, and 025.
    """
    bind = op.get_bind()
    return bool(bind.dialect.name == "postgresql")


def upgrade() -> None:
    """Create console_chat_projects + indexes, then (Postgres only)
    enable + force RLS with a per-user policy."""
    op.create_table(
        "console_chat_projects",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("chat_project_id", sa.String(length=255), nullable=False),
        sa.Column("user_sub", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at_ms", sa.BigInteger(), nullable=True),
        sa.Column(
            "metadata", _JsonType, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.UniqueConstraint(
            "user_sub",
            "chat_project_id",
            name="uq_console_chat_projects_user_project",
        ),
    )
    op.create_index(
        "ix_console_chat_projects_user_sub",
        "console_chat_projects",
        ["user_sub"],
        unique=False,
    )
    # Cursor-pagination shape (list_chat_projects orders newest-first by
    # updated_at_ms within the caller's own rows) — same rationale as
    # migration 024's console_presets index.
    op.create_index(
        "ix_console_chat_projects_user_sub_updated_at_ms",
        "console_chat_projects",
        ["user_sub", "updated_at_ms"],
        unique=False,
    )

    if not _is_postgres():
        return

    op.execute("ALTER TABLE console_chat_projects ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE console_chat_projects FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_console_chat_projects ON console_chat_projects
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
            "DROP POLICY IF EXISTS tenant_isolation_console_chat_projects "
            "ON console_chat_projects"
        )
        op.execute("ALTER TABLE console_chat_projects NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE console_chat_projects DISABLE ROW LEVEL SECURITY")

    op.drop_index(
        "ix_console_chat_projects_user_sub_updated_at_ms",
        table_name="console_chat_projects",
    )
    op.drop_index(
        "ix_console_chat_projects_user_sub", table_name="console_chat_projects"
    )
    op.drop_table("console_chat_projects")
