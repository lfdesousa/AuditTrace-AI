"""REAL-Postgres RLS proof for ``services/console_store`` + the migrated
Tool-Favorites domain (ADDENDUM A §3: "SQLite does not enforce RLS, so a
SQLite-only isolation proof is vacuous by construction").

Set-up mirrors ``tests/test_rls_isolation.py``: a throwaway ``postgres:16``
container is brought UP at collection and torn DOWN at exit (or
``AUDITTRACE_TEST_POSTGRES_URL`` is used when set — portability: the
target is a parameter, never a hardcoded host). Inside it, every test gets
a fresh schema holding the REAL ``console_tool_favorites`` DDL (from
``audittrace.db.models``) with migration 030's RLS statements verbatim,
and the application connects as a NON-superuser ``NOBYPASSRLS`` role —
superusers always bypass RLS regardless of ``FORCE``, so a superuser
"proof" proves nothing.

What is verified, each with the DB as the witness:

1. Through the migrated ``PostgresConsoleToolFavoritesService`` with the
   RLS ContextVar bound per caller (as ``require_user`` does): bob cannot
   list/remove alice's favorite; a raw ``SELECT`` as the app role sees only
   the GUC-bound user's rows and ZERO rows with no GUC (safe-by-default).
2. With the ContextVar UNBOUND (background-worker shape): the base pushes
   the GUC itself, so the DB layer is still scoped.
3. ``WITH CHECK`` rejects a raw INSERT whose ``user_sub`` is not the GUC —
   the DB-layer backstop behind the base's unconditional stamp.
4. Even an UNSCOPED ``select()`` through the base's own guarded opener
   returns only the caller's rows — the pre-wrapping is at the DB layer.
"""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import sys
import time
import warnings
from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from audittrace.db.models import Base, ConsoleToolFavorite
from audittrace.db.rls import install_rls_listener, set_current_user_id
from audittrace.identity import UserContext
from audittrace.services.console_store import (
    ConsoleStoreScopeError,
    PostgresConsoleStore,
)
from audittrace.services.console_tool_favorites import (
    PostgresConsoleToolFavoritesService,
    ToolFavoritesDomain,
)

# ───────────────────── ephemeral postgres scaffolding ────────────────────

_APP_ROLE = "console_store_app"
_APP_PASSWORD = "console_store_pw"  # noqa: S105 - throwaway container credential


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_ephemeral_postgres() -> str | None:
    if shutil.which("docker") is None:
        return None
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=10)
    except Exception:
        return None
    port = _free_port()
    name = f"audittrace-console-store-pg-{os.getpid()}"
    password = "cs_ephemeral_pw"  # noqa: S105 - throwaway container credential
    db = "audittrace_console_store"
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "-e",
                f"POSTGRES_PASSWORD={password}",
                "-e",
                f"POSTGRES_DB={db}",
                "-p",
                f"{port}:5432",
                "postgres:16",
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except Exception:
        return None
    atexit.register(
        lambda: subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    )
    dsn = f"postgresql+psycopg2://postgres:{password}@127.0.0.1:{port}/{db}"
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            engine = create_engine(dsn, future=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            return dsn
        except Exception:
            time.sleep(0.5)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    return None


def _resolve_admin_url() -> str | None:
    return os.environ.get("AUDITTRACE_TEST_POSTGRES_URL") or _start_ephemeral_postgres()


_ADMIN_URL = _resolve_admin_url()

# A2 (fix round 1): a bare ``skipif`` degrades "4 passed" to "4 skipped" on a
# TARGETED run of this file with no failure and no obviously-loud signal —
# `make test`'s zero-skip gate (`scripts/check-no-skipped-tests.py`) catches
# it on a FULL run, but SQLite does not enforce RLS
# (`feedback_unit_tests_miss_rls`), so a targeted run of just this file is
# the one place this specific proof could silently vanish and look, at a
# glance, indistinguishable from "the proof ran and passed". Make the
# degradation impossible to miss: a ``UserWarning`` (shown in pytest's
# warnings summary on EVERY run, targeted or full, per ``pytest`` filterwarnings
# not silencing it — see ``pyproject.toml``) plus a stderr banner at
# collection time, in addition to the skip itself.
_SKIP_REASON = (
    "REAL-Postgres RLS proof SKIPPED (all 4 tests in this file) — Docker "
    "and AUDITTRACE_TEST_POSTGRES_URL are both unavailable. SQLite does not "
    "enforce RLS: this proof is REQUIRED, not optional, before the "
    "console-store RLS claim can be trusted. Set AUDITTRACE_TEST_POSTGRES_URL "
    "or make Docker available so a throwaway postgres:16 can be started."
)
if _ADMIN_URL is None:
    warnings.warn(_SKIP_REASON, UserWarning, stacklevel=1)
    print(
        f"\n{'=' * 78}\n[RLS-PROOF-SKIPPED] {_SKIP_REASON}\n{'=' * 78}\n",
        file=sys.stderr,
    )

pytestmark = pytest.mark.skipif(_ADMIN_URL is None, reason=_SKIP_REASON)


def _rls_ddl(table: str) -> list[str]:
    """Migration 030's RLS statements, verbatim in shape."""
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"""
        CREATE POLICY tenant_isolation_{table}
            ON {table}
            FOR ALL
            USING (user_sub = current_setting('app.current_user_id', true))
            WITH CHECK (user_sub = current_setting('app.current_user_id', true))
        """,
    ]


