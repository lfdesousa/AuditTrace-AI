"""add summarized_at to sessions (ADR-030 Part 2)

Revision ID: b0c2d4e6f8a0
Revises: a8b0c2d4e6f8
Create Date: 2026-04-15 10:30:00.000000

Adds a nullable ``summarized_at`` timestamp column on ``sessions``. The
background session summariser (``services/session_summarizer.py``)
uses it to decide which rows are eligible for re-summarisation:

- NULL                          → never summarised, always eligible
- summarized_at < last_ts       → stale, re-summarise
- summarized_at >= last_ts      → up to date, skip

Paired with an index so the eligibility query can filter cheaply
without a sequential scan.

See ADR-030 §4 for the full algorithm.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b0c2d4e6f8a0"
down_revision: str | Sequence[str] | None = "a8b0c2d4e6f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add summarized_at column + index."""
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(sa.Column("summarized_at", sa.DateTime(), nullable=True))
        batch_op.create_index(
            "ix_sessions_summarized_at", ["summarized_at"], unique=False
        )


def downgrade() -> None:
    """Reverse: drop index then column."""
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_index("ix_sessions_summarized_at")
        batch_op.drop_column("summarized_at")
