"""add trace_id column to interactions

Revision ID: d2f4e6a8c1b3
Revises: c1e3f5a7b9c0
Create Date: 2026-05-03 09:00:00.000000

Adds ``trace_id`` (32-char hex string) to ``interactions`` so a Postgres
row can be correlated to its Tempo trace in a single SQL lookup, instead
of the current 3-tuple join on ``(user_id, session_id, timestamp)``. The
column is indexed because the lookup pattern is "find the audit row(s)
for trace abc123…".

Captured value is the OpenTelemetry current-span trace_id formatted as
``format(ctx.trace_id, "032x")`` (same shape Langfuse uses on its
``langfuse.trace.id`` attribute and Tempo emits in its UI URLs). The
column is ``nullable=True`` because rows that pre-date this migration
have no captured trace_id, and rows whose request had no active span
(should be rare — chat path always opens one) also leave it NULL rather
than fabricate.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2f4e6a8c1b3"
down_revision: str | Sequence[str] | None = "c1e3f5a7b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the trace_id column + lookup index."""
    with op.batch_alter_table("interactions") as batch_op:
        batch_op.add_column(sa.Column("trace_id", sa.String(length=32), nullable=True))
        batch_op.create_index("ix_interactions_trace_id", ["trace_id"], unique=False)


def downgrade() -> None:
    """Reverse: drop index then column."""
    with op.batch_alter_table("interactions") as batch_op:
        batch_op.drop_index("ix_interactions_trace_id")
        batch_op.drop_column("trace_id")
