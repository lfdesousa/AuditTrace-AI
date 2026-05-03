"""create memory_items manifest table

Revision ID: e4a8c2d6f1e9
Revises: d2f4e6a8c1b3
Create Date: 2026-05-03 16:30:00.000000

Adds the ``memory_items`` table — operator-facing manifest for items
managed via the memory-layer CRUD backoffice (Phase 3.1 work, see plan
file at ~/.claude/plans/eager-tumbling-matsumoto.md and
project_session_20260503).

Why a manifest table separate from the underlying storage backends:

* S3-backed layers (episodic / procedural) only carry an object's
  Last-Modified timestamp and have no native concept of "who created
  it" or "soft-deleted". Putting authorship + soft-delete + sub-second
  timestamps in S3 metadata couples lifecycle concerns to the
  storage layer and makes LIST queries an O(N) bucket scan.
* ChromaDB (semantic) doesn't surface authorship metadata cleanly
  either.
* Audit rule: "every operator change to memory must be reconstructible"
  (see feedback_traceability_requirement). Storing this in a single
  Postgres table gives a uniform CRUD surface across all three layers.

Per user directive (2026-05-03 evening): timestamps are stored as
**Unix epoch milliseconds UTC** (BIGINT) rather than DateTime. Gives
sub-second ordering for back-to-back creates and matches modern API
convention (`Date.now()` in JS / `time.time() * 1000` in Python).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4a8c2d6f1e9"
down_revision: str | Sequence[str] | None = "d2f4e6a8c1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create memory_items + supporting indexes."""
    op.create_table(
        "memory_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        # 'episodic' | 'procedural' | 'semantic'. VARCHAR(16) leaves
        # headroom in case a fourth layer kind appears later.
        sa.Column("layer", sa.String(length=16), nullable=False),
        # S3 filename for episodic/procedural ('ADR-025-foo.md');
        # '<collection>/<doc_id>' for semantic.
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        # Unix epoch milliseconds UTC. BigInteger because seconds-since-epoch
        # in milliseconds overflows Integer 32-bit in 2038; BIGINT is good
        # until ~year 292 million.
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("modified_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("modified_by_user_id", sa.String(length=36), nullable=False),
        # Soft-delete. NULL = live. Setting this and deleted_by_user_id
        # is reversible (the row can be undeleted by clearing both
        # fields). A separate ?hard=true API path also removes the
        # underlying S3 object / Chroma doc.
        sa.Column("deleted_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("deleted_by_user_id", sa.String(length=36), nullable=True),
        # One manifest row per (layer, key) over the lifetime of the key.
        # Deleting and recreating the same key uses the same row (with a
        # new modified_at_ms + cleared deleted_at_ms).
        sa.UniqueConstraint("layer", "key", name="uq_memory_items_layer_key"),
    )
    # LIST queries scope by layer + filter on deleted_at_ms IS NULL.
    op.create_index(
        "ix_memory_items_layer_deleted_at",
        "memory_items",
        ["layer", "deleted_at_ms"],
        unique=False,
    )
    # GET-by-key path uses the unique constraint above; no separate
    # index needed for it.


def downgrade() -> None:
    """Reverse: drop indexes + table."""
    op.drop_index("ix_memory_items_layer_deleted_at", table_name="memory_items")
    op.drop_table("memory_items")
