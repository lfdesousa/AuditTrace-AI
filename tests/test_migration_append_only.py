"""Contract test for the ADR-058 WS-A2 append-only migration.

Postgres enforces the trigger at runtime (a ``BEFORE UPDATE OR DELETE``
trigger that raises even for the schema owner) — proven by ADR-049 live
evidence, since the SQLite unit suite builds schema via ``create_all``, not
these migrations. This test pins the migration's DDL shape so a future
refactor cannot silently drop the append-only guarantee on either audit
table.
"""

from __future__ import annotations

from pathlib import Path

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "audittrace"
    / "migrations"
    / "versions"
    / "016_append_only_audit_rows.py"
)


def _text() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


class TestAppendOnlyMigration:
    """The migration must install an append-only trigger on both audit
    tables and be cleanly reversible."""

    def test_migration_exists(self) -> None:
        assert _MIGRATION.is_file()

    def test_trigger_on_both_audit_tables(self) -> None:
        text = _text()
        assert '_APPEND_ONLY_TABLES = ("interactions", "tool_calls")' in text
        assert "{table}_append_only" in text

    def test_blocks_update_and_delete_and_raises(self) -> None:
        text = _text()
        assert "BEFORE UPDATE OR DELETE" in text
        assert "RAISE EXCEPTION" in text

    def test_postgres_guarded_so_sqlite_is_a_noop(self) -> None:
        # Mirrors RLS migration 005: no-op off Postgres so the migration
        # runner (test_alembic) stays green on SQLite.
        assert "_is_postgres" in _text()

    def test_downgrade_drops_trigger_and_function(self) -> None:
        text = _text()
        assert "DROP TRIGGER IF EXISTS" in text
        assert "DROP FUNCTION IF EXISTS audittrace_append_only" in text
