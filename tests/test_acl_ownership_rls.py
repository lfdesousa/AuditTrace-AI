"""REAL-Postgres RLS proof — ACL WU-2a resource-ownership verification
(``2026-09-18-SPEC-acl-wu2a-resource-ownership-verification.md``).

Set-up mirrors ``tests/test_console_store_rls_postgres.py`` (itself
mirroring ``tests/test_rls_isolation.py``): a throwaway ``postgres:16``
container is brought UP at collection and torn DOWN at exit (or
``AUDITTRACE_TEST_POSTGRES_URL`` is used when set — the target is a
parameter, never a hardcoded host), and every test connects through a
non-superuser, ``NOBYPASSRLS`` LOGIN role — superusers always bypass
RLS regardless of ``FORCE ROW LEVEL SECURITY``, so a superuser "proof"
would prove nothing.

**Two schema variants — ``console_agents``/``console_prompt_groups``
from ``Base.metadata`` (the REAL ORM DDL), and ``console_acl_entries``
built by RUNNING THE ACTUAL migration files (031, and 032 on top of it
for the 'after' variant) via Alembic's own ``Operations`` bound to the
live connection — not a hand-retyped copy of their SQL. See
``_run_migration_upgrade`` below for why: an earlier draft of this file
hand-duplicated migration 032's policy text and a neuter round proved
that copy could drift from the shipped migration without any test here
noticing.**

* ``app_factory_before`` — schema with ONLY migration 031 applied —
  the ORIGINAL single ``FOR ALL`` policy on ``console_acl_entries``
  (owner-derived ``WITH CHECK``, broad owner-OR-principal-OR-public
  ``USING``, no ownership check at all). This is what ships on
  ``main`` before this WU.
* ``app_factory_after`` — schema with 031 THEN 032 applied, in
  sequence — exactly how a real deployment would receive them.

**What is verified, the DB itself as the witness — the §1 escalation
test's before/after evidence, plus §2's H-1/H-2 hypotheses:**

1. ``TestBeforeFix`` — reproduces the vulnerability CURRENT ``main``
   ships: an attacker can INSERT a grant naming a resource they do not
   own (§1's escalation), DELETE any PUBLIC grant on any resource
   (H-1), and take ownership of a PUBLIC grant via UPDATE (H-2). Each
   test's assertion is that the attack SUCCEEDS against
   ``schema_before`` — this is the "before" half of the acceptance
   centrepiece, captured in this run's own output.
2. ``TestAfterFix`` — the SAME three attacks against ``schema_after``
   all FAIL; the SAME legitimate operations (owner inserts a grant on
   their own resource, of either resolved resource_type,
   individually) still SUCCEED; an unmapped ``resource_type`` is
   refused at the DB layer too (fail-closed parity with
   ``services/console_acl/_ownership.py``); the SELECT contract
   (WU-1's read path) is unchanged.
"""

from __future__ import annotations

import atexit
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, insert, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from audittrace.db.models import Base
from audittrace.db.rls import install_rls_listener

# ───────────────────── ephemeral postgres scaffolding ────────────────────
# Duplicated (not shared/imported) from tests/test_console_store_rls_postgres.py
# and tests/test_rls_isolation.py by the same convention those two files
# already use with each other — each RLS-proof file is a hermetic,
# independently-runnable witness.

_APP_ROLE = "acl_wu2a_app"
_APP_PASSWORD = "acl_wu2a_pw"  # noqa: S105 - throwaway container credential


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
    name = f"audittrace-acl-wu2a-pg-{os.getpid()}"
    password = "acl_wu2a_ephemeral_pw"  # noqa: S105 - throwaway container credential
    db = "audittrace_acl_wu2a"
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
    # NOTE (WU-2a housekeeping — filed, not fixed): the spec's
    # ADDENDUM-L flagged that this exact atexit-based cleanup did not
    # fire on a --collect-only run in the sibling files, leaking a
    # container. Registering it here too (same known limitation,
    # shared by all three ephemeral-postgres test files) rather than
    # inventing a different, unproven cleanup mechanism for this file
    # alone. `docker run --rm` still removes the container on a clean
    # `docker stop`/process exit; the gap is specifically the
    # --collect-only path. Follow-up: a shared pytest_sessionfinish
    # hook in conftest.py would close it for all three files at once —
    # out of this WU's scope (§3 "OUT of scope" does not list it, and
    # touching shared conftest behavior for 3 independent RLS-proof
    # files is a bigger blast radius than this WU should take on).
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

