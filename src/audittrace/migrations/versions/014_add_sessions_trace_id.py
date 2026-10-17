"""add trace_id column to sessions

Revision ID: a2c4e6b8d013
Revises: f1b3d5a7c2e4
Create Date: 2026-06-28 12:00:00.000000

#344 — adds ``trace_id`` (32-char hex string) to ``sessions`` so a summary
row written by the background session summariser can be correlated to its
Tempo/Langfuse trace in a single SQL lookup.

The summariser sweep is a background asyncio task: its model call would
otherwise surface as an unattributed orphan root span (no ``user.id`` /
``session.id`` / parent). Pairing an attributed span (set in
``SessionSummarizer._summarise_one``) with this persisted ``trace_id``
closes the audit-trail gap — the row points back at the trace that
produced it, mirroring ``interactions.trace_id`` (migration 008).

Captured value is the OpenTelemetry span trace_id formatted as
``format(ctx.trace_id, "032x")``. ``nullable=True`` because rows that
pre-date this migration have no captured trace_id, and rows written when
tracing is disabled (no active span) leave it NULL rather than fabricate.
The column is indexed because the lookup pattern is "find the summary row
for trace abc123…".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2c4e6b8d013"
down_revision: str | Sequence[str] | None = "f1b3d5a7c2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the trace_id column + lookup index."""
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(sa.Column("trace_id", sa.String(length=32), nullable=True))
        batch_op.create_index("ix_sessions_trace_id", ["trace_id"], unique=False)


def downgrade() -> None:
    """Reverse: drop index then column."""
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_index("ix_sessions_trace_id")
        batch_op.drop_column("trace_id")
