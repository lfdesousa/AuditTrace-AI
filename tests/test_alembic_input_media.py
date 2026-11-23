"""LIBRECHAT-IMAGE-SLICE AC-2 -- migration: ``interactions.input_media`` (nullable TEXT).

Additive only: one nullable column, no default, no backfill, no index, no policy. The
ORM model and the migrated schema agree.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from audittrace.db.models import InteractionRecord

REV_040 = "ae606b421cf9"


@pytest.fixture
def migrated_columns() -> dict[str, dict[str, object]]:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", "sqlite://")
    engine = create_engine("sqlite://", echo=False)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        columns = {
            c["name"]: dict(c) for c in inspect(conn).get_columns("interactions")
        }
    engine.dispose()
    return columns


def test_the_model_declares_a_nullable_text_column() -> None:
    column = InteractionRecord.__table__.columns.get("input_media")
    assert column is not None
    assert column.nullable is True
    assert column.default is None and column.server_default is None
    assert str(column.type) == "TEXT"


def test_upgrade_head_adds_the_column_and_nothing_else_to_interactions(
    migrated_columns: dict[str, dict[str, object]],
) -> None:
    assert migrated_columns["input_media"]["nullable"] is True
    assert migrated_columns["input_media"]["default"] is None
    assert sorted(migrated_columns) == sorted(
        c.name for c in InteractionRecord.__table__.columns
    )


def test_downgrade_one_step_removes_only_that_column() -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", "sqlite://")
    engine = create_engine("sqlite://", echo=False)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        # CONSOLE-AUDIT-EVIDENCE: 041 now sits on 040; step down to 040 first so the
        # single step under test is still the one that removes ``input_media``.
        command.downgrade(cfg, REV_040)
        before = {c["name"] for c in inspect(conn).get_columns("interactions")}
        command.downgrade(cfg, "-1")
        after = {c["name"] for c in inspect(conn).get_columns("interactions")}
    engine.dispose()
    assert before - after == {"input_media"}
    assert after - before == set()
