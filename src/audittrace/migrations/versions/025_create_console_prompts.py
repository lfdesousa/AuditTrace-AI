"""create console_prompt_groups + console_prompt_versions tables + RLS policies

Revision ID: c4a8f16e9d72
Revises: f1c3a9d7b2e4
Create Date: 2026-09-11 00:00:00.000000

Mongo-repl WU-prompts of the MongoDB-elimination EPIC
(2026-09-11-SPEC-mongo-repl-wu-prompts-store.md) — the AuditTrace-side
sovereign, RLS-isolated store for LibreChat's prompts (the fork's Mongo
``PromptGroup``/``Prompt`` collections). Adds ``console_prompt_groups``
(one row per prompt group, client-supplied string ``group_id``, plus a
``production_prompt_id`` pointer) and ``console_prompt_versions`` (one
row per prompt VERSION, client-supplied string ``prompt_id`` +
server-assigned monotonic ``version`` integer) — mirrors migration
023's group/child-rows shape (console_conversations/console_messages)
for a different domain.

RLS mirrors migration 023 (``dbce4d563e86``) verbatim in shape: ENABLE +
FORCE ROW LEVEL SECURITY, one ``FOR ALL`` policy per table comparing
``user_sub`` against ``current_setting('app.current_user_id', true)`` in
both USING and WITH CHECK. Guarded by ``_is_postgres()`` so SQLite (the
unit-test factory, ``InMemoryPostgresFactory``) creates the plain tables
with no RLS — the service layer (``PostgresConsolePromptsService``)
additionally filters every query by ``user_sub`` explicitly so
cross-user isolation is still caught by the SQLite unit suite, not only
a live-Postgres integration run (feedback_unit_tests_miss_rls).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "c4a8f16e9d72"
down_revision: str | Sequence[str] | None = "f1c3a9d7b2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MetadataType = sa.JSON().with_variant(JSONB(), "postgresql")


def _is_postgres() -> bool:
    """Return True when Alembic is running against PostgreSQL.

    SQLite (the in-memory test factory) has no RLS concept; the
    upgrade/downgrade RLS statements below are skipped on that path —
    same guard shape as migrations 005, 022, 023, and 024.
    """
    bind = op.get_bind()
    return bool(bind.dialect.name == "postgresql")


def upgrade() -> None:
    """Create console_prompt_groups + console_prompt_versions + indexes,
    then (Postgres only) enable + force RLS with a per-user policy on
    each."""
    op.create_table(
        "console_prompt_groups",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("group_id", sa.String(length=255), nullable=False),
        sa.Column("user_sub", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("oneliner", sa.Text(), nullable=False, server_default=""),
        sa.Column("command", sa.String(length=128), nullable=True),
        sa.Column("production_prompt_id", sa.String(length=255), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at_ms", sa.BigInteger(), nullable=True),
        sa.Column(
            "metadata", _MetadataType, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.UniqueConstraint(
            "user_sub", "group_id", name="uq_console_prompt_groups_user_group"
        ),
    )
    op.create_index(
        "ix_console_prompt_groups_user_sub",
        "console_prompt_groups",
        ["user_sub"],
        unique=False,
    )
    # Cursor-pagination shape (list_groups orders newest-first by
    # updated_at_ms within the caller's own rows) — same rationale as
    # migration 023/024's cursor index.
    op.create_index(
        "ix_console_prompt_groups_user_sub_updated_at_ms",
        "console_prompt_groups",
        ["user_sub", "updated_at_ms"],
        unique=False,
    )

    op.create_table(
        "console_prompt_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("prompt_id", sa.String(length=255), nullable=False),
        sa.Column("group_id", sa.String(length=255), nullable=False),
        sa.Column("user_sub", sa.String(length=36), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False, server_default="text"),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column(
            "metadata", _MetadataType, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.UniqueConstraint(
            "user_sub", "prompt_id", name="uq_console_prompt_versions_user_prompt"
        ),
    )
    op.create_index(
        "ix_console_prompt_versions_user_sub",
        "console_prompt_versions",
        ["user_sub"],
        unique=False,
    )
    op.create_index(
        "ix_console_prompt_versions_group_id",
        "console_prompt_versions",
        ["group_id"],
        unique=False,
    )
    # The per-group version-listing shape (get_group's versions fetch)
    # always filters by both user_sub AND group_id together.
    op.create_index(
        "ix_console_prompt_versions_user_sub_group_id",
        "console_prompt_versions",
        ["user_sub", "group_id"],
        unique=False,
    )

    if not _is_postgres():
        return

    for table, policy in (
        ("console_prompt_groups", "tenant_isolation_console_prompt_groups"),
        ("console_prompt_versions", "tenant_isolation_console_prompt_versions"),
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {policy} ON {table}
                FOR ALL
                USING (user_sub = current_setting('app.current_user_id', true))
                WITH CHECK (user_sub = current_setting('app.current_user_id', true))
            """
        )


def downgrade() -> None:
    """Reverse: drop the RLS policies (Postgres only), then the indexes +
    tables (versions before groups, no FK but matches creation order in
    reverse)."""
    if _is_postgres():
        for table, policy in (
            ("console_prompt_versions", "tenant_isolation_console_prompt_versions"),
            ("console_prompt_groups", "tenant_isolation_console_prompt_groups"),
        ):
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index(
        "ix_console_prompt_versions_user_sub_group_id",
        table_name="console_prompt_versions",
    )
    op.drop_index(
        "ix_console_prompt_versions_group_id", table_name="console_prompt_versions"
    )
    op.drop_index(
        "ix_console_prompt_versions_user_sub", table_name="console_prompt_versions"
    )
    op.drop_table("console_prompt_versions")

    op.drop_index(
        "ix_console_prompt_groups_user_sub_updated_at_ms",
        table_name="console_prompt_groups",
    )
    op.drop_index(
        "ix_console_prompt_groups_user_sub", table_name="console_prompt_groups"
    )
    op.drop_table("console_prompt_groups")
