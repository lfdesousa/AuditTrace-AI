"""add indexed_at_ms outbox marker to memory_items (SPEC #387 Phase 1 WU-1)

Revision ID: c3a7e9d1f5b2
Revises: e1c2b4d6a859
Create Date: 2026-08-09 00:00:00.000000

Adds ``memory_items.indexed_at_ms`` — the place→index leg of the Hohpe
Transactional Outbox pattern, one hop after ``published_at_ms`` (migration
013, place→publish). NULL means the row's bytes have not yet been embedded
+ upserted into ChromaDB; ``IndexWorker`` (services/index_worker.py) sets it
on success, ``IndexJanitor`` (services/index_janitor.py) re-drives any row
still NULL past the grace window. Closes SPEC #387 GAP-1 — a
``scanned_clean`` verdict previously stopped after re-pointing the manifest
key, with nothing to trigger indexing.

Nullable + no backfill needed: every pre-migration-019 row (and every row
outside the scan pipeline, which never populates ``scan_status`` either)
correctly reads NULL forever — nothing outside the scan pipeline claims to
have been auto-indexed.

Indexed: a composite index on ``(indexed_at_ms, created_at_ms)`` mirrors
migration 013's ``ix_memory_items_outbox_pending`` so the janitor's
``WHERE indexed_at_ms IS NULL AND created_at_ms < ?`` query is an index
scan, not a sequential scan, as the manifest grows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3a7e9d1f5b2"
down_revision: str | Sequence[str] | None = "e1c2b4d6a859"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the ``indexed_at_ms`` outbox marker + its composite index."""
    op.add_column(
        "memory_items",
        sa.Column("indexed_at_ms", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_memory_items_index_pending",
        "memory_items",
        ["indexed_at_ms", "created_at_ms"],
        unique=False,
    )


def downgrade() -> None:
    """Reverse the upgrade."""
    op.drop_index("ix_memory_items_index_pending", table_name="memory_items")
    op.drop_column("memory_items", "indexed_at_ms")
