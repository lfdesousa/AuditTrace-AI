"""Schema/migration tests for migration 032 — ACL WU-2a
(resource-ownership verification, ``2026-09-18-SPEC-acl-wu2a-
resource-ownership-verification.md``).

Migration 031's sibling test file (``test_console_acl_migration.py``)
pins the migration's SOURCE text directly, because that migration's
SQL is written as plain string literals. Migration 032 shares its SQL
fragments (the ownership subquery, the owner-only ``USING`` clause,
the SELECT predicate) across all four policies via module-level
f-string constants — DRY, but it means the SOURCE text of, say, the
INSERT policy's ``op.execute(...)`` call contains only the
placeholder ``{_OWNERSHIP_SUBQUERY}``, not the rendered SQL. A plain
substring pin on the source file would therefore pass or fail on the
wrong thing (variable NAMES, not the SQL Postgres actually receives).

So this file loads the migration module directly and calls
``upgrade()``/``downgrade()`` with ``op.execute``/``op.get_bind``
faked out, capturing the ACTUAL rendered SQL strings Postgres would
receive — the real behavioural contract — then asserts on THAT text.
This is stronger than a source-text pin: it also catches an f-string
composition bug (e.g. a fragment silently resolving to ``''``) that a
source-text pin would miss entirely.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from audittrace.services.console_acl._ownership import RESOLVED_RESOURCE_TYPES

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "audittrace"
    / "migrations"
    / "versions"
    / "032_tighten_console_acl_entries_ownership.py"
)


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_acl_wu2a_migration_032", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeDialect:
    name = "postgresql"


class _FakeBind:
    dialect = _FakeDialect()


def _rendered_statements(module: Any, *, postgres: bool = True) -> list[str]:
    """Fake ``op`` on the loaded module, call the given lifecycle
    function, and return every SQL string it would have sent to
    Postgres, IN ORDER."""
    captured: list[str] = []
    module.op = SimpleNamespace(
        get_bind=lambda: (
            _FakeBind()
            if postgres
            else SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
        ),
        execute=lambda sql: captured.append(sql),
    )
    return captured


def _upgrade_statements() -> list[str]:
    module = _load_migration()
    captured = _rendered_statements(module)
    module.upgrade()
    return captured


def _downgrade_statements() -> list[str]:
    module = _load_migration()
    captured = _rendered_statements(module)
    module.downgrade()
    return captured


def _statement_for(statements: list[str], marker: str) -> str:
    matches = [s for s in statements if marker in s]
    assert len(matches) == 1, (
        f"expected exactly one rendered statement containing {marker!r}, "
        f"got {len(matches)}: {matches!r}"
    )
    return matches[0]


class TestMigrationFile:
    def test_migration_exists(self) -> None:
        assert _MIGRATION_PATH.is_file()

    def test_chains_onto_migration_031(self) -> None:
        module = _load_migration()
        assert module.down_revision == "b3f8a1c6d9e2"

    def test_revision_id_matches_the_expected_head(self) -> None:
        module = _load_migration()
        assert module.revision == "742a8b743c94"

    def test_sqlite_is_a_noop_on_upgrade_and_downgrade(self) -> None:
        """SQLite dialect: neither lifecycle function emits any SQL —
        the ``_is_postgres()`` guard short-circuits before ``op.execute``
        is ever called."""
        for fn_name in ("upgrade", "downgrade"):
            module = _load_migration()
            captured = _rendered_statements(module, postgres=False)
            getattr(module, fn_name)()
            assert captured == [], f"{fn_name}() emitted SQL on a non-Postgres dialect"


class TestUpgradeRenderedSql:
    def test_old_wu1_policy_is_dropped_first(self) -> None:
        statements = _upgrade_statements()
        assert (
            "DROP POLICY IF EXISTS tenant_isolation_console_acl_entries "
            "ON console_acl_entries" == statements[0].strip()
        )

    def test_four_command_scoped_policies_are_created(self) -> None:
        statements = _upgrade_statements()
        for suffix, command in (
            ("select", "FOR SELECT"),
            ("insert", "FOR INSERT"),
            ("update", "FOR UPDATE"),
            ("delete", "FOR DELETE"),
        ):
            stmt = _statement_for(
                statements, f"tenant_isolation_console_acl_entries_{suffix}"
            )
            assert command in stmt, f"{suffix} policy is not declared {command!r}"

    def test_select_policy_preserves_the_original_three_way_using_clause(self) -> None:
        """The read contract (owner OR direct-user-principal OR public)
        must be untouched — the fix narrows WRITE, never READ."""
        statements = _upgrade_statements()
        stmt = _statement_for(statements, "tenant_isolation_console_acl_entries_select")
        assert "user_sub = current_setting('app.current_user_id', true)" in stmt
        assert "principal_type = 'user'" in stmt
        assert "principal_type = 'public'" in stmt
        assert "WITH CHECK" not in stmt

    def test_insert_and_update_gate_on_the_ownership_subquery(self) -> None:
        statements = _upgrade_statements()
        for suffix in ("insert", "update"):
            stmt = _statement_for(
                statements, f"tenant_isolation_console_acl_entries_{suffix}"
            )
            assert "resource_type = 'agent'" in stmt
            assert "console_agents" in stmt
            assert "resource_type = 'promptGroup'" in stmt
            assert "console_prompt_groups" in stmt
            assert "EXISTS" in stmt
            # Ownership is verified against the CALLER's own GUC, never
            # an attacker-suppliable value.
            assert stmt.count("current_setting('app.current_user_id', true)") >= 2

    def test_update_and_delete_using_clauses_are_owner_only(self) -> None:
        """H-1/H-2's root cause: the broad owner-OR-principal-OR-public
        USING clause must NOT gate UPDATE or DELETE — only the owner's
        own rows may ever be selected for either command."""
        statements = _upgrade_statements()
        for suffix in ("update", "delete"):
            stmt = _statement_for(
                statements, f"tenant_isolation_console_acl_entries_{suffix}"
            )
            using_clause = stmt.split("USING (")[1].split(")\n")[0]
            assert "principal_type" not in using_clause, (
                f"{suffix} USING clause still admits a non-owner row"
            )

    def test_delete_policy_has_no_with_check(self) -> None:
        """Postgres never applies WITH CHECK to DELETE — asserting its
        absence documents that the USING-only owner restriction IS the
        entire DELETE guard, not an oversight."""
        statements = _upgrade_statements()
        stmt = _statement_for(statements, "tenant_isolation_console_acl_entries_delete")
        assert "WITH CHECK" not in stmt

    def test_exactly_five_statements_are_emitted(self) -> None:
        """1 DROP + 4 CREATE — a batched 'ownership works' migration
        would be indistinguishable from a correct one at the count
        level alone, but a MISSING or DUPLICATED policy changes this
        count, so it is a cheap, real guard alongside the per-policy
        content assertions above."""
        assert len(_upgrade_statements()) == 5


class TestDowngradeRenderedSql:
    def test_all_four_new_policies_are_dropped(self) -> None:
        statements = _downgrade_statements()
        for suffix in ("select", "insert", "update", "delete"):
            _statement_for(
                statements,
                f"DROP POLICY IF EXISTS tenant_isolation_console_acl_entries_{suffix} "
                "ON console_acl_entries",
            )

    def test_original_single_policy_is_restored(self) -> None:
        statements = _downgrade_statements()
        stmt = statements[-1]
        assert (
            "CREATE POLICY tenant_isolation_console_acl_entries ON console_acl_entries"
            in stmt
        )
        assert "FOR ALL" in stmt
        assert "principal_type = 'public'" in stmt
        assert "WITH CHECK" in stmt

    def test_exactly_five_statements_are_emitted(self) -> None:
        """4 DROP + 1 CREATE — symmetric with the upgrade count guard."""
        assert len(_downgrade_statements()) == 5


class TestOwnershipSubqueryParityWithTheApplicationLayer:
    """The DB-level ownership subquery (this migration) and the
    application-level dispatch table
    (``services/console_acl/_ownership.py``) must name the SAME
    resource_types — a mismatch would mean one layer is stricter than
    the other, silently. Extracted by regex from the RENDERED SQL, not
    hand-copied, so a resolver added to one without the other is
    caught here."""

    def test_subquery_resource_types_match_resolved_resource_types(self) -> None:
        statements = _upgrade_statements()
        insert_stmt = _statement_for(
            statements, "tenant_isolation_console_acl_entries_insert"
        )
        found = set(re.findall(r"resource_type = '([^']+)'", insert_stmt))
        assert found == set(RESOLVED_RESOURCE_TYPES)
