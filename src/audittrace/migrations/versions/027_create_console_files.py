"""create console_files table + RLS policy

Revision ID: 6694d8019051
Revises: a3f7c92e1d5b
Create Date: 2026-09-11 00:00:00.000000

Files-metadata domain of the MongoDB-elimination EPIC
(2026-09-11-SPEC-mongo-repl-wu-files-metadata-store.md) — mirrors the
Chat-Projects domain's migration 026 (and WU-1/WU-presets/WU-prompts'
migrations 023/024/025) shape exactly, for a different domain
(LibreChat's file METADATA record,
``packages/data-schemas/src/schema/file.ts``, Mongo ``File``
collection). Adds ``console_files`` (one row per caller-minted
``file_id``): the metadata-only fields the ratified spec names
(filename/type/bytes/object_key/width/height/context/usage/embedded/
temp_file_id/metadata) as first-class columns — the file BYTES stay in
object storage (S3/MinIO, ``feedback_storage_always_s3``); this table
never stores content, only the record that references it.

RLS mirrors migrations 022/023/024/025/026 verbatim in shape: ENABLE +
FORCE ROW LEVEL SECURITY, one ``FOR ALL`` policy comparing ``user_sub``
against ``current_setting('app.current_user_id', true)`` in both USING
and WITH CHECK. Guarded by ``_is_postgres()`` so SQLite (the unit-test
factory, ``InMemoryPostgresFactory``) creates the plain table with no
RLS — the service layer (``PostgresConsoleFilesService``) additionally
filters every query by ``user_sub`` explicitly so cross-user isolation
is still caught by the SQLite unit suite, not only a live-Postgres
integration run (feedback_unit_tests_miss_rls).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "6694d8019051"
down_revision: str | Sequence[str] | None = "a3f7c92e1d5b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JsonType = sa.JSON().with_variant(JSONB(), "postgresql")


def _is_postgres() -> bool:
    """Return True when Alembic is running against PostgreSQL.

    SQLite (the in-memory test factory) has no RLS concept; the
    upgrade/downgrade RLS statements below are skipped on that path —
    same guard shape as migrations 005, 022, 023, 024, 025, and 026.
    """
    bind = op.get_bind()
    return bool(bind.dialect.name == "postgresql")


def upgrade() -> None:
    """Create console_files + indexes, then (Postgres only) enable +
    force RLS with a per-user policy."""
    op.create_table(
        "console_files",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("file_id", sa.String(length=255), nullable=False),
        sa.Column("user_sub", sa.String(length=36), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("type", sa.String(length=255), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("object_key", sa.Text(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("context", sa.String(length=128), nullable=True),
        sa.Column("usage", _JsonType, nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "embedded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("temp_file_id", sa.String(length=255), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at_ms", sa.BigInteger(), nullable=True),
        sa.Column(
            "metadata", _JsonType, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.UniqueConstraint(
            "user_sub",
            "file_id",
            name="uq_console_files_user_file",
        ),
    )
    op.create_index(
        "ix_console_files_user_sub",
        "console_files",
        ["user_sub"],
        unique=False,
    )
    # Cursor-pagination shape (list_files orders newest-first by
    # updated_at_ms within the caller's own rows) — same rationale as
    # migration 026's console_chat_projects index.
    op.create_index(
        "ix_console_files_user_sub_updated_at_ms",
        "console_files",
        ["user_sub", "updated_at_ms"],
        unique=False,
    )
    # Batch-get-by-ids (``IN (file_id, file_id, ...)``, still scoped by
    # user_sub) is the fourth read shape this domain adds beyond the
    # other WUs' CRUD+cursor pair — this index makes that lookup an
    # index scan rather than a per-user sequential scan.
    op.create_index(
        "ix_console_files_user_sub_file_id",
        "console_files",
        ["user_sub", "file_id"],
        unique=False,
    )

    if not _is_postgres():
        return

    op.execute("ALTER TABLE console_files ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE console_files FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_console_files ON console_files
            FOR ALL
            USING (user_sub = current_setting('app.current_user_id', true))
            WITH CHECK (user_sub = current_setting('app.current_user_id', true))
        """
    )


def downgrade() -> None:
    """Reverse: drop the RLS policy (Postgres only), then the indexes +
    table."""
    if _is_postgres():
        op.execute(
            "DROP POLICY IF EXISTS tenant_isolation_console_files ON console_files"
        )
        op.execute("ALTER TABLE console_files NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE console_files DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_console_files_user_sub_file_id", table_name="console_files")
    op.drop_index("ix_console_files_user_sub_updated_at_ms", table_name="console_files")
    op.drop_index("ix_console_files_user_sub", table_name="console_files")
    op.drop_table("console_files")
