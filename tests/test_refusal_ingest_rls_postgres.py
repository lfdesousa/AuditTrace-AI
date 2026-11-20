"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-3 (gate S4): the refused-turn row on REAL
Postgres, through the real route, under the application role and FORCE RLS.

The suite's aiosqlite factory enforces no Row-Level Security and the RLS
listener skips it, so on SQLite the route is green whether or not it binds the
RLS identity. Here the database is the control:

(a) the insert succeeds, as the NOBYPASSRLS application role, with the RLS
    identity bound to the VERIFIED subject of the token;
(b) the owner reads exactly that row; a second user reads none;
(c) without the binding the database itself refuses the insert (migration
    005's ``WITH CHECK``): the route fails and no row exists.

Neuters (each alone): P1 remove ``set_current_user_id`` from the route ->
``test_the_row_is_written_as_the_application_role`` and ``test_the_owner_reads_
it_and_nobody_else_does``; P2 bind the writer's identity instead of the
subject's -> the same two; P3 make the policy permissive (DDL in the test
itself) is what ``test_without_the_binding_the_database_refuses`` proves from
the other side.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from audittrace import config, dependencies
from audittrace.db.rls import install_rls_listener, set_current_user_id
from audittrace.server import create_app
from tests.test_acl_ownership_rls import (
    _ADMIN_URL,
    _APP_PASSWORD,
    _APP_ROLE,
    _SKIP_REASON,
    _drop_schema,
    _StaticPostgresFactory,
)
from tests.test_acl_write_path_rls import _build_acl_write_schema
from tests.test_refusal_ingest import (
    AUDIENCE,
    BFF,
    FROM_BFF,
    ISSUER,
    PATH,
    REALM_JWK,
    SESSION,
    SUB,
    _body,
)

pytestmark = pytest.mark.skipif(_ADMIN_URL is None, reason=_SKIP_REASON)

OTHER_USER = "1c1dee5e-15d4-539f-bcae-48c58530d492"


class Harness:
    """The real app wired to a throwaway schema as the application role."""

    def __init__(self, schema: str, factory: Any, client: TestClient) -> None:
        assert _ADMIN_URL is not None
        self.schema = schema
        self.factory = factory
        self.client = client
        self.admin = create_engine(_ADMIN_URL, pool_pre_ping=True)

    def all_rows(self) -> list[dict[str, Any]]:
        """Ground truth through the ADMIN connection (it bypasses RLS)."""
        with self.admin.begin() as conn:
            conn.execute(text(f'SET search_path TO "{self.schema}"'))
            result = conn.execute(
                text(
                    "SELECT user_id, session_id, status, failure_class, source "
                    "FROM interactions ORDER BY id"
                )
            )
            return [dict(row) for row in result.mappings()]

    async def rows_seen_by(self, user_id: str) -> int:
        """What ``user_id`` can read, as the application role under RLS."""
        set_current_user_id(user_id)
        async with self.factory() as session:
            count = await session.execute(text("SELECT count(*) FROM interactions"))
            return int(count.scalar_one())


@pytest_asyncio.fixture
async def h(
    test_container: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Harness]:
    assert _ADMIN_URL is not None
    monkeypatch.setenv("AUDITTRACE_REFUSAL_INGEST_WRITER", BFF)
    monkeypatch.setenv("AUDITTRACE_KEYCLOAK_ISSUER", ISSUER)
    monkeypatch.setenv("AUDITTRACE_KEYCLOAK_JWKS_URL", "http://keycloak.test/jwks")
    monkeypatch.setenv("AUDITTRACE_JWT_AUDIENCE", AUDIENCE)
    config.get_settings.cache_clear()

    async def jwks(_url: str) -> list[Any]:
        return [REALM_JWK]

    monkeypatch.setattr("audittrace.auth._fetch_jwks_keys", jwks)
    monkeypatch.setattr("audittrace.auth._jwks_cache", {})
    monkeypatch.setattr("audittrace.auth._jwks_fetch_lock", None)

    schema = _build_acl_write_schema()
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
    # Fail closed, never skip: the harness must really be the NOBYPASSRLS role.
    async with factory() as session:
        current = (await session.execute(text("SELECT current_user"))).scalar_one()
        bypass = (
            await session.execute(
                text(
                    "SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).scalar_one()
    assert (current, bypass) == (_APP_ROLE, False)
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


def test_the_row_is_written_as_the_application_role(h: Harness) -> None:
    response = h.client.post(PATH, json=_body(), headers=FROM_BFF)

    assert response.status_code == 201
    assert h.all_rows() == [
        {
            "user_id": SUB,
            "session_id": SESSION,
            "status": "failed",
            "failure_class": "token_exchange_refused",
            "source": "librechat-bff",
        }
    ]


@pytest.mark.asyncio
async def test_the_owner_reads_it_and_nobody_else_does(h: Harness) -> None:
    assert h.client.post(PATH, json=_body(), headers=FROM_BFF).status_code == 201

    assert await h.rows_seen_by(SUB) == 1
    assert await h.rows_seen_by(OTHER_USER) == 0


def test_without_the_binding_the_database_refuses(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route's ``set_current_user_id`` is what lets the insert pass
    ``WITH CHECK``. Take it away and Postgres, not the application, says no."""
    monkeypatch.setattr(
        "audittrace.routes.refusals.set_current_user_id", lambda _sub: None
    )
    response = h.client.post(PATH, json=_body(), headers=FROM_BFF)

    assert response.status_code == 500
    assert h.all_rows() == []