_SKIP_REASON = (
    "REAL-Postgres RLS proof SKIPPED (ACL WU-2a resource-ownership tests) — "
    "Docker and AUDITTRACE_TEST_POSTGRES_URL are both unavailable. SQLite "
    "does not enforce RLS: this proof is REQUIRED, not optional, before the "
    "WU-2a ownership-tightening claim can be trusted. Set "
    "AUDITTRACE_TEST_POSTGRES_URL or make Docker available so a throwaway "
    "postgres:16 can be started."
)
if _ADMIN_URL is None:
    warnings.warn(_SKIP_REASON, UserWarning, stacklevel=1)
    print(
        f"\n{'=' * 78}\n[RLS-PROOF-SKIPPED] {_SKIP_REASON}\n{'=' * 78}\n",
        file=sys.stderr,
    )

pytestmark = pytest.mark.skipif(_ADMIN_URL is None, reason=_SKIP_REASON)


# ─────────────────── REAL migration execution (zero drift) ─────────────────
# The console_acl_entries table + RLS policy are built by running the
# ACTUAL migration 031 (and, for the 'after' schema, 032 on top of it)
# via Alembic's own ``Operations`` bound to the live throwaway-schema
# connection — NOT a hand-retyped copy of their SQL. An earlier draft
# of this file duplicated the policy SQL by hand (matching the sibling
# RLS-proof files' established convention); a neuter round caught that
# a hand-typed copy can SILENTLY DRIFT from the shipped migration (see
# this WU's build record, neuter N1) — the DB-level guard was real, but
# THIS FILE'S drift meant breaking migration 032 didn't turn any test
# here red. Running the real files closes that gap: a bug in either
# migration file is now, by construction, a bug in what this file
# exercises.

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "audittrace"
    / "migrations"
    / "versions"
)

_OWNER_ONLY_TABLES = ("console_agents", "console_prompt_groups")


def _owner_only_rls_ddl(table: str) -> list[str]:
    """Migrations 028/025's shape verbatim — owner-only FOR ALL. Only
    console_acl_entries (031/032) is this WU's subject; agents/prompt-
    groups' own RLS shape is simple, unchanging, and already covered by
    their own migration test suites, so a hand-mirrored copy here (same
    convention ``test_console_store_rls_postgres.py`` uses) is low risk
    by comparison."""
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"""
        CREATE POLICY tenant_isolation_{table} ON {table}
            FOR ALL
            USING (user_sub = current_setting('app.current_user_id', true))
            WITH CHECK (user_sub = current_setting('app.current_user_id', true))
        """,
    ]


