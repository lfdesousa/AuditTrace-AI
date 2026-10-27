"""memory_items: a live private row's key is pinned to its owner (LC-9 WP1)

Revision ID: c76f1b005b75
Revises: 2c013428184a
Create Date: 2026-10-27 00:00:00.000000

SECURITY (LC-9, cross-user private-row isolation; spec
``2026-10-26-SPEC-lc9-cross-user-private-row-isolation`` R2 + ADDENDUM-1 C2).
The revision id is ``sha256("lc9 migration 034 private key owner check")[:12]``
(the A1-B4 method); LC-9 ships before console WU-A, which therefore becomes
035 with ``down_revision = 'c76f1b005b75'``.

Adds ONE constraint, ``ck_memory_items_private_key_owner``::

    CHECK (tier <> 'private'
           OR deleted_at_ms IS NOT NULL
           OR substr(key, 1, length(created_by_user_id) + 1)
              = created_by_user_id || '/')   NOT VALID

* It is an EQUALITY on a substring, not a ``LIKE`` (``_`` is a LIKE wildcard,
  so ``user_a`` would otherwise match ``userXa``).
* ``NOT VALID`` leaves every existing (legacy, bare-key) row unvalidated,
  while EVERY INSERT and UPDATE is checked. So a legacy private row can be
  tombstoned (``deleted_at_ms`` set) but never revived or edited in place,
  which is fail-closed: copy-forward (resolver kind ``legacy_own``) is the
  only way a legacy row's owner writes it again.
* A tombstoned row is exempt, so a soft-deleted legacy row survives any later
  UPDATE that keeps it tombstoned.
* Corpus rows (``tier = 'corpus'``, the column default, incl. the scan
  pipeline's ``insert_pending_scan`` rows) are untouched.

Postgres-only, like 016: a no-op on SQLite (the aiosqlite unit-test factory
builds its schema with ``create_all`` from the model, which does not carry
this CHECK, so unit tests can still seed legacy rows). No column is added,
removed or retyped; ``interactions``/``tool_calls`` (016's trigger) and 017's
``content_hash`` are untouched, so the frozen audit schema holds.

**Rollout / rollback order (gate S3, measured).** Under the CHECK the
PRE-LC-9 ``record_create`` for a bare private key raises ``IntegrityError``.
This migration and the owner-keyed resolver therefore ship in the SAME PR and
image, and a rollback is ``alembic downgrade`` FIRST, then the previous image.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c76f1b005b75"
down_revision: str | Sequence[str] | None = "2c013428184a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "memory_items"
_CONSTRAINT = "ck_memory_items_private_key_owner"


def _is_postgres() -> bool:
    """True only on PostgreSQL (SQLite cannot ADD CONSTRAINT ... NOT VALID)."""
    return bool(op.get_bind().dialect.name == "postgresql")


def upgrade() -> None:
    """Add the NOT VALID owner-prefix CHECK (Postgres only)."""
    if not _is_postgres():
        return
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} "
        "CHECK (tier <> 'private' OR deleted_at_ms IS NOT NULL "
        "OR substr(key, 1, length(created_by_user_id) + 1) "
        "= created_by_user_id || '/') NOT VALID"
    )


def downgrade() -> None:
    """Drop the CHECK (Postgres only). Run BEFORE rolling the image back."""
    if not _is_postgres():
        return
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
