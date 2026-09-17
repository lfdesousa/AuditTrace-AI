"""Schema/migration tests for ``console_acl_entries`` (Sovereign
Authorization Layer EPIC, WU-1).

Two kinds of proof, mirroring ``test_migration_append_only.py``'s
split:

* DDL-TEXT pins on the migration file itself for statements that only
  run on live Postgres (``ENABLE``/``FORCE ROW LEVEL SECURITY``, the
  ``CREATE POLICY`` predicate) — the SQLite unit suite cannot execute
  these, so this test guards against silent drift/removal.
* LIVE guards proven against the REAL ORM-declared CHECK constraints
  and the partial unique index, exercised through
  ``InMemoryPostgresFactory`` (aiosqlite) — SQLite enforces CHECK
  constraints and (modern SQLite) partial unique indexes natively, so
  these ARE real, falsifiable, DB-level proofs, not text pins.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError

from audittrace.db.models import ConsoleAclEntry
from audittrace.db.postgres import InMemoryPostgresFactory

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "audittrace"
    / "migrations"
    / "versions"
    / "031_create_console_acl_entries.py"
)


def _text() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


class TestMigrationFile:
    def test_migration_exists(self) -> None:
        assert _MIGRATION.is_file()

    def test_single_alembic_head(self) -> None:
        """The epic's gate: exactly one alembic head after this
        migration lands — a forked chain is a silent split-brain
        schema, never acceptable."""
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        repo_root = Path(__file__).resolve().parent.parent
        cfg = Config(str(repo_root / "alembic.ini"))
        cfg.set_main_option(
            "script_location", str(repo_root / "src" / "audittrace" / "migrations")
        )
        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
        assert len(heads) == 1, f"expected a single alembic head, got {heads!r}"

    def test_chains_onto_the_real_prior_head(self) -> None:
        assert 'down_revision: str | Sequence[str] | None = "9d4e2b7f1c63"' in _text()

    def test_postgres_guarded_so_sqlite_is_a_noop(self) -> None:
        assert "_is_postgres" in _text()

    def test_group_principal_check_constraint_present(self) -> None:
        text = _text()
        assert "ck_console_acl_entries_principal_type" in text
        assert "principal_type IN ('user', 'public', 'role')" in text
        # 'group' must never appear as an allowed value anywhere in the
        # migration — ADDENDUM I ruling 1.
        assert "'group'" not in text

    def test_public_principal_null_check_constraint_present(self) -> None:
        assert "ck_console_acl_entries_public_principal_null" in _text()

    def test_principal_model_matches_type_check_constraint_present(self) -> None:
        assert "ck_console_acl_entries_principal_model_matches_type" in _text()

    def test_perm_bits_range_check_constraint_present(self) -> None:
        text = _text()
        assert "ck_console_acl_entries_perm_bits_range" in text
        assert "perm_bits >= 0 AND perm_bits <= 15" in text

    def test_partial_unique_public_index_present(self) -> None:
        text = _text()
        assert "uq_console_acl_entries_public_resource" in text
        assert "principal_type = 'public'" in text

    def test_four_wu0_indexes_present(self) -> None:
        text = _text()
        for name in (
            "ix_console_acl_entries_principal_resource",
            "ix_console_acl_entries_resource_principal",
            "ix_console_acl_entries_principal_permbits",
            "ix_console_acl_entries_public_lookup",
        ):
            assert name in text

    def test_expiry_index_present(self) -> None:
        assert "ix_console_acl_entries_expired_at" in _text()

    def test_rls_enabled_and_forced(self) -> None:
        text = _text()
        assert "ENABLE ROW LEVEL SECURITY" in text
        assert "FORCE ROW LEVEL SECURITY" in text

    def test_rls_policy_is_not_owner_only(self) -> None:
        """The predicate must name all three branches — owner, direct
        user-principal, and public — never collapse to owner-only
        (which would break every read the moment a grant exists)."""
        text = _text()
        assert "user_sub = current_setting" in text
        assert "principal_type = 'user'" in text
        assert "principal_type = 'public'" in text

    def test_with_check_stays_owner_only(self) -> None:
        text = _text()
        with_check = text.split("WITH CHECK (")[1]
        assert "user_sub = current_setting" in with_check
        # The WITH CHECK clause itself must not also open the
        # principal/public branches — only the owner may write (WU-2).
        clause_body = with_check.split(")")[0]
        assert "principal_type" not in clause_body

    def test_downgrade_drops_policy_and_table(self) -> None:
        text = _text()
        assert "DROP POLICY IF EXISTS tenant_isolation_console_acl_entries" in text
        assert "DISABLE ROW LEVEL SECURITY" in text
        assert 'drop_table("console_acl_entries")' in text


# ── Live (SQLite-enforced) schema guards ─────────────────────────────────


@pytest_asyncio.fixture
async def pg_factory():
    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    return factory


def _base_row(**overrides):
    row = {
        "id": "row-1",
        "user_sub": "owner-1",
        "principal_type": "public",
        "principal_id": None,
        "principal_model": None,
        "resource_type": "agent",
        "resource_id": "res-1",
        "perm_bits": 1,
        "granted_at_ms": 0,
        "created_at_ms": 0,
        "updated_at_ms": 0,
    }
    row.update(overrides)
    return row


class TestLiveSchemaGuards:
    """Each guard here is FALSIFIABLE: comment out the corresponding
    CHECK constraint in ``db/models.py`` and the matching test goes
    RED (an ``IntegrityError`` stops being raised)."""

    async def test_group_principal_is_refused(self, pg_factory) -> None:
        session_factory = pg_factory.get_session_factory()
        async with session_factory() as session:
            session.add(
                ConsoleAclEntry(
                    **_base_row(
                        id="g1",
                        principal_type="group",
                        principal_id="grp-1",
                        principal_model="Group",
                    )
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_public_row_with_principal_id_is_refused(self, pg_factory) -> None:
        session_factory = pg_factory.get_session_factory()
        async with session_factory() as session:
            session.add(ConsoleAclEntry(**_base_row(id="p1", principal_id="someone")))
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_non_public_row_missing_principal_id_is_refused(
        self, pg_factory
    ) -> None:
        session_factory = pg_factory.get_session_factory()
        async with session_factory() as session:
            session.add(
                ConsoleAclEntry(
                    **_base_row(
                        id="np1",
                        principal_type="user",
                        principal_id=None,
                        principal_model="User",
                    )
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_user_principal_with_role_model_is_refused(self, pg_factory) -> None:
        session_factory = pg_factory.get_session_factory()
        async with session_factory() as session:
            session.add(
                ConsoleAclEntry(
                    **_base_row(
                        id="m1",
                        principal_type="user",
                        principal_id="u1",
                        principal_model="Role",
                    )
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_perm_bits_above_max_is_refused(self, pg_factory) -> None:
        session_factory = pg_factory.get_session_factory()
        async with session_factory() as session:
            session.add(ConsoleAclEntry(**_base_row(id="pb1", perm_bits=16)))
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_perm_bits_negative_is_refused(self, pg_factory) -> None:
        session_factory = pg_factory.get_session_factory()
        async with session_factory() as session:
            session.add(ConsoleAclEntry(**_base_row(id="pb2", perm_bits=-1)))
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_duplicate_public_row_per_resource_is_refused(
        self, pg_factory
    ) -> None:
        session_factory = pg_factory.get_session_factory()
        async with session_factory() as session:
            session.add(ConsoleAclEntry(**_base_row(id="dup1", resource_id="dup-res")))
            await session.commit()
        async with session_factory() as session:
            session.add(
                ConsoleAclEntry(
                    **_base_row(id="dup2", resource_id="dup-res", perm_bits=2)
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_two_different_resources_can_each_be_public(self, pg_factory) -> None:
        """Non-vacuity companion to the duplicate-PUBLIC guard above —
        proves the unique index is scoped per-resource, not global."""
        session_factory = pg_factory.get_session_factory()
        async with session_factory() as session:
            session.add(ConsoleAclEntry(**_base_row(id="ok1", resource_id="res-a")))
            session.add(ConsoleAclEntry(**_base_row(id="ok2", resource_id="res-b")))
            await session.commit()

    async def test_role_principal_with_user_model_is_refused(self, pg_factory) -> None:
        session_factory = pg_factory.get_session_factory()
        async with session_factory() as session:
            session.add(
                ConsoleAclEntry(
                    **_base_row(
                        id="rm1",
                        principal_type="role",
                        principal_id="admin-role",
                        principal_model="User",
                    )
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_valid_user_row_is_accepted(self, pg_factory) -> None:
        session_factory = pg_factory.get_session_factory()
        async with session_factory() as session:
            session.add(
                ConsoleAclEntry(
                    **_base_row(
                        id="v1",
                        principal_type="user",
                        principal_id="u1",
                        principal_model="User",
                        resource_id="res-v1",
                    )
                )
            )
            await session.commit()

    async def test_valid_role_row_is_accepted(self, pg_factory) -> None:
        session_factory = pg_factory.get_session_factory()
        async with session_factory() as session:
            session.add(
                ConsoleAclEntry(
                    **_base_row(
                        id="v2",
                        principal_type="role",
                        principal_id="editor-role",
                        principal_model="Role",
                        resource_id="res-v2",
                    )
                )
            )
            await session.commit()
