"""LC-9 WP1 -- migration 034 (``c76f1b005b75``) on REAL Postgres, plus the
real manifest service against the live CHECK.

Static half (no Docker): revision identity, the single head, the exact
substring comparison, ``NOT VALID``, the Postgres guard. PG half (throwaway
``postgres:18.3``, or ``AUDITTRACE_TEST_POSTGRES_URL``): upgrade/downgrade, a
legacy row survives and can be tombstoned but not revived or edited, an
owner-prefixed insert passes, a foreign / bare private insert is refused, and
``MemoryManifestService`` (record_create / copy-forward / update / delete)
passes the CHECK while the PRE-LC-9 shape (a bare private key) is refused.

RT-13 = ``TestMigration034OnPostgres``; RT-13b = ``TestScanPipelineWritersUnderTheCheck``.

Neuters (each alone): drop the ``ADD CONSTRAINT`` from ``upgrade()`` ->
every ``TestMigration034OnPostgres`` refusal test + RT-13b's legacy leg RED;
swap ``substr(...) = ... || '/'`` for ``LIKE`` -> ``test_underscore_is_not_a_wildcard``
RED; drop ``NOT VALID`` -> ``test_legacy_row_survives_the_upgrade`` RED;
drop ``OR deleted_at_ms IS NOT NULL`` -> ``test_legacy_row_can_be_tombstoned``
RED; ``insert_pending_scan`` gaining ``tier="private"`` -> RT-13b RED.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
import warnings
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from audittrace.db.models import Base, MemoryItem
from audittrace.identity import UserContext
from audittrace.routes.memory_upload.manifest import insert_pending_scan
from audittrace.services.memory_manifest import MemoryManifestService

_REPO = Path(__file__).resolve().parent.parent
_MIGRATIONS = _REPO / "src" / "audittrace" / "migrations"
_MIGRATION_FILE = (
    _MIGRATIONS / "versions" / "034_memory_items_private_key_owner_check.py"
)
_REVISION = "c76f1b005b75"
_DOWN_REVISION = "2c013428184a"
_CONSTRAINT = "ck_memory_items_private_key_owner"


def _text() -> str:
    return _MIGRATION_FILE.read_text(encoding="utf-8")


def _script() -> ScriptDirectory:
    cfg = Config(str(_REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(_MIGRATIONS))
    return ScriptDirectory.from_config(cfg)


class TestMigration034Static:
    def test_file_exists(self) -> None:
        assert _MIGRATION_FILE.is_file()

    def test_revision_identity(self) -> None:
        rev = _script().get_revision(_REVISION)
        assert rev is not None
        assert rev.down_revision == _DOWN_REVISION

    def test_revision_id_is_the_documented_sha256_prefix(self) -> None:
        import hashlib

        seed = "lc9 migration 034 private key owner check"
        assert hashlib.sha256(seed.encode()).hexdigest()[:12] == _REVISION

    def test_single_alembic_head_and_it_is_034(self) -> None:
        """The single-head guard (a forked chain is a silent split-brain
        schema), with 034 an ANCESTOR of the head. LC-10a (039,
        ``5b4bd766374f``) is the head after this; console WU-A (035) rebases
        its ``down_revision`` onto 039's id when it lands second. The
        identity of the head is pinned by the LC-10a migration's own test
        (``tests/test_lc10a_migration_039_static.py``).

        Neuters: a second head -> RED; 034 not in the chain -> RED."""
        script = _script()
        heads = script.get_heads()
        assert len(heads) == 1, f"alembic must have exactly one head, got {heads}"
        ancestors = {r.revision for r in script.walk_revisions("base", heads[0])}
        assert _REVISION in ancestors

    def test_exact_substring_comparison_not_like(self) -> None:
        text_ = _text()
        assert "substr(key, 1, length(created_by_user_id) + 1)" in text_
        assert "= created_by_user_id || '/'" in text_
        assert " LIKE " not in text_.split('"""', 2)[2]  # `_` is a LIKE wildcard

    def test_not_valid_and_tombstone_exempt(self) -> None:
        text_ = _text()
        assert "NOT VALID" in text_
        assert "deleted_at_ms IS NOT NULL" in text_
        assert "tier <> 'private'" in text_

    def test_postgres_guarded_so_sqlite_is_a_noop(self) -> None:
        assert "_is_postgres" in _text()

    def test_downgrade_drops_the_constraint(self) -> None:
        assert "DROP CONSTRAINT IF EXISTS {_CONSTRAINT}" in _text()

    def test_model_does_not_carry_the_check(self) -> None:
        """Like 016: unit tests build the schema with ``create_all`` and must
        still be able to seed legacy rows."""
        table = Base.metadata.tables["memory_items"]
        assert _CONSTRAINT not in {c.name for c in table.constraints}


