"""CONSOLE-AUDIT-EVIDENCE WU-A -- migration 041 (G21', G28).

Three nullable columns, one migration, one head:
``interactions.client_message_id``, ``tool_calls.recall_matches``,
``tool_calls.recall_matches_truncated``. Revision id is
``sha256("console-evidence migration 041 client_message_id + recall_matches")[:12]``.

Neuters (each alone): G21' change either literal of the 041 module, or add a second
migration on ``ae606b421cf9`` -> ``test_single_head_and_041_identity`` /
``test_a_second_child_of_the_040_head_is_a_second_head`` RED; G28 write ``false`` beside a
NULL ``recall_matches`` -> ``test_the_flag_is_null_exactly_when_the_list_is_null`` RED.
"""

from __future__ import annotations

import hashlib
import importlib
from datetime import datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from audittrace.db.models import InteractionRecord, ToolCall

MODULE = (
    "audittrace.migrations.versions.041_console_evidence_correlation_and_recall_matches"
)
SEED = "console-evidence migration 041 client_message_id + recall_matches"
REVISION = "12128bc04e08"
DOWN = "ae606b421cf9"


def _cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", "sqlite://")
    return cfg


def test_the_revision_id_is_the_a1_b4_method_over_the_seed() -> None:
    assert hashlib.sha256(SEED.encode()).hexdigest()[:12] == REVISION


def test_single_head_and_041_identity() -> None:
    script = ScriptDirectory.from_config(_cfg())
    assert script.get_heads() == [REVISION]
    module = importlib.import_module(MODULE)
    assert module.revision == REVISION
    assert module.down_revision == DOWN


def test_a_second_child_of_the_040_head_is_a_second_head(tmp_path: Path) -> None:
    """The guard's RED half: a sibling on the 040 head makes two heads, so the
    ``== [REVISION]`` assertion above is a real single-head pin."""
    versions = Path(importlib.import_module(MODULE).__file__).parent
    cfg = _cfg()
    script_dir = tmp_path / "versions"
    script_dir.mkdir()
    for source in versions.glob("*.py"):
        (script_dir / source.name).write_text(source.read_text())
    (script_dir / "099_sibling.py").write_text(
        'revision = "aaaaaaaaaaaa"\n'
        f'down_revision = "{DOWN}"\n'
        "branch_labels = None\ndepends_on = None\n"
        "def upgrade() -> None:\n    pass\n"
        "def downgrade() -> None:\n    pass\n"
    )
    cfg.set_main_option("script_location", str(versions.parent))
    cfg.set_main_option("version_locations", str(script_dir))
    assert sorted(ScriptDirectory.from_config(cfg).get_heads()) == sorted(
        [REVISION, "aaaaaaaaaaaa"]
    )


def _migrated(conn: Any) -> dict[str, dict[str, dict[str, Any]]]:
    cfg = _cfg()
    cfg.attributes["connection"] = conn
    command.upgrade(cfg, "head")
    insp = inspect(conn)
    return {
        table: {c["name"]: dict(c) for c in insp.get_columns(table)}
        for table in ("interactions", "tool_calls")
    }


def test_upgrade_adds_three_nullable_columns_and_the_model_agrees() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        cols = _migrated(conn)
    engine.dispose()
    assert cols["interactions"]["client_message_id"]["nullable"] is True
    assert cols["tool_calls"]["recall_matches"]["nullable"] is True
    flag = cols["tool_calls"]["recall_matches_truncated"]
    assert flag["nullable"] is True
    assert isinstance(flag["type"], sa.Boolean)
    assert sorted(cols["interactions"]) == sorted(
        c.name for c in InteractionRecord.__table__.columns
    )
    assert sorted(cols["tool_calls"]) == sorted(
        c.name for c in ToolCall.__table__.columns
    )
    for column in (
        InteractionRecord.__table__.columns["client_message_id"],
        ToolCall.__table__.columns["recall_matches"],
        ToolCall.__table__.columns["recall_matches_truncated"],
    ):
        assert column.nullable is True
        assert column.default is None and column.server_default is None


def test_downgrade_one_step_drops_exactly_the_three_columns() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        cfg = _cfg()
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        before = {
            t: {c["name"] for c in inspect(conn).get_columns(t)}
            for t in ("interactions", "tool_calls")
        }
        command.downgrade(cfg, "-1")
        after = {
            t: {c["name"] for c in inspect(conn).get_columns(t)}
            for t in ("interactions", "tool_calls")
        }
    engine.dispose()
    assert before["interactions"] - after["interactions"] == {"client_message_id"}
    assert before["tool_calls"] - after["tool_calls"] == {
        "recall_matches",
        "recall_matches_truncated",
    }
    assert after["interactions"] - before["interactions"] == set()
    assert after["tool_calls"] - before["tool_calls"] == set()


def _tool_call_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "tc-1",
        "interaction_id": 1,
        "user_id": "u",
        "agent_type": "a",
        "tool_name": "recall_semantic",
        "args": "{}",
        "started_at": datetime(2026, 1, 1),
        "granted_scope": "memory:semantic:read",
    }
    row.update(over)
    return row


# The G28 pair rule, as the SQL the spec states: (recall_matches IS NULL) ==
# (recall_matches_truncated IS NULL). ``rows_violating_the_pair`` is the oracle; the
# writer under test is the ORM model's own defaults plus the values a writer supplies.
_VIOLATIONS = (
    "SELECT id FROM tool_calls WHERE (recall_matches IS NULL) "
    "!= (recall_matches_truncated IS NULL)"
)


def _store(conn: Any, **over: Any) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO interactions (id, project, source, question, answer, "
            "prompt_tokens, completion_tokens, timestamp, status) VALUES "
            "(1, 'p', 's', 'q', 'a', 0, 0, 't', 'success')"
        )
    )
    conn.execute(ToolCall.__table__.insert().values(**_tool_call_row(**over)))


def test_the_flag_is_null_exactly_when_the_list_is_null() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        _migrated(conn)
        _store(conn)  # a writer that supplies neither: both NULL
        assert conn.execute(sa.text(_VIOLATIONS)).fetchall() == []
        conn.execute(
            ToolCall.__table__.insert().values(
                **_tool_call_row(
                    id="tc-2",
                    recall_matches=[{"id": "1"}],
                    recall_matches_truncated=False,
                )
            )
        )
        assert conn.execute(sa.text(_VIOLATIONS)).fetchall() == []
        # the neuter row: a flag beside a NULL list is a violation the oracle sees
        conn.execute(
            ToolCall.__table__.insert().values(
                **_tool_call_row(id="tc-3", recall_matches_truncated=False)
            )
        )
        assert [r[0] for r in conn.execute(sa.text(_VIOLATIONS)).fetchall()] == ["tc-3"]
    engine.dispose()


def test_a_model_row_written_without_either_column_keeps_both_null() -> None:
    """The model must not default the flag to ``false`` (that would break the pair)."""
    assert ToolCall.__table__.columns["recall_matches_truncated"].default is None
    assert ToolCall.__table__.columns["recall_matches"].default is None
