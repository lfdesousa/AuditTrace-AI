"""LIBRECHAT-IMAGE-SLICE ADDENDUM-2 AC-10 (gate r2 S5) -- the downgrade guard on REAL Postgres.

The guard of migration 040 lifts FORCE ROW LEVEL SECURITY inside the migration's transaction
so that its count sees every owner's rows. Three properties make that safe, and the SQLite
guard test can show none of them:

(a) the count is right UNDER FORCE with other users' rows (a plain count sees none of them);
(b) FORCE is on again after a REFUSED downgrade and after a SUCCESSFUL one;
(c) a role that does not own the table fails closed: it raises, and nothing changes.

Throwaway ``postgres:18.3`` (or ``AUDITTRACE_TEST_POSTGRES_URL``); the migrations are run
through alembic by a role that is NOT a superuser and owns the tables, as a deployment's
migration role does. (c) is decided by the SERVER (only an owner may ALTER the table) and by
transactional DDL; no edit of the migration makes it pass or fail, so it has no code neuter:
it pins the operating assumption the guard relies on.
"""

from __future__ import annotations

import contextlib
import os
import uuid
import warnings
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

MEDIA = '{"count":1,"items":[],"schema":1}'
IMAGE_ROWS = "SELECT count(*) FROM interactions WHERE input_media IS NOT NULL"
FORCED = "SELECT relforcerowsecurity FROM pg_class WHERE oid = 'interactions'::regclass"
HAS_COLUMN = (
    "SELECT count(*) FROM information_schema.columns "
    "WHERE table_name = 'interactions' AND column_name = 'input_media'"
)
INSERT = text(
    "INSERT INTO interactions (project, source, question, answer, prompt_tokens, "
    "completion_tokens, timestamp, status, user_id, input_media) VALUES "
    "('p', 's', 'q', 'a', 0, 0, '2026-11-20T00:00:00', 'success', :user_id, :media)"
)


def _resolve_admin_url() -> str | None:
    env_url = os.environ.get("AUDITTRACE_TEST_POSTGRES_URL")
    if env_url:
        return env_url
    from tests._pg_ephemeral import start_ephemeral_postgres

    return start_ephemeral_postgres(
        "audittrace-image-slice-m040-pg-",
        "image_slice_m040_ephemeral_pw",  # noqa: S106 - throwaway container credential
        "postgres",
    )


_ADMIN_URL = _resolve_admin_url()
_SKIP_REASON = (
    "migration 040 downgrade-guard REAL-Postgres proof SKIPPED: Docker and "
    "AUDITTRACE_TEST_POSTGRES_URL are both unavailable. This proof is REQUIRED "
    "before the downgrade guard can be trusted."
)
if _ADMIN_URL is None:
    warnings.warn(_SKIP_REASON, UserWarning, stacklevel=1)

pytestmark = pytest.mark.skipif(_ADMIN_URL is None, reason=_SKIP_REASON)