def _load_migration_module(filename: str) -> Any:
    path = _MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(f"_acl_wu2a_{filename}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration_upgrade(conn: Any, filename: str) -> None:
    """Load the REAL migration file and run its ``upgrade()`` against
    ``conn`` using a genuine Alembic ``Operations`` instance — the same
    mechanism Alembic itself uses, minus the CLI/env.py scaffolding."""
    module = _load_migration_module(filename)
    context = MigrationContext.configure(conn)
    module.op = Operations(context)
    module.upgrade()


def _build_schema(*, apply_032: bool) -> str:
    """Create a fresh throwaway schema holding the REAL
    ``console_agents``/``console_prompt_groups`` DDL (from
    ``Base.metadata``) plus ``console_acl_entries`` built by RUNNING
    THE REAL migration 031 (and, when ``apply_032``, 032 on top).
    Returns the schema name; the caller owns dropping it."""
    assert _ADMIN_URL is not None
    admin = create_engine(_ADMIN_URL, pool_pre_ping=True)
    name = f"acl_wu2a_{datetime.now().strftime('%H%M%S_%f')}"
    owner_tables = [
        Base.metadata.tables["console_agents"],
        Base.metadata.tables["console_prompt_groups"],
    ]
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
        for table in owner_tables:
            table.create(bind=conn)
        for table_name in _OWNER_ONLY_TABLES:
            for statement in _owner_only_rls_ddl(table_name):
                conn.execute(text(statement))
        _run_migration_upgrade(conn, "031_create_console_acl_entries.py")
        if apply_032:
            _run_migration_upgrade(conn, "032_tighten_console_acl_entries_ownership.py")
        for table_name in (*_OWNER_ONLY_TABLES, "console_acl_entries"):
            conn.execute(
                text(
                    f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{name}".{table_name} '
                    f"TO {_APP_ROLE}"
                )
            )
    admin.dispose()
    return name


def _drop_schema(name: str) -> None:
    assert _ADMIN_URL is not None
    admin = create_engine(_ADMIN_URL, pool_pre_ping=True)
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
    admin.dispose()


@pytest_asyncio.fixture
async def app_factory_before() -> Any:
    """Async session factory, connected AS THE APP ROLE, scoped to a
    throwaway schema with migration 031's ORIGINAL (vulnerable) ACL
    policy — the 'before' half of the acceptance evidence."""
    schema = _build_schema(apply_032=False)
    factory = _app_session_factory(schema)
    install_rls_listener()
    try:
        yield factory
    finally:
        await factory.kw["bind"].dispose()
        _drop_schema(schema)


@pytest_asyncio.fixture
async def app_factory_after() -> Any:
    """Async session factory, connected AS THE APP ROLE, scoped to a
    throwaway schema with migration 032's TIGHTENED ACL policy —
    the 'after' half of the acceptance evidence."""
    schema = _build_schema(apply_032=True)
    factory = _app_session_factory(schema)
    install_rls_listener()
    try:
        yield factory
    finally:
        await factory.kw["bind"].dispose()
        _drop_schema(schema)


def _app_session_factory(schema: str) -> Any:
    assert _ADMIN_URL is not None
    admin = _ADMIN_URL.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    at = admin.index("@")
    app_url = f"postgresql+asyncpg://{_APP_ROLE}:{_APP_PASSWORD}{admin[at:]}"
    engine = create_async_engine(
        app_url, connect_args={"server_settings": {"search_path": schema}}
    )
    return async_sessionmaker(bind=engine, expire_on_commit=False)


# ───────────────────────────── seed helpers ─────────────────────────────────

_OWNER = "owner-sub-0001"
_ATTACKER = "attacker-sub-0002"
_VIEWER = "viewer-sub-0003"


async def _seed_owner_resources(factory: Any) -> None:
    """Owner creates one agent and one prompt group, each owned by
    ``_OWNER``. Seeded through SQLAlchemy Core ``insert()`` against the
    REAL ``Table`` objects (not raw ``text()`` SQL) so every NOT NULL
    column without an explicit value here still gets the ORM model's
    own Python-side default (``description=''``,
    ``model_parameters={}``, etc.) — those defaults are Core-level
    (``Column.default``), not DDL ``server_default``s, so they apply
    to any ``insert()`` statement but NOT to a hand-typed ``text()``
    INSERT that omits the column. No service layer is used here
    regardless — this file proves the DB barrier, independent of any
    application code."""
    agents = Base.metadata.tables["console_agents"]
    groups = Base.metadata.tables["console_prompt_groups"]
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": _OWNER},
        )
        await session.execute(
            insert(agents).values(
                id="agent-row-1",
                agent_id="agent-1",
                user_sub=_OWNER,
                name="Agent One",
                created_at_ms=0,
                updated_at_ms=0,
            )
        )
        await session.execute(
            insert(groups).values(
                id="group-row-1",
                group_id="group-1",
                user_sub=_OWNER,
                name="Group One",
                created_at_ms=0,
                updated_at_ms=0,
            )
        )
        await session.commit()


