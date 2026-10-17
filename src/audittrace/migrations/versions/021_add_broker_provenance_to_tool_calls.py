"""add broker provenance columns to tool_calls (ADR-063 Phase 2 Track B)

Revision ID: b1d4f6a9c235
Revises: 5fe377bec101
Create Date: 2026-08-23 00:00:00.000000

Adds the columns that make a brokered ``tools/call`` audit row provably
distinguishable from a Phase-1 own-tool row (spec "Provenance: brokered
rows clearly distinguishable from Phase-1 own-tool rows"): ``provenance``
(NULL on every pre-existing / Phase-1 row, ``"brokered"`` on the new
path only), ``phase`` (``"request"`` | ``"result"`` — the broker writes
exactly two rows per call), ``downstream_server`` / ``downstream_tool``
(the downstream identity), and ``args_digest`` / ``result_digest``
(SHA-256 hex over the canonicalised call/response, so a reviewer can
confirm the request and result rows describe the same exchange without
re-parsing the free-text ``args``/``result_summary`` columns).

All six columns are nullable, no server_default, no backfill — additive
and backward compatible, matching every prior ``tool_calls``/
``interactions`` column migration in this file's history (e.g. 017, 018,
019, 020). Pre-existing rows simply read every new column as NULL,
which is itself the "this is a Phase-1 row" signal.

``tool_calls`` already carries the ADR-058 append-only trigger (migration
016) and the migration-005 RLS policy — neither needs touching here: the
new columns are covered by both automatically (append-only guards the
whole row; RLS filters on the existing ``user_id`` column).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1d4f6a9c235"
down_revision: str | Sequence[str] | None = "5fe377bec101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_COLUMNS: tuple[tuple[str, sa.String], ...] = (
    ("provenance", sa.String(length=16)),
    ("phase", sa.String(length=16)),
    ("downstream_server", sa.String(length=255)),
    ("downstream_tool", sa.String(length=255)),
    ("args_digest", sa.String(length=64)),
    ("result_digest", sa.String(length=64)),
)


def upgrade() -> None:
    """Add the nullable broker-provenance columns to ``tool_calls``."""
    for name, col_type in _NEW_COLUMNS:
        op.add_column("tool_calls", sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    """Drop the broker-provenance columns from ``tool_calls``."""
    for name, _col_type in reversed(_NEW_COLUMNS):
        op.drop_column("tool_calls", name)
