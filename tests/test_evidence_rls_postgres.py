"""CONSOLE-AUDIT-EVIDENCE WU-A (G7, PG half): the evidence routes on REAL Postgres, as the
NOBYPASSRLS application role under FORCE row-level security.

The suite's aiosqlite factory enforces no RLS, so ``test_evidence_routes.py`` can only
prove the service-layer ``user_id`` filter. Here the database is the control:

(a) the owner reads the evidence of its own message and the integrity verdict is
    ``match`` (the hash was computed with ``audittrace.integrity`` and stored by a
    bypass connection, then read back through the policy);
(b) another user asking for the same message id and a nonexistent id get the identical
    ``(status, body)`` 404;
(c) with the service-layer filter out of the picture, the DATABASE alone hides the row:
    the same ``SELECT`` the route issues, as the second user, returns nothing.

Neuters (each alone): drop ``interactions``' policy (DDL) -> (c) RED; answer 403 for a
foreign row -> (b) RED; drop the ``user_id`` conjunct AND the policy -> (b) RED.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from audittrace import config, dependencies
from audittrace.db.models import InteractionRecord
from audittrace.db.rls import install_rls_listener, set_current_user_id
from audittrace.integrity import content_hash
from audittrace.server import create_app
from tests._evidence_rig import get
from tests.test_acl_ownership_rls import (
    _ADMIN_URL,
    _APP_PASSWORD,
    _APP_ROLE,
    _SKIP_REASON,
    _drop_schema,
    _run_migration_upgrade,
    _StaticPostgresFactory,
)
from tests.test_acl_write_path_rls import _build_acl_write_schema

pytestmark = pytest.mark.skipif(_ADMIN_URL is None, reason=_SKIP_REASON)

ALICE = "0b0cdd4d-04c3-428f-ab9d-37b47429c381"
BOB = "1c1dee5e-15d4-539f-bcae-48c58530d492"
MID = "00000000-0000-4000-8000-0000000000aa"


def _fields(user_id: str) -> dict[str, Any]:
    return {
        "project": "default",
        "source": "librechat",
        "question": "q",
        "answer": "a",
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "timestamp": "2026-01-01T00:00:00",
        "session_id": "s-1",
        "model": "m",
        "user_id": user_id,
        "status": "success",
        "failure_class": None,
        "error_detail": None,
        "duration_ms": 3,
        "trace_id": "0" * 31 + "1",
        "event_class": None,
    }


class Harness:
    def __init__(self, schema: str, factory: Any, client: TestClient) -> None:
        assert _ADMIN_URL is not None
        self.schema = schema
        self.factory = factory
        self.client = client
        self.admin = create_engine(_ADMIN_URL, pool_pre_ping=True)

    def seed_as_admin(self, user_id: str, message_id: str) -> None:
        fields = _fields(user_id)
        with self.admin.begin() as conn:
            conn.execute(text(f'SET search_path TO "{self.schema}"'))
            conn.execute(
                text(
                    "INSERT INTO interactions (project, source, question, answer, "
                    "prompt_tokens, completion_tokens, timestamp, session_id, model, "
                    "user_id, status, duration_ms, trace_id, content_hash, "
                    "client_message_id) VALUES (:project, :source, :question, :answer, "
                    ":prompt_tokens, :completion_tokens, :timestamp, :session_id, "
                    ":model, :user_id, :status, :duration_ms, :trace_id, :h, :mid)"
                ),
                {
                    **{
                        k: v
                        for k, v in fields.items()
                        if k not in ("failure_class", "error_detail", "event_class")
                    },
                    "h": content_hash(fields),
                    "mid": message_id,
                },
            )

    async def visible_to(self, user_id: str, message_id: str) -> int:
        """The route's own predicate, as ``user_id`` and WITHOUT the service filter."""
        set_current_user_id(user_id)
        async with self.factory() as session:
            rows = (
                (
                    await session.execute(
                        select(InteractionRecord).where(
                            InteractionRecord.client_message_id == message_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        return len(rows)


@pytest_asyncio.fixture
async def h(test_container: Any) -> AsyncIterator[Harness]:
    assert _ADMIN_URL is not None
    config.get_settings.cache_clear()
    schema = _build_acl_write_schema()
    # the shared builder stops before 021: the ORM ``tool_calls`` row carries the
    # broker-provenance columns the evidence route selects
    boot = create_engine(_ADMIN_URL, pool_pre_ping=True)
    with boot.begin() as conn:
        conn.execute(text(f'SET search_path TO "{schema}"'))
        _run_migration_upgrade(conn, "021_add_broker_provenance_to_tool_calls.py")
    boot.dispose()
    admin_url = _ADMIN_URL.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    app_url = f"postgresql+asyncpg://{_APP_ROLE}:{_APP_PASSWORD}{admin_url[admin_url.index('@') :]}"
    engine = create_async_engine(
        app_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    install_rls_listener()
    dependencies.container = test_container
    dependencies.container._instances["postgres_factory"] = _StaticPostgresFactory(
        factory
    )
    async with factory() as session:
        current, bypass = (
            await session.execute(
                text(
                    "SELECT current_user, rolbypassrls OR rolsuper FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
        ).one()
    assert (current, bypass) == (_APP_ROLE, False)  # fail closed, never skip
    harness = Harness(
        schema, factory, TestClient(create_app(), raise_server_exceptions=False)
    )
    try:
        yield harness
    finally:
        harness.client.close()
        harness.admin.dispose()
        await engine.dispose()
        _drop_schema(schema)
        config.get_settings.cache_clear()
        set_current_user_id(None)


def test_the_owner_reads_the_evidence_and_it_verifies(h: Harness) -> None:
    h.seed_as_admin(ALICE, MID)
    r = get(h.client, f"/evidence/messages/{MID}", ALICE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["interaction"]["client_message_id"] == MID
    assert body["interaction"]["user_id"] == ALICE
    assert body["integrity"]["recomputed"] == "match"
    assert body["lookup"]["candidates"] == 1


def test_another_users_message_equals_a_nonexistent_one(h: Harness) -> None:
    h.seed_as_admin(ALICE, MID)
    theirs = get(h.client, f"/evidence/messages/{MID}", BOB)
    nothing = get(h.client, "/evidence/messages/no-such-message", BOB)
    assert (theirs.status_code, theirs.text) == (nothing.status_code, nothing.text)
    assert theirs.status_code == 404
    by_id = get(h.client, "/evidence/interactions/1", BOB)
    gone = get(h.client, "/evidence/interactions/424242", BOB)
    assert (by_id.status_code, by_id.text) == (gone.status_code, gone.text)
    assert by_id.status_code == 404


@pytest.mark.asyncio
async def test_the_database_alone_hides_the_row(h: Harness) -> None:
    h.seed_as_admin(ALICE, MID)
    assert await h.visible_to(ALICE, MID) == 1
    assert await h.visible_to(BOB, MID) == 0


@pytest.mark.parametrize(
    "bad", ["2147483648", "99999999999999999999", "abc", "1.5", "0", "-1"]
)
def test_a_malformed_interaction_id_is_the_same_404_on_postgres(
    h: Harness, bad: str
) -> None:
    """F1: no driver error (500) for an id beyond the 32-bit key, no 422."""
    h.seed_as_admin(ALICE, MID)
    nothing = get(h.client, "/evidence/interactions/424242", BOB)
    theirs = get(h.client, "/evidence/interactions/1", BOB)
    r = get(h.client, f"/evidence/interactions/{bad}", BOB)
    assert (r.status_code, r.text) == (nothing.status_code, nothing.text)
    assert (r.status_code, r.text) == (theirs.status_code, theirs.text)
    assert r.status_code == 404


@pytest.mark.parametrize("bad", ["2147483648", "99999999999999999999"])
def test_tool_calls_out_of_range_id_is_not_a_500_on_postgres(
    h: Harness, bad: str
) -> None:
    h.seed_as_admin(ALICE, MID)
    r = get(h.client, f"/interactions/{bad}/tool-calls", ALICE)
    assert r.status_code == 200
    assert r.json()["tool_calls"] == [] and r.json()["total"] == 0
