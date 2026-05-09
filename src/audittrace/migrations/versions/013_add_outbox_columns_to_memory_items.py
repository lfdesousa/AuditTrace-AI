"""add outbox columns to memory_items (ADR-048 PR-B3)

Revision ID: f1b3d5a7c2e4
Revises: c8d2e7a4f1a9
Create Date: 2026-05-10 18:00:00.000000

PR-B3 — /memory/upload PDF rewrite to 202. Adds the two columns
needed for the Hohpe Transactional Outbox pattern:

* ``memory_items.published_at_ms`` — BIGINT (epoch ms), nullable.
  NULL = "row inserted by /memory/upload but the AMQP basic_publish
  hasn't completed yet". Set to ``time.time() * 1000`` once the
  publisher acks. The janitor scans for NULL rows older than
  ``Settings.scan_janitor_grace_seconds`` and re-enqueues them.

* ``memory_items.trace_id`` — VARCHAR(64), nullable. The
  W3C-traceparent-derived trace id of the originating
  /memory/upload request. Carried into the AMQP message header so
  content-control's worker stitches the same trace across the
  async boundary (PR-A3 ScanWorker reads ``request.trace_id``).

Both nullable so existing rows pre-dating PR-B3 read NULL and the
janitor's WHERE-clause naturally skips them (``created_at_ms`` is
also old enough that the grace window has long expired).

Indexed: a partial / composite index on ``(published_at_ms,
created_at_ms)`` makes the janitor's query
``WHERE published_at_ms IS NULL AND created_at_ms < ?`` an index
scan instead of a sequential scan once the manifest grows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1b3d5a7c2e4"
down_revision: str | Sequence[str] | None = "c8d2e7a4f1a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the outbox marker + trace_id columns + composite index."""
    op.add_column(
        "memory_items",
        sa.Column("published_at_ms", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "memory_items",
        sa.Column("trace_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_memory_items_outbox_pending",
        "memory_items",
        ["published_at_ms", "created_at_ms"],
        unique=False,
    )


def downgrade() -> None:
    """Reverse the upgrade."""
    op.drop_index("ix_memory_items_outbox_pending", table_name="memory_items")
    op.drop_column("memory_items", "trace_id")
    op.drop_column("memory_items", "published_at_ms")
