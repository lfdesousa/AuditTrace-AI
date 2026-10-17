"""add content_hash to interactions (ADR-058 WS-A3)

Revision ID: d8a0c2e4f637
Revises: c7f9a1b3d526
Create Date: 2026-07-14 00:00:00.000000

ADR-058 Half A (integrity, tamper-evidence). Adds ``content_hash`` — a
SHA-256 over each audit row's immutable content (see ``integrity.py``) —
so a mutation that slips past the append-only trigger (WS-A2) is detectable
by recomputation.

Additive, forward-only, nullable (rows predating the column read NULL and
are simply un-verifiable, not invalid). No index: the verify path walks
rows by their existing keys, it does not look rows up by hash.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8a0c2e4f637"
down_revision: str | Sequence[str] | None = "c7f9a1b3d526"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``interactions.content_hash`` column."""
    op.add_column(
        "interactions",
        sa.Column("content_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Drop the ``interactions.content_hash`` column."""
    op.drop_column("interactions", "content_hash")
