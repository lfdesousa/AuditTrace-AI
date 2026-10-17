"""add server-set created_at to interactions (ADR-058 WS-A1)

Revision ID: b5e7c9a1d024
Revises: a2c4e6b8d013
Create Date: 2026-07-14 00:00:00.000000

ADR-058 Half A (recorder integrity, contemporaneity). Adds a
DB-server-assigned ``created_at`` timestamp to ``interactions`` so the
record's clock is written by the store at insert time, independent of
the application process that produced the row.

Today ``interactions.timestamp`` is a String set by the application
(``datetime.now().isoformat()`` in ``_persist_interaction``); a caller
or a clock-skewed pod controls it. ``created_at`` is written by
Postgres via ``server_default=now()`` on INSERT, giving contemporaneity
a writer-independent anchor (the "queryable vs audit-grade" line the
series draws).

Additive and forward-only. Existing rows are backfilled once with the
migration-time ``now()`` (they pre-date the column; their application
``timestamp`` remains the pre-existing signal). Indexed because the
authoritative-clock query pattern is "rows created since T".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5e7c9a1d024"
down_revision: str | Sequence[str] | None = "a2c4e6b8d013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the server-set ``interactions.created_at`` column and index."""
    op.add_column(
        "interactions",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_interactions_created_at",
        "interactions",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``interactions.created_at`` column and index."""
    op.drop_index("ix_interactions_created_at", table_name="interactions")
    op.drop_column("interactions", "created_at")
