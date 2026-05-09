"""add event_class to interactions + scan_status to memory_items (ADR-048)

Revision ID: c8d2e7a4f1a9
Revises: a7c3f5e1b482
Create Date: 2026-05-10 09:00:00.000000

ADR-048 PR-B1 — contract scaffolding. Adds two closed-set enum
columns; the values are pinned by tests in
``tests/test_memory_routes.py::TestScanStatusCodes`` and
``::TestEventClassValues``. The verdict-consumer that *writes*
these values lands in PR-B4 (blocked on PR-A3); this migration's
purpose is to lock the schema so PR-B4 cannot drift.

Two columns:

* ``interactions.event_class`` — String(16) nullable. Existing rows
  pre-dating this migration read NULL; the verdict consumer in PR-B4
  writes ``"security"`` when emitting content-control verdicts; the
  chat-completion path in
  ``src/audittrace/routes/chat.py:_persist_interaction()`` keeps
  emitting NULL until PR-B4 backfills it to ``"interaction"``.
  Closed-set: ``{"interaction", "security"}`` per ADR-048
  §"Audit trail integration".

* ``memory_items.scan_status`` — String(32) nullable. Existing rows
  pre-dating this migration read NULL (non-uploads, pre-ADR-048
  uploads). PR-B3's rewrite of ``/memory/upload`` will write
  ``"pending_scan"`` on insert; PR-B4's verdict consumer transitions
  it to one of the terminal states. Closed-set per ADR-048
  §Failure modes: ``{"pending_scan", "scanning", "scanned_clean",
  "rejected_malware", "scan_failed", "scan_unrecoverable"}``.

Both columns are indexed because the audit-query patterns are
"find SECURITY rows in the last hour" (ops alerting on rejections)
and "find pending_scan rows older than N minutes" (operator
checking the scanner is making progress).

Schema-only — no behaviour change. Backwards-compatible with the
existing ``InteractionRecord`` and ``MemoryItem`` dataclasses;
both columns nullable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8d2e7a4f1a9"
down_revision: str | Sequence[str] | None = "a7c3f5e1b482"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add event_class to interactions + scan_status to memory_items."""
    op.add_column(
        "interactions",
        sa.Column("event_class", sa.String(length=16), nullable=True),
    )
    op.create_index(
        "ix_interactions_event_class",
        "interactions",
        ["event_class"],
        unique=False,
    )
    op.add_column(
        "memory_items",
        sa.Column("scan_status", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_memory_items_scan_status",
        "memory_items",
        ["scan_status"],
        unique=False,
    )


def downgrade() -> None:
    """Drop migration 012 columns + indexes."""
    op.drop_index("ix_memory_items_scan_status", table_name="memory_items")
    op.drop_column("memory_items", "scan_status")
    op.drop_index("ix_interactions_event_class", table_name="interactions")
    op.drop_column("interactions", "event_class")
