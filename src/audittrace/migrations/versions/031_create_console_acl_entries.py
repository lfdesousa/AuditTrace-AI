"""create console_acl_entries table + RLS policy (WU-1, read path only)

Revision ID: b3f8a1c6d9e2
Revises: 9d4e2b7f1c63
Create Date: 2026-09-17 00:00:00.000000

Sovereign Authorization Layer EPIC, WU-1 — the sovereign ACL store,
READ PATH ONLY (2026-09-17-SPEC-sovereign-authorization-layer-acl-WU0-
ratified-candidate.md, ratified by 2026-09-18-SPEC-ADDENDUM-I). Adds
``console_acl_entries``, AuditTrace's first-party replacement for
LibreChat's Mongo ``AclEntry`` collection
(``packages/data-schemas/src/schema/aclEntry.ts``). No write method
touches this table in WU-1 (WU-2) — the table exists purely so the
read methods (``services/console_acl.py``) have a real store + RLS
policy.

**Ruling 1 (operator, ADDENDUM I) — groups are OUT OF SCOPE AND
DISABLED, enforced at the schema level.** ``ck_console_acl_entries_
principal_type`` allows only ``('user', 'public', 'role')`` — a
``group``-typed principal is refused by Postgres itself (fails closed
and loudly: a raw ``IntegrityError``, never a silent no-op, never an
application-code branch a config flag could disable —
feedback_config_flag_not_enforced_control). WU-0's live-Mongo count
(task zero, see the build record) found ZERO group-principal ACL rows
and ZERO users depending on group-only access, so this ruling revokes
no live access.

**Ruling 2 (operator, ADDENDUM I) — ``expired_at_ms``: filtered on
read, NEVER purged.** No CronJob, no TTL emulation, no chart change.
See ``ConsoleAclEntry``'s docstring (db/models.py) for the full
rationale and the read-path/audit-path split this implies.

RLS is NOT owner-only (unlike every prior console-* migration in this
series, 022-030) — an ACL row exists precisely to grant ANOTHER
principal visibility, so both the resource owner AND the direct
``user`` principal named on a row (AND every ``public`` row) must be
visible to a session, while ``tenant_id`` stays an ADDITIONAL filter,
never the sole isolation boundary (Deliverable 4 finding #4). See the
``CREATE POLICY`` statement below for the exact predicate. Guarded by
``_is_postgres()`` exactly like migrations 005/022-030, so SQLite (the
unit-test factory) creates the plain table (CHECK constraints DO
apply on SQLite too — enforced by the DBAPI, not RLS) with no RLS; the
service layer (``PostgresConsoleAclEntriesService``) mirrors the same
OR-predicate explicitly so cross-user isolation is caught by the
SQLite unit suite too (feedback_unit_tests_miss_rls).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f8a1c6d9e2"
down_revision: str | Sequence[str] | None = "9d4e2b7f1c63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    """Return True when Alembic is running against PostgreSQL.

    SQLite (the in-memory test factory) has no RLS concept; the
    upgrade/downgrade RLS statements below are skipped on that path —
    same guard shape as migrations 005, 022-030. CHECK constraints are
    NOT guarded by this — SQLite enforces CHECK constraints natively,
    so the group-principal refusal (ruling 1) is exercised by the unit
    suite too, not only a live-Postgres integration run.
    """
    bind = op.get_bind()
    return bool(bind.dialect.name == "postgresql")


def upgrade() -> None:
    """Create console_acl_entries + CHECK constraints + indexes, then
    (Postgres only) enable + force RLS with the owner-OR-principal-OR-
    public policy."""
    op.create_table(
        "console_acl_entries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_sub", sa.String(length=36), nullable=False),
        sa.Column("principal_type", sa.String(length=16), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=True),
        sa.Column("principal_model", sa.String(length=16), nullable=True),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=False),
        sa.Column("perm_bits", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("role_id", sa.String(length=36), nullable=True),
        sa.Column("inherited_from", sa.String(length=36), nullable=True),
        sa.Column("granted_by", sa.String(length=36), nullable=True),
        sa.Column("granted_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("expired_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "principal_type IN ('user', 'public', 'role')",
            name="ck_console_acl_entries_principal_type",
        ),
        sa.CheckConstraint(
            "(principal_type = 'public' AND principal_id IS NULL "
            "AND principal_model IS NULL) OR "
            "(principal_type != 'public' AND principal_id IS NOT NULL "
            "AND principal_model IS NOT NULL)",
            name="ck_console_acl_entries_public_principal_null",
        ),
        sa.CheckConstraint(
            "(principal_type = 'user' AND principal_model = 'User') OR "
            "(principal_type = 'role' AND principal_model = 'Role') OR "
            "(principal_type = 'public' AND principal_model IS NULL)",
            name="ck_console_acl_entries_principal_model_matches_type",
        ),
        sa.CheckConstraint(
            "perm_bits >= 0 AND perm_bits <= 15",
            name="ck_console_acl_entries_perm_bits_range",
        ),
    )
    op.create_index(
        "ix_console_acl_entries_user_sub",
        "console_acl_entries",
        ["user_sub"],
        unique=False,
    )
    # Partial unique index — at most one PUBLIC row per resource. Makes
    # "key missing" (any other principal_type) and "deliberately
    # public" distinguishable at the schema level (Deliverable 4
    # finding #1) and prevents duplicate PUBLIC grants accumulating.
    op.create_index(
        "uq_console_acl_entries_public_resource",
        "console_acl_entries",
        ["resource_type", "resource_id"],
        unique=True,
        postgresql_where=sa.text("principal_type = 'public'"),
        sqlite_where=sa.text("principal_type = 'public'"),
    )
    # The four indexes WU-0 found on the live Mongo schema, mirrored as
    # Postgres composite indexes (Deliverable 3).
    op.create_index(
        "ix_console_acl_entries_principal_resource",
        "console_acl_entries",
        ["principal_id", "principal_type", "resource_type", "resource_id", "tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_console_acl_entries_resource_principal",
        "console_acl_entries",
        ["resource_id", "principal_type", "principal_id", "tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_console_acl_entries_principal_permbits",
        "console_acl_entries",
        ["principal_id", "perm_bits", "resource_type", "tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_console_acl_entries_public_lookup",
        "console_acl_entries",
        ["principal_type", "resource_type", "perm_bits", "resource_id"],
        unique=False,
    )
    # Ruling 2 — expired_at_ms is filtered on every authorization-
    # decision read, never purged. This index keeps that filter cheap
    # as the table grows (rows accumulate by design).
    op.create_index(
        "ix_console_acl_entries_expired_at",
        "console_acl_entries",
        ["expired_at_ms"],
        unique=False,
    )

    if not _is_postgres():
        return

    op.execute("ALTER TABLE console_acl_entries ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE console_acl_entries FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_console_acl_entries ON console_acl_entries
            FOR ALL
            USING (
                user_sub = current_setting('app.current_user_id', true)
                OR (
                    principal_type = 'user'
                    AND principal_id = current_setting('app.current_user_id', true)
                )
                OR principal_type = 'public'
            )
            WITH CHECK (
                user_sub = current_setting('app.current_user_id', true)
            )
        """
    )


def downgrade() -> None:
    """Reverse: drop the RLS policy (Postgres only), then the indexes +
    table."""
    if _is_postgres():
        op.execute(
            "DROP POLICY IF EXISTS tenant_isolation_console_acl_entries "
            "ON console_acl_entries"
        )
        op.execute("ALTER TABLE console_acl_entries NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE console_acl_entries DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_console_acl_entries_expired_at", table_name="console_acl_entries")
    op.drop_index(
        "ix_console_acl_entries_public_lookup", table_name="console_acl_entries"
    )
    op.drop_index(
        "ix_console_acl_entries_principal_permbits", table_name="console_acl_entries"
    )
    op.drop_index(
        "ix_console_acl_entries_resource_principal", table_name="console_acl_entries"
    )
    op.drop_index(
        "ix_console_acl_entries_principal_resource", table_name="console_acl_entries"
    )
    op.drop_index(
        "uq_console_acl_entries_public_resource", table_name="console_acl_entries"
    )
    op.drop_index("ix_console_acl_entries_user_sub", table_name="console_acl_entries")
    op.drop_table("console_acl_entries")
