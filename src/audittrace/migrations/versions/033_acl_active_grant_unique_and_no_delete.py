"""console_acl_entries: active-grant uniqueness + no-delete trigger
(ACL 2b-core-A1 — the write path's schema half)

Revision ID: 2c013428184a
Revises: 742a8b743c94
Create Date: 2026-09-26 00:00:00.000000

Sovereign Authorization Layer EPIC, **ACL 2b-core-A1**
(``2026-09-26-SPEC-acl-2b-core-A-write-path.md`` + ADDENDA U/V/W/X). This
migration adds the two schema-level guards the write path (2b-core-A)
needs and 2b-core-B's audit writer assumes:

**O-3 — at most one ACTIVE grant per ``(principal_type, principal_id,
resource_type, resource_id, tenant_id)``, both dialects.** A partial
unique index scoped to ``expired_at_ms IS NULL`` — an EXPIRED row never
counts against the constraint, so ``grant_permission``'s expire-and-
insert (O-6) can freely accumulate history. On Postgres,
``postgresql_nulls_not_distinct=True`` makes two rows that both carry
``tenant_id IS NULL`` collide (every sovereign row inserts
``tenant_id = NULL`` today, migration 031's ``tenant_id`` column being
nullable) — **rendered identical on SQLAlchemy 2.0.51 and 2.1.0** (spec
§2 Fact 2 / ADDENDUM U-7). PG15+ syntax; the real-Postgres harness here
is ``postgres:16``, live is PG 18.3 (both support it). **On SQLite the
index exists but ``NULLS DISTINCT`` applies** (SQLite has no
``NULLS NOT DISTINCT`` syntax at all) — two active rows that both carry
``tenant_id IS NULL`` do NOT collide there. **The O-3 constraint is
therefore unproven on the aiosqlite path** — disclosed, not silently
assumed proven; the real guard is the Postgres-only neuter (spec §9 #9a).

Interaction with migration 031's ``uq_console_acl_entries_public_resource``
(031:129-136): a second PUBLIC row for the same resource is refused by
031's index FIRST (it fires on every INSERT regardless of this one) —
consistent, not a conflict; 031's index is un-scoped by ``principal_id``
(PUBLIC rows carry none) where this one is scoped by all five columns.

**Q-4 — no-delete trigger (Postgres only), REUSING migration 016's
shared function.** ``console_acl_entries`` becomes append-only exactly
like ``interactions``/``tool_calls`` (016) — revocation and
``deleteAclEntries`` EXPIRE and retain rows (R-1); nothing in
``services/console_acl/`` ever issues a real ``DELETE`` against this
table. This migration does **NOT** create or replace
``audittrace_append_only()`` — 016 owns it (``016:49-57``); this
migration's ``down_revision`` chain makes 016 an ancestor, so the
function already exists by the time this trigger is created.
``downgrade()`` drops the trigger (and the index) but does **NOT** drop
the function — in a linear Alembic chain this migration's ``downgrade()``
runs BEFORE 016's, so 016's own ``DROP FUNCTION`` (which would fail
while THIS trigger still references the function) never has to contend
with it (Q-verdict A-5). Migration 032's owner-only DELETE policy
(``032:162-168``) is untouched and becomes moot for this table — a
non-owner's DELETE was already refused by RLS; now an OWNER's DELETE is
refused too, by the trigger, at the same ``insufficient_privilege``
error class 016 already established. ``TRUNCATE`` is statement-level and
unaffected by a row-level trigger (out of scope either way — no code
path issues one). Guarded by the SAME ``_is_postgres()`` shape as
016/031/032, so SQLite (the aiosqlite unit-test factory) is a no-op for
this half; the partial unique index above is NOT guarded — SQLite
creates it too (with the disclosed ``NULLS DISTINCT`` caveat).

Model-level mirror: ``ConsoleAclEntry.__table_args__``
(``db/models.py:1175``) gains the same ``Index(...)`` (same kwargs) so
``Base.metadata.create_all`` (the aiosqlite unit-test path via
``InMemoryPostgresFactory``) carries the index too — ``create_all`` never
runs migrations, so the model and the migration must independently agree
on the DDL shape (same convention every other console-* index in this
module follows).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2c013428184a"
down_revision: str | Sequence[str] | None = "742a8b743c94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "console_acl_entries"
_ACTIVE_GRANT_INDEX = "uq_console_acl_entries_active_grant"
_NO_DELETE_TRIGGER = "console_acl_entries_no_delete"
# Migration 016's shared function (``016:49-57``) — NOT created here.
_APPEND_ONLY_FUNCTION = "audittrace_append_only"


def _is_postgres() -> bool:
    """Return True when Alembic is running against PostgreSQL — same
    guard as migrations 005/016/031/032; SQLite has no trigger/plpgsql
    concept and no ``NULLS NOT DISTINCT`` syntax."""
    bind = op.get_bind()
    return bool(bind.dialect.name == "postgresql")


def upgrade() -> None:
    """O-3's partial unique index (both dialects) + Q-4's no-delete
    trigger (Postgres only, reusing 016's function)."""
    op.create_index(
        _ACTIVE_GRANT_INDEX,
        _TABLE,
        ["principal_type", "principal_id", "resource_type", "resource_id", "tenant_id"],
        unique=True,
        postgresql_where=sa.text("expired_at_ms IS NULL"),
        sqlite_where=sa.text("expired_at_ms IS NULL"),
        postgresql_nulls_not_distinct=True,
    )

    if not _is_postgres():
        return

    op.execute(
        f"CREATE TRIGGER {_NO_DELETE_TRIGGER} "
        f"BEFORE DELETE ON {_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION {_APPEND_ONLY_FUNCTION}()"
    )


def downgrade() -> None:
    """Reverse: drop the trigger (Postgres only — the shared function
    is 016's, never dropped here) then the partial unique index (both
    dialects)."""
    if _is_postgres():
        op.execute(f"DROP TRIGGER IF EXISTS {_NO_DELETE_TRIGGER} ON {_TABLE}")

    op.drop_index(_ACTIVE_GRANT_INDEX, table_name=_TABLE)
