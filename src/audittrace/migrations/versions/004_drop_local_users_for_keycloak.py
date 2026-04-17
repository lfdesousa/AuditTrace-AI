"""drop local users tables — identity delegated to Keycloak

Revision ID: e6f8a0c2d4e6
Revises: c4e6f8a0b2d4
Create Date: 2026-04-11 18:00:00.000000

Forward migration that retires the Phase 0 local-users schema in favour
of the Keycloak-delegated identity model. See
docs/ADR-026-multi-user-identity.md §15.

What this migration does on upgrade:

1. Drops the named FKs from ``interactions`` and ``sessions`` to
   ``users``. The underlying ``user_id`` columns are KEPT — they still
   hold an opaque identifier, just now a Keycloak ``sub`` claim
   instead of a row id in our own table.
2. Recreates ``tool_calls`` from scratch (drop + create). Migration 003
   created the FK to ``users`` inline (no name), and the only
   cross-database way to drop an unnamed FK is a table recreate. There
   is no production data on this feature branch yet, so a recreate is
   safe.
3. Drops the ``user_roles`` table (replaced by Keycloak realm roles).
4. Drops the ``pat_tokens`` table (replaced by an in-Redis token
   cache; the cache is not a database table at all — see DESIGN §15.4).
5. Drops the ``users`` table itself, now that nothing references it.

The downgrade reverses every step so that ``alembic downgrade base``
walks cleanly back to revision 0 even after this migration is applied.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f8a0c2d4e6"
down_revision: str | Sequence[str] | None = "c4e6f8a0b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_tool_calls_table(*, with_users_fk: bool) -> None:
    """Create the tool_calls table.

    ``with_users_fk=False`` is the post-004 shape (Keycloak-delegated).
    ``with_users_fk=True`` is the pre-004 / migration 003 shape, used
    by the downgrade path.
    """
    fks: list[sa.ForeignKeyConstraint] = [
        sa.ForeignKeyConstraint(
            ["interaction_id"], ["interactions.id"], ondelete="CASCADE"
        ),
    ]
    if with_users_fk:
        fks.append(
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE")
        )
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
        *fks,
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


def _drop_tool_calls_table() -> None:
    op.drop_index(op.f("ix_tool_calls_tool_name"), table_name="tool_calls")
    op.drop_index(op.f("ix_tool_calls_user_id"), table_name="tool_calls")
    op.drop_index(op.f("ix_tool_calls_interaction_id"), table_name="tool_calls")
    op.drop_table("tool_calls")


def upgrade() -> None:
    """Drop local identity tables; keep user_id columns as opaque strings."""
    # ── tool_calls: recreate without FK to users ─────────────────────────
    # The FK in migration 003 was inline/unnamed; cleanest cross-DB way
    # to drop it is a table recreate.
    _drop_tool_calls_table()
    _create_tool_calls_table(with_users_fk=False)

    # ── interactions: drop the named FK to users ─────────────────────────
    with op.batch_alter_table("interactions") as batch_op:
        batch_op.drop_constraint("fk_interactions_user_id", type_="foreignkey")

    # ── sessions: drop the named FK to users ─────────────────────────────
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_constraint("fk_sessions_user_id", type_="foreignkey")

    # ── Drop the now-orphaned identity tables ───────────────────────────
    op.drop_index(op.f("ix_pat_tokens_user_id"), table_name="pat_tokens")
    op.drop_table("pat_tokens")
    op.drop_table("user_roles")
    op.drop_table("users")


def downgrade() -> None:
    """Reverse every step of upgrade() so that downgrade chains stay valid.

    Recreates the identity tables, restores FKs on interactions/sessions,
    and rebuilds tool_calls with the FK to users so migration 003's
    downgrade can drop it cleanly.
    """
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
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "role"),
    )
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

    # Restore the FKs on interactions/sessions so 003's downgrade can drop them
    with op.batch_alter_table("interactions") as batch_op:
        batch_op.create_foreign_key(
            "fk_interactions_user_id",
            "users",
            ["user_id"],
            ["id"],
        )
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.create_foreign_key(
            "fk_sessions_user_id",
            "users",
            ["user_id"],
            ["id"],
        )

    # Rebuild tool_calls with the FK to users
    _drop_tool_calls_table()
    _create_tool_calls_table(with_users_fk=True)
