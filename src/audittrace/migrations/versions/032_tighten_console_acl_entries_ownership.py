"""tighten console_acl_entries RLS: resource-ownership at write time
(ACL WU-2a — resource-ownership verification)

Revision ID: 742a8b743c94
Revises: b3f8a1c6d9e2
Create Date: 2026-09-18 00:00:00.000000

**Why this migration exists.** Migration 031 (WU-1, read-path only)
shipped ``console_acl_entries`` with a single ``FOR ALL`` policy whose
entire write guard was ``WITH CHECK (user_sub = current_setting
('app.current_user_id', true))``. That guard proves the GRANTOR is who
they say they are; it proves nothing about whether the grantor OWNS
``resource_id``. ``db/models.py`` (``ConsoleAclEntry``) says so
explicitly: *"validating the reference is explicitly out of [WU-1's]
scope."* ``2026-09-18-SPEC-acl-wu2a-resource-ownership-verification.md``
(ACL WU-2a) closes that gap at the ONE layer an application bug cannot
skip — the database — while ``services/console_acl/_ownership.py``
closes it (as defence in depth, not the control) at the application
layer.

**The two further holes the same broad ``USING``/narrow ``WITH CHECK``
shape opened, proven against this migration by
``tests/test_acl_ownership_rls.py`` (see that file's docstring for the
before/after evidence):**

* **H-1** — Postgres gates ``DELETE`` by ``USING`` alone; ``WITH
  CHECK`` never applies to ``DELETE``. Migration 031's ``USING``
  included an unconditional ``principal_type = 'public'`` disjunct, so
  ANY authenticated user could ``DELETE`` ANY public ACL row on ANY
  resource.
* **H-2** — the same broad ``USING`` admits a public row for
  ``UPDATE``, and the narrow ``WITH CHECK`` only checked the row's
  (new) ``user_sub`` — so ``UPDATE ... SET user_sub = <attacker> WHERE
  principal_type = 'public'`` let an attacker TAKE OWNERSHIP of any
  public grant.

**The fix: replace the single ``FOR ALL`` policy with four
command-scoped policies.** ``SELECT`` keeps the ORIGINAL owner-OR-
principal-OR-public predicate verbatim (migration 031's read
contract, exercised by every WU-1 test, must not change).
``INSERT``/``UPDATE``/``DELETE`` are now OWNER-ONLY at the ``USING``
level (``user_sub = current_setting(...)`` — a "public" or "user"
principal row is never selectable for a write, closing H-1 and H-2 at
the root: neither hole's ``UPDATE``/``DELETE`` can even see a row it
doesn't own). ``INSERT``/``UPDATE`` additionally gate their ``WITH
CHECK`` on a per-``resource_type`` ownership subquery against the
resource's OWN sovereign store (mirrors ``services/console_acl/
_ownership.py``'s dispatch table exactly — ``agent`` against
``console_agents``, ``promptGroup`` against ``console_prompt_groups``):
a ``resource_type`` with no wired subquery branch evaluates to
``FALSE`` unconditionally, so an unmigrated or unrecognised
``resource_type`` is refused at the DB layer too, not just the
application layer — the two layers fail closed the SAME way,
independently.

Guarded by ``_is_postgres()`` exactly like migration 031, so SQLite
(the unit-test factory) is unaffected — this migration is a Postgres-
only DDL change with no new columns/tables, hence no SQLite branch is
needed at all beyond the existing guard.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "742a8b743c94"
down_revision: str | Sequence[str] | None = "b3f8a1c6d9e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_POLICY = "tenant_isolation_console_acl_entries"

# The per-resource_type ownership subquery, shared verbatim by the
# INSERT and UPDATE WITH CHECK clauses below. A resource_type absent
# from this OR-chain evaluates to FALSE — fail closed, mirroring
# ``services/console_acl/_ownership.py``'s ``UnknownResourceTypeError``
# at the DB layer. Extend BOTH this subquery and that module's
# ``_OWNERSHIP_RESOLVERS`` table together when a new sovereign store
# ships — a mismatch between the two would only be a *stricter* DB
# barrier than the app layer, never a laxer one, but keeping them in
# lockstep is what the parity test in
# ``tests/test_console_acl_ownership_migration.py`` enforces.
_OWNERSHIP_SUBQUERY = """
    (
        (
            resource_type = 'agent'
            AND EXISTS (
                SELECT 1 FROM console_agents ca
                WHERE ca.agent_id = resource_id
                  AND ca.user_sub = current_setting('app.current_user_id', true)
                  AND ca.deleted_at_ms IS NULL
            )
        )
        OR (
            resource_type = 'promptGroup'
            AND EXISTS (
                SELECT 1 FROM console_prompt_groups cpg
                WHERE cpg.group_id = resource_id
                  AND cpg.user_sub = current_setting('app.current_user_id', true)
                  AND cpg.deleted_at_ms IS NULL
            )
        )
    )
"""

_OWNER_ONLY_USING = "user_sub = current_setting('app.current_user_id', true)"

_SELECT_USING = """
    user_sub = current_setting('app.current_user_id', true)
    OR (
        principal_type = 'user'
        AND principal_id = current_setting('app.current_user_id', true)
    )
    OR principal_type = 'public'
"""


def _is_postgres() -> bool:
    """Return True when Alembic is running against PostgreSQL — same
    guard as migration 031; SQLite has no RLS concept."""
    bind = op.get_bind()
    return bool(bind.dialect.name == "postgresql")


def upgrade() -> None:
    """Replace the single WU-1 ``FOR ALL`` policy with four
    command-scoped policies (Postgres only)."""
    if not _is_postgres():
        return

    op.execute(f"DROP POLICY IF EXISTS {_OLD_POLICY} ON console_acl_entries")

    op.execute(
        f"""
        CREATE POLICY {_OLD_POLICY}_select ON console_acl_entries
            FOR SELECT
            USING ({_SELECT_USING})
        """
    )
    op.execute(
        f"""
        CREATE POLICY {_OLD_POLICY}_insert ON console_acl_entries
            FOR INSERT
            WITH CHECK (
                {_OWNER_ONLY_USING}
                AND {_OWNERSHIP_SUBQUERY}
            )
        """
    )
    op.execute(
        f"""
        CREATE POLICY {_OLD_POLICY}_update ON console_acl_entries
            FOR UPDATE
            USING ({_OWNER_ONLY_USING})
            WITH CHECK (
                {_OWNER_ONLY_USING}
                AND {_OWNERSHIP_SUBQUERY}
            )
        """
    )
    op.execute(
        f"""
        CREATE POLICY {_OLD_POLICY}_delete ON console_acl_entries
            FOR DELETE
            USING ({_OWNER_ONLY_USING})
        """
    )


def downgrade() -> None:
    """Reverse: drop the four command-scoped policies and restore
    migration 031's single ``FOR ALL`` policy verbatim."""
    if not _is_postgres():
        return

    for suffix in ("select", "insert", "update", "delete"):
        op.execute(
            f"DROP POLICY IF EXISTS {_OLD_POLICY}_{suffix} ON console_acl_entries"
        )

    op.execute(
        f"""
        CREATE POLICY {_OLD_POLICY} ON console_acl_entries
            FOR ALL
            USING ({_SELECT_USING})
            WITH CHECK ({_OWNER_ONLY_USING})
        """
    )
