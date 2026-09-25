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

**Fix round 1 additions (spec §3.4's UPDATE case, F1/F2/F3 of the
reviewer's REJECT):**

3. **UPDATE-side ``resource_id`` escalation** — the sibling of §1's
   INSERT escalation, proven per resolved resource_type individually
   (``test_update_resource_id_escalation_is_blocked_for_agent``/
   ``..._for_prompt_group``) plus its 'before' counterpart
   (``test_update_resource_id_hijack_via_legitimately_owned_row``). The
   original build's real-Postgres suite proved INSERT and DELETE but
   never exercised this UPDATE case behaviourally — a reviewer neuter
   of migration 032's UPDATE ownership subquery only went RED on a
   rendered-SQL TEXT pin (``test_console_acl_ownership_migration.py``),
   with all 11 real-Postgres tests staying GREEN. A text pin is not a
   behavioural guard; these tests are.
4. **The corrected H-2 story** — ``test_h2_general_takeover_blocked_
   even_when_attacker_repoints_to_owned_resource`` replaces a FALSE
   claim an earlier evidence draft made (that the UPDATE ``WITH CHECK``
   ownership subquery makes the owner-only ``USING`` clause "redundant"
   for H-2). It is not: an attacker who ALSO repoints ``resource_id``
   to a resource they legitimately own satisfies the ownership
   subquery too, so ``WITH CHECK`` alone would pass that combination.
   The owner-only ``USING`` clause is the ONLY general defence against
   ACL-row ownership takeover; this test proves it directly.
5. **The public-resource-squatting DoS variant** (a bonus finding) —
   ``uq_console_acl_entries_public_resource`` allows at most one public
   row per resource; before this WU an attacker's escalation-INSERT
   could squat a victim's resource FIRST, so the victim's own
   legitimate public grant then fails on the unique constraint
   (``test_resource_squatting_blocks_the_legitimate_owner``). Closed as
   a side effect of closing the escalation itself
   (``test_squatting_is_prevented_so_the_owner_can_still_grant_publicly``).

**What "unbypassable" means here, precisely.** This file proves
migration 032 is unbypassable WHERE POSTGRES RLS IS ENFORCED — that is
what a real, non-superuser-role-gated Postgres demonstrates. It does
NOT, and cannot, prove anything about whether RLS is actually enforced
at runtime on any given deployment; that is an operational property of
the target cluster, not of this migration. See the build record for
the open, out-of-scope production finding this qualifies.
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

# ── ACL 2b-core-B additions-only imports (§2 exception). Deliberately
# placed HERE, after the first non-import statement above, rather than
# merged into the top-of-file import block: merging would make ruff's
# import-sort check (I001) want to reorder/re-wrap the EXISTING lines
# above (e.g. collapsing ``from audittrace.db.rls import
# install_rls_listener`` into a multi-name import), which would show as
# a REMOVED line under §2's "removed lines = 0" restriction. This block
# is its own, independently-sorted import group instead.
import contextlib  # noqa: E402
import json  # noqa: E402
import uuid  # noqa: E402
from dataclasses import dataclass  # noqa: E402

from audittrace import dependencies  # noqa: E402
from audittrace.db.postgres import PostgresFactory  # noqa: E402
from audittrace.db.rls import set_current_user_id  # noqa: E402
from audittrace.identity import UserContext  # noqa: E402
from audittrace.services.console_acl import _audit  # noqa: E402
from audittrace.services.console_store import ConsoleStoreScopeError  # noqa: E402


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


async def _seed_attacker_resources(factory: Any) -> None:
    """Attacker creates one agent and one prompt group, each owned by
    ``_ATTACKER`` — used by the UPDATE-side resource_id-escalation tests
    (fix round 1, F1): the attacker legitimately owns THESE resources,
    so a grant naming them passes the ownership subquery at INSERT
    time; the attack under test is UPDATE-ing that legitimate row's
    ``resource_id`` to point at a resource the attacker does NOT own."""
    agents = Base.metadata.tables["console_agents"]
    groups = Base.metadata.tables["console_prompt_groups"]
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": _ATTACKER},
        )
        await session.execute(
            insert(agents).values(
                id="agent-row-attacker",
                agent_id="agent-attacker",
                user_sub=_ATTACKER,
                name="Attacker's Own Agent",
                created_at_ms=0,
                updated_at_ms=0,
            )
        )
        await session.execute(
            insert(groups).values(
                id="group-row-attacker",
                group_id="group-attacker",
                user_sub=_ATTACKER,
                name="Attacker's Own Group",
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


async def _current_resource_id(factory: Any, as_user: str, row_id: str) -> str:
    """The row's ACTUAL ``resource_id`` column value, read as
    ``as_user`` — same rationale as :func:`_current_owner`, applied to
    the OTHER field an ownership-takeover attack can move (fix round 1,
    F1/F2): visibility alone cannot distinguish "resource_id unchanged"
    from "resource_id was hijacked to point elsewhere"."""
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": as_user},
        )
        result = await session.execute(
            text("SELECT resource_id FROM console_acl_entries WHERE id = :id"),
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

    async def test_update_resource_id_hijack_via_legitimately_owned_row(
        self, app_factory_before: Any
    ) -> None:
        """Fix round 1, F1: the UPDATE-side sibling of §1's INSERT
        escalation. Attacker creates a grant naming a resource they
        LEGITIMATELY own (``agent-attacker``), then UPDATEs that SAME
        row's ``resource_id`` to point at the victim's resource
        (``agent-1``) instead — migration 031's WITH CHECK only
        re-checks ``user_sub`` (unchanged here), never ``resource_id``,
        so the hijack succeeds."""
        await _seed_owner_resources(app_factory_before)
        await _seed_attacker_resources(app_factory_before)
        await _insert_grant(
            app_factory_before,
            as_user=_ATTACKER,
            row_id="attacker-owned-row",
            user_sub=_ATTACKER,
            principal_type="public",
            resource_type="agent",
            resource_id="agent-attacker",
        )
        async with app_factory_before() as session:
            await session.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": _ATTACKER},
            )
            result = await session.execute(
                text(
                    "UPDATE console_acl_entries SET resource_id = 'agent-1' "
                    "WHERE id = 'attacker-owned-row'"
                )
            )
            await session.commit()
        assert result.rowcount == 1, (
            "UPDATE-side escalation confirmed: attacker hijacked their own "
            "row's resource_id onto the victim's resource"
        )
        assert (
            await _current_resource_id(
                app_factory_before, _ATTACKER, "attacker-owned-row"
            )
            == "agent-1"
        )

    async def test_resource_squatting_blocks_the_legitimate_owner(
        self, app_factory_before: Any
    ) -> None:
        """Bonus finding (reviewer, fix round 1): ``uq_console_acl_
        entries_public_resource`` allows at most ONE public row per
        ``(resource_type, resource_id)``. Since §1's escalation lets an
        attacker insert a PUBLIC grant on a resource they do not own,
        an attacker can 'squat' the victim's resource FIRST — and the
        victim's own, entirely legitimate attempt to create their own
        public grant on their own resource then fails with a unique-
        constraint violation. A DoS variant of the same root cause,
        not a separate hole."""
        await _seed_owner_resources(app_factory_before)
        await _insert_grant(
            app_factory_before,
            as_user=_ATTACKER,
            row_id="squatter-row",
            user_sub=_ATTACKER,
            principal_type="public",
            resource_type="agent",
            resource_id="agent-1",  # owned by _OWNER — the squat
        )
        with pytest.raises(DBAPIError):
            await _insert_grant(
                app_factory_before,
                as_user=_OWNER,
                row_id="legitimate-owner-row",
                user_sub=_OWNER,
                principal_type="public",
                resource_type="agent",
                resource_id="agent-1",
            )


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

    async def test_h2_general_takeover_blocked_even_when_attacker_repoints_to_owned_resource(
        self, app_factory_after: Any
    ) -> None:
        """Fix round 1, F2 correction: the ORIGINAL H-2 test above only
        varies ``user_sub`` while leaving ``resource_id`` pointed at the
        victim's resource — which the ownership subquery alone WOULD
        catch, and an earlier draft of this evidence wrongly generalised
        that into "the WITH CHECK ownership subquery makes the owner-
        only USING clause redundant for H-2". That claim is FALSE: an
        attacker who ALSO repoints ``resource_id`` to a resource they
        legitimately own (here, ``agent-attacker``) satisfies the
        ownership subquery too, so WITH CHECK alone would pass this
        combination. This test proves what actually stops it: the
        owner-only ``USING`` clause denies the attacker even SELECTing
        the victim-owned row for UPDATE in the first place, regardless
        of what the SET clause contains — the ownership subquery is
        never even reached."""
        await _seed_owner_resources(app_factory_after)
        await _seed_attacker_resources(app_factory_after)
        await _insert_grant(
            app_factory_after,
            as_user=_OWNER,
            row_id="public-row-3",
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
                    "UPDATE console_acl_entries "
                    "SET user_sub = :attacker, resource_id = 'agent-attacker' "
                    "WHERE id = 'public-row-3'"
                ),
                {"attacker": _ATTACKER},
            )
            await session.commit()
        assert result.rowcount == 0, (
            "the general takeover (repointing BOTH user_sub AND resource_id "
            "to something the attacker owns) is still blocked by USING alone"
        )
        assert await _current_owner(app_factory_after, _OWNER, "public-row-3") == _OWNER
        assert (
            await _current_resource_id(app_factory_after, _OWNER, "public-row-3")
            == "agent-1"
        )

    async def test_update_resource_id_escalation_is_blocked_for_agent(
        self, app_factory_after: Any
    ) -> None:
        """Fix round 1, F1 (blocking): the UPDATE-side sibling of the
        INSERT escalation, proven per resolved resource_type
        individually. Attacker creates a grant naming a resource they
        LEGITIMATELY own (passes the INSERT-time ownership subquery),
        then tries to UPDATE that same row's ``resource_id`` onto the
        victim's resource. The RED this guards against lands on the
        refused write (a real DBAPIError) or the row's resource_id
        VALUE — never on rendered SQL text."""
        await _seed_owner_resources(app_factory_after)
        await _seed_attacker_resources(app_factory_after)
        await _insert_grant(
            app_factory_after,
            as_user=_ATTACKER,
            row_id="attacker-owned-row",
            user_sub=_ATTACKER,
            principal_type="public",
            resource_type="agent",
            resource_id="agent-attacker",
        )
        with pytest.raises(DBAPIError, match="row-level security"):
            async with app_factory_after() as session:
                await session.execute(
                    text("SELECT set_config('app.current_user_id', :uid, true)"),
                    {"uid": _ATTACKER},
                )
                await session.execute(
                    text(
                        "UPDATE console_acl_entries SET resource_id = 'agent-1' "
                        "WHERE id = 'attacker-owned-row'"
                    )
                )
                await session.commit()
        assert (
            await _current_resource_id(
                app_factory_after, _ATTACKER, "attacker-owned-row"
            )
            == "agent-attacker"
        ), "the row's resource_id must remain the attacker's own agent"

    async def test_update_resource_id_escalation_is_blocked_for_prompt_group(
        self, app_factory_after: Any
    ) -> None:
        """Per resolved resource_type, individually (same rationale as
        the agent variant above) — a dead ownership check for JUST
        promptGroup on UPDATE must not hide behind agent's pass."""
        await _seed_owner_resources(app_factory_after)
        await _seed_attacker_resources(app_factory_after)
        await _insert_grant(
            app_factory_after,
            as_user=_ATTACKER,
            row_id="attacker-owned-group-row",
            user_sub=_ATTACKER,
            principal_type="public",
            resource_type="promptGroup",
            resource_id="group-attacker",
        )
        with pytest.raises(DBAPIError, match="row-level security"):
            async with app_factory_after() as session:
                await session.execute(
                    text("SELECT set_config('app.current_user_id', :uid, true)"),
                    {"uid": _ATTACKER},
                )
                await session.execute(
                    text(
                        "UPDATE console_acl_entries SET resource_id = 'group-1' "
                        "WHERE id = 'attacker-owned-group-row'"
                    )
                )
                await session.commit()
        assert (
            await _current_resource_id(
                app_factory_after, _ATTACKER, "attacker-owned-group-row"
            )
            == "group-attacker"
        ), "the row's resource_id must remain the attacker's own group"

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

    async def test_squatting_is_prevented_so_the_owner_can_still_grant_publicly(
        self, app_factory_after: Any
    ) -> None:
        """Bonus finding (reviewer, fix round 1) — the AFTER half of
        the squatting DoS variant: the attacker's squat attempt itself
        is refused (the same escalation-INSERT guard, already proven
        above), so it never occupies
        ``uq_console_acl_entries_public_resource``'s one-public-row
        slot and the legitimate owner's own public grant succeeds
        without a unique-constraint conflict."""
        await _seed_owner_resources(app_factory_after)
        with pytest.raises(DBAPIError, match="row-level security"):
            await _insert_grant(
                app_factory_after,
                as_user=_ATTACKER,
                row_id="squatter-row",
                user_sub=_ATTACKER,
                principal_type="public",
                resource_type="agent",
                resource_id="agent-1",  # owned by _OWNER — the squat attempt
            )
        # The squat never landed — the owner's own public grant on the
        # SAME resource_id now succeeds cleanly.
        await _insert_grant(
            app_factory_after,
            as_user=_OWNER,
            row_id="legitimate-owner-row",
            user_sub=_OWNER,
            principal_type="public",
            resource_type="agent",
            resource_id="agent-1",
        )
        assert (
            await _row_count(
                app_factory_after, _OWNER, "WHERE id = 'legitimate-owner-row'"
            )
            == 1
        )


