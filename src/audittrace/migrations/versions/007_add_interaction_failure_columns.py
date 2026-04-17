"""add failure-audit columns to interactions

Revision ID: c1e3f5a7b9c0
Revises: b0c2d4e6f8a0
Create Date: 2026-04-16 09:00:00.000000

Adds four columns to ``interactions`` so failed requests produce an
audit row just like successful ones:

- ``status``         — 'success' (default) or 'failed'
- ``failure_class``  — controlled vocabulary (proxy_timeout,
                       upstream_error, upstream_unreachable,
                       internal_error); NULL on success rows
- ``error_detail``   — truncated exception str; NULL on success
- ``duration_ms``    — wall-clock ms the request ran before resolving;
                       NULL on success rows predating this migration

Forensic context: prior to this change, ``httpx.ReadTimeout`` on the
Qwen proxy escaped the streaming generator and the tool-loop without
ever calling ``_persist_interaction``. The resulting 500s left zero
audit footprint — 10 such events between 2026-04-14 and 2026-04-15
were only reconstructible from Loki logs. This migration is the
schema side of the fix; the route-handler try/except is the code
side.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1e3f5a7b9c0"
down_revision: str | Sequence[str] | None = "b0c2d4e6f8a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the four failure-audit columns + index on status."""
    with op.batch_alter_table("interactions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="success",
            )
        )
        batch_op.add_column(
            sa.Column("failure_class", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(sa.Column("error_detail", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("duration_ms", sa.Integer(), nullable=True))
        batch_op.create_index("ix_interactions_status", ["status"], unique=False)


def downgrade() -> None:
    """Reverse: drop index then columns."""
    with op.batch_alter_table("interactions") as batch_op:
        batch_op.drop_index("ix_interactions_status")
        batch_op.drop_column("duration_ms")
        batch_op.drop_column("error_detail")
        batch_op.drop_column("failure_class")
        batch_op.drop_column("status")
