"""interactions: add ``input_media`` (LIBRECHAT-IMAGE-SLICE)

Revision ID: ae606b421cf9
Revises: 5b4bd766374f
Create Date: 2026-11-21 00:00:00.000000

One nullable TEXT column, no default, no backfill, no index, no policy change.
The revision id is ``sha256("librechat image slice migration interactions
input_media")[:12]``. Both dialects run the same statement (no ``_is_postgres()``
guard). The column holds the canonical JSON naming every image part of the chat
request (declared type, decoded size, sha256), never the bytes.

Upgrade is catalog-only on PostgreSQL (no table rewrite). A row can only carry the
column at INSERT: the append-only trigger refuses every UPDATE, so no backfill
exists and rows written before this migration stay NULL ("no image recorded").

Downgrade is GUARDED: it refuses while any row records an image, because dropping
the column destroys that record and makes those rows fail ``verify_content_hash``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ae606b421cf9"
down_revision: str | Sequence[str] | None = "5b4bd766374f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("interactions", sa.Column("input_media", sa.Text(), nullable=True))


def _count_image_rows(forced: bool) -> int:
    """Count the rows that record an image, as the TRUE number.

    On PostgreSQL ``interactions`` has row-level security FORCED, so a plain count
    by the migration role (the table owner, not a superuser) sees only the rows of
    the identity bound to the session, normally none: the guard would pass and the
    drop would destroy every owner's records. The role that may drop the column
    owns the table, so FORCE is lifted for this transaction (restored by the
    caller) and ``row_security = off`` makes a count that a policy would still
    filter fail instead of answer.
    """
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"
    if postgres:
        if forced:
            op.execute("ALTER TABLE interactions NO FORCE ROW LEVEL SECURITY")
        op.execute("SET LOCAL row_security = off")
    recorded = int(
        bind.execute(
            sa.text("SELECT count(*) FROM interactions WHERE input_media IS NOT NULL")
        ).scalar_one()
    )
    if postgres:
        op.execute("SET LOCAL row_security = on")
    return recorded


def downgrade() -> None:
    """Refuse while any row records an image; change nothing when refusing.

    The refusal raises inside the migration's transaction, so the lifted FORCE is
    rolled back with it. ``alembic downgrade --sql`` (offline) cannot run the count
    and fails instead of emitting an unguarded ``DROP``.
    """
    if op.get_context().as_sql:
        raise RuntimeError(
            "refusing an offline (--sql) downgrade of interactions.input_media: the "
            "guard must count the rows that record an image, which a script cannot "
            "do; run the downgrade online"
        )
    bind = op.get_bind()
    forced = False
    if bind.dialect.name == "postgresql":
        forced = bool(
            bind.execute(
                sa.text(
                    "SELECT relforcerowsecurity FROM pg_class "
                    "WHERE oid = 'interactions'::regclass"
                )
            ).scalar_one()
        )
    recorded = _count_image_rows(forced)
    if recorded:
        raise RuntimeError(
            f"refusing to drop interactions.input_media: {recorded} "
            f"{'row records' if recorded == 1 else 'rows record'} an image; "
            "dropping the column destroys that record and makes those rows fail "
            "content_hash verification"
        )
    op.drop_column("interactions", "input_media")
    if forced:
        op.execute("ALTER TABLE interactions FORCE ROW LEVEL SECURITY")