# ═════════════════ ACL 2b-core-B — the sovereign audit writer ═════════════
# REAL-Postgres proof for services/console_acl/_audit.py's guards that need
# actual RLS/CHECK/trigger enforcement (the aiosqlite param lives in
# tests/test_console_acl_audit_writer.py). §9: "N1-N4 assert on the ORM
# interactions table (aiosqlite param AND real PG)"; N5/N6 are Postgres-only
# by nature; N8's forged-identity note needs both sides to make its point.
# §2 exception: additions only — everything above this section is untouched
# (git diff on removed lines above this point is 0).
#
# Identity here is bound via ``set_current_user_id`` (the ContextVar +
# ``after_begin`` listener — the REAL production path) rather than the
# WU-2a helpers' hand-set ``SELECT set_config(...)`` per session — §5.4's
# explicit instruction: "N3 runs with set_current_user_id() set, not a
# hand-set GUC."
#
# A SEPARATE schema builder (``_build_interactions_schema``): the WU-2a
# builder above only creates console_agents/console_prompt_groups +
# console_acl_entries (031/032). This WU's subject is ``interactions``
# (migrations 002, 003, 004, 005, 007, 008, 012, 015, 016, 017 — §8's exact
# list) plus console_acl_entries (031 only — N3's abort scenario does not
# need 032's ownership tightening). ``sessions``/``memory_items`` are
# minimal STUB tables: migrations 003 and 012 each ALTER a table this WU
# does not otherwise care about (003 adds ``sessions.user_id``; 012 adds
# ``memory_items.scan_status``) — running the REAL migration file (never
# hand-retyping its DDL, per §8's closing instruction) requires the
# ALTER's target table to exist first. The stub's own shape plays no role
# in any guard exercised here.
#
# Per-guard neuter table (PG side only — aiosqlite side is the sibling
# file's own table):
#
# | guard | PG test |
# |---|---|
# | N1 | ``TestAuditWriterRealPostgres::test_n1_...`` |
# | N2 | ``TestAuditWriterRealPostgres::test_n2_...`` |
# | N3 | ``TestAuditWriterRealPostgres::test_n3_...`` |
# | N4 | ``TestAuditWriterRealPostgres::test_n4_...`` |
# | N5 | ``TestAppendOnlyTriggerNeuter`` |
# | N6 | ``TestCrossSubjectAuditReadIsImpossible`` (exactly one test) |
# | N8 | ``TestForgedUserIdRefusedOnPostgres`` |

