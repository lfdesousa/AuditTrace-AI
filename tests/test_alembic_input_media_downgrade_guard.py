"""LIBRECHAT-IMAGE-SLICE ADDENDUM-1 AC-10 -- the downgrade never destroys an image record.

Gate r1 S5: dropping ``interactions.input_media`` loses the record AND makes every image
row fail ``verify_content_hash`` (its stored hash covers a value that is gone): a false
tamper signal. Ruling: the downgrade refuses while any row records an image, and changes
nothing when it refuses. SQLite here; the PostgreSQL arm (forced row-level security hides
other owners' rows from a plain count) is the builder's PG-lane test, measured in the
addendum by a probe on the pinned engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, inspect, text

from audittrace.integrity import content_hash

# CONSOLE-AUDIT-EVIDENCE: 041 now sits on 040. The guard belongs to 040's downgrade, so each
# test first steps the database down to 040 (SQLite DDL is not transactional: a refusal
# AFTER 041 had been undone would not roll that back) and then takes the single step.
REV_040 = "ae606b421cf9"
MEDIA = '{"count":1,"items":[],"schema":1}'
FIELDS: dict[str, object] = {
    "project": "p",
    "source": "s",
    "question": "q",
    "answer": "a",
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "timestamp": "2026-11-20T00:00:00",
    "status": "success",
}
INSERT = text(
    "INSERT INTO interactions (project, source, question, answer, prompt_tokens, "
    "completion_tokens, timestamp, status, input_media, content_hash) VALUES "
    "(:project, :source, :question, :answer, :prompt_tokens, :completion_tokens, "
    ":timestamp, :status, :input_media, :content_hash)"
)


def _cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", "sqlite://")
    return cfg


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return create_engine(f"sqlite:///{tmp_path / 'guard.db'}", echo=False)


def _migrate(engine: Engine, step: str) -> None:
    cfg = _cfg()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        if step == "head":
            command.upgrade(cfg, "head")
        else:
            command.downgrade(cfg, step)


def _insert(engine: Engine, media: str | None) -> None:
    row = {**FIELDS, "input_media": media}
    with engine.begin() as conn:
        conn.execute(INSERT, {**row, "content_hash": content_hash(row)})


def _state(engine: Engine) -> tuple[set[str], str, list[tuple[object, ...]]]:
    with engine.connect() as conn:
        columns = {c["name"] for c in inspect(conn).get_columns("interactions")}
        version = str(
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        )
        wanted = (
            "input_media, content_hash" if "input_media" in columns else "content_hash"
        )
        rows = [
            tuple(r)
            for r in conn.execute(
                text(f"SELECT {wanted} FROM interactions ORDER BY id")
            )
        ]
    return columns, version, rows


def test_downgrade_refuses_while_a_row_records_an_image(engine: Engine) -> None:
    _migrate(engine, "head")
    _insert(engine, None)
    _insert(engine, MEDIA)
    _migrate(engine, REV_040)
    before = _state(engine)
    assert before[1] == REV_040, "control: the database is at 040"
    head = ScriptDirectory.from_config(_cfg()).get_current_head()
    assert head != REV_040, "control: 040 is no longer the head"
    with pytest.raises(RuntimeError, match=r"input_media.*\b1 row"):
        _migrate(engine, "-1")
    assert _state(engine) == before, "a refused downgrade changes nothing"


def test_downgrade_proceeds_when_no_row_records_an_image(engine: Engine) -> None:
    _migrate(engine, "head")
    _insert(engine, None)
    _migrate(engine, REV_040)
    columns_before, _, _ = _state(engine)
    assert "input_media" in columns_before
    _migrate(engine, "-1")
    columns, _, rows = _state(engine)
    assert columns_before - columns == {"input_media"}
    assert len(rows) == 1, "the row without an image survives"


def test_the_refusal_says_what_would_be_lost(engine: Engine) -> None:
    _migrate(engine, "head")
    _insert(engine, MEDIA)
    _insert(engine, MEDIA)
    _migrate(engine, REV_040)
    with pytest.raises(RuntimeError) as caught:
        _migrate(engine, "-1")
    message = str(caught.value)
    assert "2 rows" in message
    assert "content_hash" in message, "names the verification consequence"
