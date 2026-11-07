"""session_memory_items: add the per-chat ``session_id`` compartment (LC-10a)

Revision ID: 5b4bd766374f
Revises: c76f1b005b75
Create Date: 2026-11-07 00:00:00.000000

LC-10a (per-chat session recall, spec
``2026-11-03-SPEC-lc10a-per-chat-session-recall`` + ADDENDUM-4/5). The
revision id is ``sha256("lc10a migration session_memory_items session_id")[:12]``.

Adds ONE nullable column and ONE index to ``session_memory_items``::

    session_id VARCHAR(255) NULL
    ix_session_memory_items_user_session (user_id, session_id, created_at_ms DESC)

* The column is a COMPARTMENT inside one user's rows, never a principal: the
  RLS policy of migration 022 still compares ``user_id`` only and is not
  touched here (R5).
* No backfill (operator Q1): rows written before this migration keep
  ``session_id NULL`` and are never returned by a bound recall; they age out
  under ``session_retention_hours`` (24 h).
* Both dialects run the column and the index; there is no Postgres-only
  statement, so there is no ``_is_postgres()`` guard.

Numbering: 035-038 stay reserved for console WU-A and the 2d chain (LC-9
ADDENDUM-1 C2). Whichever lands second rebases its ``down_revision``: console
WU-A (035) rebases onto ``5b4bd766374f`` when it lands after this migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b4bd766374f"
down_revision: str | Sequence[str] | None = "c76f1b005b75"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "session_memory_items"
_INDEX = "ix_session_memory_items_user_session"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("session_id", sa.String(length=255), nullable=True),
    )
    op.create_index(
        _INDEX,
        _TABLE,
        ["user_id", "session_id", sa.text("created_at_ms DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "session_id")
