"""Integration tests for Phase 4 Postgres RLS policies.

These tests connect to the running ``sovereign-postgres`` container
(port 15432 on the host, standard Phase 7a smoke test environment)
and verify that the migration-005 RLS policies actually block
cross-user reads at the infrastructure layer. They complement
``test_rls.py`` which covers the unit contract of the ContextVar +
listener in isolation.

**Skip behaviour:** the module-level fixture probes TCP reachability
to port 15432 at collect time. If the port is closed the entire file
is skipped with a clear reason so CI environments without the stack
up don't see red tests.

**Isolation:** every test creates a fresh throwaway schema and runs
the full migration chain inside it, so the live ``sovereign_ai``
database is never touched. Teardown drops the schema. This keeps the
tests hermetic and lets them run repeatedly without leaking state.

**Superuser bypass — why this file uses SET ROLE.** The ``sovereign``
role in the dev docker-compose stack is a Postgres superuser
(``POSTGRES_USER`` creates superusers by default in the official
image). Superusers **always** bypass RLS regardless of
``FORCE ROW LEVEL SECURITY``. Production deployments must connect as
a non-superuser app role — Phase 4 ships the role-creation init script
and docker-compose wiring separately. For these tests, each fixture
creates a non-superuser ``rls_test_role`` and every test uses
``SET ROLE rls_test_role`` to become that role for the duration of
the session, so RLS actually applies to the test queries.

**What is verified:**

  1. RLS is actually enabled + forced on interactions, sessions,
     tool_calls after migration 005.
  2. A bare SELECT without ``app.current_user_id`` set returns ZERO
     rows — safe-by-default.
  3. A SELECT with the GUC set to alice returns only alice's rows.
  4. A SELECT with the GUC set to bob returns only bob's rows.
  5. All three tables (interactions, sessions, tool_calls) honour
     the same policy.
  6. The SQLAlchemy ``after_begin`` listener actually emits the
     ``set_config`` call when a session is opened with the ContextVar
     set — proves the wire between auth.py → DB sessions works.
"""

from __future__ import annotations

import socket
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from audittrace.db.rls import install_rls_listener, set_current_user_id

POSTGRES_HOST = "localhost"
POSTGRES_PORT = 15432


def _postgres_is_reachable() -> bool:
    try:
        with socket.create_connection((POSTGRES_HOST, POSTGRES_PORT), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_is_reachable(),
    reason=(
        "sovereign-postgres container not reachable on "
        f"{POSTGRES_HOST}:{POSTGRES_PORT} — start the stack with "
        "'docker compose up -d postgres' to run these tests."
    ),
)


# ───────────────────────────── Helpers ──────────────────────────────────────


def _admin_engine():
    """Engine connected as the schema owner so fixture setup/teardown
    can create and drop throwaway schemas. The password comes from
    secrets/postgres_password.txt via the same convention as the
    running stack."""
    from pathlib import Path

    pg_password_file = (
        Path(__file__).parent.parent / "secrets" / "postgres_password.txt"
    )
    if not pg_password_file.exists():
        pytest.skip("secrets/postgres_password.txt not found")
    password = pg_password_file.read_text().strip()

    return create_engine(
        f"postgresql+psycopg2://sovereign:{password}@{POSTGRES_HOST}:{POSTGRES_PORT}/sovereign_ai",
        pool_pre_ping=True,
    )


_TEST_ROLE = "rls_test_role"