_INTERACTIONS_MIGRATION_FILES: tuple[str, ...] = (
    "002_create_interactions_table.py",
    "003_multi_user_identity.py",
    "004_drop_local_users_for_keycloak.py",
    "005_enable_rls_policies.py",
    "007_add_interaction_failure_columns.py",
    "008_add_interactions_trace_id.py",
    "012_add_event_class_and_scan_status.py",
    "015_add_interactions_created_at.py",
    "016_append_only_audit_rows.py",
    "017_add_interactions_content_hash.py",
)


def _build_interactions_schema() -> str:
    """Fresh throwaway schema holding ``interactions``/``tool_calls``
    (built by running the REAL migration files listed in §8, in the
    exact order Alembic itself would apply them) plus
    ``console_acl_entries`` (migration 031, for N3's abort scenario) —
    connected AS THE APP ROLE, same NOBYPASSRLS discipline as
    :func:`_build_schema`. Returns the schema name; the caller owns
    dropping it."""
    assert _ADMIN_URL is not None
    admin = create_engine(_ADMIN_URL, pool_pre_ping=True)
    name = f"acl_2b_core_b_{datetime.now().strftime('%H%M%S_%f')}"
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
        # Minimal stub targets for 003's / 012's ALTER — see the section
        # docstring above for why these are stubs, not the real ORM shape.
        conn.execute(text("CREATE TABLE sessions (id VARCHAR(36) PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE memory_items (id VARCHAR(36) PRIMARY KEY)"))
        for filename in _INTERACTIONS_MIGRATION_FILES:
            _run_migration_upgrade(conn, filename)
        _run_migration_upgrade(conn, "031_create_console_acl_entries.py")
        for table_name in ("interactions", "tool_calls", "console_acl_entries"):
            conn.execute(
                text(
                    f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{name}".{table_name} '
                    f"TO {_APP_ROLE}"
                )
            )
        # `interactions.id`/`tool_calls` PKs are sequence-backed
        # (Integer autoincrement) — INSERT needs USAGE on the sequence
        # too, not just the table grant above.
        conn.execute(
            text(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA "{name}" TO {_APP_ROLE}')
        )
    admin.dispose()
    return name


@dataclass
class _InteractionsHarness:
    """The schema name (needed by N5's admin-level DROP/CREATE TRIGGER —
    the app role does not own the table) plus the app-role session
    factory."""

    schema: str
    factory: Any


@pytest_asyncio.fixture
async def interactions_harness() -> Any:
    """Async session factory, connected AS THE APP ROLE, scoped to a
    throwaway schema holding the REAL interactions/tool_calls/
    console_acl_entries schema (§8's exact migration list)."""
    schema = _build_interactions_schema()
    factory = _app_session_factory(schema)
    install_rls_listener()
    try:
        yield _InteractionsHarness(schema=schema, factory=factory)
    finally:
        await factory.kw["bind"].dispose()
        _drop_schema(schema)


class _StaticPostgresFactory(PostgresFactory):
    """Minimal concrete :class:`PostgresFactory` wrapping an already-built
    ``async_sessionmaker`` — wires ``_audit.record_denial``'s internal
    ``get_postgres_factory()`` call to THIS test's throwaway schema,
    exactly the way production DI wires it to the real one."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    def get_engine(self) -> Any:
        raise NotImplementedError("not exercised by _audit.record_denial")

    def get_session_factory(self) -> Any:
        return self._session_factory


@contextlib.contextmanager
def _wired_postgres_factory(factory: Any) -> Any:
    """Wire ``get_postgres_factory()`` to ``factory`` for the duration of
    the block. ``conftest.py``'s autouse ``_reset_global_container``
    fixture clears the container before the NEXT test regardless; this
    also restores whatever was registered before, for tests that run
    other DI-dependent code in the same body."""
    previous = dependencies.container._instances.get("postgres_factory")
    dependencies.container._instances["postgres_factory"] = _StaticPostgresFactory(
        factory
    )
    try:
        yield
    finally:
        if previous is None:
            dependencies.container._instances.pop("postgres_factory", None)
        else:
            dependencies.container._instances["postgres_factory"] = previous


def _new_user_context(sub: str) -> UserContext:
    return UserContext(user_id=sub, username=sub, agent_type="test", scopes=())


class TestAuditWriterRealPostgres:
    """N1/N2/N3/N4 — the REAL-Postgres half of ``services/console_acl/
    _audit.py``'s guards (aiosqlite half: ``tests/
    test_console_acl_audit_writer.py``)."""

    async def test_n1_successful_write_produces_a_full_payload_row(
        self, interactions_harness: _InteractionsHarness
    ) -> None:
        factory = interactions_harness.factory
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            async with factory() as db:
                row = await _audit.record(
                    db,
                    user_context=owner,
                    op="grantPermission",
                    principal_type="user",
                    principal_id="principal-1",
                    resource_type="agent",
                    resource_id="agent-pg-1",
                    perm_bits=3,
                    acl_entry_ids=["acl-pg-1"],
                )
                await db.commit()
                row_id = row.id

            async with factory() as fresh:
                stored = (
                    (
                        await fresh.execute(
                            text(
                                "SELECT user_id, event_class, status, failure_class, "
                                "answer, content_hash FROM interactions WHERE id = :id"
                            ),
                            {"id": row_id},
                        )
                    )
                    .mappings()
                    .one()
                )
        finally:
            set_current_user_id(None)

        assert stored["user_id"] == _OWNER
        assert stored["event_class"] == _audit.EVENT_CLASS_ACL_AUTHZ
        assert stored["status"] == "success"
        assert stored["failure_class"] is None
        answer = json.loads(stored["answer"])
        assert answer["granted_by"] == _OWNER
        assert answer["acl_entry_ids"] == ["acl-pg-1"]
        assert stored["content_hash"] is not None

    async def test_n2_denied_write_produces_a_failed_row(
        self, interactions_harness: _InteractionsHarness
    ) -> None:
        factory = interactions_harness.factory
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            with _wired_postgres_factory(factory):
                row = await _audit.record_denial(
                    user_context=owner,
                    op="grantPermission",
                    principal_type="user",
                    principal_id="p1",
                    resource_type="agent",
                    resource_id="agent-pg-denied",
                    perm_bits=1,
                    failure_class=_audit.FAILURE_CLASS_ACL_DENIED_POLICY,
                    predicate_or_attempted_row={"resource_id": "agent-pg-denied"},
                    db_error_class="IntegrityError",
                )

            async with factory() as fresh:
                stored = (
                    (
                        await fresh.execute(
                            text(
                                "SELECT status, failure_class, answer, error_detail "
                                "FROM interactions WHERE id = :id"
                            ),
                            {"id": row.id},
                        )
                    )
                    .mappings()
                    .one()
                )
        finally:
            set_current_user_id(None)

        assert stored["status"] == "failed"
        assert stored["failure_class"] == _audit.FAILURE_CLASS_ACL_DENIED_POLICY
        assert stored["answer"] == "{}"
        detail = json.loads(stored["error_detail"])
        assert detail["predicate_or_attempted_row"] == {
            "resource_id": "agent-pg-denied"
        }

    async def test_n3_denial_row_survives_an_rls_aborted_transaction(
        self, interactions_harness: _InteractionsHarness
    ) -> None:
        """N3, PG half — the ``031:97-100`` CHECK is proven on aiosqlite
        already; here the abort is a REAL RLS ``WITH CHECK`` refusal
        (owner-only INSERT guard, migration 031) — "RLS on PG".

        Fix round 1 (F2) — the ordering IS the proof: ``record_denial``
        runs INSIDE the SAME ``async with factory() as db:`` block as the
        failed INSERT, WHILE that transaction is still open (aborted, not
        yet rolled back). The first cut called ``record_denial`` only
        after the ``async with`` block had already exited (which rolls
        back on the raised exception), so no overlap ever existed and
        "survives the rollback" held for an unrelated reason."""
        factory = interactions_harness.factory
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            async with factory() as db:
                with pytest.raises(DBAPIError, match="row-level security"):
                    await db.execute(
                        text(
                            "INSERT INTO console_acl_entries "
                            "(id, user_sub, principal_type, principal_id, "
                            "principal_model, resource_type, resource_id, "
                            "perm_bits, granted_at_ms, created_at_ms, updated_at_ms) "
                            "VALUES (:id, :user_sub, 'user', :pid, 'User', "
                            "'agent', :rid, 1, 0, 0, 0)"
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            # Mismatched vs. the bound identity (_OWNER) —
                            # migration 031's WITH CHECK (user_sub =
                            # current_setting(...)) refuses this INSERT.
                            "user_sub": _ATTACKER,
                            "pid": _VIEWER,
                            "rid": "agent-rls-abort",
                        },
                    )

                # `db`'s transaction is STILL OPEN (Postgres has marked it
                # aborted, but no ROLLBACK has been issued yet). A writer
                # that shared this session would itself raise
                # "current transaction is aborted" on the very next
                # statement — only a genuinely INDEPENDENT session
                # (opened by record_denial's own get_postgres_factory()
                # call) can succeed here.
                with _wired_postgres_factory(factory):
                    denial = await _audit.record_denial(
                        user_context=owner,
                        op="grantPermission",
                        principal_type="user",
                        principal_id=_VIEWER,
                        resource_type="agent",
                        resource_id="agent-rls-abort",
                        perm_bits=1,
                        failure_class=_audit.FAILURE_CLASS_ACL_DENIED_POLICY,
                        predicate_or_attempted_row={"resource_id": "agent-rls-abort"},
                        db_error_class="row-level security",
                    )

                await db.rollback()  # now clean up the still-pending abort

            async with factory() as fresh:
                acl_count = (
                    await fresh.execute(
                        text(
                            "SELECT count(*) FROM console_acl_entries "
                            "WHERE resource_id = 'agent-rls-abort'"
                        )
                    )
                ).scalar_one()
                assert acl_count == 0, "the RLS-refused ACL insert must not have landed"
                denial_count = (
                    await fresh.execute(
                        text("SELECT count(*) FROM interactions WHERE id = :id"),
                        {"id": denial.id},
                    )
                ).scalar_one()
                assert denial_count == 1, (
                    "the denial row must survive the STILL-OPEN RLS abort"
                )
        finally:
            set_current_user_id(None)

    async def test_n3_a_shared_session_would_fail_where_the_real_writer_succeeds(
        self, interactions_harness: _InteractionsHarness
    ) -> None:
        """The F2 falsifiability proof, COMMITTED (fix round 2 — a
        reviewer noted this claim previously depended on an uncommitted
        throwaway script; "a claim that needs a script should have a
        test"). Simulates the "tempting fix" §5.4 forbids — a
        ``record_denial`` that reused the CALLER's still-aborted session
        instead of opening its own — inline, against a real aborted
        transaction, and shows it fails with Postgres's own
        "current transaction is aborted" error, while the REAL writer
        (independent session, unmodified) succeeds under the identical
        conditions."""
        factory = interactions_harness.factory
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            # BAD PATTERN — a hypothetical record_denial that shares the
            # caller's session instead of opening its own. Both the abort
            # and the write attempt are Core-level (text()) statements on
            # the SAME session, mirroring test_n3's own abort mechanism —
            # an ORM flush of a newly-added pending object behaves
            # differently (SQLAlchemy marks the whole ORM Session
            # "pending rollback" immediately, raising its OWN
            # PendingRollbackError on ANY further use, which would mask
            # the actual DB-level "transaction is aborted" error this
            # test needs to demonstrate).
            async with factory() as db:
                with pytest.raises(DBAPIError, match="row-level security"):
                    await db.execute(
                        text(
                            "INSERT INTO console_acl_entries "
                            "(id, user_sub, principal_type, principal_id, "
                            "principal_model, resource_type, resource_id, "
                            "perm_bits, granted_at_ms, created_at_ms, "
                            "updated_at_ms) "
                            "VALUES (:id, :user_sub, 'user', :pid, 'User', "
                            "'agent', :rid, 1, 0, 0, 0)"
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "user_sub": _ATTACKER,  # mismatched — RLS aborts
                            "pid": "p1",
                            "rid": "agent-f2-falsify-bad",
                        },
                    )

                with pytest.raises(DBAPIError, match="current transaction is aborted"):
                    await db.execute(
                        text(
                            "INSERT INTO interactions "
                            "(project, source, question, answer, "
                            "prompt_tokens, completion_tokens, timestamp, "
                            "user_id, status, failure_class, event_class) "
                            "VALUES ('console-acl', 'console-acl', "
                            "'denial-shared-session-bad-pattern', '{}', 0, "
                            "0, '2026-09-25T00:00:00+00:00', :uid, "
                            "'failed', 'acl_denied_policy', 'acl_authz')"
                        ),
                        {"uid": owner.user_id},
                    )
                await db.rollback()

            # GOOD PATTERN — the REAL writer, unmodified, independent
            # session, under the identical abort conditions.
            async with factory() as db2:
                with pytest.raises(DBAPIError, match="row-level security"):
                    await db2.execute(
                        text(
                            "INSERT INTO console_acl_entries "
                            "(id, user_sub, principal_type, principal_id, "
                            "principal_model, resource_type, resource_id, "
                            "perm_bits, granted_at_ms, created_at_ms, "
                            "updated_at_ms) "
                            "VALUES (:id, :user_sub, 'user', :pid, 'User', "
                            "'agent', :rid, 1, 0, 0, 0)"
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "user_sub": _ATTACKER,
                            "pid": "p1",
                            "rid": "agent-f2-falsify-good",
                        },
                    )

                with _wired_postgres_factory(factory):
                    denial = await _audit.record_denial(
                        user_context=owner,
                        op="grantPermission",
                        principal_type="user",
                        principal_id="p1",
                        resource_type="agent",
                        resource_id="agent-f2-falsify-good",
                        perm_bits=99,
                        failure_class=_audit.FAILURE_CLASS_ACL_DENIED_POLICY,
                        predicate_or_attempted_row={},
                    )
                await db2.rollback()

            async with factory() as fresh:
                denial_count = (
                    await fresh.execute(
                        text("SELECT count(*) FROM interactions WHERE id = :id"),
                        {"id": denial.id},
                    )
                ).scalar_one()
                assert denial_count == 1, (
                    "the real (independent-session) writer must succeed "
                    "under the identical conditions the shared-session "
                    "pattern just failed under"
                )
        finally:
            set_current_user_id(None)

    async def test_n4_writer_raises_on_a_broken_audit_write(
        self,
        interactions_harness: _InteractionsHarness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """N4, PG half — the writer's own half (see the module docstring
        for the caller-half forward obligation)."""

        def _raise(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError("content_hash broken for this test")

        monkeypatch.setattr(_audit, "_content_hash", _raise)
        factory = interactions_harness.factory
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            with pytest.raises(RuntimeError, match="content_hash broken"):
                async with factory() as db:
                    await _audit.record(
                        db,
                        user_context=owner,
                        op="grantPermission",
                        principal_type="user",
                        principal_id="p1",
                        resource_type="agent",
                        resource_id="agent-1",
                        perm_bits=1,
                        acl_entry_ids=["acl-1"],
                    )
            with _wired_postgres_factory(factory):
                with pytest.raises(RuntimeError, match="content_hash broken"):
                    await _audit.record_denial(
                        user_context=owner,
                        op="grantPermission",
                        principal_type="user",
                        principal_id="p1",
                        resource_type="agent",
                        resource_id="agent-1",
                        perm_bits=1,
                        failure_class=_audit.FAILURE_CLASS_ACL_DENIED_POLICY,
                        predicate_or_attempted_row={},
                    )
        finally:
            set_current_user_id(None)

    async def test_n4_no_ambient_identity_gets_with_check_refusal_which_propagates(
        self, interactions_harness: _InteractionsHarness
    ) -> None:
        """§5.4's exact scenario: a writer opening its own session with NO
        GUC bound (the ContextVar is ``None`` — no ``require_user`` ran)
        gets a ``WITH CHECK`` refusal from migration 005's RLS policy on
        ``interactions`` itself, and that refusal propagates — fail
        closed, never a silent downgrade. ``set_current_user_id`` is
        deliberately NEVER called in this test."""
        factory = interactions_harness.factory
        owner = _new_user_context(_OWNER)
        assert set_current_user_id.__module__ == "audittrace.db.rls"  # sanity
        with _wired_postgres_factory(factory):
            with pytest.raises(DBAPIError, match="row-level security"):
                await _audit.record_denial(
                    user_context=owner,
                    op="grantPermission",
                    principal_type="user",
                    principal_id="p1",
                    resource_type="agent",
                    resource_id="agent-no-guc",
                    perm_bits=1,
                    failure_class=_audit.FAILURE_CLASS_ACL_DENIED_POLICY,
                    predicate_or_attempted_row={},
                )

    async def test_n4_unbound_ambient_identity_refuses_any_subject_forged_or_not(
        self, interactions_harness: _InteractionsHarness
    ) -> None:
        """Fix round 2 correction — this test used to live in
        ``TestForgedUserIdRefusedOnPostgres`` as if it exercised a
        forged-identity-SPECIFIC guard. It does not: with the ambient
        ContextVar unbound, ``resolve_user_sub``'s mismatch check is a
        no-op by its own documented design (there is nothing to compare
        against), so the caller-supplied ``user_context.user_id`` reaches
        the database layer unchanged — and migration 005's RLS refuses
        it REGARDLESS of whether that subject is forged or perfectly
        legitimate. This is the SAME §5.4 fail-closed path as the sibling
        test above, demonstrated with an (irrelevantly) forged subject to
        make the point that forging buys an attacker nothing extra here —
        the refusal is unconditional on identity, conditional only on the
        ContextVar being unbound."""
        factory = interactions_harness.factory
        forged = _new_user_context("attacker-unbound-forged-pg")
        # Deliberately NEVER call set_current_user_id.
        with _wired_postgres_factory(factory):
            with pytest.raises(DBAPIError, match="row-level security"):
                await _audit.record_denial(
                    user_context=forged,
                    op="grantPermission",
                    principal_type="user",
                    principal_id="p1",
                    resource_type="agent",
                    resource_id="agent-unbound-forged-pg",
                    perm_bits=1,
                    failure_class=_audit.FAILURE_CLASS_ACL_DENIED_POLICY,
                    predicate_or_attempted_row={},
                )


class TestAppendOnlyTriggerNeuter:
    """N5 — migration 016's append-only trigger, proven BEHAVIOURALLY as
    the NOBYPASSRLS role (the only existing test,
    ``test_migration_append_only.py:41-44``, is a DDL text pin — barred
    as a RED target by §9), then neutered by ``DROP TRIGGER`` to show the
    mutation succeeds, then restored via the single ``CREATE TRIGGER``
    statement (re-running ``upgrade()`` alone FAILS — 016 creates TWO
    triggers and only one is dropped here). ``pg_trigger`` is compared to
    the pre-drop capture — the ``cmp``-verified restore §9 requires."""

    async def test_trigger_blocks_update_and_delete_as_nobypassrls(
        self, interactions_harness: _InteractionsHarness
    ) -> None:
        factory = interactions_harness.factory
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            async with factory() as db:
                row = await _audit.record(
                    db,
                    user_context=owner,
                    op="grantPermission",
                    principal_type="user",
                    principal_id="p1",
                    resource_type="agent",
                    resource_id="agent-trigger",
                    perm_bits=1,
                    acl_entry_ids=["a1"],
                )
                await db.commit()
                row_id = row.id

            with pytest.raises(DBAPIError, match="append-only"):
                async with factory() as db:
                    await db.execute(
                        text(
                            "UPDATE interactions SET answer = 'tampered' WHERE id = :id"
                        ),
                        {"id": row_id},
                    )
                    await db.commit()

            with pytest.raises(DBAPIError, match="append-only"):
                async with factory() as db:
                    await db.execute(
                        text("DELETE FROM interactions WHERE id = :id"), {"id": row_id}
                    )
                    await db.commit()
        finally:
            set_current_user_id(None)

    async def test_neutering_the_trigger_lets_the_mutation_succeed_then_restore(
        self, interactions_harness: _InteractionsHarness
    ) -> None:
        schema = interactions_harness.schema
        factory = interactions_harness.factory
        owner = _new_user_context(_OWNER)

        set_current_user_id(owner.user_id)
        try:
            async with factory() as db:
                row = await _audit.record(
                    db,
                    user_context=owner,
                    op="grantPermission",
                    principal_type="user",
                    principal_id="p1",
                    resource_type="agent",
                    resource_id="agent-trigger-neuter",
                    perm_bits=1,
                    acl_entry_ids=["a1"],
                )
                await db.commit()
                row_id = row.id
        finally:
            set_current_user_id(None)

        admin = create_engine(_ADMIN_URL, pool_pre_ping=True)
        try:
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{schema}"'))
                before = sorted(
                    conn.execute(
                        text(
                            "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger "
                            "WHERE tgrelid = 'interactions'::regclass "
                            "AND NOT tgisinternal"
                        )
                    ).all()
                )
            before_names = [row[0] for row in before]
            assert "interactions_append_only" in before_names

            # NEUTER — drop only the interactions trigger (016 creates a
            # SECOND one on tool_calls, untouched here).
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{schema}"'))
                conn.execute(
                    text("DROP TRIGGER interactions_append_only ON interactions")
                )

            set_current_user_id(owner.user_id)
            try:
                async with factory() as db:
                    await db.execute(
                        text(
                            "UPDATE interactions SET answer = 'tampered-while-neutered' "
                            "WHERE id = :id"
                        ),
                        {"id": row_id},
                    )
                    await db.commit()  # succeeds — the guard is gone
            finally:
                set_current_user_id(None)

            # RESTORE — the single CREATE TRIGGER statement (NOT
            # upgrade(), which would try to recreate BOTH triggers and
            # fail on the still-present tool_calls one).
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{schema}"'))
                conn.execute(
                    text(
                        "CREATE TRIGGER interactions_append_only "
                        "BEFORE UPDATE OR DELETE ON interactions "
                        "FOR EACH ROW EXECUTE FUNCTION audittrace_append_only()"
                    )
                )
                after = sorted(
                    conn.execute(
                        text(
                            "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger "
                            "WHERE tgrelid = 'interactions'::regclass "
                            "AND NOT tgisinternal"
                        )
                    ).all()
                )
            # Compare BOTH name AND full definition (pg_get_triggerdef) —
            # a name-only comparison would miss a restore that recreates
            # the trigger with a DIFFERENT timing/function/event and
            # still happens to reuse the same name.
            assert after == before, (
                "cmp-verified restore — pg_trigger name+definition must match exactly"
            )
        finally:
            admin.dispose()

        # The guard is re-enforced after restore.
        set_current_user_id(owner.user_id)
        try:
            with pytest.raises(DBAPIError, match="append-only"):
                async with factory() as db:
                    await db.execute(
                        text(
                            "UPDATE interactions SET answer = 'should-fail-again' "
                            "WHERE id = :id"
                        ),
                        {"id": row_id},
                    )
                    await db.commit()
        finally:
            set_current_user_id(None)


class TestCrossSubjectAuditReadIsImpossible:
    """§10.1 — PINS the limitation, never "proves" it as a control: a
    denial row stamped with subject A's ``user_id`` is invisible to
    subject B (INCLUDING an auditor) because per-subject RLS on
    ``interactions`` is the only boundary and there is no BYPASSRLS
    administrative plane (§10.2). Exactly ONE test, named so it can
    never be misread as a passing security check — "0 rows visible"
    here means "an auditor using the ordinary scope literally cannot
    reconstruct subject A's ACL denial", not "isolation works"."""

    async def test_this_pins_a_limitation_subject_b_sees_zero_of_subject_as_denial_rows(
        self, interactions_harness: _InteractionsHarness
    ) -> None:
        factory = interactions_harness.factory
        subject_a = _new_user_context(_OWNER)

        set_current_user_id(subject_a.user_id)
        try:
            with _wired_postgres_factory(factory):
                denial = await _audit.record_denial(
                    user_context=subject_a,
                    op="grantPermission",
                    principal_type="user",
                    principal_id="p1",
                    resource_type="agent",
                    resource_id="agent-subject-a",
                    perm_bits=1,
                    failure_class=_audit.FAILURE_CLASS_ACL_DENIED_POLICY,
                    predicate_or_attempted_row={"resource_id": "agent-subject-a"},
                )
        finally:
            set_current_user_id(None)

        # Subject A sees their own row.
        set_current_user_id(subject_a.user_id)
        try:
            async with factory() as db:
                own_count = (
                    await db.execute(
                        text("SELECT count(*) FROM interactions WHERE id = :id"),
                        {"id": denial.id},
                    )
                ).scalar_one()
        finally:
            set_current_user_id(None)
        assert own_count == 1

        # Subject B — an ordinary audittrace:audit-scoped subject, no
        # different from an auditor — sees ZERO of it. Not a control
        # being verified: a limitation being pinned (§10.1's exact
        # sentence belongs in the build record / PR body verbatim).
        set_current_user_id(_ATTACKER)
        try:
            async with factory() as db:
                other_count = (
                    await db.execute(
                        text("SELECT count(*) FROM interactions WHERE id = :id"),
                        {"id": denial.id},
                    )
                ).scalar_one()
        finally:
            set_current_user_id(None)
        assert other_count == 0


