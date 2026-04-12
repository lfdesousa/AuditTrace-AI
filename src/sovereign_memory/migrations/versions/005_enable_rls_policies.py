"""enable row-level security policies on audit tables

Revision ID: a8b0c2d4e6f8
Revises: e6f8a0c2d4e6
Create Date: 2026-04-11 21:00:00.000000

ADR-026 §16 Phase 4. Adds Postgres RLS policies
on ``interactions``, ``sessions`` and ``tool_calls`` so cross-user
reads are blocked at the database layer regardless of whether the
service code remembers the ``WHERE user_id =`` clause.

Contract:

- Every table gets ``ENABLE ROW LEVEL SECURITY`` **and**
  ``FORCE ROW LEVEL SECURITY``. FORCE is required because the
  application connects as the schema owner (``sovereign``); without
  FORCE the owner bypasses the policy by default.
- One policy per table, ``FOR ALL``, with
  ``USING (user_id = current_setting('app.current_user_id', true))``
  and the same expression in ``WITH CHECK``. Any row whose user_id
  doesn't match the current GUC is invisible to reads AND rejected
  on INSERT/UPDATE.
- Empty-string default: when the GUC is unset,
  ``current_setting('app.current_user_id', true)`` returns an empty
  string, which won't match any real user_id. That's the
  safe-by-default semantics — a caller that forgets to set the
  ContextVar sees zero rows instead of leaking.

SQLite has no RLS concept. When this migration runs against the
in-memory SQLite test factory the upgrade/downgrade are **no-ops**
so the existing fast test path stays working. The
``test_rls_isolation.py`` integration file exercises the Postgres
path explicitly against the running sovereign-postgres container.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8b0c2d4e6f8"
down_revision: str | Sequence[str] | None = "e6f8a0c2d4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The three tables that carry per-user rows. Each gets the same policy
# shape so the migration body below is a simple loop.
_RLS_TABLES: tuple[str, ...] = ("interactions", "sessions", "tool_calls")


def _is_postgres() -> bool:
    """Return True when Alembic is running against PostgreSQL.

    SQLite (the in-memory test factory) has no RLS concept; returning
    False here makes the whole migration a no-op on that path.
    """
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    """Enable + force RLS on the three audit tables, one permissive
    policy each gated on ``app.current_user_id``."""
    if not _is_postgres():
        # SQLite / other dialects: no-op. Tests use the service-layer
        # filter from Phase 2; the Postgres path is verified by the
        # dedicated test_rls_isolation.py integration file.
        return

    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation_{table} ON {table}
                FOR ALL
                USING (user_id = current_setting('app.current_user_id', true))
                WITH CHECK (user_id = current_setting('app.current_user_id', true))
            """
        )


def downgrade() -> None:
    """Drop the policies and disable RLS so the tables are readable
    again without the GUC. Reverses upgrade() step for step."""
    if not _is_postgres():
        return

    for table in _RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