# ───────────────────────── throwaway postgres scaffolding ────────────────────


def _resolve_admin_url() -> str | None:
    env_url = os.environ.get("AUDITTRACE_TEST_POSTGRES_URL")
    if env_url:
        return env_url
    from tests._pg_ephemeral import start_ephemeral_postgres

    return start_ephemeral_postgres(
        "audittrace-lc9-m034-pg-",
        "lc9_m034_ephemeral_pw",  # noqa: S106 - throwaway container credential
        "audittrace_lc9_m034",
    )


_ADMIN_URL = _resolve_admin_url()
_SKIP_REASON = (
    "LC-9 migration 034 REAL-Postgres proof SKIPPED: Docker and "
    "AUDITTRACE_TEST_POSTGRES_URL are both unavailable. This proof is "
    "REQUIRED before the CHECK can be trusted."
)
if _ADMIN_URL is None:
    warnings.warn(_SKIP_REASON, UserWarning, stacklevel=1)

pg = pytest.mark.skipif(_ADMIN_URL is None, reason=_SKIP_REASON)


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("_lc9_m034", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Schema:
    """A fresh schema holding ``memory_items`` built from the model."""

    def __init__(self, admin_url: str) -> None:
        self.name = f"lc9_{uuid.uuid4().hex[:10]}"
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA "{self.name}"'))
        admin.dispose()
        self.url = admin_url
        self.engine = create_engine(
            admin_url,
            connect_args={"options": f"-csearch_path={self.name}"},
        )
        Base.metadata.tables["memory_items"].create(self.engine)
        base = admin_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        self.async_engine = create_async_engine(
            base, connect_args={"server_settings": {"search_path": self.name}}
        )

    def migrate(self, direction: str) -> None:
        module = _load_migration()
        with self.engine.begin() as conn:
            module.op = Operations(MigrationContext.configure(conn))
            getattr(module, direction)()

    def insert(self, **kw: Any) -> None:
        cols = {
            "id": str(uuid.uuid4()),
            "layer": "episodic",
            "title": None,
            "size_bytes": 1,
            "created_at_ms": 1,
            "modified_at_ms": 1,
            "deleted_at_ms": None,
            "deleted_by_user_id": None,
            "tier": "private",
        }
        cols.update(kw)
        cols.setdefault("modified_by_user_id", cols["created_by_user_id"])
        names = ", ".join(cols)
        marks = ", ".join(f":{k}" for k in cols)
        with self.engine.begin() as conn:
            conn.execute(
                text(f"INSERT INTO memory_items ({names}) VALUES ({marks})"), cols
            )

    def update(self, key: str, sets: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(f"UPDATE memory_items SET {sets} WHERE key = :k"), {"k": key}
            )

    def row(self, key: str) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT * FROM memory_items WHERE key = :k"), {"k": key}
            ).first()

    def constraint_state(self) -> tuple[bool, bool] | None:
        """(exists, validated) for the 034 constraint, from pg_catalog."""
        with self.engine.connect() as conn:
            r = conn.execute(
                text(
                    "SELECT convalidated FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace "
                    "WHERE c.conname = :c AND n.nspname = :s"
                ),
                {"c": _CONSTRAINT, "s": self.name},
            ).first()
        return None if r is None else (True, bool(r[0]))

    def drop(self) -> None:
        self.engine.dispose()
        admin = create_engine(self.url, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'DROP SCHEMA "{self.name}" CASCADE'))
        admin.dispose()


@pytest.fixture
def schema():
    assert _ADMIN_URL is not None
    s = Schema(_ADMIN_URL)
    try:
        yield s
    finally:
        s.drop()


