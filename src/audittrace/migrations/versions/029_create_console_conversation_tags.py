"""create console_conversation_tags table + RLS policy

Revision ID: 7a1c9f3e5b02
Revises: 43568ad57fba
Create Date: 2026-09-13 00:00:00.000000

Conversation-Tags domain of the MongoDB-elimination EPIC
(2026-09-13-SPEC-mongo-repl-wu-conversation-tags-store.md) — mirrors the
Agents domain's migration 028 (and Chat-Projects' migration 026) shape
exactly, for a different domain (LibreChat's ``conversationTag`` record,
``packages/data-schemas/src/schema/conversationTag.ts``, Mongo
``ConversationTag`` collection). Adds ``console_conversation_tags`` (one
row per caller-minted ``tag`` string): the fields the ratified spec
names (tag/description/count/position/metadata) as first-class columns.

**Own-tags-only v1 (the ratified spec's scope boundary).** Every row is
owned by exactly one ``user_sub`` (the unique constraint below) — there
is no shared/global tag concept in this table.

RLS mirrors migrations 022-028 verbatim in shape: ENABLE + FORCE ROW
LEVEL SECURITY, one ``FOR ALL`` policy comparing ``user_sub`` against
``current_setting('app.current_user_id', true)`` in both USING and WITH
CHECK. Guarded by ``_is_postgres()`` so SQLite (the unit-test factory,
``InMemoryPostgresFactory``) creates the plain table with no RLS — the
service layer (``PostgresConsoleConversationTagsService``) additionally
filters every query by ``user_sub`` explicitly so cross-user isolation
is still caught by the SQLite unit suite, not only a live-Postgres
integration run (feedback_unit_tests_miss_rls).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "7a1c9f3e5b02"
down_revision: str | Sequence[str] | None = "43568ad57fba"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JsonType = sa.JSON().with_variant(JSONB(), "postgresql")


def _is_postgres() -> bool:
    """Return True when Alembic is running against PostgreSQL.

    SQLite (the in-memory test factory) has no RLS concept; the
    upgrade/downgrade RLS statements below are skipped on that path —
    same guard shape as migrations 005, 022-028.
    """
    bind = op.get_bind()
    return bool(bind.dialect.name == "postgresql")


def upgrade() -> None:
    """Create console_conversation_tags + indexes, then (Postgres only)
    enable + force RLS with a per-user policy."""
    op.create_table(
        "console_conversation_tags",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tag", sa.String(length=512), nullable=False),
        sa.Column("user_sub", sa.String(length=36), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "position", sa.Integer(), nullable=False, server_default=sa.text("0")
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
            "tag",
            name="uq_console_conversation_tags_user_tag",
        ),
    )
    op.create_index(
        "ix_console_conversation_tags_user_sub",
        "console_conversation_tags",
        ["user_sub"],
        unique=False,
    )
    # Cursor-pagination shape (list_conversation_tags orders newest-first
    # by updated_at_ms within the caller's own rows) — same rationale as
    # migration 028's console_agents index.
    op.create_index(
        "ix_console_conversation_tags_user_sub_updated_at_ms",
        "console_conversation_tags",
        ["user_sub", "updated_at_ms"],
        unique=False,
    )

    if not _is_postgres():
        return

    op.execute("ALTER TABLE console_conversation_tags ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE console_conversation_tags FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_console_conversation_tags
            ON console_conversation_tags
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
            "DROP POLICY IF EXISTS tenant_isolation_console_conversation_tags "
            "ON console_conversation_tags"
        )
        op.execute("ALTER TABLE console_conversation_tags NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE console_conversation_tags DISABLE ROW LEVEL SECURITY")

    op.drop_index(
        "ix_console_conversation_tags_user_sub_updated_at_ms",
        table_name="console_conversation_tags",
    )
    op.drop_index(
        "ix_console_conversation_tags_user_sub",
        table_name="console_conversation_tags",
    )
    op.drop_table("console_conversation_tags")
