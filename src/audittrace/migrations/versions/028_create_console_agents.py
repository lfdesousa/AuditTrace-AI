"""create console_agents table + RLS policy

Revision ID: 43568ad57fba
Revises: 6694d8019051
Create Date: 2026-09-12 00:00:00.000000

Agents domain of the MongoDB-elimination EPIC
(2026-09-12-SPEC-mongo-repl-wu-agents-store.md) — mirrors the
Files-metadata domain's migration 027 (and WU-1/WU-presets/WU-prompts/
Chat-Projects' migrations 023/024/025/026) shape exactly, for a
different domain (LibreChat's Agent record,
``packages/data-schemas/src/schema/agent.ts``, Mongo ``Agent``
collection). Adds ``console_agents`` (one row per caller-minted
``agent_id``): the fields the ratified spec names (name/description/
instructions/provider/model/model_parameters/tools/artifacts/
end_after_tools/project_ids/metadata) as first-class columns.

**Own-agents-only v1 (the ratified spec's scope boundary).** Agent
SHARING/marketplace (the fork's ``author``/global-agent concept) is OUT
OF SCOPE — this table has no "shared" or "global" row; every row is
owned by exactly one ``user_sub`` (the unique constraint below), same
discipline as every other console-* domain in this migration series.

RLS mirrors migrations 022-027 verbatim in shape: ENABLE + FORCE ROW
LEVEL SECURITY, one ``FOR ALL`` policy comparing ``user_sub`` against
``current_setting('app.current_user_id', true)`` in both USING and WITH
CHECK. Guarded by ``_is_postgres()`` so SQLite (the unit-test factory,
``InMemoryPostgresFactory``) creates the plain table with no RLS — the
service layer (``PostgresConsoleAgentsService``) additionally filters
every query by ``user_sub`` explicitly so cross-user isolation is still
caught by the SQLite unit suite, not only a live-Postgres integration
run (feedback_unit_tests_miss_rls).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "43568ad57fba"
down_revision: str | Sequence[str] | None = "6694d8019051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JsonType = sa.JSON().with_variant(JSONB(), "postgresql")


def _is_postgres() -> bool:
    """Return True when Alembic is running against PostgreSQL.

    SQLite (the in-memory test factory) has no RLS concept; the
    upgrade/downgrade RLS statements below are skipped on that path —
    same guard shape as migrations 005, 022-027.
    """
    bind = op.get_bind()
    return bool(bind.dialect.name == "postgresql")


def upgrade() -> None:
    """Create console_agents + indexes, then (Postgres only) enable +
    force RLS with a per-user policy."""
    op.create_table(
        "console_agents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("user_sub", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=255), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column(
            "model_parameters",
            _JsonType,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("tools", _JsonType, nullable=False, server_default=sa.text("'[]'")),
        sa.Column(
            "artifacts", _JsonType, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "end_after_tools",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # References console_chat_projects.chat_project_id — string keys,
        # NO FK, same non-FK cross-table-reference convention as every
        # other console-* domain in this module (e.g.
        # ConsoleConversation.chat_project_id).
        sa.Column(
            "project_ids", _JsonType, nullable=False, server_default=sa.text("'[]'")
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
            "agent_id",
            name="uq_console_agents_user_agent",
        ),
    )
    op.create_index(
        "ix_console_agents_user_sub",
        "console_agents",
        ["user_sub"],
        unique=False,
    )
    # Cursor-pagination shape (list_agents orders newest-first by
    # updated_at_ms within the caller's own rows) — same rationale as
    # migration 027's console_files index.
    op.create_index(
        "ix_console_agents_user_sub_updated_at_ms",
        "console_agents",
        ["user_sub", "updated_at_ms"],
        unique=False,
    )
    # Batch-get-by-ids (``IN (agent_id, agent_id, ...)``, still scoped
    # by user_sub) — same rationale as migration 027's equivalent index.
    op.create_index(
        "ix_console_agents_user_sub_agent_id",
        "console_agents",
        ["user_sub", "agent_id"],
        unique=False,
    )

    if not _is_postgres():
        return

    op.execute("ALTER TABLE console_agents ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE console_agents FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_console_agents ON console_agents
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
            "DROP POLICY IF EXISTS tenant_isolation_console_agents ON console_agents"
        )
        op.execute("ALTER TABLE console_agents NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE console_agents DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_console_agents_user_sub_agent_id", table_name="console_agents")
    op.drop_index(
        "ix_console_agents_user_sub_updated_at_ms", table_name="console_agents"
    )
    op.drop_index("ix_console_agents_user_sub", table_name="console_agents")
    op.drop_table("console_agents")