@pg
class TestMigration034OnPostgres:
    """RT-13."""

    def test_upgrade_adds_a_not_validated_constraint_and_downgrade_removes_it(
        self, schema: Schema
    ) -> None:
        assert schema.constraint_state() is None
        schema.migrate("upgrade")
        assert schema.constraint_state() == (True, False)  # exists, NOT VALID
        schema.migrate("downgrade")
        assert schema.constraint_state() is None
        schema.migrate("upgrade")  # re-appliable after a rollback
        assert schema.constraint_state() == (True, False)

    def test_legacy_row_survives_the_upgrade(self, schema: Schema) -> None:
        schema.insert(key="legacy.md", created_by_user_id="user-a")
        schema.migrate("upgrade")  # would raise if the CHECK were validated
        assert schema.row("legacy.md") is not None

    def test_legacy_row_can_be_tombstoned(self, schema: Schema) -> None:
        schema.insert(key="legacy.md", created_by_user_id="user-a")
        schema.migrate("upgrade")
        schema.update("legacy.md", "deleted_at_ms = 5, deleted_by_user_id = 'user-a'")
        assert schema.row("legacy.md").deleted_at_ms == 5

    def test_legacy_row_cannot_be_revived_or_edited_in_place(
        self, schema: Schema
    ) -> None:
        schema.insert(key="live.md", created_by_user_id="user-a")
        schema.insert(key="dead.md", created_by_user_id="user-a", deleted_at_ms=3)
        schema.migrate("upgrade")
        with pytest.raises(IntegrityError):
            schema.update("live.md", "title = 'edited'")
        with pytest.raises(IntegrityError):
            schema.update("dead.md", "deleted_at_ms = NULL")  # revival

    def test_owner_prefixed_insert_passes(self, schema: Schema) -> None:
        schema.migrate("upgrade")
        schema.insert(key="user-a/note.md", created_by_user_id="user-a")
        assert schema.row("user-a/note.md") is not None

    def test_foreign_prefixed_insert_is_refused(self, schema: Schema) -> None:
        schema.migrate("upgrade")
        with pytest.raises(IntegrityError):
            schema.insert(key="user-b/note.md", created_by_user_id="user-a")

    def test_bare_private_insert_is_refused(self, schema: Schema) -> None:
        schema.migrate("upgrade")
        with pytest.raises(IntegrityError):
            schema.insert(key="note.md", created_by_user_id="user-a")

    def test_prefix_must_end_at_the_slash(self, schema: Schema) -> None:
        schema.migrate("upgrade")
        with pytest.raises(IntegrityError):
            schema.insert(key="user-a_x/note.md", created_by_user_id="user-a")

    def test_underscore_is_not_a_wildcard(self, schema: Schema) -> None:
        """``user_a`` as a LIKE pattern would match ``userXa/...``; an
        equality does not."""
        schema.migrate("upgrade")
        with pytest.raises(IntegrityError):
            schema.insert(key="userXa/note.md", created_by_user_id="user_a")
        schema.insert(key="user_a/note.md", created_by_user_id="user_a")

    def test_corpus_rows_are_untouched(self, schema: Schema) -> None:
        schema.migrate("upgrade")
        schema.insert(
            key="decisions/shared.md", created_by_user_id="curator", tier="corpus"
        )
        schema.update("decisions/shared.md", "title = 'edited'")

    def test_tombstoned_bare_private_insert_passes(self, schema: Schema) -> None:
        schema.migrate("upgrade")
        schema.insert(key="x.md", created_by_user_id="user-a", deleted_at_ms=9)

    def test_bare_private_insert_passes_after_downgrade(self, schema: Schema) -> None:
        schema.migrate("upgrade")
        schema.migrate("downgrade")
        schema.insert(key="note.md", created_by_user_id="user-a")


def _user(sub: str) -> UserContext:
    return UserContext(
        user_id=sub, username=sub, agent_type="test", scopes=(), is_admin=False
    )