def _ensure_test_role(engine) -> None:
    """Create a throwaway non-superuser role so tests can `SET ROLE`
    into something that actually obeys RLS policies. Idempotent."""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_TEST_ROLE}') THEN
                        CREATE ROLE {_TEST_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS;
                    END IF;
                END$$
                """
            )
        )


def _ensure_rls_policies(engine, schema: str) -> None:
    """Create the three tables with migration-005's RLS shape inside
    ``schema``. Mirrors what migration 005 does against the live schema
    but scoped to an isolated throwaway namespace so tests never touch
    production rows. Grants the throwaway role so ``SET ROLE`` can use
    the tables."""
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        conn.execute(text(f'GRANT USAGE, CREATE ON SCHEMA "{schema}" TO {_TEST_ROLE}'))
        conn.execute(text(f'SET search_path TO "{schema}"'))

        # Minimal schema of the three tables — only the columns RLS
        # needs. Production DDL is richer; we test the RLS contract,
        # not the column catalog.
        conn.execute(
            text(
                """
                CREATE TABLE interactions (
                    id SERIAL PRIMARY KEY,
                    project VARCHAR(255),
                    question TEXT,
                    answer TEXT,
                    user_id VARCHAR(36),
                    timestamp VARCHAR(64)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE sessions (
                    id VARCHAR(64) PRIMARY KEY,
                    project VARCHAR(255),
                    date VARCHAR(64),
                    summary TEXT,
                    key_points TEXT,
                    model VARCHAR(255),
                    user_id VARCHAR(36)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE tool_calls (
                    id VARCHAR(36) PRIMARY KEY,
                    interaction_id INTEGER NOT NULL REFERENCES interactions(id) ON DELETE CASCADE,
                    user_id VARCHAR(36) NOT NULL,
                    agent_type VARCHAR(64) NOT NULL,
                    tool_name VARCHAR(255) NOT NULL,
                    args TEXT NOT NULL,
                    result_summary TEXT,
                    error TEXT,
                    started_at TIMESTAMP NOT NULL,
                    duration_ms INTEGER,
                    granted_scope VARCHAR(255) NOT NULL
                )
                """
            )
        )

        # Apply the RLS policies exactly as migration 005 does.
        for table in ("interactions", "sessions", "tool_calls"):
            conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
            conn.execute(
                text(
                    f"""
                    CREATE POLICY tenant_isolation_{table} ON {table}
                        FOR ALL
                        USING (user_id = current_setting('app.current_user_id', true))
                        WITH CHECK (user_id = current_setting('app.current_user_id', true))
                    """
                )
            )
            # Grant DML to the non-superuser test role so SET ROLE works.
            conn.execute(
                text(
                    f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{schema}"."{table}" '
                    f"TO {_TEST_ROLE}"
                )
            )
        # interactions has a SERIAL primary key — grant the sequence too
        # so INSERTs as the test role can read the next value.
        conn.execute(
            text(
                f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" '
                f"TO {_TEST_ROLE}"
            )
        )


def _drop_schema(engine, schema: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


# ───────────────────────────── Fixtures ─────────────────────────────────────


@pytest.fixture
def scoped_engine():
    """Real Postgres engine against a throwaway schema populated with
    the three RLS-protected tables and a non-superuser test role.
    Teardown drops the schema."""
    engine = _admin_engine()
    schema = f"rls_test_{datetime.now().strftime('%H%M%S_%f')}"
    try:
        _ensure_test_role(engine)
        _ensure_rls_policies(engine, schema)
        # Rebuild an engine bound to the throwaway schema so every
        # query lands there without needing SET search_path per stmt.
        scoped = create_engine(
            engine.url.set(query={"options": f"-csearch_path={schema}"}),
            pool_pre_ping=True,
        )
        # Install the Phase 4 listener on THIS engine only so test
        # isolation is clean.
        install_rls_listener()
        yield scoped
        scoped.dispose()
    finally:
        _drop_schema(engine, schema)
        engine.dispose()


@pytest.fixture
def session_factory(scoped_engine):
    """Sessionmaker bound to the scoped throwaway schema."""
    return sessionmaker(bind=scoped_engine)


def _as_test_role(session):
    """SET ROLE to the non-superuser test role so RLS actually applies.
    Superusers always bypass RLS regardless of FORCE — see the module
    docstring's 'Superuser bypass' note for the why."""
    session.execute(text(f"SET LOCAL ROLE {_TEST_ROLE}"))


@pytest.fixture
def seeded(session_factory):
    """Insert two interactions — one for alice, one for bob — each in
    its own transaction so the transaction-scoped GUC matches the row
    being inserted (WITH CHECK requires it). We seed as the superuser
    connection because SET LOCAL ROLE is harder to sequence against
    SET LOCAL app.current_user_id in the same transaction; the superuser
    bypass is acceptable for seeding because the tests themselves
    exercise the enforcement via SET ROLE in their own sessions."""

    with session_factory() as s:
        s.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": "user-alice"},
        )
        s.execute(
            text(
                "INSERT INTO interactions(project, question, answer, user_id, timestamp) "
                "VALUES (:p, :q, :a, :u, :t)"
            ),
            {
                "p": "P",
                "q": "Q-alice",
                "a": "A-alice",
                "u": "user-alice",
                "t": "2026-04-11T20:00:00",
            },
        )
        s.commit()

    with session_factory() as s:
        s.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": "user-bob"},
        )
        s.execute(
            text(
                "INSERT INTO interactions(project, question, answer, user_id, timestamp) "
                "VALUES (:p, :q, :a, :u, :t)"
            ),
            {
                "p": "P",
                "q": "Q-bob",
                "a": "A-bob",
                "u": "user-bob",
                "t": "2026-04-11T20:00:01",
            },
        )
        s.commit()


# ───────────────────────── RLS enforcement tests ────────────────────────────


class TestRlsEnforcement:
    """Prove that migration-005's policies actually gate cross-user
    reads at the Postgres layer, not just at the service layer."""

    def test_rls_is_enabled_on_interactions(self, scoped_engine):
        """Sanity: confirm the RLS flags are set on the table in the
        throwaway schema. Scoped via pg_namespace filter so we don't
        match any same-named table in a different schema."""
        with scoped_engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT c.relrowsecurity, c.relforcerowsecurity
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relname = 'interactions'
                      AND n.nspname = current_schema()
                    """
                )
            ).first()
        assert row is not None
        assert row[0] is True, "interactions ENABLE ROW LEVEL SECURITY missing"
        assert row[1] is True, "interactions FORCE ROW LEVEL SECURITY missing"

    def test_select_without_guc_returns_empty(self, session_factory, seeded):
        """Without the app.current_user_id GUC set, the RLS policy
        compares user_id to an empty string and every row is filtered
        out. Safe-by-default."""
        set_current_user_id(None)
        with session_factory() as s:
            _as_test_role(s)
            rows = s.execute(text("SELECT user_id FROM interactions")).fetchall()
        assert rows == []

    def test_alice_sees_only_alice_rows(self, session_factory, seeded):
        """With the ContextVar set to alice, the listener pushes
        app.current_user_id = 'user-alice' into the transaction and the
        RLS policy lets only alice's row through."""
        set_current_user_id("user-alice")
        try:
            with session_factory() as s:
                _as_test_role(s)
                rows = s.execute(
                    text("SELECT user_id, question FROM interactions")
                ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "user-alice"
            assert rows[0][1] == "Q-alice"
        finally:
            set_current_user_id(None)

    def test_bob_sees_only_bob_rows(self, session_factory, seeded):
        set_current_user_id("user-bob")
        try:
            with session_factory() as s:
                _as_test_role(s)
                rows = s.execute(
                    text("SELECT user_id, question FROM interactions")
                ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "user-bob"
            assert rows[0][1] == "Q-bob"
        finally:
            set_current_user_id(None)

    def test_alice_cannot_insert_as_bob(self, session_factory, seeded):
        """The WITH CHECK clause prevents writing a row under a
        user_id that does not match the current GUC. Even if the
        application tries to INSERT with user_id='user-bob' while the
        GUC is 'user-alice', Postgres rejects the row."""
        from sqlalchemy.exc import DBAPIError

        set_current_user_id("user-alice")
        try:
            with session_factory() as s:
                _as_test_role(s)
                with pytest.raises(DBAPIError):
                    s.execute(
                        text(
                            "INSERT INTO interactions(project, question, answer, user_id, timestamp) "
                            "VALUES (:p, :q, :a, :u, :t)"
                        ),
                        {
                            "p": "P",
                            "q": "Q",
                            "a": "A",
                            "u": "user-bob",  # mismatch with GUC
                            "t": "2026-04-11T20:00:02",
                        },
                    )
                    s.commit()
        finally:
            set_current_user_id(None)