async def _insert_grant(
    factory: Any,
    *,
    as_user: str,
    row_id: str,
    user_sub: str,
    principal_type: str,
    resource_type: str,
    resource_id: str,
    principal_id: str | None = None,
    principal_model: str | None = None,
) -> None:
    """Raw INSERT into console_acl_entries — deliberately bypassing any
    Python-level ``owns()`` check, since no write SERVICE exists yet
    (WU-2b) and the point of this file is the DB barrier's own
    unbypassability (WU-2a §4 neuter 3 — 'a hostile caller attempting
    the ACL write without going through it')."""
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": as_user},
        )
        await session.execute(
            text(
                "INSERT INTO console_acl_entries "
                "(id, user_sub, principal_type, principal_id, principal_model, "
                "resource_type, resource_id, perm_bits, granted_at_ms, "
                "created_at_ms, updated_at_ms) "
                "VALUES (:id, :user_sub, :ptype, :pid, :pmodel, "
                ":rtype, :rid, 1, 0, 0, 0)"
            ),
            {
                "id": row_id,
                "user_sub": user_sub,
                "ptype": principal_type,
                "pid": principal_id,
                "pmodel": principal_model,
                "rtype": resource_type,
                "rid": resource_id,
            },
        )
        await session.commit()


async def _row_count(factory: Any, as_user: str | None, where: str = "") -> int:
    async with factory() as session:
        if as_user is not None:
            await session.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": as_user},
            )
        result = await session.execute(
            text(f"SELECT count(*) FROM console_acl_entries {where}")
        )
        return int(result.scalar_one())


async def _current_owner(factory: Any, as_user: str, row_id: str) -> str:
    """The row's ACTUAL ``user_sub`` column value, read as ``as_user``.

    Deliberately NOT a visibility count: a ``public`` row stays
    SELECT-visible to every authenticated caller regardless of who
    owns it (the third ``USING`` disjunct is unconditional), so
    counting visible rows cannot distinguish "still owned by the
    original owner" from "ownership was taken over" — only the
    column's actual value can.
    """
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": as_user},
        )
        result = await session.execute(
            text("SELECT user_sub FROM console_acl_entries WHERE id = :id"),
            {"id": row_id},
        )
        return str(result.scalar_one())


# ═══════════════════════════ BEFORE — main today ════════════════════════════


class TestBeforeFix:
    """Reproduces the vulnerability CURRENT ``main`` ships (migration
    031 alone). Every assertion here is that the attack SUCCEEDS — the
    'before' half of the acceptance evidence WU-2a §4 neuter 1
    requires."""

    async def test_escalation_attacker_grants_self_access_on_victims_agent(
        self, app_factory_before: Any
    ) -> None:
        """§1's headline finding: attacker A inserts a grant naming a
        resource owned by victim (the owner fixture), with A as the
        grant's ``user_sub``. ``WITH CHECK`` only compares ``user_sub``
        to the GUC — A really is A — so the insert succeeds despite A
        owning nothing named in the row."""
        await _seed_owner_resources(app_factory_before)
        await _insert_grant(
            app_factory_before,
            as_user=_ATTACKER,
            row_id="escalation-row",
            user_sub=_ATTACKER,
            principal_type="public",
            resource_type="agent",
            resource_id="agent-1",  # owned by _OWNER, not _ATTACKER
        )
        # The row exists and A's own resolved principal set can see it.
        assert await _row_count(app_factory_before, _ATTACKER) == 1

    async def test_h1_any_user_deletes_any_public_grant(
        self, app_factory_before: Any
    ) -> None:
        """H-1: DELETE is gated by USING alone in Postgres; the third
        USING disjunct is unconditional ``principal_type = 'public'``,
        so any authenticated user can delete any public row."""
        await _seed_owner_resources(app_factory_before)
        await _insert_grant(
            app_factory_before,
            as_user=_OWNER,
            row_id="public-row",
            user_sub=_OWNER,
            principal_type="public",
            resource_type="agent",
            resource_id="agent-1",
        )
        assert await _row_count(app_factory_before, None) >= 0  # sanity, GUC-agnostic
        async with app_factory_before() as session:
            await session.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": _ATTACKER},
            )
            result = await session.execute(
                text("DELETE FROM console_acl_entries WHERE id = 'public-row'")
            )
            await session.commit()
        assert result.rowcount == 1, (
            "H-1 confirmed: attacker deleted a public row it does not own"
        )
        assert await _row_count(app_factory_before, _OWNER) == 0

    async def test_h2_any_user_takes_ownership_of_a_public_grant(
        self, app_factory_before: Any
    ) -> None:
        """H-2: UPDATE admits the public row via the broad USING, and
        the narrow WITH CHECK only re-checks the NEW user_sub — which
        the attacker sets to themselves."""
        await _seed_owner_resources(app_factory_before)
        await _insert_grant(
            app_factory_before,
            as_user=_OWNER,
            row_id="public-row-2",
            user_sub=_OWNER,
            principal_type="public",
            resource_type="agent",
            resource_id="agent-1",
        )
        async with app_factory_before() as session:
            await session.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": _ATTACKER},
            )
            result = await session.execute(
                text(
                    "UPDATE console_acl_entries SET user_sub = :attacker "
                    "WHERE id = 'public-row-2'"
                ),
                {"attacker": _ATTACKER},
            )
            await session.commit()
        assert result.rowcount == 1, (
            "H-2 confirmed: attacker took ownership of a public row"
        )
        assert (
            await _current_owner(app_factory_before, _OWNER, "public-row-2")
            == _ATTACKER
        ), "H-2 confirmed: the row's user_sub column now names the attacker"


