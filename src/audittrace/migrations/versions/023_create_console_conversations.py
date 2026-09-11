"""create console_conversations + console_messages tables + RLS policies

Revision ID: dbce4d563e86
Revises: a8e6ec51e78b
Create Date: 2026-09-11 00:00:00.000000

WU-1 of the MongoDB-elimination EPIC
(2026-09-11-SPEC-mongo-repl-wu1-console-conversations-store.md) — the
AuditTrace-side sovereign, RLS-isolated store for LibreChat's
conversations + messages (the user's actual chat content), replacing
Mongo for that domain. Adds ``console_conversations`` (one row per
conversation, client-supplied string ``conversation_id``) and
``console_messages`` (the message tree, ``parent_message_id`` chain).

RLS mirrors migration 022 (``a8e6ec51e78b``) verbatim in shape: ENABLE +
FORCE ROW LEVEL SECURITY, one ``FOR ALL`` policy per table comparing
``user_sub`` against ``current_setting('app.current_user_id', true)`` in
both USING and WITH CHECK. Guarded by ``_is_postgres()`` so SQLite (the
unit-test factory, ``InMemoryPostgresFactory``) creates the plain tables
with no RLS — the service layer
(``PostgresConsoleConversationsService``) additionally filters every
query by ``user_sub`` explicitly so cross-user isolation is still caught
by the SQLite unit suite, not only a live-Postgres integration run
(feedback_unit_tests_miss_rls).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "dbce4d563e86"
down_revision: str | Sequence[str] | None = "a8e6ec51e78b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MetadataType = sa.JSON().with_variant(JSONB(), "postgresql")


def _is_postgres() -> bool:
    """Return True when Alembic is running against PostgreSQL.

    SQLite (the in-memory test factory) has no RLS concept; the
    upgrade/downgrade RLS statements below are skipped on that path —
    same guard shape as migrations 005 and 022.
    """
    bind = op.get_bind()
    return bool(bind.dialect.name == "postgresql")


def upgrade() -> None:
    """Create console_conversations + console_messages + indexes, then
    (Postgres only) enable + force RLS with a per-user policy on each."""
    op.create_table(
        "console_conversations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=255), nullable=False),
        sa.Column("user_sub", sa.String(length=36), nullable=False),
        sa.Column(
            "title",
            sa.String(length=512),
            nullable=False,
            server_default="New Chat",
        ),
        sa.Column("endpoint", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column(
            "is_temporary", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("agent_id", sa.String(length=255), nullable=True),
        sa.Column("chat_project_id", sa.String(length=255), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at_ms", sa.BigInteger(), nullable=True),
        sa.Column(
            "metadata", _MetadataType, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.UniqueConstraint(
            "user_sub", "conversation_id", name="uq_console_conversations_user_convo"
        ),
    )
    op.create_index(
        "ix_console_conversations_user_sub",
        "console_conversations",
        ["user_sub"],
        unique=False,
    )
    # Cursor-pagination shape (list_conversations orders newest-first by
    # updated_at_ms within the caller's own rows).
    op.create_index(
        "ix_console_conversations_user_sub_updated_at_ms",
        "console_conversations",
        ["user_sub", "updated_at_ms"],
        unique=False,
    )

    op.create_table(
        "console_messages",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("message_id", sa.String(length=255), nullable=False),
        sa.Column("conversation_id", sa.String(length=255), nullable=False),
        sa.Column("user_sub", sa.String(length=36), nullable=False),
        sa.Column("parent_message_id", sa.String(length=255), nullable=True),
        sa.Column("sender", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "is_created_by_user",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("endpoint", sa.String(length=64), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column(
            "metadata", _MetadataType, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.UniqueConstraint(
            "user_sub", "message_id", name="uq_console_messages_user_message"
        ),
    )
    op.create_index(
        "ix_console_messages_user_sub", "console_messages", ["user_sub"], unique=False
    )
    op.create_index(
        "ix_console_messages_conversation_id",
        "console_messages",
        ["conversation_id"],
        unique=False,
    )
    # The message-tree retrieval shape (get_messages) always filters by
    # both user_sub AND conversation_id together.
    op.create_index(
        "ix_console_messages_user_sub_conversation_id",
        "console_messages",
        ["user_sub", "conversation_id"],
        unique=False,
    )

    if not _is_postgres():
        return

    for table, policy in (
        ("console_conversations", "tenant_isolation_console_conversations"),
        ("console_messages", "tenant_isolation_console_messages"),
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
    tables (messages before conversations, no FK but matches creation
    order in reverse)."""
    if _is_postgres():
        for table, policy in (
            ("console_messages", "tenant_isolation_console_messages"),
            ("console_conversations", "tenant_isolation_console_conversations"),
        ):
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index(
        "ix_console_messages_user_sub_conversation_id", table_name="console_messages"
    )
    op.drop_index("ix_console_messages_conversation_id", table_name="console_messages")
    op.drop_index("ix_console_messages_user_sub", table_name="console_messages")
    op.drop_table("console_messages")

    op.drop_index(
        "ix_console_conversations_user_sub_updated_at_ms",
        table_name="console_conversations",
    )
    op.drop_index(
        "ix_console_conversations_user_sub", table_name="console_conversations"
    )
    op.drop_table("console_conversations")
