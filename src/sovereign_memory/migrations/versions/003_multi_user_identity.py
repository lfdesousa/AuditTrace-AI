"""multi-user identity (Phase 0)

Revision ID: c4e6f8a0b2d4
Revises: a2b4c6d8e0f2
Create Date: 2026-04-11 14:00:00.000000

Adds the User, UserRole, PatToken, and ToolCall tables, plus a
``user_id`` column on ``interactions`` and ``sessions``.

See docs/ADR-026-multi-user-identity.md Phase 0.

UUIDs are stored as VARCHAR(36) so the migration applies cleanly to
both PostgreSQL (production) and SQLite (test). Postgres-specific
features (Row-Level Security policies, BYPASSRLS roles) land in a
later migration during Phase 4.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e6f8a0b2d4"
down_revision: str | Sequence[str] | None = "a2b4c6d8e0f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create identity tables and add user_id columns to existing tables."""
    # ── users ─────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("keycloak_sub", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("keycloak_sub", name="uq_users_keycloak_sub"),
    )

    # ── user_roles ────────────────────────────────────────────────────
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "role"),
    )

    # ── pat_tokens ────────────────────────────────────────────────────
    op.create_table(
        "pat_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("agent_type", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_pat_tokens_token_hash"),
    )
    op.create_index(
        op.f("ix_pat_tokens_user_id"), "pat_tokens", ["user_id"], unique=False
    )

    # ── tool_calls ────────────────────────────────────────────────────
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("interaction_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("agent_type", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column("args", sa.Text(), nullable=False),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("granted_scope", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(
            ["interaction_id"], ["interactions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_tool_calls_interaction_id"),
        "tool_calls",
        ["interaction_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_tool_calls_user_id"), "tool_calls", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_tool_calls_tool_name"),
        "tool_calls",
        ["tool_name"],
        unique=False,
    )

    # ── interactions.user_id (nullable for backwards compat) ──────────
    # Phase 5 of ADR-026 will flip this to
    # NOT NULL after backfilling existing rows. Phase 0 keeps it
    # nullable so the migration applies to a non-empty table.
    with op.batch_alter_table("interactions") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_interactions_user_id",
            "users",
            ["user_id"],
            ["id"],
        )
        batch_op.create_index("ix_interactions_user_id", ["user_id"], unique=False)

    # ── sessions.user_id (nullable for backwards compat) ──────────────
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_sessions_user_id",
            "users",
            ["user_id"],
            ["id"],
        )
        batch_op.create_index("ix_sessions_user_id", ["user_id"], unique=False)


def downgrade() -> None:
    """Drop identity tables and remove user_id columns."""
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_index("ix_sessions_user_id")
        batch_op.drop_constraint("fk_sessions_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    with op.batch_alter_table("interactions") as batch_op:
        batch_op.drop_index("ix_interactions_user_id")
        batch_op.drop_constraint("fk_interactions_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    op.drop_index(op.f("ix_tool_calls_tool_name"), table_name="tool_calls")
    op.drop_index(op.f("ix_tool_calls_user_id"), table_name="tool_calls")
    op.drop_index(op.f("ix_tool_calls_interaction_id"), table_name="tool_calls")
    op.drop_table("tool_calls")

    op.drop_index(op.f("ix_pat_tokens_user_id"), table_name="pat_tokens")
    op.drop_table("pat_tokens")

    op.drop_table("user_roles")
    op.drop_table("users")