@pg
class TestServiceAgainstTheLiveCheck:
    """The shipped service passes the CHECK on every private write it makes,
    and the PRE-LC-9 shape (a bare private key) is what the CHECK refuses."""

    async def _svc(self, schema: Schema) -> MemoryManifestService:
        return MemoryManifestService(async_sessionmaker(schema.async_engine))

    async def test_create_update_delete_pass_the_check(self, schema: Schema) -> None:
        schema.migrate("upgrade")
        svc = await self._svc(schema)
        created = await svc.record_create("episodic", "n.md", "t", 1, "user-a")
        assert created.storage_key == "user-a/n.md" and created.key == "n.md"
        upd = await svc.record_update("episodic", "n.md", 2, "user-a", title="u")
        assert upd.title == "u"
        gone = await svc.record_delete("episodic", "n.md", "user-a")
        assert gone.deleted_at_ms is not None
        revived = await svc.record_create("episodic", "n.md", "r", 3, "user-a")
        assert revived.deleted_at_ms is None and revived.id == created.id

    async def test_two_users_same_logical_key_on_postgres(self, schema: Schema) -> None:
        schema.migrate("upgrade")
        svc = await self._svc(schema)
        a = await svc.record_create("semantic", "decisions/d.md", "A", 1, "user-a")
        b = await svc.record_create("semantic", "decisions/d.md", "B", 1, "user-b")
        assert a.id != b.id
        assert (await svc.get("semantic", "user-a/decisions/d.md")).title == "A"

    async def test_copy_forward_from_a_pre_034_legacy_row(self, schema: Schema) -> None:
        """RT-17 on Postgres: the legacy row pre-dates the CHECK, the owner's
        write copy-forwards it, the tombstone UPDATE passes the CHECK."""
        schema.insert(
            key="decisions/k5.md",
            layer="semantic",
            created_by_user_id="user-a",
            created_at_ms=1000,
        )
        schema.migrate("upgrade")
        svc = await self._svc(schema)
        new = await svc.record_create("semantic", "decisions/k5.md", "n", 1, "user-a")
        assert new.created_at_ms == 1000
        assert new.storage_key == "user-a/decisions/k5.md"
        legacy = await svc.get("semantic", "decisions/k5.md")
        assert legacy is not None and legacy.deleted_by_user_id == "user-a"

    async def test_b_over_a_legacy_on_postgres(self, schema: Schema) -> None:
        schema.insert(
            key="decisions/k1.md", layer="semantic", created_by_user_id="user-a"
        )
        schema.migrate("upgrade")
        svc = await self._svc(schema)
        b = await svc.record_create("semantic", "decisions/k1.md", "B", 1, "user-b")
        assert b.storage_key == "user-b/decisions/k1.md"
        legacy = await svc.get("semantic", "decisions/k1.md")
        assert legacy is not None and legacy.deleted_at_ms is None

    async def test_curator_corpus_over_legacy_private_is_refused_by_the_resolver(
        self, schema: Schema
    ) -> None:
        from audittrace.services.memory_keys import CrossOwnerWriteError

        schema.insert(
            key="decisions/k1.md", layer="semantic", created_by_user_id="user-a"
        )
        schema.migrate("upgrade")
        svc = await self._svc(schema)
        with pytest.raises(CrossOwnerWriteError):
            await svc.record_create(
                "semantic",
                "decisions/k1.md",
                None,
                1,
                "curator",
                tier="corpus",
                caller_can_write_shared=True,
            )

    async def test_the_pre_lc9_shape_is_what_the_check_refuses(
        self, schema: Schema
    ) -> None:
        """Gate S3: a PRE-LC-9 image writing a bare private key fails under the
        CHECK -- hence one PR, and downgrade BEFORE the image on rollback."""
        schema.migrate("upgrade")
        async with async_sessionmaker(schema.async_engine)() as session:
            session.add(
                MemoryItem(
                    layer="episodic",
                    key="bare.md",
                    size_bytes=1,
                    created_at_ms=1,
                    modified_at_ms=1,
                    created_by_user_id="user-a",
                    modified_by_user_id="user-a",
                    tier="private",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()


@pg
class TestScanPipelineWritersUnderTheCheck:
    """RT-13b (ADDENDUM-1 S3): the four ``update(MemoryItem)`` writers key on
    ``id == scan_id``. A pending-scan row (``insert_pending_scan``, tier
    corpus) passes all four UPDATE shapes under the CHECK; a seeded legacy
    PRIVATE row fails all four. The statements below are the writers' own
    ``update(MemoryItem)`` value-sets (index_worker ``_mark_indexed`` /
    ``_record_failure``, scan_request_publisher ``_publish_one``,
    scan_verdict_consumer ``_apply_verdict``)."""

    _WRITERS = (
        "indexed_at_ms = 7",
        "index_attempts = 1, index_failed_at_ms = 7, index_failure_code = 'x'",
        "published_at_ms = 7",
        "scan_status = 'clean', indexed_at_ms = NULL",
    )

    async def test_insert_pending_scan_row_is_corpus_and_passes_all_four(
        self, schema: Schema
    ) -> None:
        schema.migrate("upgrade")
        scan_id = str(uuid.uuid4())
        async with async_sessionmaker(schema.async_engine)() as session:
            row = await insert_pending_scan(
                session,
                scan_id=scan_id,
                user_id="user-a",
                object_uri="s3://quarantine/abc",
                object_sha256="0" * 64,
                size_bytes=1,
                title=None,
                trace_id="t",
            )
            assert row.tier == "corpus"
        for sets in self._WRITERS:
            with schema.engine.begin() as conn:
                conn.execute(
                    text(f"UPDATE memory_items SET {sets} WHERE id = :i"),
                    {"i": scan_id},
                )

    async def test_legacy_private_row_fails_all_four(self, schema: Schema) -> None:
        schema.insert(key="legacy.md", created_by_user_id="user-a", id="legacy-id")
        schema.migrate("upgrade")
        for sets in self._WRITERS:
            with pytest.raises(IntegrityError), schema.engine.begin() as conn:
                conn.execute(
                    text(f"UPDATE memory_items SET {sets} WHERE id = 'legacy-id'")
                )
