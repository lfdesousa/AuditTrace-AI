"""add index dead-letter columns to memory_items (SPEC #450 WU-1)

Revision ID: 5fe377bec101
Revises: c3a7e9d1f5b2
Create Date: 2026-08-15 00:00:00.000000

Adds the THIRD state ``indexed_at_ms`` (migration 019) never had:
"will never succeed" — a corrupt/unindexable PDF must stop being
re-enqueued by ``IndexJanitor`` forever instead of looping every janitor
tick. ``index_attempts`` counts every failed ``IndexWorker`` pass;
``index_failed_at_ms`` NULL = still retryable, a timestamp = terminally
dead-lettered; ``index_failure_code`` names why — either a permanent
structural code (``PERMANENT_INDEX_FAILURE_CODES``,
routes/memory_pdf/classification.py) or ``"max_attempts_exceeded"`` once
``index_attempts`` reaches ``Settings.index_max_attempts``.

Nullable/defaulted + no backfill needed: every pre-migration-020 row
reads ``index_attempts=0``, ``index_failed_at_ms=NULL``,
``index_failure_code=NULL`` — additive, backward compatible, matching
migration 019's own no-backfill rationale for ``indexed_at_ms``.

``IndexJanitor._scan_orphans`` (services/index_janitor.py) adds
``WHERE index_failed_at_ms IS NULL`` to its existing predicate so
dead-lettered rows never re-enqueue.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5fe377bec101"
down_revision: str | Sequence[str] | None = "c3a7e9d1f5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the index dead-letter columns."""
    op.add_column(
        "memory_items",
        sa.Column(
            "index_attempts",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "memory_items",
        sa.Column("index_failed_at_ms", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "memory_items",
        sa.Column("index_failure_code", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Reverse the upgrade."""
    op.drop_column("memory_items", "index_failure_code")
    op.drop_column("memory_items", "index_failed_at_ms")
    op.drop_column("memory_items", "index_attempts")
