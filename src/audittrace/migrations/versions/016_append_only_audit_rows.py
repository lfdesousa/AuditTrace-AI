"""append-only trigger on interactions + tool_calls (ADR-058 WS-A2)

Revision ID: c7f9a1b3d526
Revises: b5e7c9a1d024
Create Date: 2026-07-14 00:00:00.000000

ADR-058 Half A (integrity, append-only). The audit rows are write-once in
the application (verified 2026-07-14: no ORM UPDATE/DELETE on
``InteractionRow`` or ``ToolCall`` anywhere). Enforce that at the database
with a ``BEFORE UPDATE OR DELETE`` trigger that raises, so even the schema
owner (``sovereign`` — which bypasses GRANT-based REVOKE, and bypasses RLS
without FORCE) cannot amend or delete an audit row after it lands.

This is the append-only half of the tamper-evidence story; the hash-chain
(WS-A3) then makes a privileged bypass *detectable* past the line the
trigger draws.

Postgres-only (plpgsql), guarded like the RLS migration (005): a no-op on
SQLite so the ``test_alembic`` migration runner stays green. The trigger's
*enforcement* is proven by ADR-049 live evidence, not the unit suite; a
structural contract test pins the DDL shape.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c7f9a1b3d526"
down_revision: str | Sequence[str] | None = "b5e7c9a1d024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPEND_ONLY_TABLES = ("interactions", "tool_calls")


def _is_postgres() -> bool:
    """True only on PostgreSQL. SQLite has no plpgsql/trigger equivalent."""
    return bool(op.get_bind().dialect.name == "postgresql")


def upgrade() -> None:
    """Install the append-only trigger on the audit tables (Postgres only)."""
    if not _is_postgres():
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audittrace_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION
                'append-only: % on % is not permitted (ADR-058 audit integrity)',
                TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$;
        """
    )
    for table in _APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION audittrace_append_only();"
        )


def downgrade() -> None:
    """Remove the append-only triggers and the shared function."""
    if not _is_postgres():
        return
    for table in _APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};")
    op.execute("DROP FUNCTION IF EXISTS audittrace_append_only();")