@pytest.fixture
def schema() -> Any:
    """Fresh schema with the REAL console_tool_favorites DDL + RLS, plus
    a non-superuser app role that can log in. Dropped on teardown."""
    assert _ADMIN_URL is not None
    admin = create_engine(_ADMIN_URL, pool_pre_ping=True)
    name = f"cs_{datetime.now().strftime('%H%M%S_%f')}"
    table = Base.metadata.tables["console_tool_favorites"]
    with admin.begin() as conn:
        conn.execute(
            text(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                        CREATE ROLE {_APP_ROLE} LOGIN PASSWORD '{_APP_PASSWORD}'
                            NOSUPERUSER NOBYPASSRLS;
                    END IF;
                END$$
                """
            )
        )
        conn.execute(text(f'CREATE SCHEMA "{name}"'))
        conn.execute(text(f'GRANT USAGE ON SCHEMA "{name}" TO {_APP_ROLE}'))
        conn.execute(text(f'SET search_path TO "{name}"'))
        table.create(bind=conn)
        for statement in _rls_ddl("console_tool_favorites"):
            conn.execute(text(statement))
        conn.execute(
            text(
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{name}".console_tool_favorites '
                f"TO {_APP_ROLE}"
            )
        )
    try:
        yield name
    finally:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        admin.dispose()


@pytest_asyncio.fixture
async def app_factory(schema: str) -> Any:
    """asyncpg engine connected AS THE APP ROLE, search_path pinned to the
    throwaway schema."""
    assert _ADMIN_URL is not None
    admin = _ADMIN_URL.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    at = admin.index("@")
    app_url = f"postgresql+asyncpg://{_APP_ROLE}:{_APP_PASSWORD}{admin[at:]}"
    engine = create_async_engine(
        app_url, connect_args={"server_settings": {"search_path": schema}}
    )
    install_rls_listener()
    try:
        yield async_sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def alice(user_context: UserContext) -> UserContext:
    return replace(user_context, user_id="alice-pg-sub", is_admin=False)


@pytest.fixture
def bob(user_context: UserContext) -> UserContext:
    return replace(user_context, user_id="bob-pg-sub", is_admin=False)


@pytest.fixture(autouse=True)
def _clear_context() -> Any:
    set_current_user_id(None)
    yield
    set_current_user_id(None)


async def _raw_count_as_app(factory: Any, guc: str | None) -> int:
    """Raw SELECT as the app role, bypassing the base — the DB is the witness."""
    async with factory() as session:
        if guc is not None:
            await session.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": guc},
            )
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM console_tool_favorites")
                )
            ).scalar_one()
        )


class TestMigratedDomainUnderRealRls:
    async def test_bob_cannot_read_or_remove_alices_favorite(
        self, app_factory: Any, alice: UserContext, bob: UserContext
    ) -> None:
        service = PostgresConsoleToolFavoritesService(session_factory=app_factory)
        set_current_user_id(alice.user_id)
        await service.add_tool_favorite(alice, "tool", "web-search", tenant_id="acme")
        await service.add_tool_favorite(alice, "tool", "calculator")

        set_current_user_id(bob.user_id)
        assert await service.list_tool_favorites(bob) == []
        assert await service.remove_tool_favorite(bob, "tool", "web-search") is False
        assert await _raw_count_as_app(app_factory, bob.user_id) == 0

        set_current_user_id(alice.user_id)
        assert [f["item_id"] for f in await service.list_tool_favorites(alice)] == [
            "calculator",
            "web-search",
        ] or len(await service.list_tool_favorites(alice)) == 2
        assert await _raw_count_as_app(app_factory, alice.user_id) == 2

        set_current_user_id(None)
        assert await _raw_count_as_app(app_factory, None) == 0, (
            "RLS is not safe-by-default: rows visible with no GUC bound"
        )

    async def test_base_scopes_the_db_layer_even_without_the_request_contextvar(
        self, app_factory: Any, alice: UserContext, bob: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            ToolFavoritesDomain(), app_factory
        )
        await store.upsert(alice, {"item_type": "tool", "item_id": "x"}, {})
        assert (await store.list(bob))[0] == []
        assert await store.count(bob) == 0
        assert await store.count(alice) == 1
        assert await _raw_count_as_app(app_factory, None) == 0

    async def test_with_check_rejects_a_forged_user_sub_at_the_db_layer(
        self, app_factory: Any, bob: UserContext
    ) -> None:
        async with app_factory() as session:
            await session.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": bob.user_id},
            )
            with pytest.raises(DBAPIError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO console_tool_favorites "
                        "(id, user_sub, item_type, item_id, created_at_ms, updated_at_ms, metadata) "
                        "VALUES ('forged-id', 'alice-pg-sub', 'tool', 'x', 1, 1, '{}')"
                    )
                )
            await session.rollback()

    async def test_unscoped_select_through_the_guarded_opener_is_still_db_scoped(
        self, app_factory: Any, alice: UserContext, bob: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            ToolFavoritesDomain(), app_factory
        )
        await store.upsert(alice, {"item_type": "tool", "item_id": "secret"}, {})
        async with store._sessions.get_session_scoped(bob) as session:
            rows = (await session.execute(select(ConsoleToolFavorite))).scalars().all()
            assert [r.user_sub for r in rows] == [], (
                "unscoped select leaked alice's row"
            )
        async with store._sessions.get_session_scoped(alice) as session:
            rows = (await session.execute(select(ConsoleToolFavorite))).scalars().all()
            assert [r.user_sub for r in rows] == [alice.user_id]
        set_current_user_id(bob.user_id)
        with pytest.raises(ConsoleStoreScopeError):
            async with store._sessions.get_session_scoped(alice):
                pass