class Lane:
    """One database, migrated to head by a non-superuser owner; a second, non-owner role."""

    def __init__(self, admin_url: str, stack: contextlib.ExitStack) -> None:
        tag = uuid.uuid4().hex[:10]
        self.db, self.owner, self.other = f"m040_{tag}", f"owner_{tag}", f"other_{tag}"
        secret = uuid.uuid4().hex  # throwaway role password, never leaves this process
        server = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        stack.callback(server.dispose)
        with server.connect() as conn:
            for role in (self.owner, self.other):
                conn.execute(
                    text(
                        f'CREATE ROLE "{role}" LOGIN NOSUPERUSER NOBYPASSRLS '
                        f"PASSWORD '{secret}'"
                    )
                )
            conn.execute(text(f'CREATE DATABASE "{self.db}" OWNER "{self.owner}"'))
        base = make_url(admin_url).set(database=self.db)
        self.admin = create_engine(base, isolation_level="AUTOCOMMIT")
        self.as_owner = create_engine(base.set(username=self.owner, password=secret))
        self.as_other = create_engine(base.set(username=self.other, password=secret))
        for engine in (self.admin, self.as_owner, self.as_other):
            stack.callback(engine.dispose)
        self.migrate(self.as_owner, "upgrade", "head")
        self.head = self.scalar("SELECT version_num FROM alembic_version")
        with self.admin.connect() as conn:
            conn.execute(
                text(f'GRANT ALL ON ALL TABLES IN SCHEMA public TO "{self.other}"')
            )

    def migrate(self, engine: Engine, direction: str, step: str) -> None:
        cfg = Config("alembic.ini")
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            getattr(command, direction)(cfg, step)

    def scalar(self, sql: str, engine: Engine | None = None) -> Any:
        with (engine or self.admin).connect() as conn:
            return conn.execute(text(sql)).scalar_one()

    def write_rows(self, image_rows: int, plain_rows: int = 1) -> None:
        """Rows of ANOTHER user, written the way the service does (identity bound)."""
        with self.as_owner.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_user_id', 'u1', true)"))
            for index in range(image_rows + plain_rows):
                media = MEDIA if index < image_rows else None
                conn.execute(INSERT, {"user_id": "u1", "media": media})

    def state(self) -> dict[str, Any]:
        return {
            "column": self.scalar(HAS_COLUMN) == 1,
            "forced": self.scalar(FORCED),
            "at_head": self.scalar("SELECT version_num FROM alembic_version")
            == self.head,
            "rows": self.scalar("SELECT count(*) FROM interactions"),
        }


@pytest.fixture
def lane() -> Iterator[Lane]:
    assert _ADMIN_URL is not None
    with contextlib.ExitStack() as stack:
        yield Lane(_ADMIN_URL, stack)


class TestCountUnderForcedRowSecurity:
    def test_control_the_migration_role_is_an_owner_without_bypass(
        self, lane: Lane
    ) -> None:
        role = (
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        owns = (
            "SELECT pg_get_userbyid(relowner) = current_user FROM pg_class "
            "WHERE oid = 'interactions'::regclass"
        )
        assert lane.scalar(role, lane.as_owner) is False
        assert lane.scalar(owns, lane.as_owner) is True
        assert lane.scalar(FORCED) is True
        print("server_version:", lane.scalar("SHOW server_version"))

    def test_a_plain_count_is_blind_and_the_guard_is_not(self, lane: Lane) -> None:
        lane.write_rows(image_rows=2)
        assert lane.scalar(IMAGE_ROWS) == 2, "control: the rows exist"
        assert lane.scalar(IMAGE_ROWS, lane.as_owner) == 0, "why a plain count fails"
        with pytest.raises(RuntimeError, match="2 rows record an image"):
            lane.migrate(lane.as_owner, "downgrade", "-1")


class TestForceIsRestored:
    def test_after_a_refused_downgrade_nothing_changed(self, lane: Lane) -> None:
        lane.write_rows(image_rows=2)
        with pytest.raises(RuntimeError, match="refusing to drop"):
            lane.migrate(lane.as_owner, "downgrade", "-1")
        assert lane.state() == {
            "column": True,
            "forced": True,
            "at_head": True,
            "rows": 3,
        }
        assert lane.scalar(IMAGE_ROWS) == 2
        assert lane.scalar(IMAGE_ROWS, lane.as_owner) == 0, "the policy filters again"

    def test_after_a_successful_downgrade_force_is_on_again(self, lane: Lane) -> None:
        lane.write_rows(image_rows=0)
        lane.migrate(lane.as_owner, "downgrade", "-1")
        assert lane.state() == {
            "column": False,
            "forced": True,
            "at_head": False,
            "rows": 1,
        }
        assert lane.scalar("SELECT count(*) FROM interactions", lane.as_owner) == 0


class TestNonOwnerFailsClosed:
    @pytest.mark.parametrize("image_rows", [0, 2])
    def test_a_role_that_does_not_own_the_table_changes_nothing(
        self, lane: Lane, image_rows: int
    ) -> None:
        lane.write_rows(image_rows=image_rows)
        before = lane.state()
        with pytest.raises(ProgrammingError, match="must be owner of table"):
            lane.migrate(lane.as_other, "downgrade", "-1")
        assert lane.state() == before
        assert before["column"] is True and before["forced"] is True
        assert lane.scalar(IMAGE_ROWS) == image_rows
