"""LC-10a AC-10 (SQLite / static leg): the session_id migration's identity.

No Postgres here (layer U); the Postgres leg lives in
``tests/integration/test_lc10a_session_recall.py``.

Neuters: change ``down_revision`` -> chain tests RED; drop the column or the
index from ``upgrade`` -> the text and SQLite-upgrade tests RED; drop either
from ``downgrade`` -> the downgrade test RED.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

_REPO = Path(__file__).resolve().parent.parent
_MIGRATIONS = _REPO / "src" / "audittrace" / "migrations"
_FILE = _MIGRATIONS / "versions" / "039_session_memory_items_session_id.py"
_REVISION = "5b4bd766374f"
_PARENT = "c76f1b005b75"
_INDEX = "ix_session_memory_items_user_session"


def _script() -> ScriptDirectory:
    cfg = Config(str(_REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(_MIGRATIONS))
    return ScriptDirectory.from_config(cfg)


def test_file_exists() -> None:
    assert _FILE.is_file()


def test_revision_id_is_the_documented_sha256_prefix() -> None:
    seed = "lc10a migration session_memory_items session_id"
    assert hashlib.sha256(seed.encode()).hexdigest()[:12] == _REVISION


def test_single_head_is_039_and_parent_is_034() -> None:
    script = _script()
    assert script.get_heads() == [_REVISION]
    rev = script.get_revision(_REVISION)
    assert rev is not None and rev.down_revision == _PARENT


def test_column_and_index_appear_in_the_file() -> None:
    text = _FILE.read_text(encoding="utf-8")
    assert '"session_id"' in text and "String(length=255)" in text
    assert text.count(_INDEX) >= 2  # created in upgrade, dropped in downgrade
    assert "created_at_ms DESC" in text
    code = text.split('"""', 2)[2]
    assert "_is_postgres" not in code and "POLICY" not in code  # R5: no RLS statement


def _run(direction: str, conn: sa.engine.Connection) -> None:
    ctx = MigrationContext.configure(conn)
    module = _script().get_revision(_REVISION).module
    with Operations.context(ctx):
        getattr(module, direction)()


def test_upgrade_then_downgrade_on_sqlite() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE session_memory_items (id VARCHAR(36) PRIMARY KEY, "
                "user_id VARCHAR(36) NOT NULL, created_at_ms BIGINT NOT NULL)"
            )
        )
        _run("upgrade", conn)
        insp = sa.inspect(conn)
        assert "session_id" in {
            c["name"] for c in insp.get_columns("session_memory_items")
        }
        assert _INDEX in {i["name"] for i in insp.get_indexes("session_memory_items")}
        _run("downgrade", conn)
        insp = sa.inspect(conn)
        assert "session_id" not in {
            c["name"] for c in insp.get_columns("session_memory_items")
        }
        assert _INDEX not in {
            i["name"] for i in insp.get_indexes("session_memory_items")
        }
