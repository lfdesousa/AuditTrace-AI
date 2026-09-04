"""create session_memory_items table + RLS policy

Revision ID: a8e6ec51e78b
Revises: b1d4f6a9c235
Create Date: 2026-09-04 00:00:00.000000

WU-1 of the Sovereign-Attach EPIC (2026-09-03-SPEC-wu1-session-layer-
narrow-ingest-scope.md) — the least-privilege wall the ratified
ephemeral-default decision requires. Adds ``session_memory_items``, the
backing store for the new ``session`` memory layer
(``src.audittrace.routes.memory.MemoryLayer.session``), gated by the new
``memory:session:write`` scope.

Unlike ``episodic``/``procedural`` (S3-backed, dual-tier per ADR-062
Phase B), session content is Postgres-only: there is no corpus/shared
tier for this layer in this WU (promotion is WU-4, out of scope here),
so a lightweight table is the natural fit rather than an S3 object plus
a ``memory_items`` manifest row.

RLS mirrors migration 005 (``a8b0c2d4e6f8``) verbatim in shape: ENABLE +
FORCE ROW LEVEL SECURITY, one ``FOR ALL`` policy comparing ``user_id``
against ``current_setting('app.current_user_id', true)`` in both USING
and WITH CHECK. Guarded by ``_is_postgres()`` so SQLite (the unit-test
factory, ``InMemoryPostgresFactory``) creates the plain table with no
RLS — the service layer (``PostgresSessionMemoryService``) additionally
filters every query by ``user_id`` explicitly so cross-user isolation is
still caught by the SQLite unit suite, not only a live-Postgres
integration run (feedback_unit_tests_miss_rls).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8e6ec51e78b"
down_revision: str | Sequence[str] | None = "b1d4f6a9c235"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    """Return True when Alembic is running against PostgreSQL.

    SQLite (the in-memory test factory) has no RLS concept; the
    upgrade/downgrade RLS statements below are skipped on that path —
    same guard shape as migration 005.
    """
    bind = op.get_bind()
    return bool(bind.dialect.name == "postgresql")


def upgrade() -> None:
    """Create session_memory_items + index, then (Postgres only) enable
    + force RLS with a per-user policy."""
    op.create_table(
        "session_memory_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
    )
    # LIST-by-owner is the only query shape this WU needs (read_own);
    # WU-5 (recall) may add further indexes when it lands.
    op.create_index(
        "ix_session_memory_items_user_id",
        "session_memory_items",
        ["user_id"],
        unique=False,
    )

    if not _is_postgres():
        return

    op.execute("ALTER TABLE session_memory_items ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE session_memory_items FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_session_memory_items ON session_memory_items
            FOR ALL
            USING (user_id = current_setting('app.current_user_id', true))
            WITH CHECK (user_id = current_setting('app.current_user_id', true))
        """
    )


def downgrade() -> None:
    """Reverse: drop the RLS policy (Postgres only), then the index + table."""
    if _is_postgres():
        op.execute(
            "DROP POLICY IF EXISTS tenant_isolation_session_memory_items "
            "ON session_memory_items"
        )
        op.execute("ALTER TABLE session_memory_items NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE session_memory_items DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_session_memory_items_user_id", table_name="session_memory_items")
    op.drop_table("session_memory_items")
