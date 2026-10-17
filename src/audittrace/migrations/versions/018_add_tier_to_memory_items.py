"""add tier to memory_items (ADR-062 Phase B, WU-B4)

Revision ID: e1c2b4d6a859
Revises: d8a0c2e4f637
Create Date: 2026-08-04 00:00:00.000000

ADR-062 Phase B introduces a real per-user private tier alongside the
pre-existing shared corpus. ``tier`` is the manifest-side half of that
split: a closed-set ``"corpus"`` | ``"private"`` column on
``memory_items``, mirrored by ``ManifestEntry.tier`` in
``services/memory_manifest.py``.

D2 (ratified 2026-08-04): existing rows (everything written before this
migration) read ``tier="corpus"`` — today's shared knowledge base stays
visible to everyone unchanged, with zero backfill script required for
THIS migration (the row-by-row Chroma/S3 corpus migration is WU-B6, a
later PR). ``server_default="corpus"`` is what makes that true for both
already-committed rows AND any INSERT that doesn't set the column
explicitly. New manifest rows created via the per-layer CRUD routes
(``POST /memory/episodic|procedural|semantic``) pass an explicit
``tier`` from WU-B5 onward — default ``"private"`` unless the caller
holds the corpus-write scope and explicitly promotes.

Additive, forward-only, NOT NULL with a server default (so no existing
row is ever NULL — the owner-or-corpus predicate in
``MemoryManifestService.list_for_layer`` treats the column as always
populated, never coalescing a NULL).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1c2b4d6a859"
down_revision: str | Sequence[str] | None = "d8a0c2e4f637"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the NOT NULL ``memory_items.tier`` column, defaulting existing
    (and any tier-unaware future) rows to ``"corpus"`` (D2)."""
    op.add_column(
        "memory_items",
        sa.Column(
            "tier",
            sa.String(length=16),
            nullable=False,
            server_default="corpus",
        ),
    )


def downgrade() -> None:
    """Reverse the upgrade — drop the ``tier`` column."""
    op.drop_column("memory_items", "tier")