class TestForgedUserIdRefusedOnPostgres:
    """The Postgres half of the forged-identity guards. Two DISTINCT
    scenarios, each catching the forgery at a DIFFERENT layer (fix
    round 2 correction: a THIRD test used to live here claiming to
    exercise "the unbound-ContextVar forged case" — it is REMOVED from
    this class and reframed as what it actually is: an N4 fail-closed
    test, moved to ``TestAuditWriterRealPostgres`` — see that class'
    ``test_n4_unbound_ambient_identity_refuses_any_subject_forged_or_not``.
    With the ContextVar unbound, RLS refuses ANY subject, forged or
    legitimate; there is nothing forged-identity-SPECIFIC about that
    refusal, so naming it as an F1 test was misleading):

    1. ``test_record_denial_refuses_a_forged_user_context_when_ambient_
       identity_bound`` — the ambient ContextVar IS bound to the real
       caller; a forged ``UserContext`` passed THROUGH the writer's own
       API is refused by the APP-level cross-check
       (``console_store.resolve_user_sub``) BEFORE any session opens —
       RLS is never even reached. THIS is the forged-identity-specific
       guard: it is the mismatch between the bound identity and the
       forged one that trips it, not merely the absence of an identity.
    2. ``test_a_forged_user_id_bypassing_the_writer_is_refused_by_rls`` —
       the writer's API is bypassed ENTIRELY with a raw INSERT (no
       ``resolve_user_sub`` call happens at all, by construction); RLS is
       the ONLY layer in play here, unconditionally. The aiosqlite
       counterpart in ``tests/test_console_acl_audit_writer.py`` shows
       this SAME raw INSERT succeeds silently there (no RLS on aiosqlite)."""

    async def test_record_denial_refuses_a_forged_user_context_when_ambient_identity_bound(
        self, interactions_harness: _InteractionsHarness
    ) -> None:
        factory = interactions_harness.factory
        forged = _new_user_context("attacker-forged-subject-pg")
        set_current_user_id(_OWNER)  # ambient identity: the REAL caller
        try:
            with _wired_postgres_factory(factory):
                with pytest.raises(
                    ConsoleStoreScopeError, match="disagrees with the RLS"
                ):
                    await _audit.record_denial(
                        user_context=forged,  # mismatched vs. the bound _OWNER
                        op="grantPermission",
                        principal_type="user",
                        principal_id="p1",
                        resource_type="agent",
                        resource_id="agent-forged-pg",
                        perm_bits=1,
                        failure_class=_audit.FAILURE_CLASS_ACL_DENIED_POLICY,
                        predicate_or_attempted_row={},
                    )
        finally:
            set_current_user_id(None)

        async with factory() as fresh:
            count = (
                await fresh.execute(
                    text(
                        "SELECT count(*) FROM interactions "
                        "WHERE user_id = 'attacker-forged-subject-pg'"
                    )
                )
            ).scalar_one()
            assert count == 0, "the forged write must never land"

    async def test_a_forged_user_id_bypassing_the_writer_is_refused_by_rls(
        self, interactions_harness: _InteractionsHarness
    ) -> None:
        factory = interactions_harness.factory
        set_current_user_id(_OWNER)
        try:
            with pytest.raises(DBAPIError, match="row-level security"):
                async with factory() as db:
                    await db.execute(
                        text(
                            "INSERT INTO interactions "
                            "(project, source, question, answer, prompt_tokens, "
                            "completion_tokens, timestamp, user_id, status, "
                            "event_class) "
                            "VALUES ('console-acl', 'console-acl', 'forged', '{}', "
                            "0, 0, '2026-09-25T00:00:00+00:00', :forged_user_id, "
                            "'success', 'acl_authz')"
                        ),
                        {"forged_user_id": _ATTACKER},
                    )
                    await db.commit()
        finally:
            set_current_user_id(None)
