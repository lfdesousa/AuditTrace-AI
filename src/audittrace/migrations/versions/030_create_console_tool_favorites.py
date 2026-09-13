"""create console_tool_favorites table + RLS policy

Revision ID: 9d4e2b7f1c63
Revises: 7a1c9f3e5b02
Create Date: 2026-09-13 00:00:00.000000

Tool-Favorites domain of the MongoDB-elimination EPIC
(2026-09-13-SPEC-mongo-repl-wu-tool-favorites-store.md) — mirrors the
Conversation-Tags domain's migration 029 (and Agents' migration 028)
shape exactly, for a different domain (LibreChat's ``ToolFavorite``
record, ``packages/data-schemas/src/schema/favorite.ts``, Mongo
``ToolFavorite`` collection). Adds ``console_tool_favorites`` (one row
per caller-favorited ``(item_type, item_id)`` pair): the fields the
ratified spec names (item_type/item_id/tenant_id/metadata) as
first-class columns.

**Own-favorites-only v1 (the ratified spec's scope boundary).** Every
row is owned by exactly one ``user_sub`` — there is no shared/global
favorite concept in this table.

RLS mirrors migrations 022-029 verbatim in shape: ENABLE + FORCE ROW
LEVEL SECURITY, one ``FOR ALL`` policy comparing ``user_sub`` against
``current_setting('app.current_user_id', true)`` in both USING and WITH
CHECK. Guarded by ``_is_postgres()`` so SQLite (the unit-test factory,
``InMemoryPostgresFactory``) creates the plain table with no RLS — the
service layer (``PostgresConsoleToolFavoritesService``) additionally
filters every query by ``user_sub`` explicitly so cross-user isolation
is still caught by the SQLite unit suite, not only a live-Postgres
integration run (feedback_unit_tests_miss_rls).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "9d4e2b7f1c63"
down_revision: str | Sequence[str] | None = "7a1c9f3e5b02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JsonType = sa.JSON().with_variant(JSONB(), "postgresql")


def _is_postgres() -> bool:
    """Return True when Alembic is running against PostgreSQL.

    SQLite (the in-memory test factory) has no RLS concept; the
    upgrade/downgrade RLS statements below are skipped on that path —
    same guard shape as migrations 005, 022-029.
    """
    bind = op.get_bind()
    return bool(bind.dialect.name == "postgresql")


def upgrade() -> None:
    """Create console_tool_favorites + indexes, then (Postgres only)
    enable + force RLS with a per-user policy."""
    op.create_table(
        "console_tool_favorites",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_sub", sa.String(length=36), nullable=False),
        sa.Column("item_type", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=256), nullable=False),
        sa.Column("tenant_id", sa.String(length=256), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at_ms", sa.BigInteger(), nullable=True),
        sa.Column(
            "metadata", _JsonType, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.UniqueConstraint(
            "user_sub",
            "item_type",
            "item_id",
            name="uq_console_tool_favorites_user_item",
        ),
    )
    op.create_index(
        "ix_console_tool_favorites_user_sub",
        "console_tool_favorites",
        ["user_sub"],
        unique=False,
    )
    # Cap-enforcement + listing shape (list_tool_favorites returns the
    # caller's own rows; add_tool_favorite counts the caller's own
    # non-deleted rows to enforce MAX_TOOL_FAVORITES) — same rationale
    # as migration 029's console_conversation_tags index.
    op.create_index(
        "ix_console_tool_favorites_user_sub_created_at_ms",
        "console_tool_favorites",
        ["user_sub", "created_at_ms"],
        unique=False,
    )

    if not _is_postgres():
        return

    op.execute("ALTER TABLE console_tool_favorites ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE console_tool_favorites FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_console_tool_favorites
            ON console_tool_favorites
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
            "DROP POLICY IF EXISTS tenant_isolation_console_tool_favorites "
            "ON console_tool_favorites"
        )
        op.execute("ALTER TABLE console_tool_favorites NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE console_tool_favorites DISABLE ROW LEVEL SECURITY")

    op.drop_index(
        "ix_console_tool_favorites_user_sub_created_at_ms",
        table_name="console_tool_favorites",
    )
    op.drop_index(
        "ix_console_tool_favorites_user_sub",
        table_name="console_tool_favorites",
    )
    op.drop_table("console_tool_favorites")