# ═══════════════════════════ AFTER — migration 032 ══════════════════════════


class TestAfterFix:
    """The SAME three attacks against the tightened policy all FAIL;
    legitimate ownership-backed writes still succeed."""

    async def test_escalation_insert_on_agent_is_blocked(
        self, app_factory_after: Any
    ) -> None:
        await _seed_owner_resources(app_factory_after)
        with pytest.raises(DBAPIError, match="row-level security"):
            await _insert_grant(
                app_factory_after,
                as_user=_ATTACKER,
                row_id="escalation-row",
                user_sub=_ATTACKER,
                principal_type="public",
                resource_type="agent",
                resource_id="agent-1",  # owned by _OWNER, not _ATTACKER
            )

    async def test_escalation_insert_on_prompt_group_is_blocked(
        self, app_factory_after: Any
    ) -> None:
        """Per-resource_type, individually (WU-2a §4 neuter 5) — the
        SAME escalation attempt against the OTHER resolved
        resource_type, proven separately so a dead resolver arm for
        just one type cannot hide behind the other's pass."""
        await _seed_owner_resources(app_factory_after)
        with pytest.raises(DBAPIError, match="row-level security"):
            await _insert_grant(
                app_factory_after,
                as_user=_ATTACKER,
                row_id="escalation-row-2",
                user_sub=_ATTACKER,
                principal_type="public",
                resource_type="promptGroup",
                resource_id="group-1",  # owned by _OWNER, not _ATTACKER
            )

    async def test_h1_delete_of_public_row_by_non_owner_is_blocked(
        self, app_factory_after: Any
    ) -> None:
        await _seed_owner_resources(app_factory_after)
        await _insert_grant(
            app_factory_after,
            as_user=_OWNER,
            row_id="public-row",
            user_sub=_OWNER,
            principal_type="public",
            resource_type="agent",
            resource_id="agent-1",
        )
        async with app_factory_after() as session:
            await session.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": _ATTACKER},
            )
            result = await session.execute(
                text("DELETE FROM console_acl_entries WHERE id = 'public-row'")
            )
            await session.commit()
        assert result.rowcount == 0, "H-1 closed: non-owner delete affects zero rows"
        assert await _row_count(app_factory_after, _OWNER) == 1

    async def test_h2_update_ownership_takeover_is_blocked(
        self, app_factory_after: Any
    ) -> None:
        await _seed_owner_resources(app_factory_after)
        await _insert_grant(
            app_factory_after,
            as_user=_OWNER,
            row_id="public-row-2",
            user_sub=_OWNER,
            principal_type="public",
            resource_type="agent",
            resource_id="agent-1",
        )
        async with app_factory_after() as session:
            await session.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": _ATTACKER},
            )
            result = await session.execute(
                text(
                    "UPDATE console_acl_entries SET user_sub = :attacker "
                    "WHERE id = 'public-row-2'"
                ),
                {"attacker": _ATTACKER},
            )
            await session.commit()
        assert result.rowcount == 0, "H-2 closed: non-owner update affects zero rows"
        assert (
            await _current_owner(app_factory_after, _OWNER, "public-row-2") == _OWNER
        ), "ownership unchanged: the row's user_sub column still names the owner"

    async def test_owner_can_still_grant_on_their_own_agent(
        self, app_factory_after: Any
    ) -> None:
        """Positive control, resource_type='agent' — legitimate writes
        must keep working; a fix that closes the hole by blocking
        EVERYTHING would be a different (worse) defect."""
        await _seed_owner_resources(app_factory_after)
        await _insert_grant(
            app_factory_after,
            as_user=_OWNER,
            row_id="legit-agent-grant",
            user_sub=_OWNER,
            principal_type="public",
            resource_type="agent",
            resource_id="agent-1",
        )
        assert await _row_count(app_factory_after, _OWNER) == 1

    async def test_owner_can_still_grant_on_their_own_prompt_group(
        self, app_factory_after: Any
    ) -> None:
        """Positive control, resource_type='promptGroup' — individually,
        same rationale as the agent positive control above."""
        await _seed_owner_resources(app_factory_after)
        await _insert_grant(
            app_factory_after,
            as_user=_OWNER,
            row_id="legit-group-grant",
            user_sub=_OWNER,
            principal_type="public",
            resource_type="promptGroup",
            resource_id="group-1",
        )
        assert await _row_count(app_factory_after, _OWNER) == 1

    async def test_unmapped_resource_type_is_refused_at_the_db_layer(
        self, app_factory_after: Any
    ) -> None:
        """Fail-closed parity with ``services/console_acl/_ownership.py``:
        a resource_type with no ownership-subquery branch (e.g.
        'mcpServer', not yet migrated to a sovereign store) evaluates
        the OR-chain to FALSE unconditionally — refused at the DB layer
        even for the resource's own would-be owner, since there is no
        store yet to prove ownership against."""
        with pytest.raises(DBAPIError, match="row-level security"):
            await _insert_grant(
                app_factory_after,
                as_user=_OWNER,
                row_id="unmapped-row",
                user_sub=_OWNER,
                principal_type="public",
                resource_type="mcpServer",
                resource_id="whatever",
            )

    async def test_select_read_path_contract_is_unchanged(
        self, app_factory_after: Any
    ) -> None:
        """WU-1's read contract (SELECT: owner OR direct-user-principal
        OR public) must be untouched by this migration — the fix
        narrows WRITE, never READ. A PUBLIC row stays visible to
        everyone unconditionally (including with no GUC bound at all
        — that disjunct never depended on the GUC); a PRIVATE ('user')
        row stays gated to its owner and its named principal only,
        exactly as migration 031 shipped it."""
        await _seed_owner_resources(app_factory_after)
        await _insert_grant(
            app_factory_after,
            as_user=_OWNER,
            row_id="public-row-3",
            user_sub=_OWNER,
            principal_type="public",
            resource_type="agent",
            resource_id="agent-1",
        )
        await _insert_grant(
            app_factory_after,
            as_user=_OWNER,
            row_id="private-row",
            user_sub=_OWNER,
            principal_type="user",
            principal_id=_VIEWER,
            principal_model="User",
            resource_type="agent",
            resource_id="agent-1",
        )
        where_public = "WHERE id = 'public-row-3'"
        where_private = "WHERE id = 'private-row'"

        # The public row is visible to anyone, GUC bound or not — the
        # third USING disjunct is unconditional.
        assert await _row_count(app_factory_after, _ATTACKER, where_public) == 1
        assert await _row_count(app_factory_after, None, where_public) == 1

        # The private row is visible to its owner and its named
        # principal, but NOT to an unrelated attacker, and NOT with no
        # GUC bound at all (current_setting returns NULL, matching
        # neither the owner nor the principal branch).
        assert await _row_count(app_factory_after, _OWNER, where_private) == 1
        assert await _row_count(app_factory_after, _VIEWER, where_private) == 1
        assert await _row_count(app_factory_after, _ATTACKER, where_private) == 0
        assert await _row_count(app_factory_after, None, where_private) == 0
