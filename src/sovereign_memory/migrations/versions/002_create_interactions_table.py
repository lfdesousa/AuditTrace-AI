"""create interactions table

Revision ID: a2b4c6d8e0f2
Revises: 149ca54b0d19
Create Date: 2026-04-10 21:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2b4c6d8e0f2"
down_revision: str | Sequence[str] | None = "149ca54b0d19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create interactions audit trail table."""
    op.create_table(
        "interactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_interactions_project"), "interactions", ["project"], unique=False
    )
    op.create_index(
        op.f("ix_interactions_timestamp"), "interactions", ["timestamp"], unique=False
    )


def downgrade() -> None:
    """Drop interactions table."""
    op.drop_index(op.f("ix_interactions_timestamp"), table_name="interactions")
    op.drop_index(op.f("ix_interactions_project"), table_name="interactions")
    op.drop_table("interactions")
