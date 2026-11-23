"""console evidence: ``interactions.client_message_id`` + ``tool_calls.recall_matches`` (+ flag)

Revision ID: 12128bc04e08
Revises: ae606b421cf9
Create Date: 2026-11-23 00:00:00.000000

CONSOLE-AUDIT-EVIDENCE WU-A. Three nullable columns, no default, no backfill, no index,
no policy change:

* ``interactions.client_message_id`` (TEXT) -- the LibreChat message id a chat turn
  carried in ``X-AuditTrace-Message-Id``. Correlation metadata: it is NOT part of the
  hashed content (``integrity._CONTENT_FIELDS``), so no existing ``content_hash`` moves.
* ``tool_calls.recall_matches`` (JSON) -- the per-match identity of a recall, written by
  the sources work unit (WU-B); NULL = not captured, ``[]`` = captured, zero matches.
* ``tool_calls.recall_matches_truncated`` (BOOLEAN) -- NULL wherever ``recall_matches`` is
  NULL, otherwise a boolean (ADDENDUM-A4: the pair is one unit).

The revision id is ``sha256("console-evidence migration 041 client_message_id +
recall_matches")[:12]`` (the A1-B4 method). Both dialects run the same statements (the
039/040 pattern); upgrade is catalog-only on PostgreSQL. Numbers 035-038 stay unused by
this chain (ACL WU-2d renumbers after this slice and chains onto this revision).

Downgrade drops the three columns. It loses only correlation metadata and the
(not yet populated) recall record; no hashed field is touched, so no row fails
``verify_content_hash`` afterwards.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "12128bc04e08"
down_revision: str | Sequence[str] | None = "ae606b421cf9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "interactions", sa.Column("client_message_id", sa.Text(), nullable=True)
    )
    op.add_column("tool_calls", sa.Column("recall_matches", sa.JSON(), nullable=True))
    op.add_column(
        "tool_calls", sa.Column("recall_matches_truncated", sa.Boolean(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("tool_calls", "recall_matches_truncated")
    op.drop_column("tool_calls", "recall_matches")
    op.drop_column("interactions", "client_message_id")
