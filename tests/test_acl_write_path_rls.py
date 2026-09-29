"""REAL-Postgres proof for the sovereign ACL WRITE path — **ACL
2b-core-A1** (``2026-09-26-SPEC-acl-2b-core-A-write-path.md`` + ADDENDA
U/V/W/X, spec §8). A NEW file, per spec's explicit instruction (§2 —
``tests/test_acl_ownership_rls.py`` is frozen for A: additions-only, and
A adds NOTHING to it). Imports the harness helpers from
``tests.test_acl_ownership_rls`` and WU-2a's/B's constants — precedented
by ``tests/test_mcp_rls_isolation.py:78`` importing from
``tests.test_rls_isolation``.

**What this file proves that the mock/aiosqlite unit file
(``tests/test_console_acl_write_path.py``) cannot:**

* The abort-shape derivation (spec §0/§6, ADDENDA U-2/V-1/V-2/X-1) — the
  measured ``(rollback events at catch, pg_stat_activity.state)`` pair,
  and the further-rollback split after the caller's own ``await
  db.rollback()``.
* O-3's real Postgres unique index (aiosqlite cannot enforce
  ``NULLS NOT DISTINCT`` — spec §4's disclosed gap).
* Q-4's no-delete trigger (Postgres-only DDL).
* R-8's constraint-redundancy finding (``_postgres_write.py``'s module
  docstring) — MEASURED here, on real Postgres, not assumed.
* The savepoint pin (SP) — a connection-event assertion that no write
  path in this module ever opens a nested transaction.
* P-4 — the DEFENSIVE proof that the shape-2 helper (``_write_denial``
  called from inside an active ``begin_nested()``) survives even though
  A's own code never produces that shape.

**SQLAlchemy / asyncpg / aiosqlite versions of THIS build's venv**
(recorded once, per spec §2 Fact 2 / ADDENDUM U-7/V-5 — every mechanism
claim below carries this): see
``TestVenvVersionsRecorded::test_versions`` — it asserts each version
string is non-empty (so a broken/uninstalled package fails the run) and
PRINTS the literal values into this run's own output for the build
record to cite verbatim; it does NOT pin them to specific numbers (that
would make this file fail on every routine dependency bump), so a venv
resolving DIFFERENT versions does not fail this test — the build record
is where a version discrepancy must be checked and disclosed.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, event, insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from audittrace.db.models import Base, ConsoleAclEntry
from audittrace.db.rls import install_rls_listener, set_current_user_id
from audittrace.services.console_acl import AclGrantOp, _audit, _postgres_write
from audittrace.services.console_acl._errors import (
    AclBulkRolledBackError,
    AclPrincipalTypeRefused,
    AclWriteRefused,
)
from audittrace.services.console_acl._postgres import (
    PostgresConsoleAclEntriesService,
)
from audittrace.services.console_acl._postgres import (
    _not_expired_clause as _real_not_expired_clause,
)
from tests.test_acl_ownership_rls import (
    _ADMIN_URL,
    _APP_PASSWORD,
    _APP_ROLE,
    _ATTACKER,
    _INTERACTIONS_MIGRATION_FILES,
    _OWNER,
    _SKIP_REASON,
    _VIEWER,
    _app_session_factory,
    _drop_schema,
    _new_user_context,
    _owner_only_rls_ddl,
    _run_migration_upgrade,
    _seed_attacker_resources,
    _seed_owner_resources,
    _wired_postgres_factory,
)

pytestmark = pytest.mark.skipif(_ADMIN_URL is None, reason=_SKIP_REASON)

_OWNER_ONLY_TABLES = ("console_agents", "console_prompt_groups")


def _build_acl_write_schema() -> str:
    """A fresh throwaway schema holding EVERYTHING A1's write path
    touches: owner tables (``console_agents``/``console_prompt_groups``,
    WU-2a owner-only RLS) + the ten interactions migrations (016's
    append-only function + trigger) + **031 + 032 + 033** — 033 is
    applied ONLY here, never against ``app_factory_before``/
    ``app_factory_after`` (those run frozen tests that issue a raw
    ``DELETE FROM console_acl_entries``, which 033's trigger would now
    refuse — D-S A-FWD-2). Returns the schema name; the caller drops it.
    """
    assert _ADMIN_URL is not None
    admin = create_engine(_ADMIN_URL, pool_pre_ping=True)
    name = f"acl_2b_core_a1_{datetime.now().strftime('%H%M%S_%f')}"
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
        # Minimal stub targets for the interactions migrations' ALTERs
        # (same convention as _build_interactions_schema).
        conn.execute(text("CREATE TABLE sessions (id VARCHAR(36) PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE memory_items (id VARCHAR(36) PRIMARY KEY)"))
        for filename in _INTERACTIONS_MIGRATION_FILES:
            _run_migration_upgrade(conn, filename)
        _run_migration_upgrade(conn, "031_create_console_acl_entries.py")
        _run_migration_upgrade(conn, "032_tighten_console_acl_entries_ownership.py")
        _run_migration_upgrade(conn, "033_acl_active_grant_unique_and_no_delete.py")
        for table_name in (
            *_OWNER_ONLY_TABLES,
            "interactions",
            "tool_calls",
            "console_acl_entries",
        ):
            conn.execute(
                text(
                    f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{name}".{table_name} '
                    f"TO {_APP_ROLE}"
                )
            )
        conn.execute(
            text(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA "{name}" TO {_APP_ROLE}')
        )
    admin.dispose()
    return name


class _WriteHarness:
    def __init__(self, schema: str, factory: Any) -> None:
        self.schema = schema
        self.factory = factory
        self.service = PostgresConsoleAclEntriesService(session_factory=factory)


@pytest_asyncio.fixture
async def write_harness() -> Any:
    """**Deliberately TWO separate engines on the SAME schema** — one
    bound to ``write_harness.factory`` (the service's own session
    factory, and the ONE this file's rollback-event listeners attach
    to), a SECOND, independent one wired as ``postgres_factory`` for
    ``_audit.record_denial``'s own independent session. Measured
    (this build, SQLAlchemy 2.1.1): sharing ONE engine/pool between the
    two makes a THIRD, unrelated event appear on the shared listener —
    the connection pool's defensive ``ROLLBACK`` on CHECK-IN of
    ``record_denial``'s own (successfully committed) session, which
    fires the SAME ``"rollback"`` connection event a genuine abort
    does. That artefact would inflate every shape's rollback count by
    one and was caught BY this fixture's design, not assumed away —
    separating the two engines is the correct instrument: the shape
    pins in this file measure the CALLER's own connection, never a
    sibling session's pool housekeeping."""
    schema = _build_acl_write_schema()
    factory = _app_session_factory(schema)
    denial_factory = _app_session_factory(schema)
    install_rls_listener()
    try:
        await _seed_owner_resources(factory)
        with _wired_postgres_factory(denial_factory):
            yield _WriteHarness(schema=schema, factory=factory)
    finally:
        await factory.kw["bind"].dispose()
        await denial_factory.kw["bind"].dispose()
        _drop_schema(schema)


def _admin_engine() -> Any:
    assert _ADMIN_URL is not None
    return create_engine(_ADMIN_URL, pool_pre_ping=True)


def _interactions_rows(admin: Any, schema: str, *, resource_id: str) -> list[dict]:
    with admin.begin() as conn:
        conn.execute(text(f'SET search_path TO "{schema}"'))
        rows = (
            (
                conn.execute(
                    text(
                        "SELECT status, failure_class, answer, error_detail, "
                        "question, trace_id "
                        "FROM interactions WHERE question LIKE :pat"
                    ),
                    {"pat": f"%:{resource_id}%"},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def _backend_state(admin: Any, schema: str) -> str | None:
    """``pg_stat_activity.state`` for the harness's OWN backend — used
    for the (rollback events, backend state) pair (ADDENDUM X-1).

    **S5, disclosed limitation, not fully closed here:** this reads the
    most recently active ``client backend`` on the SAME database,
    excluding background workers (autovacuum, walwriter, ...) that
    would otherwise be mistaken for the harness's own connection — it
    does NOT filter by an exact PID (the harness's session never
    surfaces its own backend PID to this helper, and threading one
    through every call site was judged out of scope for a should-fix).
    Correct ONLY under this file's own single-connection-per-test
    discipline (one ``async with factory() as db:`` block active at a
    time) — NOT safe if this suite were ever run with parallel workers
    sharing one throwaway schema."""
    with admin.connect() as conn:
        row = conn.execute(
            text(
                "SELECT state FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND backend_type = 'client backend' "
                "AND state != 'idle' AND pid != pg_backend_pid() "
                "ORDER BY query_start DESC LIMIT 1"
            )
        ).first()
        if row is not None:
            return row[0]
        # Nothing "active" (non-idle) found — the backend already
        # settled to idle (shape 1's own connection-level rollback).
        row = conn.execute(
            text(
                "SELECT state FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND backend_type = 'client backend' AND pid != pg_backend_pid() "
                "ORDER BY query_start DESC LIMIT 1"
            )
        ).first()
        return row[0] if row is not None else None


# ── Documentation checks — the two greps (spec §11, ADDENDUM V-1/X-4) ───


class TestDocumentationGreps:
    def test_no_begin_nested_in_console_acl_package(self) -> None:
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [
                "grep",
                "-rn",
                "begin_nested",
                str(repo_root / "src" / "audittrace" / "services" / "console_acl"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.stdout == "", (
            f"begin_nested found in src/: {result.stdout!r} (spec §11)"
        )

    def test_no_text_in_the_write_core(self) -> None:
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [
                "grep",
                "-n",
                "text(",
                str(
                    repo_root
                    / "src"
                    / "audittrace"
                    / "services"
                    / "console_acl"
                    / "_postgres_write.py"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.stdout == "", (
            f"text( found in _postgres_write.py: {result.stdout!r} (ADDENDUM V-1 / X-4)"
        )


class TestVenvVersionsRecorded:
    def test_versions(self) -> None:
        import aiosqlite
        import asyncpg
        import sqlalchemy

        # Recorded, not asserted to a specific pin (the pin is
        # sqlalchemy>=2.0.25, unbounded — spec §2 Fact 2 / ADDENDUM V-5).
        # This test's PURPOSE is to make the versions appear in this
        # run's own output for the build record to cite verbatim.
        print(f"SQLAlchemy={sqlalchemy.__version__}")
        print(f"asyncpg={asyncpg.__version__}")
        print(f"aiosqlite={aiosqlite.__version__}")
        assert sqlalchemy.__version__
        assert asyncpg.__version__
        assert aiosqlite.__version__


class TestHarnessSmoke:
    async def test_grant_lands_and_is_effective(self, write_harness) -> None:
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            row = await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=3,
            )
            assert row["perm_bits"] == 3
        finally:
            set_current_user_id(None)


# ── SP — the savepoint pin (spec §6.2), autouse for every test that
# requests write_harness (skipped for tests that don't need Postgres) ──


@pytest_asyncio.fixture
async def sp_pin(write_harness: Any) -> Any:
    """SP — spec §6.2. Requested explicitly by every write-path test in
    this file (not literally ``autouse`` — an autouse fixture cannot
    itself depend on another async fixture without a nested-event-loop
    conflict under pytest-asyncio; every test that performs a write
    below takes this fixture as a parameter, which is the same
    protective effect)."""
    sync_engine = write_harness.factory.kw["bind"].sync_engine
    events: dict[str, list[bool]] = {"savepoint": [], "rollback_savepoint": []}

    def _on_savepoint(*_args: object) -> None:
        events["savepoint"].append(True)

    def _on_rollback_savepoint(*_args: object) -> None:
        events["rollback_savepoint"].append(True)

    event.listen(sync_engine, "savepoint", _on_savepoint)
    event.listen(sync_engine, "rollback_savepoint", _on_rollback_savepoint)
    try:
        yield events
    finally:
        event.remove(sync_engine, "savepoint", _on_savepoint)
        event.remove(sync_engine, "rollback_savepoint", _on_rollback_savepoint)
        assert events["savepoint"] == [], (
            "SP — a write path opened a savepoint (begin_nested somewhere "
            "in A's code, or a driver-level one) — spec §6.2"
        )
        assert events["rollback_savepoint"] == [], (
            "SP — a write path rolled back to a savepoint"
        )


def _rollback_listener(sync_engine: Any) -> tuple[list[bool], Any, Any]:
    events: list[bool] = []

    def _on_rollback(_conn: object) -> None:
        events.append(True)

    event.listen(sync_engine, "rollback", _on_rollback)
    return events, sync_engine, _on_rollback


class _RollbackSplitProbe:
    """Wraps ``AsyncSession.rollback`` (via ``monkeypatch``) to snapshot,
    for EVERY call the wrapped method makes to ``db.rollback()``:
    ``(rollback events fired BEFORE this call, backend state BEFORE this
    call, rollback events fired DURING/by this call)`` — i.e. the exact
    "at catch" / "further" split spec §6.2 / ADDENDUM X-1 describe.

    **This closes a REJECTED deviation.** A prior round of this file
    claimed the split "can never be observed" once instrumentation sits
    outside `grant_permission`/`bulk_write_acl_entries` (both call their
    OWN `await db.rollback()` internally before re-raising). That claim
    was FALSE — wrapping `AsyncSession.rollback` itself observes exactly
    the moment those internal calls happen, from OUTSIDE the method,
    with no code change to production. The independent reviewer
    measured this with the same ~15-line technique and reproduced the
    predicted pairs `(1, 'idle', 0)` (shape 1) and `(0, 'idle in
    transaction (aborted)', 1)` (shape 3) on real ``postgres:16``."""

    def __init__(
        self, harness: Any, admin: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.events: list[bool] = []
        self.snapshots: list[tuple[int, str | None, int]] = []
        self._admin = admin
        self._harness = harness
        sync_engine = harness.factory.kw["bind"].sync_engine
        self._sync_engine = sync_engine
        event.listen(sync_engine, "rollback", self._on_rollback)
        original_rollback = AsyncSession.rollback
        probe = self

        async def _wrapped_rollback(
            session: AsyncSession, *args: object, **kwargs: object
        ) -> Any:
            at_catch = len(probe.events)
            state = _backend_state(admin, harness.schema)
            result = await original_rollback(session, *args, **kwargs)
            probe.snapshots.append((at_catch, state, len(probe.events) - at_catch))
            return result

        monkeypatch.setattr(AsyncSession, "rollback", _wrapped_rollback)

    def _on_rollback(self, _connection: object) -> None:
        self.events.append(True)

    def close(self) -> None:
        event.remove(self._sync_engine, "rollback", self._on_rollback)


# ── The abort-shape derivation — shape (3): a refusal AT _expire_active
# (a Core-level UPDATE, no flush) ─────────────────────────────────────────


class TestShapeThreeExpireActiveRefusal:
    """The soft-deleted-resource scenario (spec §5.8 / P-3's fact, A1's
    own path via a re-grant since revoke_permission is A2): a PRE-
    EXISTING active row on the key, then the resource is soft-deleted,
    then a re-grant's ``_expire_active`` UPDATE touches that row and
    032's UPDATE ``WITH CHECK`` (``deleted_at_ms IS NULL``) refuses it.

    **Two levels of proof.** The first test below instruments
    ``_expire_active`` DIRECTLY (the PRIMITIVE level) to isolate the
    SQLAlchemy/driver mechanism from this module's own exception
    handling. The second test proves the SAME pair through the FULL
    ``grant_permission`` method, externally, via ``_RollbackSplitProbe``
    (a wrapper on ``AsyncSession.rollback`` — spec §6.2 / ADDENDUM X-1's
    split IS observable from outside the method; an earlier round of
    this file wrongly claimed otherwise, REJECTED on review and
    corrected here, matching the independent reviewer's own
    measurement)."""

    async def test_shape_at_the_primitive_level(self, write_harness, sp_pin) -> None:
        owner = _new_user_context(_OWNER)
        admin = _admin_engine()
        try:
            set_current_user_id(owner.user_id)
            await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
            )
            set_current_user_id(None)

            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(
                    text(
                        "UPDATE console_agents SET deleted_at_ms = 1 "
                        "WHERE agent_id = 'agent-1'"
                    )
                )

            sync_engine = write_harness.factory.kw["bind"].sync_engine
            events, _, listener = _rollback_listener(sync_engine)
            try:
                set_current_user_id(owner.user_id)
                async with write_harness.factory() as db:
                    with pytest.raises(DBAPIError, match="row-level security"):
                        await _postgres_write._expire_active(
                            db,
                            principal_type="user",
                            principal_id=_VIEWER,
                            resource_type="agent",
                            resource_id="agent-1",
                            tenant_id=None,
                            now_ms=2,
                        )
                    # AT CATCH — shape (3): zero rollback events, the
                    # backend is left "idle in transaction (aborted)".
                    assert events == [], "shape (3) fires no rollback event at catch"
                    state = _backend_state(admin, write_harness.schema)
                    assert state == "idle in transaction (aborted)"

                    await db.rollback()
                    # AFTER the explicit rollback — exactly ONE further
                    # event (ADDENDUM X-1's split).
                    assert events == [True], (
                        "shape (3) fires exactly one event after the "
                        "caller's own explicit db.rollback()"
                    )
            finally:
                set_current_user_id(None)
                event.remove(sync_engine, "rollback", listener)
        finally:
            admin.dispose()

    async def test_the_full_method_denies_by_name_and_leaves_no_acl_row(
        self, write_harness, sp_pin, monkeypatch
    ) -> None:
        owner = _new_user_context(_OWNER)
        admin = _admin_engine()
        try:
            agents = Base.metadata.tables["console_agents"]
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(
                    insert(agents).values(
                        id="agent-row-2",
                        agent_id="agent-2",
                        user_sub=_OWNER,
                        name="Agent Two",
                        created_at_ms=0,
                        updated_at_ms=0,
                    )
                )

            set_current_user_id(owner.user_id)
            await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-2",
                perm_bits=1,
            )
            set_current_user_id(None)

            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(
                    text(
                        "UPDATE console_agents SET deleted_at_ms = 1 "
                        "WHERE agent_id = 'agent-2'"
                    )
                )

            probe = _RollbackSplitProbe(write_harness, admin, monkeypatch)
            set_current_user_id(owner.user_id)
            try:
                with pytest.raises(AclWriteRefused) as excinfo:
                    await write_harness.service.grant_permission(
                        owner,
                        principal_type="user",
                        principal_id=_VIEWER,
                        resource_type="agent",
                        resource_id="agent-2",
                        perm_bits=2,
                    )
            finally:
                set_current_user_id(None)
                probe.close()

            # ADDENDUM X-1's Core-refusal pair, proven THROUGH the full
            # method (not the primitive above): zero events at catch,
            # backend left aborted, exactly one further event from the
            # method's OWN explicit db.rollback().
            assert probe.snapshots[-1] == (0, "idle in transaction (aborted)", 1)

            assert excinfo.value.failure_class == _audit.FAILURE_CLASS_ACL_DENIED_POLICY
            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-2"
            )
            denial_rows = [r for r in rows if r["status"] == "failed"]
            assert len(denial_rows) == 1

            viewer = _new_user_context(_VIEWER)
            set_current_user_id(viewer.user_id)
            try:
                effective = await write_harness.service.get_effective_permissions(
                    viewer, "agent", "agent-2"
                )
            finally:
                set_current_user_id(None)
            assert effective == 1, "only the ORIGINAL grant (bits=1) survives"
        finally:
            admin.dispose()


# ── Shape (1) — flush refusal, at the primitive level ────────────────────


class TestShapeOneFlushRefusal:
    """The first test below is an INSTRUMENT-SANITY probe — it calls no
    production code, only a raw ``db.add()``/``db.flush()`` against
    ``ConsoleAclEntry`` directly, to measure SQLAlchemy's OWN
    connection-level behaviour for a flush refusal in isolation from
    this package's exception handling (the independent reviewer's own
    characterisation, correct). The SECOND test is the real proof:
    spec §6.3(1)'s attacker-grant scenario (an attacker grants a
    permission naming the OWNER's resource; 032's INSERT ``WITH CHECK``
    refuses it) run through the FULL ``grant_permission`` method, with
    ``_RollbackSplitProbe`` proving the identical shape-(1) pair
    externally."""

    async def test_flush_refusal_pair_and_the_further_split(
        self, write_harness, sp_pin
    ) -> None:
        owner = _new_user_context(_OWNER)
        admin = _admin_engine()
        try:
            sync_engine = write_harness.factory.kw["bind"].sync_engine
            events, _, listener = _rollback_listener(sync_engine)
            try:
                set_current_user_id(owner.user_id)
                async with write_harness.factory() as db:
                    db.add(
                        ConsoleAclEntry(
                            id=str(uuid.uuid4()),
                            user_sub=owner.user_id,
                            principal_type="user",
                            principal_id=_VIEWER,
                            principal_model="User",
                            resource_type="agent",
                            resource_id="agent-1",
                            perm_bits=99,  # violates ck_..._perm_bits_range
                            granted_at_ms=0,
                            created_at_ms=0,
                            updated_at_ms=0,
                        )
                    )
                    with pytest.raises(DBAPIError):
                        await db.flush()
                    # AT CATCH — shape (1): ONE rollback event fires
                    # INSIDE the failing flush; the backend is "idle".
                    assert events == [True], (
                        "shape (1) fires exactly one event at catch"
                    )
                    state = _backend_state(admin, write_harness.schema)
                    assert state == "idle"

                    await db.rollback()
                    # AFTER the explicit rollback — NO further event
                    # (nothing left to roll back at connection level).
                    assert events == [True], (
                        "shape (1) fires NO further event after the "
                        "caller's own explicit db.rollback()"
                    )
            finally:
                set_current_user_id(None)
                event.remove(sync_engine, "rollback", listener)
        finally:
            admin.dispose()

    async def test_full_method_insert_refusal_split_attacker_grant(
        self, write_harness, sp_pin, monkeypatch
    ) -> None:
        """spec §6.3(1): the attacker grants on the OWNER's agent — 032's
        INSERT ``WITH CHECK`` (ownership subquery) refuses it. Proven
        THROUGH ``grant_permission`` (never A's own code opening a
        savepoint — ``sp_pin`` still applies), with the split-probe
        showing the identical shape-(1) pair the primitive-level test
        above measures in isolation."""
        attacker = _new_user_context(_ATTACKER)
        admin = _admin_engine()
        try:
            probe = _RollbackSplitProbe(write_harness, admin, monkeypatch)
            set_current_user_id(attacker.user_id)
            try:
                with pytest.raises(AclWriteRefused) as excinfo:
                    await write_harness.service.grant_permission(
                        attacker,
                        principal_type="user",
                        principal_id=_VIEWER,
                        resource_type="agent",
                        resource_id="agent-1",  # OWNER's resource
                        perm_bits=1,
                    )
            finally:
                set_current_user_id(None)
                probe.close()

            assert probe.snapshots[-1] == (1, "idle", 0), (
                "shape (1): one event at catch, backend idle, no further "
                "event after the method's own explicit rollback"
            )
            assert excinfo.value.failure_class == _audit.FAILURE_CLASS_ACL_DENIED_POLICY
            # B5 — pinned on the EMITTED row: measured on real postgres:16,
            # the RLS refusal's SQLSTATE is insufficient_privilege (42501),
            # matching spec §5.6's "SQLSTATE when present" rule — never
            # type(exc).__name__ ("ProgrammingError").
            assert excinfo.value.db_error_class == "42501"
            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-1"
            )
            denial_rows = [r for r in rows if r["status"] == "failed"]
            assert len(denial_rows) == 1
            detail = json.loads(denial_rows[0]["error_detail"])
            assert detail["db_error_class"] == "42501"
        finally:
            admin.dispose()


# ── #9a — O-3's real Postgres unique index ───────────────────────────────


class TestO3ActiveGrantIndex:
    async def test_duplicate_active_grant_is_refused_and_the_index_restores(
        self, write_harness, sp_pin
    ) -> None:
        owner = _new_user_context(_OWNER)
        admin = _admin_engine()
        try:
            set_current_user_id(owner.user_id)
            await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
            )
            set_current_user_id(None)

            def _indexdef() -> str | None:
                with admin.begin() as conn:
                    conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                    row = conn.execute(
                        text(
                            "SELECT indexdef FROM pg_indexes WHERE "
                            "indexname = 'uq_console_acl_entries_active_grant'"
                        )
                    ).first()
                return row[0] if row else None

            before = _indexdef()
            assert before is not None

            # NEUTER — raw INSERT bypassing the service, as the app
            # role, duplicating the exact same active key.
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(text(f"SET ROLE {_APP_ROLE}"))
                conn.execute(
                    text("SELECT set_config('app.current_user_id', :uid, false)"),
                    {"uid": _OWNER},
                )
                with pytest.raises(
                    IntegrityError, match="uq_console_acl_entries_active_grant"
                ):
                    conn.execute(
                        text(
                            "INSERT INTO console_acl_entries "
                            "(id, user_sub, principal_type, principal_id, "
                            "principal_model, resource_type, resource_id, "
                            "perm_bits, granted_at_ms, created_at_ms, updated_at_ms) "
                            "VALUES (:id, :owner, 'user', :viewer, 'User', "
                            "'agent', 'agent-1', 2, 0, 0, 0)"
                        ),
                        {"id": str(uuid.uuid4()), "owner": _OWNER, "viewer": _VIEWER},
                    )
            # a failed statement leaves the transaction aborted; disposing
            # the pool forces a FRESH connection (and role) for what follows
            # — simpler and more robust than rolling back mid-block.
            admin.dispose()

            # DROP the index (admin, schema-local).
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(text("DROP INDEX uq_console_acl_entries_active_grant"))

            # The SAME duplicate now LANDS — RED.
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(text(f"SET ROLE {_APP_ROLE}"))
                conn.execute(
                    text("SELECT set_config('app.current_user_id', :uid, false)"),
                    {"uid": _OWNER},
                )
                conn.execute(
                    text(
                        "INSERT INTO console_acl_entries "
                        "(id, user_sub, principal_type, principal_id, "
                        "principal_model, resource_type, resource_id, "
                        "perm_bits, granted_at_ms, created_at_ms, updated_at_ms) "
                        "VALUES (:id, :owner, 'user', :viewer, 'User', "
                        "'agent', 'agent-1', 4, 0, 0, 0)"
                    ),
                    {"id": str(uuid.uuid4()), "owner": _OWNER, "viewer": _VIEWER},
                )
            admin.dispose()

            # Clean up the duplicate the neuter let land (033's own
            # append-only trigger refuses a DELETE by the app role, but
            # the admin/superuser role bypasses RLS + the trigger check
            # is BEFORE DELETE only for the app role's own attempt —
            # actually the trigger fires for ANY role; use the admin
            # connection's superuser bypass of RLS is irrelevant here,
            # the trigger still fires — so instead just expire it,
            # which is what a real caller would do, and is sufficient
            # for the restored index to accept the remaining row.
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(
                    text(
                        "UPDATE console_acl_entries SET expired_at_ms = 1 "
                        "WHERE perm_bits = 4 AND resource_id = 'agent-1'"
                    )
                )

            # RESTORE — recreate the index via the migration's own
            # upgrade() (idempotent for JUST this index — 033 also
            # tries the trigger, which already exists; guard for that).
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX uq_console_acl_entries_active_grant "
                        "ON console_acl_entries "
                        "(principal_type, principal_id, resource_type, resource_id, tenant_id) "
                        "NULLS NOT DISTINCT WHERE (expired_at_ms IS NULL)"
                    )
                )
            after = _indexdef()
            assert after == before, (
                "cmp-verified restore — pg_indexes definition must match"
            )
        finally:
            admin.dispose()


# ── #9b — duplicate grant, then a raw-UPDATE expiry kills the bit ────────


class TestDuplicateGrantThenExpireKillsBit:
    async def test_expiring_the_first_grant_removes_its_bit(
        self, write_harness, sp_pin
    ) -> None:
        owner = _new_user_context(_OWNER)
        viewer = _new_user_context(_VIEWER)
        admin = _admin_engine()
        try:
            set_current_user_id(owner.user_id)
            first = await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
            )
            set_current_user_id(None)

            # O-6 — the SAME grant_permission call already expired
            # "first" and inserted a NEW row (never two ACTIVE rows for
            # one key — that is what O-3 enforces). Simulate a
            # "duplicate active grant" scenario for THIS neuter's own
            # premise (#9b) by expiring the row via a RAW UPDATE (A2's
            # revoke_permission not being in scope) rather than a second
            # service-level grant, which would itself re-run expire.
            set_current_user_id(viewer.user_id)
            try:
                effective_before = (
                    await write_harness.service.get_effective_permissions(
                        viewer, "agent", "agent-1"
                    )
                )
            finally:
                set_current_user_id(None)
            assert effective_before == 1

            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(text(f"SET ROLE {_APP_ROLE}"))
                conn.execute(
                    text("SELECT set_config('app.current_user_id', :uid, false)"),
                    {"uid": _OWNER},
                )
                conn.execute(
                    text(
                        "UPDATE console_acl_entries SET expired_at_ms = 999999999999 "
                        "WHERE id = :id"
                    ),
                    {"id": first["id"]},
                )

            set_current_user_id(viewer.user_id)
            try:
                effective_after = await write_harness.service.get_effective_permissions(
                    viewer, "agent", "agent-1"
                )
            finally:
                set_current_user_id(None)
            assert effective_after == 0, (
                "expiring the sole active grant must remove the bit entirely"
            )
        finally:
            admin.dispose()

    async def _double_grant_downgrade(
        self, write_harness: Any, admin: Any, *, resource_id: str, tenant_id: str | None
    ) -> None:
        """A GENUINE double grant through the service (B2 — the reviewer's
        finding: no test in this file ever called ``grant_permission``
        twice), downgrading 15 -> 1, with the given ``tenant_id``.
        Asserts the OLD row is expired (direct SELECT — O-3's key is
        scoped by ``tenant_id`` too) and the new row's bits are exact."""
        owner = _new_user_context(_OWNER)
        viewer = _new_user_context(_VIEWER)
        set_current_user_id(owner.user_id)
        try:
            first = await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id=resource_id,
                perm_bits=15,
                tenant_id=tenant_id,
            )
            second = await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id=resource_id,
                perm_bits=1,
                tenant_id=tenant_id,
            )
        finally:
            set_current_user_id(None)
        assert second["id"] != first["id"]

        with admin.begin() as conn:
            conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
            old_expired_at_ms = conn.execute(
                text("SELECT expired_at_ms FROM console_acl_entries WHERE id = :id"),
                {"id": first["id"]},
            ).scalar_one()
        assert old_expired_at_ms is not None, (
            f"the OLD (bits=15) row must be expired (tenant_id={tenant_id!r})"
        )

        set_current_user_id(viewer.user_id)
        try:
            effective = await write_harness.service.get_effective_permissions(
                viewer, "agent", resource_id
            )
        finally:
            set_current_user_id(None)
        assert effective == 1, (
            f"a leftover active bits=15 row would make this 15 "
            f"(tenant_id={tenant_id!r})"
        )

    async def test_double_grant_downgrade_through_the_service_tenant_id_null(
        self, write_harness, sp_pin
    ) -> None:
        admin = _admin_engine()
        try:
            await self._double_grant_downgrade(
                write_harness, admin, resource_id="agent-1", tenant_id=None
            )
        finally:
            admin.dispose()

    async def test_double_grant_downgrade_through_the_service_tenant_id_set(
        self, write_harness, sp_pin
    ) -> None:
        admin = _admin_engine()
        try:
            await self._double_grant_downgrade(
                write_harness,
                admin,
                resource_id="agent-1",
                tenant_id="tenant-double-grant",
            )
        finally:
            admin.dispose()


# ── #11 — R-8, group principals refused at the DB — WITH the measured
# constraint-redundancy finding (see _postgres_write.py's module
# docstring): dropping ONE of the two relevant CHECK constraints alone
# does NOT let a group row land, because the OTHER one independently
# refuses it too. Both solo-neutered stay GREEN; the JOINT neuter goes
# RED. This is the honest, MEASURED shape of #11 on this schema — not
# the single-constraint neuter spec §9 literally describes.


class TestR8GroupPrincipalRefused:
    def _constraint_def(self, admin: Any, schema: str, name: str) -> str | None:
        with admin.begin() as conn:
            conn.execute(text(f'SET search_path TO "{schema}"'))
            row = conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = :name"
                ),
                {"name": name},
            ).first()
        return row[0] if row else None

    def _attempt_group_insert(self, admin: Any, schema: str) -> None:
        with admin.begin() as conn:
            conn.execute(text(f'SET search_path TO "{schema}"'))
            conn.execute(text(f"SET ROLE {_APP_ROLE}"))
            conn.execute(
                text("SELECT set_config('app.current_user_id', :uid, false)"),
                {"uid": _OWNER},
            )
            conn.execute(
                text(
                    "INSERT INTO console_acl_entries "
                    "(id, user_sub, principal_type, principal_id, "
                    "principal_model, resource_type, resource_id, "
                    "perm_bits, granted_at_ms, created_at_ms, updated_at_ms) "
                    "VALUES (:id, :owner, 'group', 'grp-1', 'Group', "
                    "'agent', 'agent-1', 1, 0, 0, 0)"
                ),
                {"id": str(uuid.uuid4()), "owner": _OWNER},
            )

    async def test_solo_neuter_of_either_constraint_alone_stays_green(
        self, write_harness, sp_pin
    ) -> None:
        """MEASURED (this build): dropping ``ck_console_acl_entries_
        principal_type`` alone is refused by the SIBLING
        ``ck_console_acl_entries_principal_model_matches_type``; dropping
        THAT one alone is refused by ``ck_console_acl_entries_principal_
        type``. Neither solo-neuter reproduces a landed group row — a
        finding recorded here per spec §9's own rule, not silently
        treated as "the guard is redundant with itself"."""
        admin = _admin_engine()
        try:
            for dropped, other in (
                (
                    "ck_console_acl_entries_principal_type",
                    "ck_console_acl_entries_principal_model_matches_type",
                ),
                (
                    "ck_console_acl_entries_principal_model_matches_type",
                    "ck_console_acl_entries_principal_type",
                ),
            ):
                with admin.begin() as conn:
                    conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                    conn.execute(
                        text(
                            f"ALTER TABLE console_acl_entries DROP CONSTRAINT {dropped}"
                        )
                    )
                admin.dispose()

                with pytest.raises(IntegrityError, match=other):
                    self._attempt_group_insert(admin, write_harness.schema)
                admin.dispose()

                # restore for the next iteration / the joint-neuter test
                def_sql = {
                    "ck_console_acl_entries_principal_type": (
                        "principal_type IN ('user', 'public', 'role')"
                    ),
                    "ck_console_acl_entries_principal_model_matches_type": (
                        "(principal_type = 'user' AND principal_model = 'User') OR "
                        "(principal_type = 'role' AND principal_model = 'Role') OR "
                        "(principal_type = 'public' AND principal_model IS NULL)"
                    ),
                }[dropped]
                with admin.begin() as conn:
                    conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                    conn.execute(
                        text(
                            f"ALTER TABLE console_acl_entries "
                            f"ADD CONSTRAINT {dropped} CHECK ({def_sql})"
                        )
                    )
                admin.dispose()
        finally:
            admin.dispose()

    async def test_service_refuses_group_principal_on_real_pg(
        self, write_harness, sp_pin
    ) -> None:
        """B4 — the guard proven THROUGH the service, not a raw INSERT
        with a hand-set ``principal_model='Group'`` (the service always
        sends ``principal_model=NULL`` for an unrecognised type —
        ``_principal_model``'s documented fallback). Asserts the error
        class, ``failure_class``, ``db_error_class``, that no ACL row
        landed, and exactly one denial row."""
        owner = _new_user_context(_OWNER)
        admin = _admin_engine()
        try:
            set_current_user_id(owner.user_id)
            try:
                with pytest.raises(AclPrincipalTypeRefused) as excinfo:
                    await write_harness.service.grant_permission(
                        owner,
                        principal_type="group",
                        principal_id="grp-service",
                        resource_type="agent",
                        resource_id="agent-1",
                        perm_bits=1,
                    )
            finally:
                set_current_user_id(None)

            assert (
                excinfo.value.failure_class
                == _audit.FAILURE_CLASS_ACL_DENIED_PRINCIPAL_TYPE
            )
            assert excinfo.value.db_error_class in (
                "ck_console_acl_entries_principal_type",
                "ck_console_acl_entries_principal_model_matches_type",
            )

            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                count = conn.execute(
                    text(
                        "SELECT count(*) FROM console_acl_entries "
                        "WHERE principal_type = 'group'"
                    )
                ).scalar_one()
            assert count == 0, "no ACL row must land"

            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-1"
            )
            denial_rows = [
                r
                for r in rows
                if r["failure_class"] == _audit.FAILURE_CLASS_ACL_DENIED_PRINCIPAL_TYPE
                and "group:grp-service" in r["question"]
            ]
            assert len(denial_rows) == 1
        finally:
            admin.dispose()

    async def test_joint_neuter_of_both_constraints_lands_the_group_row(
        self, write_harness, sp_pin
    ) -> None:
        """The joint neuter is proven THROUGH the service too (B4): once
        the constraints are gone, ``grant_permission(principal_type=
        'group', ...)`` — the SAME call
        ``test_service_refuses_group_principal_on_real_pg`` proves is
        refused above — now SUCCEEDS. That is what "the joint neuter
        must turn the guard's own test red" means; demonstrated here by
        running the identical call under the neutered schema.

        **A THIRD constraint, measured while wiring this test to the
        SERVICE rather than a raw INSERT:** the service always sends
        ``principal_model=None`` for an unrecognised ``principal_type``
        (``_principal_model``'s documented fallback — spec 5.3 forbids
        an app-level pre-check, so the value is whatever falls out of
        the dispatch table). A non-``public`` row with a NULL
        ``principal_model`` ALSO violates
        ``ck_console_acl_entries_public_principal_null`` (its second
        OR-branch requires ``principal_model IS NOT NULL`` for any
        non-public type) — independently of the other two. This
        constraint was invisible to the raw-INSERT probe above (which
        hand-sets ``principal_model='Group'``, a non-NULL value,
        satisfying it) — a different call SHAPE exposes a different
        member of the redundant set. All three are dropped/restored
        here, cmp-verified."""
        admin = _admin_engine()
        try:
            constraint_names = (
                "ck_console_acl_entries_principal_type",
                "ck_console_acl_entries_principal_model_matches_type",
                "ck_console_acl_entries_public_principal_null",
            )
            before = {
                name: self._constraint_def(admin, write_harness.schema, name)
                for name in constraint_names
            }
            assert all(before.values())

            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                for name in constraint_names:
                    conn.execute(
                        text(f"ALTER TABLE console_acl_entries DROP CONSTRAINT {name}")
                    )
            admin.dispose()

            # RED — proven THROUGH the service (B4): the SAME call that
            # raised AclPrincipalTypeRefused above now succeeds instead.
            owner = _new_user_context(_OWNER)
            set_current_user_id(owner.user_id)
            try:
                row = await write_harness.service.grant_permission(
                    owner,
                    principal_type="group",
                    principal_id="grp-joint-neuter",
                    resource_type="agent",
                    resource_id="agent-1",
                    perm_bits=1,
                )
            finally:
                set_current_user_id(None)
            assert row["principal_type"] == "group", (
                "the joint neuter lets the service's own grant_permission "
                "land a group-principal row — this is the guard's own "
                "test going RED, not a raw-INSERT bypass"
            )
            admin.dispose()

            # 033's no-delete trigger refuses ANY delete (any role) —
            # temporarily drop it too so the neuter-created row can be
            # cleaned up before the CHECK constraints are restored
            # (restoring a CHECK with an existing violating row would
            # itself fail); recreate the trigger immediately after.
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(
                    text(
                        "DROP TRIGGER console_acl_entries_no_delete "
                        "ON console_acl_entries"
                    )
                )
                conn.execute(
                    text(
                        "DELETE FROM console_acl_entries WHERE principal_type = 'group'"
                    )
                )
                conn.execute(
                    text(
                        "CREATE TRIGGER console_acl_entries_no_delete "
                        "BEFORE DELETE ON console_acl_entries "
                        "FOR EACH ROW EXECUTE FUNCTION audittrace_append_only()"
                    )
                )
            admin.dispose()

            # RESTORE all three, cmp-verified.
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(
                    text(
                        "ALTER TABLE console_acl_entries ADD CONSTRAINT "
                        "ck_console_acl_entries_principal_type "
                        "CHECK (principal_type IN ('user', 'public', 'role'))"
                    )
                )
                conn.execute(
                    text(
                        "ALTER TABLE console_acl_entries ADD CONSTRAINT "
                        "ck_console_acl_entries_principal_model_matches_type CHECK ("
                        "(principal_type = 'user' AND principal_model = 'User') OR "
                        "(principal_type = 'role' AND principal_model = 'Role') OR "
                        "(principal_type = 'public' AND principal_model IS NULL))"
                    )
                )
                conn.execute(
                    text(
                        "ALTER TABLE console_acl_entries ADD CONSTRAINT "
                        "ck_console_acl_entries_public_principal_null CHECK ("
                        "(principal_type = 'public' AND principal_id IS NULL "
                        "AND principal_model IS NULL) OR "
                        "(principal_type != 'public' AND principal_id IS NOT NULL "
                        "AND principal_model IS NOT NULL))"
                    )
                )
            admin.dispose()

            after = {
                name: self._constraint_def(admin, write_harness.schema, name)
                for name in constraint_names
            }
            assert after == before
        finally:
            admin.dispose()


# ── #13b — the no-delete trigger (Q-4) ───────────────────────────────────


class TestNoDeleteTriggerNeuter:
    async def test_trigger_refuses_delete_then_neuter_then_restore(
        self, write_harness, sp_pin
    ) -> None:
        owner = _new_user_context(_OWNER)
        admin = _admin_engine()
        try:
            set_current_user_id(owner.user_id)
            row = await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
            )
            set_current_user_id(None)

            # As the OWNER (who owns this row), a real DELETE is refused
            # by the trigger — never reaches 032's owner-only DELETE
            # policy at all.
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(text(f"SET ROLE {_APP_ROLE}"))
                conn.execute(
                    text("SELECT set_config('app.current_user_id', :uid, false)"),
                    {"uid": _OWNER},
                )
                with pytest.raises(DBAPIError, match="append-only"):
                    conn.execute(
                        text("DELETE FROM console_acl_entries WHERE id = :id"),
                        {"id": row["id"]},
                    )
            admin.dispose()

            def _trigger_def() -> tuple[str, str] | None:
                with admin.begin() as conn:
                    conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                    r = conn.execute(
                        text(
                            "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger "
                            "WHERE tgrelid = 'console_acl_entries'::regclass "
                            "AND NOT tgisinternal"
                        )
                    ).first()
                return tuple(r) if r else None

            before = _trigger_def()
            assert before is not None

            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(
                    text(
                        "DROP TRIGGER console_acl_entries_no_delete ON console_acl_entries"
                    )
                )
            admin.dispose()

            # RED — the DELETE now succeeds.
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(text(f"SET ROLE {_APP_ROLE}"))
                conn.execute(
                    text("SELECT set_config('app.current_user_id', :uid, false)"),
                    {"uid": _OWNER},
                )
                conn.execute(
                    text("DELETE FROM console_acl_entries WHERE id = :id"),
                    {"id": row["id"]},
                )
            admin.dispose()

            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(
                    text(
                        "CREATE TRIGGER console_acl_entries_no_delete "
                        "BEFORE DELETE ON console_acl_entries "
                        "FOR EACH ROW EXECUTE FUNCTION audittrace_append_only()"
                    )
                )
            admin.dispose()
            after = _trigger_def()
            assert after == before, (
                "cmp-verified restore — pg_trigger must match exactly"
            )
        finally:
            admin.dispose()


# ── #10 — bulk all-or-nothing, op_index EXACT for both refusal shapes ───


class TestBulkOpIndexVariants:
    async def test_variant_a_insert_refusal_flush_pair(
        self, write_harness, sp_pin, monkeypatch
    ) -> None:
        """Variant (a): op 1 (of 3) refuses at its OWN flush (shape 1) —
        an unmapped resource_type means _expire_active matches nothing,
        so the INSERT itself is what 032's WITH CHECK refuses (its
        ownership subquery evaluates FALSE for any unmapped
        resource_type). Asserts the FULL ADDENDUM X-1 pair (at-catch +
        further) via ``_RollbackSplitProbe``, not merely a total count."""
        owner = _new_user_context(_OWNER)
        admin = _admin_engine()
        try:
            probe = _RollbackSplitProbe(write_harness, admin, monkeypatch)
            ops = [
                AclGrantOp(
                    principal_type="user",
                    principal_id=_VIEWER,
                    resource_type="agent",
                    resource_id="agent-1",
                    perm_bits=1,
                ),
                AclGrantOp(
                    principal_type="user",
                    principal_id=_VIEWER,
                    resource_type="mcpServer",  # unmapped -> WITH CHECK FALSE
                    resource_id="mcp-1",
                    perm_bits=1,
                ),
                AclGrantOp(
                    principal_type="user",
                    principal_id=_VIEWER,
                    resource_type="agent",
                    resource_id="agent-3",
                    perm_bits=1,
                ),
            ]
            try:
                set_current_user_id(owner.user_id)
                with pytest.raises(AclBulkRolledBackError):
                    await write_harness.service.bulk_write_acl_entries(owner, ops)
            finally:
                set_current_user_id(None)
                probe.close()

            assert probe.snapshots[-1] == (1, "idle", 0), (
                "flush-refusal pair: one event at catch, idle, no further"
            )
            rows = _interactions_rows(admin, write_harness.schema, resource_id="mcp-1")
            denial = [r for r in rows if r["status"] == "failed"][0]
            detail = json.loads(denial["error_detail"])
            assert detail["predicate_or_attempted_row"]["op_index"] == 1
            # B5 — bulk's db_error_class follows §5.6's "as above" rule
            # (SQLSTATE when present) even though failure_class is
            # overridden to acl_denied_bulk_rollback; measured 42501.
            assert detail["db_error_class"] == "42501"

            for resource_id in ("agent-1", "agent-3"):
                rows = _interactions_rows(
                    admin, write_harness.schema, resource_id=resource_id
                )
                assert [r for r in rows if r["status"] == "success"] == []
        finally:
            admin.dispose()

    async def test_variant_b_expire_refusal_core_pair(
        self, write_harness, sp_pin, monkeypatch
    ) -> None:
        """Variant (b): op 1 (of 2) refuses at its OWN _expire_active
        UPDATE (shape 3) — the resource is soft-deleted, so re-granting
        the same key hits 032's UPDATE WITH CHECK. Asserts the FULL
        ADDENDUM X-1 Core-refusal pair via ``_RollbackSplitProbe``."""
        owner = _new_user_context(_OWNER)
        admin = _admin_engine()
        try:
            set_current_user_id(owner.user_id)
            await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
            )
            set_current_user_id(None)
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(
                    text(
                        "UPDATE console_agents SET deleted_at_ms = 1 "
                        "WHERE agent_id = 'agent-1'"
                    )
                )

            probe = _RollbackSplitProbe(write_harness, admin, monkeypatch)
            ops = [
                AclGrantOp(
                    principal_type="user",
                    principal_id=_VIEWER,
                    resource_type="agent",
                    resource_id="agent-1",  # soft-deleted -> UPDATE refused
                    perm_bits=2,
                ),
            ]
            try:
                set_current_user_id(owner.user_id)
                with pytest.raises(AclBulkRolledBackError):
                    await write_harness.service.bulk_write_acl_entries(owner, ops)
            finally:
                set_current_user_id(None)
                probe.close()

            assert probe.snapshots[-1] == (0, "idle in transaction (aborted)", 1), (
                "Core-refusal pair: zero events at catch, aborted, one further"
            )

            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-1"
            )
            denial = [r for r in rows if r["status"] == "failed"][-1]
            detail = json.loads(denial["error_detail"])
            assert detail["predicate_or_attempted_row"]["op_index"] == 0
        finally:
            admin.dispose()


class TestAFBulkFlushNeuter:
    """AF — deleting the per-op flush makes op_index off-by-one for
    variant (a) only (V-2/W-2's ruling: variant (b) is insensitive by
    construction — a GREEN there is not a finding)."""

    async def test_deleting_the_per_op_flush_breaks_variant_a_only(
        self, write_harness, sp_pin, monkeypatch
    ) -> None:
        owner = _new_user_context(_OWNER)
        admin = _admin_engine()
        try:
            # NEUTER — patch AsyncSession.flush to a no-op FOR THE
            # DURATION of this bulk call only, so op i's own refusal no
            # longer surfaces inside iteration i (relies on the NEXT
            # op's autoflush instead, which is off by one).
            original_flush = AsyncSession.flush

            async def _noop_flush(self: Any, *args: object, **kwargs: object) -> None:
                return None

            monkeypatch.setattr(AsyncSession, "flush", _noop_flush)

            ops = [
                AclGrantOp(
                    principal_type="user",
                    principal_id=_VIEWER,
                    resource_type="agent",
                    resource_id="agent-1",
                    perm_bits=1,
                ),
                AclGrantOp(
                    principal_type="user",
                    principal_id=_VIEWER,
                    resource_type="mcpServer",
                    resource_id="mcp-1",
                    perm_bits=1,
                ),
                AclGrantOp(
                    principal_type="user",
                    principal_id=_VIEWER,
                    resource_type="agent",
                    resource_id="agent-3",
                    perm_bits=1,
                ),
            ]
            try:
                set_current_user_id(owner.user_id)
                with pytest.raises(AclBulkRolledBackError):
                    await write_harness.service.bulk_write_acl_entries(owner, ops)
            finally:
                set_current_user_id(None)

            monkeypatch.setattr(AsyncSession, "flush", original_flush)

            # Without the per-op flush, op 1's (mcp-1) pending INSERT is
            # only discovered by op 2's (agent-3) autoflush — so the
            # denial row is attributed to op INDEX 2 (agent-3's own
            # question text), not op 1's resource — that reassignment IS
            # the off-by-one this neuter demonstrates.
            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-3"
            )
            denial = [r for r in rows if r["status"] == "failed"][0]
            detail = json.loads(denial["error_detail"])
            assert detail["predicate_or_attempted_row"]["op_index"] == 2, (
                "RED — without the per-op flush, op 1's refusal surfaces "
                "one iteration late (during op 2's autoflush)"
            )
        finally:
            admin.dispose()


# ── S-2 — user_sub / granted_by are token-derived; PG additionally
# refuses a forged identity at the RLS layer ──────────────────────────────


class TestS2TokenDerivedIdentity:
    async def test_forged_user_context_is_refused_by_rls(
        self, write_harness, sp_pin
    ) -> None:
        """No parameter on grant_permission's signature can supply
        granted_by/user_sub — the structural half of S-2. This test
        exercises the OTHER half: a caller passing a forged UserContext
        whose ``user_id`` disagrees with the ambient RLS identity is
        refused at the database (migration 031/032's WITH CHECK), never
        silently accepted."""
        from audittrace.services.console_store import ConsoleStoreScopeError

        real_owner_id = _OWNER
        forged = _new_user_context(_ATTACKER)
        set_current_user_id(real_owner_id)  # ambient identity: OWNER
        try:
            # Refused at the APPLICATION layer (build_write_stamp's
            # resolve_user_sub cross-check), BEFORE any I/O — faster
            # and stronger than a DB round-trip; the DB-level RLS
            # WITH CHECK is the fallback for the case this application
            # check cannot run (the ambient ContextVar unbound —
            # non-request code only).
            with pytest.raises(ConsoleStoreScopeError):
                await write_harness.service.grant_permission(
                    forged,  # claims to be ATTACKER
                    principal_type="user",
                    principal_id=_VIEWER,
                    resource_type="agent",
                    resource_id="agent-1",
                    perm_bits=1,
                )
        finally:
            set_current_user_id(None)

        # The ACL row must never have landed under the forged identity.
        set_current_user_id(real_owner_id)
        try:
            effective = await write_harness.service.get_effective_permissions(
                _new_user_context(_VIEWER), "agent", "agent-1"
            )
        finally:
            set_current_user_id(None)
        assert effective == 0


# ── T — trace_id link, on REAL Postgres ──────────────────────────────────


class TestTraceIdLinkRealPostgres:
    async def test_acl_row_and_audit_row_share_a_trace_id(
        self, write_harness, sp_pin
    ) -> None:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        owner = _new_user_context(_OWNER)
        admin = _admin_engine()
        try:
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
            tracer = provider.get_tracer("test")
            set_current_user_id(owner.user_id)
            try:
                with tracer.start_as_current_span("grant"):
                    span = trace.get_current_span()
                    trace_id_hex = format(span.get_span_context().trace_id, "032x")
                    row = await write_harness.service.grant_permission(
                        owner,
                        principal_type="user",
                        principal_id=_VIEWER,
                        resource_type="agent",
                        resource_id="agent-1",
                        perm_bits=1,
                    )
            finally:
                set_current_user_id(None)

            assert len(trace_id_hex) == 32
            int(trace_id_hex, 16)
            assert row["trace_id"] == trace_id_hex

            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-1"
            )
            success_rows = [r for r in rows if r["status"] == "success"]
            assert len(success_rows) == 1
            # B3 — the load-bearing assertion a prior round never made:
            # read interactions.trace_id itself and match it, not merely
            # count rows (which stays green even with trace_id=None).
            assert success_rows[0]["trace_id"] is not None
            assert success_rows[0]["trace_id"] == trace_id_hex == row["trace_id"]
        finally:
            admin.dispose()


# ── P-4 — the shape-(2) helper survives (DEFENSIVE proof; A never
# produces this shape) ────────────────────────────────────────────────────


class TestP4ShapeTwoHelperSurvives:
    async def test_denial_helper_survives_a_savepoint_scoped_refusal(
        self, write_harness
    ) -> None:
        """Deliberately does NOT request ``sp_pin`` — THIS test is the
        one place a savepoint is expected (the defensive proof of the
        helper, not of A's own code, which never opens one)."""
        owner = _new_user_context(_OWNER)
        admin = _admin_engine()
        sync_engine = write_harness.factory.kw["bind"].sync_engine
        sp_events: dict[str, list[bool]] = {
            "savepoint": [],
            "rollback_savepoint": [],
        }
        rb_events: list[bool] = []

        def _on_savepoint(c: object, n: object) -> None:
            sp_events["savepoint"].append(True)

        def _on_rollback_savepoint(c: object, n: object, ctx: object) -> None:
            sp_events["rollback_savepoint"].append(True)

        def _on_rollback(c: object) -> None:
            rb_events.append(True)

        event.listen(sync_engine, "savepoint", _on_savepoint)
        event.listen(sync_engine, "rollback_savepoint", _on_rollback_savepoint)
        event.listen(sync_engine, "rollback", _on_rollback)
        try:
            set_current_user_id(owner.user_id)

            async with write_harness.factory() as db:
                db.add(
                    ConsoleAclEntry(
                        id=str(uuid.uuid4()),
                        user_sub=owner.user_id,
                        principal_type="user",
                        principal_id=_VIEWER,
                        principal_model="User",
                        resource_type="agent",
                        resource_id="agent-1",
                        perm_bits=1,
                        granted_at_ms=0,
                        created_at_ms=0,
                        updated_at_ms=0,
                    )
                )
                await db.flush()

                async with db.begin_nested():
                    db.add(
                        ConsoleAclEntry(
                            id=str(uuid.uuid4()),
                            user_sub=owner.user_id,
                            principal_type="user",
                            principal_id=_VIEWER,
                            principal_model="User",
                            resource_type="agent",
                            resource_id="agent-p4-refused",
                            perm_bits=99,  # violates perm_bits_range CHECK
                            granted_at_ms=0,
                            created_at_ms=0,
                            updated_at_ms=0,
                        )
                    )
                    with pytest.raises(DBAPIError):
                        await db.flush()
                    denial = await _postgres_write._write_denial(
                        user_context=owner,
                        op="grantPermission",
                        principal_type="user",
                        principal_id=_VIEWER,
                        resource_type="agent",
                        resource_id="agent-p4-refused",
                        perm_bits=99,
                        attempted={},
                        exc=RuntimeError("simulated"),
                    )
                    assert isinstance(denial, AclWriteRefused)

                assert sp_events["rollback_savepoint"] == [True]
                assert rb_events == [], "the OUTER transaction is not rolled back"
                state = _backend_state(admin, write_harness.schema)
                assert state == "idle in transaction"

                await db.commit()  # the outer, legitimate INSERT commits

            # S2 — the denial row must ACTUALLY have landed (_write_denial
            # was called directly above, not silently); read it back from
            # a fresh session, on the denial_factory's own schema.
            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-p4-refused"
            )
            denial_rows = [r for r in rows if r["status"] == "failed"]
            assert len(denial_rows) == 1

            viewer = _new_user_context(_VIEWER)
            set_current_user_id(viewer.user_id)
            effective = await write_harness.service.get_effective_permissions(
                viewer, "agent", "agent-1"
            )
            assert effective == 1, "the outer grant must still commit"
        finally:
            set_current_user_id(None)
            event.remove(sync_engine, "savepoint", _on_savepoint)
            event.remove(sync_engine, "rollback_savepoint", _on_rollback_savepoint)
            event.remove(sync_engine, "rollback", _on_rollback)
            admin.dispose()


# ── Y-T1 — a time-limited grant is superseded by a re-grant, proven
# through the READ path (spec Y-3/ADDENDUM AF-1(1)). The clock-seeded
# grid (tests/test_acl_write_path_lapsed_clock.py) asserts through the
# EMITTED rows only — never through get_effective_permissions/
# has_permission, the actual authorization decision ADDENDUM Y-0
# measured as silently wrong. Neither instrument makes the other
# redundant (ADDENDUM AE-3). ──────────────────────────────────────────


async def _row_expired_at_ms(factory: Any, row_id: str) -> int | None:
    async with factory() as session:
        result = await session.execute(
            select(ConsoleAclEntry.expired_at_ms).where(ConsoleAclEntry.id == row_id)
        )
        return result.scalar_one()


class TestYT1TimeLimitedGrantSupersededThroughTheReadPath:
    async def test_downgrade_over_a_still_future_predecessor_is_effective(
        self, write_harness: Any, sp_pin: Any
    ) -> None:
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            before = await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=15,
                expired_at_ms=int(time.time() * 1000) + 3_600_000,
            )
            after = await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
            )
            assert after["id"] != before["id"]
        finally:
            set_current_user_id(None)

        set_current_user_id(_VIEWER)
        try:
            viewer = _new_user_context(_VIEWER)
            effective = await write_harness.service.get_effective_permissions(
                viewer, "agent", "agent-1"
            )
            has_bit8 = await write_harness.service.has_permission(
                viewer, "agent", "agent-1", 8
            )
        finally:
            set_current_user_id(None)

        assert effective == 1, (
            "RED under the pre-Y-1 predicate: a still-future predecessor "
            "(bits=15) would OR into the effective mask and make this 15"
        )
        assert has_bit8 is False

        set_current_user_id(owner.user_id)
        try:
            old_expired_at_ms = await _row_expired_at_ms(
                write_harness.factory, before["id"]
            )
        finally:
            set_current_user_id(None)
        assert old_expired_at_ms is not None, (
            "the OLD row must be superseded (expired_at_ms set), not left "
            "at its originally scheduled future value"
        )
        assert old_expired_at_ms <= int(time.time() * 1000), (
            "superseded AT the moment of the re-grant, strictly before its "
            "originally scheduled expiry"
        )

    async def test_y_t1n_downgrade_over_a_null_expiry_predecessor_is_effective(
        self, write_harness: Any, sp_pin: Any
    ) -> None:
        """Y-T1n — the NULL-predecessor sibling, real-Postgres half. Kept
        alongside Y-T1/Y-T2 (spec Y-3's three-row requirement) — stays
        GREEN under the pre-Y-1 predicate, which is the point: this
        sibling alone cannot see ADDENDUM Y-0's defect."""
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            before = await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=15,
            )
            after = await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
            )
            assert after["id"] != before["id"]
        finally:
            set_current_user_id(None)

        set_current_user_id(_VIEWER)
        try:
            viewer = _new_user_context(_VIEWER)
            effective = await write_harness.service.get_effective_permissions(
                viewer, "agent", "agent-1"
            )
            has_bit8 = await write_harness.service.has_permission(
                viewer, "agent", "agent-1", 8
            )
        finally:
            set_current_user_id(None)
        assert effective == 1
        assert has_bit8 is False

        set_current_user_id(owner.user_id)
        try:
            old_expired_at_ms = await _row_expired_at_ms(
                write_harness.factory, before["id"]
            )
        finally:
            set_current_user_id(None)
        assert old_expired_at_ms is not None

    async def test_y_t2_bulk_downgrade_over_a_still_future_predecessor_is_effective(
        self, write_harness: Any, sp_pin: Any
    ) -> None:
        """Y-T2 — Y-T1's BULK-site sibling, real-Postgres half: one
        ``AclGrantOp`` of 1 over a direct grant of 15 with a still-future
        expiry, same key."""
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            before = await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=15,
                expired_at_ms=int(time.time() * 1000) + 3_600_000,
            )
            result = await write_harness.service.bulk_write_acl_entries(
                owner,
                [
                    AclGrantOp(
                        principal_type="user",
                        principal_id=_VIEWER,
                        resource_type="agent",
                        resource_id="agent-1",
                        perm_bits=1,
                    )
                ],
            )
            assert result["acl_entry_ids"][0] != before["id"]
        finally:
            set_current_user_id(None)

        set_current_user_id(_VIEWER)
        try:
            viewer = _new_user_context(_VIEWER)
            effective = await write_harness.service.get_effective_permissions(
                viewer, "agent", "agent-1"
            )
            has_bit8 = await write_harness.service.has_permission(
                viewer, "agent", "agent-1", 8
            )
        finally:
            set_current_user_id(None)
        assert effective == 1, (
            "RED under the pre-Y-1 predicate: a still-future predecessor "
            "would OR into the effective mask and make this 15"
        )
        assert has_bit8 is False

        set_current_user_id(owner.user_id)
        try:
            old_expired_at_ms = await _row_expired_at_ms(
                write_harness.factory, before["id"]
            )
        finally:
            set_current_user_id(None)
        assert old_expired_at_ms is not None
        assert old_expired_at_ms <= int(time.time() * 1000), (
            "superseded AT the bulk write, strictly before its originally "
            "scheduled expiry (Y-3: Y-T2 asserts this the same way Y-T1 does)"
        )


# ── Y-T3 — Z-1's third mock site: the grant path's audit-failure
# restore must recover a time-limited predecessor's TRUE prior state,
# not a hard-coded None (ADDENDUM Z-1/AA-2/AB-3). The Postgres half is
# the PARITY reference — no code fix was needed here (a rolled-back
# transaction naturally restores the prior row), measured directly. ──


class TestYT3AuditFailureRestoresTruePriorState:
    async def test_postgres_rollback_restores_the_scheduled_future_expiry(
        self, write_harness: Any, sp_pin: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            scheduled = int(time.time() * 1000) + 3_600_000
            predecessor = await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=15,
                expired_at_ms=scheduled,
            )
            before_updated_at_ms = await _row_updated_at_ms(
                write_harness.factory, predecessor["id"]
            )

            async def _broken_record(*_args: object, **_kwargs: object) -> None:
                raise RuntimeError("simulated N4 audit-write failure")

            monkeypatch.setattr(_audit, "record", _broken_record)
            with pytest.raises(RuntimeError):
                await write_harness.service.grant_permission(
                    owner,
                    principal_type="user",
                    principal_id=_VIEWER,
                    resource_type="agent",
                    resource_id="agent-1",
                    perm_bits=1,
                )
            monkeypatch.undo()

            after_expired_at_ms = await _row_expired_at_ms(
                write_harness.factory, predecessor["id"]
            )
            after_updated_at_ms = await _row_updated_at_ms(
                write_harness.factory, predecessor["id"]
            )
        finally:
            set_current_user_id(None)

        assert after_expired_at_ms == scheduled, (
            "RED (Y-T3's target): the predecessor's ORIGINAL scheduled "
            "expiry must survive the rolled-back re-grant unchanged"
        )
        assert after_updated_at_ms == before_updated_at_ms


async def _row_updated_at_ms(factory: Any, row_id: str) -> int:
    async with factory() as session:
        result = await session.execute(
            select(ConsoleAclEntry.updated_at_ms).where(ConsoleAclEntry.id == row_id)
        )
        return result.scalar_one()


# ── AB-G / AB-G+ — kept as DRIFT-DETECTORS ONLY (ADDENDUM AD-1(2)),
# never as the correctness guard (that is AC-T-HIST above). AB-G is a
# grep-based documentation check; AB-G+ is the positive call-site pin —
# both are enumerable and known to be defeatable (ADDENDUM AC-1/AD-1),
# which is exactly why neither gates on its own. ──────────────────────


class TestABGGrepDriftDetector:
    def test_no_literal_is_none_spelling_remains_in_the_write_modules(self) -> None:
        """AB-G (ADDENDUM AB-1(2), relabelled a documentation check by
        ADDENDUM AC-1 item 4): a divergent COPY of the active predicate
        is caught by this grep — but NOT a copy that discards the
        derived call's result (ADDENDUM AC-1's e1) or one that widens it
        with a threshold/grace (ADDENDUM AF's e2k/eG) — hence
        drift-detector, not correctness guard."""
        import re
        from pathlib import Path

        pattern = re.compile(r"expired_at_ms\.is_\(None\)|\.expired_at_ms is None")
        base = Path("src/audittrace/services/console_acl")
        hits = {
            str(path): len(pattern.findall(path.read_text()))
            for path in (base / "_postgres_write.py", base / "_mock_write.py")
        }
        assert sum(hits.values()) == 0, hits


class TestABGPlusDriftDetector:
    """AB-G+ (ADDENDUM AC-1): a POSITIVE call-site pin — every exercised
    expire site must call THROUGH the read path's own active predicate,
    checked by identity (mock) or by a recording spy at the reference
    point (Postgres), never by what the compiled SQL happens to read
    (ADDENDUM AC-1's own escape, v3, shows a re-spelled wrapper compiles
    to identical SQL while failing this check)."""

    def test_mock_write_module_binds_the_same_function_object(self) -> None:
        """The lazy accessor (``_not_expired_fn``, not a module-level
        name — a plain top-level import reproduces the pinned
        pre-commit mypy hook's cold-cache cycle, MEASURED this round)
        must still return the VERY object ``_mock`` binds — a
        retrieval, not a wrapper."""
        from audittrace.services.console_acl import _mock, _mock_write

        assert _mock_write._not_expired_fn() is _mock._mock_not_expired

    async def test_postgres_expire_active_direct_call_records_the_sentinel(
        self, write_harness: Any, sp_pin: Any, monkeypatch: Any
    ) -> None:
        calls: list[int] = []
        real_clause = _real_not_expired_clause

        def _spy(now_ms: int) -> Any:
            calls.append(now_ms)
            return real_clause(now_ms)

        monkeypatch.setattr(
            "audittrace.services.console_acl._postgres._not_expired_clause", _spy
        )
        set_current_user_id(_OWNER)
        try:
            async with write_harness.factory() as db:
                await _postgres_write._expire_active(
                    db,
                    principal_type="user",
                    principal_id=_VIEWER,
                    resource_type="agent",
                    resource_id="agent-1",
                    tenant_id=None,
                    now_ms=424_242,
                )
                await db.commit()
        finally:
            set_current_user_id(None)
        assert calls == [424_242]

    async def test_postgres_grant_and_bulk_sites_record_the_write_stamps_now_ms(
        self, write_harness: Any, sp_pin: Any, monkeypatch: Any
    ) -> None:
        calls: list[int] = []
        real_clause = _real_not_expired_clause

        def _spy(now_ms: int) -> Any:
            calls.append(now_ms)
            return real_clause(now_ms)

        monkeypatch.setattr(
            "audittrace.services.console_acl._postgres._not_expired_clause", _spy
        )
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            # Distinct principals for the two sites — sharing one key
            # would let the bulk site's own _expire_active supersede the
            # grant site's row, rewriting the very stamp this test reads
            # back afterwards.
            grant_row = await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
            )
            bulk_result = await write_harness.service.bulk_write_acl_entries(
                owner,
                [
                    AclGrantOp(
                        principal_type="user",
                        principal_id=_ATTACKER,
                        resource_type="agent",
                        resource_id="agent-1",
                        perm_bits=1,
                    )
                ],
            )
            assert len(calls) >= 2, "grant and bulk must each call through the spy"
            grant_stamp = await _row_stamp_now_ms(
                write_harness.factory, grant_row["id"]
            )
            bulk_stamp = await _row_stamp_now_ms(
                write_harness.factory, bulk_result["acl_entry_ids"][0]
            )
        finally:
            set_current_user_id(None)

        assert grant_stamp in calls
        assert bulk_stamp in calls

    def test_active_clause_delegates_only_return_identity(
        self, monkeypatch: Any
    ) -> None:
        """The wrapper-legitimacy check (ADDENDUM AC-1 item 3): with a
        spy standing in for ``_not_expired_clause``, ``_active_clause``
        must record exactly ``[n]`` and return the VERY object the spy
        returned — not an equal-looking rebuild of it. A wrapper that
        post-processes, re-spells or re-``or_``s the clause fails this
        even when its compiled SQL still reads identically (ADDENDUM
        AC-1's ``v3`` escape)."""
        calls: list[int] = []
        sentinel = object()

        def _spy(now_ms: int) -> Any:
            calls.append(now_ms)
            return sentinel

        monkeypatch.setattr(
            "audittrace.services.console_acl._postgres._not_expired_clause", _spy
        )
        result = _postgres_write._active_clause(999)
        assert calls == [999]
        assert result is sentinel


async def _row_stamp_now_ms(factory: Any, row_id: str) -> int:
    """The write stamp's ``now_ms`` for a given row IS its
    ``updated_at_ms`` (both the grant and bulk sites set
    ``created_at_ms=updated_at_ms=stamp.now_ms`` on the NEW row) —
    ADDENDUM AC-1 item 1(ii)'s membership check."""
    return await _row_updated_at_ms(factory, row_id)


# ═══════════════════════════════════════════════════════════════════════
# ACL 2b-core-A2 — real-Postgres-only proofs: caller-scoped delete
# (P-1/P-1b/P-1b-F), the sole-owner consequence (P-2), the O-7
# interaction on revoke (P-3), the multi-predicate rollback (D-A2-4r),
# and the three should-fix items from the spec gate (SF-A/B/C).
# ═══════════════════════════════════════════════════════════════════════

# A separate, standalone import (not merged into the top-of-file block
# above) — this file is additions-only (spec §2): the top import block
# is an EXISTING line this WU never touches.
from audittrace.services.console_acl import MAX_PERM_BITS  # noqa: E402


async def _acl_row_pair(admin: Any, schema: str, row_id: str) -> tuple[int | None, int]:
    """``(expired_at_ms, updated_at_ms)`` for a SPECIFIC row, read as the
    schema ADMIN (bypasses RLS entirely — the only reliable way to read
    a row this test's own caller may not currently be authorized to
    see, e.g. an attacker's or a different owner's row)."""
    with admin.begin() as conn:
        conn.execute(text(f'SET search_path TO "{schema}"'))
        row = conn.execute(
            text(
                "SELECT expired_at_ms, updated_at_ms FROM console_acl_entries "
                "WHERE id = :id"
            ),
            {"id": row_id},
        ).one()
    return (row[0], row[1])


async def _insert_grant_full(
    factory: Any,
    *,
    as_user: str,
    row_id: str,
    user_sub: str,
    principal_type: str,
    resource_type: str,
    resource_id: str,
    perm_bits: int = 1,
    principal_id: str | None = None,
    principal_model: str | None = None,
    tenant_id: str | None = None,
    role_id: str | None = None,
    expired_at_ms: int | None = None,
    created_at_ms: int = 0,
    updated_at_ms: int = 0,
) -> None:
    """Like ``tests.test_acl_ownership_rls._insert_grant`` but with
    ``perm_bits``/``expired_at_ms``/``tenant_id``/``role_id`` exposed —
    P-1b needs a time-limited row, KSEL-t needs a tenant + role_id."""
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": as_user},
        )
        await session.execute(
            text(
                "INSERT INTO console_acl_entries "
                "(id, user_sub, principal_type, principal_id, principal_model, "
                "resource_type, resource_id, perm_bits, tenant_id, role_id, "
                "granted_at_ms, expired_at_ms, created_at_ms, updated_at_ms) "
                "VALUES (:id, :user_sub, :ptype, :pid, :pmodel, "
                ":rtype, :rid, :bits, :tenant, :role, :created, :expired, "
                ":created, :updated)"
            ),
            {
                "id": row_id,
                "user_sub": user_sub,
                "ptype": principal_type,
                "pid": principal_id,
                "pmodel": principal_model,
                "rtype": resource_type,
                "rid": resource_id,
                "bits": perm_bits,
                "tenant": tenant_id,
                "role": role_id,
                "created": created_at_ms,
                "expired": expired_at_ms,
                "updated": updated_at_ms,
            },
        )
        await session.commit()


class TestP1CallerScopedDeleteCrossOwner:
    """P-1 — a predicate spanning a row the caller does NOT own is
    silently OMITTED from the UPDATE (migration 032's owner-only
    ``USING``), never refused, and the OTHER owner's row is untouched
    from THEIR OWN session."""

    async def test_predicate_spanning_another_owners_row_is_scoped(
        self, write_harness: Any, sp_pin: Any
    ) -> None:
        await _seed_attacker_resources(write_harness.factory)
        owner = _new_user_context(_OWNER)

        await _insert_grant_full(
            write_harness.factory,
            as_user=_OWNER,
            row_id="row-a-owner",
            user_sub=_OWNER,
            principal_type="user",
            principal_id=_VIEWER,
            principal_model="User",
            resource_type="agent",
            resource_id="agent-1",
        )
        await _insert_grant_full(
            write_harness.factory,
            as_user=_ATTACKER,
            row_id="row-b-attacker",
            user_sub=_ATTACKER,
            principal_type="user",
            principal_id=_VIEWER,
            principal_model="User",
            resource_type="agent",
            resource_id="agent-attacker",
        )

        set_current_user_id(owner.user_id)
        try:
            result = await write_harness.service.delete_acl_entries(
                owner, [{"principal_type": "user", "principal_id": _VIEWER}]
            )
        finally:
            set_current_user_id(None)

        assert result["expired_ids"] == ["row-a-owner"]
        assert result["visible_matched_count"] == 1, (
            "B's row is neither owned by A nor A-as-principal nor public "
            "— invisible to A's own SELECT too, not merely unexpirable"
        )
        admin = _admin_engine()
        try:
            b_pair = await _acl_row_pair(admin, write_harness.schema, "row-b-attacker")
        finally:
            admin.dispose()
        assert b_pair[0] is None, "B's row must be untouched from B's own session"


class TestP1bVisibleButUnexpirable:
    """P-1b/P-1b-F — a PUBLIC row is visible to EVERY caller via RLS's
    unconditional public ``SELECT`` branch, but the UPDATE stays
    owner-only: ``visible_matched_count`` counts both public rows,
    ``expired_ids`` names only the caller's own."""

    async def test_public_rows_visible_count_exceeds_expired(
        self, write_harness: Any, sp_pin: Any
    ) -> None:
        await _seed_attacker_resources(write_harness.factory)
        owner = _new_user_context(_OWNER)

        await _insert_grant_full(
            write_harness.factory,
            as_user=_OWNER,
            row_id="row-a-public",
            user_sub=_OWNER,
            principal_type="public",
            resource_type="agent",
            resource_id="agent-1",
        )
        await _insert_grant_full(
            write_harness.factory,
            as_user=_ATTACKER,
            row_id="row-b-public",
            user_sub=_ATTACKER,
            principal_type="public",
            resource_type="agent",
            resource_id="agent-attacker",
            expired_at_ms=1_900_000_000_000,  # far future — still active
        )

        set_current_user_id(owner.user_id)
        try:
            result = await write_harness.service.delete_acl_entries(
                owner, [{"principal_type": "public"}]
            )
            effective_before_and_after = await write_harness.service.has_permission(
                owner, "agent", "agent-attacker", 1
            )
        finally:
            set_current_user_id(None)

        assert result["visible_matched_count"] == 2
        assert result["expired_ids"] == ["row-a-public"]
        assert effective_before_and_after is True, (
            "B's public row on B's own agent must stay effective — the "
            "count is a signal, never a proof of a skip (spec §4.5)"
        )
        admin = _admin_engine()
        try:
            b_pair = await _acl_row_pair(admin, write_harness.schema, "row-b-public")
        finally:
            admin.dispose()
        assert b_pair[0] == 1_900_000_000_000, (
            "B's row must be untouched, still at its ORIGINAL scheduled "
            "expiry — never a hard delete, never re-stamped"
        )


class TestP2SoleOwnerConsequence:
    """P-2 — an owner revoking their own OWNER-bits row leaves the
    resource live and owned in its OWN sovereign store; A2 builds no
    guard against it (2c/2d's rule)."""

    async def test_owner_revokes_own_owner_row(
        self, write_harness: Any, sp_pin: Any
    ) -> None:
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_OWNER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=MAX_PERM_BITS,
            )
            result = await write_harness.service.revoke_permission(
                owner,
                principal_type="user",
                principal_id=_OWNER,
                resource_type="agent",
                resource_id="agent-1",
            )
        finally:
            set_current_user_id(None)

        assert result["expired_ids"] != []
        admin = _admin_engine()
        try:
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                row = conn.execute(
                    text(
                        "SELECT expired_at_ms FROM console_acl_entries WHERE id = :id"
                    ),
                    {"id": result["expired_ids"][0]},
                ).one()
                assert row[0] is not None
                agent_row = conn.execute(
                    text(
                        "SELECT deleted_at_ms FROM console_agents WHERE agent_id = 'agent-1'"
                    )
                ).one()
                assert agent_row[0] is None, (
                    "the resource stays live in its OWN sovereign store — "
                    "no cascade exists in A2 (2c's rule)"
                )
        finally:
            admin.dispose()


class TestP3RevokeOnSoftDeletedResourceSFA:
    """P-3 + **SF-A** — a revoke's own UPDATE re-verifies 032's
    ownership subquery on EVERY row it touches: once the resource is
    soft-deleted, even the OWNER's own revoke of their own grant is
    refused. Seeded at tenant ``t1`` (SF-A) so the denial row's
    ``question`` is asserted to end ``tenant=t1`` — the neuter that
    would falsely satisfy this is dropping ``tenant_id`` from
    ``_write_denial``'s call inside ``revoke_permission``."""

    async def test_revoke_refused_and_denial_row_ends_tenant_t1(
        self, write_harness: Any, sp_pin: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
                tenant_id="t1",
            )
        finally:
            set_current_user_id(None)

        admin = _admin_engine()
        try:
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(
                    text(
                        "UPDATE console_agents SET deleted_at_ms = 1 "
                        "WHERE agent_id = 'agent-1'"
                    )
                )

            probe = _RollbackSplitProbe(write_harness, admin, monkeypatch)
            set_current_user_id(owner.user_id)
            try:
                with pytest.raises(AclWriteRefused) as excinfo:
                    await write_harness.service.revoke_permission(
                        owner,
                        principal_type="user",
                        principal_id=_VIEWER,
                        resource_type="agent",
                        resource_id="agent-1",
                        tenant_id="t1",
                    )
            finally:
                set_current_user_id(None)
                probe.close()

            assert probe.snapshots[-1] == (0, "idle in transaction (aborted)", 1)
            assert excinfo.value.failure_class == _audit.FAILURE_CLASS_ACL_DENIED_POLICY

            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-1"
            )
            denial_rows = [
                r
                for r in rows
                if r["status"] == "failed"
                and r["question"].startswith("op=revokePermission")
            ]
            assert len(denial_rows) == 1
            assert denial_rows[0]["question"].endswith("tenant=t1"), denial_rows[0][
                "question"
            ]
            # W5-t (ADDENDUM A) — the full field parse (SF-1/W-Q,
            # additive beside the full-string check above), plus
            # error_detail.predicate_or_attempted_row == attempted.
            fields = _parse_question(denial_rows[0]["question"])
            assert fields == {
                "op": "revokePermission",
                "principal_type": "user",
                "principal_id": _VIEWER,
                "resource_type": "agent",
                "resource_id": "agent-1",
                "bits": "0",
                "tenant": "t1",
            }
            error_detail = json.loads(denial_rows[0]["error_detail"])
            assert error_detail["predicate_or_attempted_row"] == {
                "principal_type": "user",
                "principal_id": _VIEWER,
                "resource_type": "agent",
                "resource_id": "agent-1",
                "tenant_id": "t1",
            }
        finally:
            admin.dispose()


class TestDA24RollbackAtPredicateI:
    """D-A2-4r — three predicates, the SECOND refused (a soft-deleted
    resource): zero rows expired, zero success rows, ONE denial row
    with ``predicate_index == 1``."""

    async def test_second_of_three_predicates_refused_rolls_back_all(
        self, write_harness: Any, sp_pin: Any
    ) -> None:
        owner = _new_user_context(_OWNER)
        admin = _admin_engine()
        try:
            agents = Base.metadata.tables["console_agents"]
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(
                    insert(agents).values(
                        id="agent-row-3",
                        agent_id="agent-3",
                        user_sub=_OWNER,
                        name="Agent Three",
                        created_at_ms=0,
                        updated_at_ms=0,
                    )
                )

            set_current_user_id(owner.user_id)
            try:
                r1 = await write_harness.service.grant_permission(
                    owner,
                    principal_type="user",
                    principal_id="p-d4r-1",
                    resource_type="agent",
                    resource_id="agent-1",
                    perm_bits=1,
                )
                r2 = await write_harness.service.grant_permission(
                    owner,
                    principal_type="user",
                    principal_id="p-d4r-2",
                    resource_type="agent",
                    resource_id="agent-3",
                    perm_bits=1,
                )
            finally:
                set_current_user_id(None)

            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(
                    text(
                        "UPDATE console_agents SET deleted_at_ms = 1 "
                        "WHERE agent_id = 'agent-3'"
                    )
                )

            set_current_user_id(owner.user_id)
            try:
                with pytest.raises(AclWriteRefused) as excinfo:
                    await write_harness.service.delete_acl_entries(
                        owner,
                        [
                            {"principal_id": "p-d4r-1", "resource_id": "agent-1"},
                            {"principal_id": "p-d4r-2", "resource_id": "agent-3"},
                            {"principal_id": "p-d4r-nonexistent"},
                        ],
                    )
            finally:
                set_current_user_id(None)

            assert excinfo.value.failure_class == _audit.FAILURE_CLASS_ACL_DENIED_POLICY

            r1_pair = await _acl_row_pair(admin, write_harness.schema, r1["id"])
            r2_pair = await _acl_row_pair(admin, write_harness.schema, r2["id"])
            assert r1_pair[0] is None, "predicate 0 must be ROLLED BACK, not committed"
            assert r2_pair[0] is None

            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-1"
            )
            delete_rows = [
                r for r in rows if r["question"].startswith("op=deleteAclEntries")
            ]
            assert len(delete_rows) == 0, "zero success rows — everything rolled back"

            rows_all = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-3"
            )
            denial_rows = [r for r in rows_all if r["status"] == "failed"]
            assert len(denial_rows) == 1
            error_detail = json.loads(denial_rows[0]["error_detail"])
            attempted = error_detail["predicate_or_attempted_row"]
            # BL-2 (A2 fix-3, ADDENDUM-A A-2 W5) — full dict equality:
            # `predicate_index` alone let `predicate` (the refused
            # predicate itself) and `predicate_count` (the batch size)
            # silently drop from the audit record and still read GREEN.
            assert attempted == {
                "predicate_index": 1,
                "predicate": {"principal_id": "p-d4r-2", "resource_id": "agent-3"},
                "predicate_count": 3,
            }
        finally:
            admin.dispose()


class TestSFCAuditFailureOnKselTKey:
    """**SF-C** — the audit-failure test (#4, N4) run on KSEL-t's own
    key (tenant ``t1``, ``role_id='r1'``): a broken ``_content_hash``
    on ``modify_permission_bits`` must propagate the ORIGINAL
    exception, leave the pre-existing row untouched, and land ONE
    ``acl_audit_write_failed`` row whose ``question`` ends
    ``tenant=t1``."""

    async def test_broken_audit_write_on_ksel_t_key(
        self, write_harness: Any, sp_pin: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        owner = _new_user_context(_OWNER)
        key = dict(
            principal_type="user",
            principal_id=_VIEWER,
            resource_type="agent",
            resource_id="agent-1",
            tenant_id="t1",
        )
        set_current_user_id(owner.user_id)
        try:
            m = await write_harness.service.grant_permission(
                owner, perm_bits=7, role_id="r1", **key
            )
        finally:
            set_current_user_id(None)

        original = _audit._content_hash
        calls = {"n": 0}

        def _flaky(*args: object, **kwargs: object) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("content_hash broken for SF-C")
            return original(*args, **kwargs)

        monkeypatch.setattr(_audit, "_content_hash", _flaky)

        set_current_user_id(owner.user_id)
        try:
            with pytest.raises(RuntimeError, match="content_hash broken"):
                await write_harness.service.modify_permission_bits(
                    owner, add_bits=1, **key
                )
        finally:
            set_current_user_id(None)

        probe_admin = _admin_engine()
        try:
            pair = await _acl_row_pair(probe_admin, write_harness.schema, m["id"])
        finally:
            probe_admin.dispose()
        assert pair[0] is None, "the pre-existing row must still be ACTIVE"

        admin = _admin_engine()
        try:
            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-1"
            )
            denial_rows = [
                r
                for r in rows
                if r["failure_class"] == _audit.FAILURE_CLASS_ACL_AUDIT_WRITE_FAILED
                and r["question"].startswith("op=modifyPermissionBits")
            ]
            assert len(denial_rows) == 1
            assert denial_rows[0]["question"].endswith("tenant=t1"), denial_rows[0][
                "question"
            ]
            # W6-f (ADDENDUM A) — the full field parse, db_error_class
            # == type(the patched exception).__name__ (SF-5), and
            # predicate_or_attempted_row == the call's own attempted.
            fields = _parse_question(denial_rows[0]["question"])
            assert fields == {
                "op": "modifyPermissionBits",
                "principal_type": "user",
                "principal_id": _VIEWER,
                "resource_type": "agent",
                "resource_id": "agent-1",
                "bits": "7",  # new_bits = old_bits(7) | add_bits(1) == 7
                "tenant": "t1",
            }
            error_detail = json.loads(denial_rows[0]["error_detail"])
            assert error_detail["db_error_class"] == "RuntimeError"
            assert error_detail["predicate_or_attempted_row"] == {
                "principal_type": "user",
                "principal_id": _VIEWER,
                "resource_type": "agent",
                "resource_id": "agent-1",
                "tenant_id": "t1",
                "add_bits": 1,
                "remove_bits": None,
            }
        finally:
            admin.dispose()


class TestSFBExplainCapture:
    """**SF-B** — the real Postgres ``EXPLAIN`` of ``delete_acl_entries``'s
    UPDATE (clause C10, ``_expire_where``), captured against a scenario
    shaped like MP's P2 (a second predicate matching several rows).
    Printed to stdout so the build record can cite it verbatim — this
    test's assertion is only that ``EXPLAIN`` runs cleanly against the
    real statement shape the write path issues, not a plan-shape pin
    (spec §4's own AD-1(3): the correctness guards are the behavioural
    rows, never a rendered-plan pin)."""

    async def test_explain_the_delete_update_statement(
        self, write_harness: Any, sp_pin: Any
    ) -> None:
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id="p-explain-d",
                resource_type="promptGroup",
                resource_id="group-1",
                perm_bits=1,
            )
            await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id="p-explain-g",
                resource_type="promptGroup",
                resource_id="group-1",
                perm_bits=1,
            )
        finally:
            set_current_user_id(None)

        admin = _admin_engine()
        try:
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(text(f"SET ROLE {_APP_ROLE}"))
                conn.execute(
                    text("SELECT set_config('app.current_user_id', :uid, false)"),
                    {"uid": _OWNER},
                )
                plan = conn.execute(
                    text(
                        "EXPLAIN UPDATE console_acl_entries "
                        "SET expired_at_ms = 1, updated_at_ms = 1 "
                        "WHERE resource_type = 'promptGroup' AND resource_id = 'group-1' "
                        "AND (expired_at_ms IS NULL OR expired_at_ms > 0) "
                        "RETURNING id, created_at_ms"
                    )
                ).all()
            plan_text = "\n".join(row[0] for row in plan)
            print("SF-B EXPLAIN (delete_acl_entries's UPDATE, MP's P2 shape):")
            print(plan_text)
            assert "console_acl_entries" in plan_text
        finally:
            admin.dispose()


# ═══════════════════════════════════════════════════════════════════════
# Fix round 1 (the independent reviewer's REJECT verdict) — BL-1 through
# BL-4. Real-Postgres-only; the mock/aiosqlite halves of BL-2/BL-4 live
# in tests/test_console_acl_write_path.py.
# ═══════════════════════════════════════════════════════════════════════

import re  # noqa: E402

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from audittrace.services.console_store import _context as _clock_module  # noqa: E402


def _seed_agent(write_harness: Any, agent_id: str) -> None:
    """Owner-owned agent, raw admin insert — some fix-round scenarios
    need a resource beyond A1's shipped ``agent-1``/``group-1``."""
    admin = _admin_engine()
    try:
        agents = Base.metadata.tables["console_agents"]
        with admin.begin() as conn:
            conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
            conn.execute(
                insert(agents).values(
                    id=f"agent-row-{agent_id}",
                    agent_id=agent_id,
                    user_sub=_OWNER,
                    name=agent_id,
                    created_at_ms=0,
                    updated_at_ms=0,
                )
            )
    finally:
        admin.dispose()


def _seed_prompt_group(write_harness: Any, group_id: str) -> None:
    """Owner-owned prompt group, raw admin insert — v4's MP scenario
    needs a ``promptGroup``-typed resource sharing the STRING
    ``resource_id`` an ``agent`` row also uses (row F: ``promptGroup``/
    ``agent-1``), a different namespace entirely from the agent."""
    admin = _admin_engine()
    try:
        groups = Base.metadata.tables["console_prompt_groups"]
        with admin.begin() as conn:
            conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
            conn.execute(
                insert(groups).values(
                    id=f"group-row-{group_id}",
                    group_id=group_id,
                    user_sub=_OWNER,
                    name=group_id,
                    created_at_ms=0,
                    updated_at_ms=0,
                )
            )
    finally:
        admin.dispose()


def _parse_question(question: str) -> dict[str, str]:
    """SF-1 (ADDENDUM A) — parse a ``_audit._question`` string into its
    SEVEN fields via the exact regex the addendum names, so a caller
    can assert each one separately rather than one six-value full-
    string equality. A ``None``/absent value renders as the literal
    ``-`` (``_audit.py``'s own rendering, never re-typed here)."""
    m = re.match(
        r"^op=(\S+) principal=([^:\s]+):(\S+) resource=([^:\s]+):(\S+) "
        r"bits=(-?\d+) tenant=(\S+)$",
        question,
    )
    assert m is not None, f"question does not match the closed shape: {question!r}"
    return {
        "op": m.group(1),
        "principal_type": m.group(2),
        "principal_id": m.group(3),
        "resource_type": m.group(4),
        "resource_id": m.group(5),
        "bits": m.group(6),
        "tenant": m.group(7),
    }


class TestBL1ModifyInsertRefusalIsPolicyDenial:
    """BL-1 — modify's INSERT is now flushed inside Stage 1 (shape (1),
    spec §4.2), so a genuine DB refusal on the INSERT itself (031's
    public-resource unique index, §8 item 12) is classified by Stage
    1's own ``except`` — ``AclWriteRefused``/``acl_denied_policy``,
    SQLSTATE ``23505`` — never misclassified as ``acl_audit_write_failed``
    by Stage 2's ``except`` (which only ever sees the AUDIT row's own
    failures after this fix)."""

    async def test_modify_over_public_row_is_policy_denial_not_audit_failure(
        self, write_harness: Any, sp_pin: Any
    ) -> None:
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            await write_harness.service.grant_permission(
                owner,
                principal_type="public",
                principal_id=None,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
            )
        finally:
            set_current_user_id(None)

        set_current_user_id(owner.user_id)
        try:
            with pytest.raises(AclWriteRefused) as excinfo:
                await write_harness.service.modify_permission_bits(
                    owner,
                    principal_type="public",
                    principal_id=None,
                    resource_type="agent",
                    resource_id="agent-1",
                    add_bits=2,
                )
        finally:
            set_current_user_id(None)

        assert excinfo.value.failure_class == _audit.FAILURE_CLASS_ACL_DENIED_POLICY
        assert excinfo.value.db_error_class == "23505", excinfo.value.db_error_class

        admin = _admin_engine()
        try:
            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-1"
            )
        finally:
            admin.dispose()
        modify_rows = [
            r for r in rows if r["question"].startswith("op=modifyPermissionBits")
        ]
        assert [r["status"] for r in modify_rows] == ["failed"]
        assert modify_rows[0]["failure_class"] == _audit.FAILURE_CLASS_ACL_DENIED_POLICY
        audit_failed = [
            r
            for r in modify_rows
            if r["failure_class"] == _audit.FAILURE_CLASS_ACL_AUDIT_WRITE_FAILED
        ]
        assert audit_failed == [], (
            "BL-1: must NEVER be classified acl_audit_write_failed — the "
            "refusal is on the ACL row's own INSERT, not the audit write"
        )

        set_current_user_id(owner.user_id)
        try:
            effective = await write_harness.service.get_effective_permissions(
                _new_user_context("anyone-at-all"), "agent", "agent-1"
            )
        finally:
            set_current_user_id(None)
        assert effective == 1, "the ORIGINAL public row must stay active, bits=1"


class TestBL2TenantRenderedOnSuccessAuditRows:
    """BL-2 — KSEL-t/SEL's tenant carriage, asserted against the REAL
    ``/interactions`` row's ``question`` string (not merely the
    returned dict), for revoke, modify and delete, on real Postgres."""

    async def test_revoke_success_audit_row_ends_tenant_t1(
        self, write_harness: Any, sp_pin: Any
    ) -> None:
        owner = _new_user_context(_OWNER)
        key = dict(
            principal_type="user",
            principal_id=_VIEWER,
            resource_type="agent",
            resource_id="agent-1",
            tenant_id="t1",
        )
        set_current_user_id(owner.user_id)
        try:
            await write_harness.service.grant_permission(owner, perm_bits=1, **key)
            result = await write_harness.service.revoke_permission(owner, **key)
        finally:
            set_current_user_id(None)
        assert result["visible_matched_count"] == 1

        admin = _admin_engine()
        try:
            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-1"
            )
        finally:
            admin.dispose()
        revoke_rows = [
            r
            for r in rows
            if r["status"] == "success"
            and r["question"].startswith("op=revokePermission")
        ]
        assert len(revoke_rows) == 1
        assert revoke_rows[0]["question"].endswith("tenant=t1"), revoke_rows[0][
            "question"
        ]
        # W2 (ADDENDUM A, W-Q) — per-field parse, additive (SF-2).
        fields = _parse_question(revoke_rows[0]["question"])
        assert fields == {
            "op": "revokePermission",
            "principal_type": "user",
            "principal_id": _VIEWER,
            "resource_type": "agent",
            "resource_id": "agent-1",
            "bits": "0",
            "tenant": "t1",
        }
        answer = json.loads(revoke_rows[0]["answer"])
        assert answer["acl_entry_ids"] == []
        assert answer["visible_matched_count"] == 1
        assert answer["expired_at_ms"] is None

    async def test_modify_success_audit_row_ends_tenant_t1(
        self, write_harness: Any, sp_pin: Any
    ) -> None:
        owner = _new_user_context(_OWNER)
        key = dict(
            principal_type="user",
            principal_id=_VIEWER,
            resource_type="agent",
            resource_id="agent-1",
            tenant_id="t1",
        )
        set_current_user_id(owner.user_id)
        try:
            await write_harness.service.grant_permission(owner, perm_bits=1, **key)
            row = await write_harness.service.modify_permission_bits(
                owner, add_bits=2, **key
            )
        finally:
            set_current_user_id(None)
        assert row is not None

        admin = _admin_engine()
        try:
            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-1"
            )
        finally:
            admin.dispose()
        modify_rows = [
            r
            for r in rows
            if r["status"] == "success"
            and r["question"].startswith("op=modifyPermissionBits")
        ]
        assert len(modify_rows) == 1
        assert modify_rows[0]["question"].endswith("tenant=t1"), modify_rows[0][
            "question"
        ]
        fields = _parse_question(modify_rows[0]["question"])
        assert fields == {
            "op": "modifyPermissionBits",
            "principal_type": "user",
            "principal_id": _VIEWER,
            "resource_type": "agent",
            "resource_id": "agent-1",
            "bits": "3",  # (1 | 2) & ~0
            "tenant": "t1",
        }
        answer = json.loads(modify_rows[0]["answer"])
        assert answer["acl_entry_ids"] == [row["id"]]
        assert answer["visible_matched_count"] == 1
        assert answer["expired_at_ms"] is None

    async def test_delete_success_audit_row_ends_tenant_t1(
        self, write_harness: Any, sp_pin: Any
    ) -> None:
        owner = _new_user_context(_OWNER)
        set_current_user_id(owner.user_id)
        try:
            await write_harness.service.grant_permission(
                owner,
                principal_type="user",
                principal_id=_VIEWER,
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
                tenant_id="t1",
            )
            result = await write_harness.service.delete_acl_entries(
                owner,
                [
                    {
                        "principal_type": "user",
                        "principal_id": _VIEWER,
                        "resource_type": "agent",
                        "resource_id": "agent-1",
                        "tenant_id": "t1",
                    }
                ],
            )
        finally:
            set_current_user_id(None)
        assert result["visible_matched_count"] == 1

        admin = _admin_engine()
        try:
            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-1"
            )
        finally:
            admin.dispose()
        delete_rows = [
            r
            for r in rows
            if r["status"] == "success"
            and r["question"].startswith("op=deleteAclEntries")
        ]
        assert len(delete_rows) == 1
        assert delete_rows[0]["question"].endswith("tenant=t1"), delete_rows[0][
            "question"
        ]


class TestBL3MultiPredicateRealPostgres:
    """BL-3 — the multi-predicate (MP) scenario on REAL Postgres
    (``write_harness``, never the aiosqlite-backed ``service`` fixture
    whose ``postgres`` label is misleading for this guard). Two
    predicates deliberately OVERLAP on one row (catches ``sum``), the
    matched rows within one predicate are seeded with created_at_ms/id
    in CONFLICTING order (catches ``uuidsort`` — sorting by id alone
    gives the wrong answer), and the two predicates' own visible/expired
    sets are kept DISTINCT and NON-EMPTY (catches per-row
    ``visible=visible_all``, per-row cumulative ``expired_ids``, and
    reversed predicate/aggregation order)."""

    async def test_mp_two_predicates_real_postgres(
        self, write_harness: Any, sp_pin: Any
    ) -> None:
        owner = _new_user_context(_OWNER)
        _seed_agent(write_harness, "agent-mp")
        # P1 = tenant t1 on agent-mp; P2 = principal_id=user-1 on agent-mp.
        # r1 matches BOTH (the overlap row) — created FIRST but with a
        # LEXICALLY LARGER id than r2 (created SECOND), so an id-only
        # sort within P1 gives the WRONG order.
        await _insert_grant_full(
            write_harness.factory,
            as_user=_OWNER,
            row_id="zzz-r1",
            user_sub=_OWNER,
            principal_type="user",
            principal_id="user-1",
            principal_model="User",
            resource_type="agent",
            resource_id="agent-mp",
            perm_bits=1,
            tenant_id="t1",
            created_at_ms=100,
            updated_at_ms=100,
        )
        await _insert_grant_full(
            write_harness.factory,
            as_user=_OWNER,
            row_id="aaa-r2",
            user_sub=_OWNER,
            principal_type="user",
            principal_id="user-2",
            principal_model="User",
            resource_type="agent",
            resource_id="agent-mp",
            perm_bits=1,
            tenant_id="t1",
            created_at_ms=101,
            updated_at_ms=101,
        )
        await _insert_grant_full(
            write_harness.factory,
            as_user=_OWNER,
            row_id="r3",
            user_sub=_OWNER,
            principal_type="user",
            principal_id="user-1",
            principal_model="User",
            resource_type="agent",
            resource_id="agent-mp",
            perm_bits=1,
            tenant_id="t2",
            created_at_ms=150,
            updated_at_ms=150,
        )
        predicates = [
            {"resource_id": "agent-mp", "tenant_id": "t1"},
            {"resource_id": "agent-mp", "principal_id": "user-1"},
        ]
        set_current_user_id(owner.user_id)
        try:
            result = await write_harness.service.delete_acl_entries(owner, predicates)
        finally:
            set_current_user_id(None)

        assert result["visible_matched_count"] == 3, (
            "DISTINCT over the OR (zzz-r1, aaa-r2, r3) — never sum(2, 2) == 4"
        )
        assert result["expired_ids"] == ["zzz-r1", "aaa-r2", "r3"], (
            "predicate 1's own (created_at_ms, id) order [zzz-r1, aaa-r2], "
            "THEN predicate 2's own (r3 only — zzz-r1 already expired by "
            "predicate 1, absent from predicate 2's set) — never reversed, "
            "never id-only sorted, never cumulative"
        )

        admin = _admin_engine()
        try:
            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-mp"
            )
        finally:
            admin.dispose()
        delete_rows = [
            r for r in rows if r["question"].startswith("op=deleteAclEntries")
        ]
        assert len(delete_rows) == 2, "one audit row per predicate"
        answers = [json.loads(r["answer"]) for r in delete_rows]
        assert answers[0]["visible_matched_count"] == 2, (
            "predicate 1's OWN count (zzz-r1, aaa-r2) — never visible_all (3)"
        )
        assert answers[1]["visible_matched_count"] == 2, (
            "predicate 2's OWN count (zzz-r1, r3) — never visible_all (3)"
        )
        assert answers[0]["expired_ids"] == ["zzz-r1", "aaa-r2"], (
            "predicate 1's OWN expired ids, in (created_at_ms, id) order"
        )
        assert answers[1]["expired_ids"] == ["r3"], (
            "predicate 2's OWN expired ids ONLY — never cumulative "
            "(predicate 1's ids must not reappear here)"
        )


class TestBL3PlanIndependentTieRealPostgres:
    """BL-3 (continued) — the ordering guard's tie must be PLAN-
    INDEPENDENT: two rows sharing the SAME ``created_at_ms``, with the
    id-lexical order DECOUPLED from the principal_id-lexical order (the
    row inserted FIRST, ``zzz-tie``, carries the principal_id that
    sorts FIRST, ``aaa-p``) — so BOTH a heap/insertion-order scan AND a
    principal_id-index-ordered scan agree on the WRONG answer
    ``[zzz-tie, aaa-tie]``; only the explicit ``(created_at_ms, id)``
    sort produces the required ``[aaa-tie, zzz-tie]``."""

    async def test_tie_plan_independent(self, write_harness: Any, sp_pin: Any) -> None:
        owner = _new_user_context(_OWNER)
        _seed_agent(write_harness, "agent-tie")
        await _insert_grant_full(
            write_harness.factory,
            as_user=_OWNER,
            row_id="zzz-tie",
            user_sub=_OWNER,
            principal_type="user",
            principal_id="aaa-p",
            principal_model="User",
            resource_type="agent",
            resource_id="agent-tie",
            perm_bits=1,
            created_at_ms=5,
            updated_at_ms=5,
        )
        await _insert_grant_full(
            write_harness.factory,
            as_user=_OWNER,
            row_id="aaa-tie",
            user_sub=_OWNER,
            principal_type="user",
            principal_id="zzz-p",
            principal_model="User",
            resource_type="agent",
            resource_id="agent-tie",
            perm_bits=1,
            created_at_ms=5,
            updated_at_ms=5,
        )
        set_current_user_id(owner.user_id)
        try:
            result = await write_harness.service.delete_acl_entries(
                owner, [{"resource_id": "agent-tie"}]
            )
        finally:
            set_current_user_id(None)
        assert result["expired_ids"] == ["aaa-tie", "zzz-tie"]


_MP_V = "viewer-sub-0003"
_MP_P1 = {"principal_id": _MP_V}
_MP_P2 = {"resource_type": "agent", "resource_id": "agent-1"}
_MP_P3 = {"principal_type": "public"}


async def _seed_mp_a_to_g(
    write_harness: Any, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    """v4 §7.3's MP scenario, built VERBATIM (ADDENDUM A §A-3, A2 fix-3
    BL-4) — same rows (A-G), same predicates (P1/P2/P3), same overlaps
    (A in P1∩P2, D in P2∩P3), same created_at_ms TIE (C/G), same
    insertion-order-vs-created_at_ms-order inversion (B created BEFORE
    A but INSERTED after it). Seeded through the CLOCK SEAM via the
    real ``grant_permission`` write path (never a raw INSERT) — every
    row's uuid is the one the service itself mints. v4's own RE-ROLL
    mechanism: ``grant_permission``'s O-6 expire-and-insert mints a
    FRESH ``uuid4`` on every call at the same key, so re-granting at
    G's (respectively B's) key until the newly-minted id satisfies the
    spec's ordering precondition reproduces v4's own re-roll (PG
    measured 0-7 re-rolls, mock 0-8, per run) — never a fixed/literal
    id substituted for it. Returns ``{letter: real_id}``; the caller
    asserts v4's own precondition, ``uuid(A) < uuid(B)`` and
    ``uuid(C) < uuid(G)``, from these SAME rows."""
    _seed_agent(write_harness, "other-resource")
    _seed_agent(write_harness, "other-resource-2")
    _seed_prompt_group(write_harness, "agent-1")
    owner = _new_user_context(_OWNER)
    t0 = 1_790_000_000_000

    async def _grant_at(clock_ms: int, **key: Any) -> str:
        monkeypatch.setattr(_clock_module, "now_ms", lambda: clock_ms)
        set_current_user_id(owner.user_id)
        try:
            row = await write_harness.service.grant_permission(
                owner, perm_bits=1, **key
            )
        finally:
            set_current_user_id(None)
        return str(row["id"])

    async def _grant_until(clock_ms: int, key: dict[str, Any], condition: Any) -> str:
        for _ in range(64):
            new_id = await _grant_at(clock_ms, **key)
            if condition(new_id):
                return new_id
        raise AssertionError(
            "v4 §7.3's uuid ordering precondition not reached after 64 re-rolls"
        )

    ids: dict[str, str] = {}
    # E — matches NONE.
    ids["E"] = await _grant_at(
        t0 + 1,
        principal_type="user",
        principal_id="untouched-principal",
        resource_type="agent",
        resource_id="other-resource-2",
    )
    # F — promptGroup/agent-1: must survive P2 (resource_type mismatch).
    ids["F"] = await _grant_at(
        t0 + 2,
        principal_type="user",
        principal_id="untouched-principal-f",
        resource_type="promptGroup",
        resource_id="agent-1",
    )
    # D — public, agent/agent-1: D in P2 ∩ P3.
    ids["D"] = await _grant_at(
        t0 + 3,
        principal_type="public",
        principal_id=None,
        resource_type="agent",
        resource_id="agent-1",
    )
    # G, then C — the created_at_ms TIE at P2's key; G inserted FIRST
    # (mirroring v4's own construction — the raw RETURNING/insertion
    # order must never be mistaken for the correct (created_at_ms, id)
    # sort); C re-rolled until uuid(C) < uuid(G), v4's own precondition.
    g_key = dict(
        principal_type="user",
        principal_id="aaa-sub-0010",
        resource_type="agent",
        resource_id="agent-1",
    )
    c_key = dict(
        principal_type="user",
        principal_id="other-sub-0009",
        resource_type="agent",
        resource_id="agent-1",
    )
    ids["G"] = await _grant_at(t0 + 4, **g_key)
    ids["C"] = await _grant_until(t0 + 4, c_key, lambda cid: cid < ids["G"])
    # A, B — A in P1 ∩ P2; B in P1 only. A created AFTER B (T0+6 vs
    # T0+5) but INSERTED before it here — insertion order != creation
    # order, so `unsorted`/`globalsort` cannot hide behind coincidence.
    # B re-rolled until uuid(A) < uuid(B), v4's own precondition.
    a_key = dict(
        principal_type="user",
        principal_id=_MP_V,
        resource_type="agent",
        resource_id="agent-1",
    )
    b_key = dict(
        principal_type="user",
        principal_id=_MP_V,
        resource_type="agent",
        resource_id="other-resource",
    )
    ids["A"] = await _grant_at(t0 + 6, **a_key)
    ids["B"] = await _grant_until(t0 + 5, b_key, lambda bid: ids["A"] < bid)

    assert ids["A"] < ids["B"], "v4 §7.3's own uuid precondition"
    assert ids["C"] < ids["G"], "v4 §7.3's own uuid precondition"
    return ids


class TestMPAToGVerbatimRealPostgres:
    """v4 §7.3's MP scenario, built VERBATIM (ADDENDUM A §A-3) — closes
    BL-A. A REDUCED scenario (2 predicates, predicate order == creation
    order) let ``globalsort`` coincide with the correct answer; this
    3-predicate, 7-row, tied, order-inverted construction cannot."""

    async def test_mp_a_to_g_real_postgres(
        self, write_harness: Any, sp_pin: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        owner = _new_user_context(_OWNER)
        ids = await _seed_mp_a_to_g(write_harness, monkeypatch)

        set_current_user_id(owner.user_id)
        try:
            result = await write_harness.service.delete_acl_entries(
                owner, [_MP_P1, _MP_P2, _MP_P3]
            )
        finally:
            set_current_user_id(None)

        assert result["visible_matched_count"] == 5, "distinct over the OR: {A,B,D,C,G}"
        assert result["expired_ids"] == [
            ids["B"],
            ids["A"],
            ids["D"],
            ids["C"],
            ids["G"],
        ]

        admin = _admin_engine()
        try:
            # NOT _interactions_rows (its LIKE %:<resource_id>% filter
            # would miss P1's and P3's rows — neither predicate names a
            # resource_id at all, so their question renders "-:-").
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                rows = [
                    dict(r)
                    for r in conn.execute(
                        text(
                            "SELECT status, failure_class, answer, question "
                            "FROM interactions WHERE question LIKE 'op=deleteAclEntries%' "
                            "ORDER BY id"
                        )
                    )
                    .mappings()
                    .all()
                ]
            e_pair = await _acl_row_pair(admin, write_harness.schema, ids["E"])
            f_pair = await _acl_row_pair(admin, write_harness.schema, ids["F"])
        finally:
            admin.dispose()
        assert e_pair[0] is None, "E untouched"
        assert f_pair[0] is None, "F untouched (survives P2 — resource_type mismatch)"

        delete_rows = [
            r for r in rows if r["question"].startswith("op=deleteAclEntries")
        ]
        assert len(delete_rows) == 3, "one audit row per predicate"
        answers = [json.loads(r["answer"]) for r in delete_rows]
        questions = [r["question"] for r in delete_rows]

        assert answers[0]["visible_matched_count"] == 2
        assert answers[0]["expired_ids"] == [ids["B"], ids["A"]]
        assert answers[1]["visible_matched_count"] == 4
        assert answers[1]["expired_ids"] == [ids["D"], ids["C"], ids["G"]]
        assert answers[2]["visible_matched_count"] == 1
        assert answers[2]["expired_ids"] == []

        # v4's own full-string equalities (SF-2 — kept, not replaced) ...
        assert questions[0] == (
            f"op=deleteAclEntries principal=-:{_MP_V} resource=-:- bits=0 tenant=-"
        )
        assert questions[1] == (
            "op=deleteAclEntries principal=-:- resource=agent:agent-1 bits=0 tenant=-"
        )
        assert questions[2] == (
            "op=deleteAclEntries principal=public:- resource=-:- bits=0 tenant=-"
        )
        # ... PLUS the per-field parse (W-Q, ADDENDUM A, additive).
        f1 = _parse_question(questions[0])
        assert f1["op"] == "deleteAclEntries"
        assert f1["principal_type"] == "-"
        assert f1["principal_id"] == _MP_V
        assert f1["resource_type"] == "-"
        assert f1["resource_id"] == "-"
        assert f1["bits"] == "0"
        assert f1["tenant"] == "-"
        f2 = _parse_question(questions[1])
        assert f2["op"] == "deleteAclEntries"
        assert f2["principal_type"] == "-"
        assert f2["principal_id"] == "-"
        assert f2["resource_type"] == "agent"
        assert f2["resource_id"] == "agent-1"
        assert f2["bits"] == "0"
        assert f2["tenant"] == "-"
        f3 = _parse_question(questions[2])
        assert f3["op"] == "deleteAclEntries"
        assert f3["principal_type"] == "public"
        assert f3["principal_id"] == "-"
        assert f3["resource_type"] == "-"
        assert f3["resource_id"] == "-"
        assert f3["bits"] == "0"
        assert f3["tenant"] == "-"


class TestBL4RemainingGuards:
    """BL-4 — noactive (modify's ``_active_rows`` without
    ``_active_clause``), E-A on revoke (count from RETURNING instead
    of the pre-UPDATE SELECT), W1's ``trace_id``/``created_at_ms``/
    ``granted_at_ms`` on modify's INSERT, W5's tenant on modify's
    denial row."""

    async def test_noactive_a_lapsed_sibling_never_contributes_bits(
        self, write_harness: Any, sp_pin: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """noactive — the lapsed sibling is seeded through the CLOCK
        SEAM, 30 MINUTES before "now" (never epoch-1970), so a widened
        ``_active_clause`` (e.g. a grace window shorter than ~56 years)
        would actually leak its SHARE bits — a 1970 timestamp cannot
        distinguish a correct ``_active_rows`` from one with a grace
        period, since no realistic grace window is that wide."""
        owner = _new_user_context(_OWNER)
        key = dict(
            principal_type="user",
            principal_id=_VIEWER,
            resource_type="agent",
            resource_id="agent-1",
        )
        t0 = 1_790_000_000_000
        thirty_min = 30 * 60 * 1000
        # A LAPSED row at the SAME key, expired 30 minutes before "now" —
        # _active_rows must NOT read its bits into old_bits.
        await _insert_grant_full(
            write_harness.factory,
            as_user=_OWNER,
            row_id="lapsed",
            user_sub=_OWNER,
            principal_model="User",
            perm_bits=15,
            expired_at_ms=t0 - thirty_min,
            created_at_ms=t0 - thirty_min - 1,
            updated_at_ms=t0 - thirty_min,
            **key,
        )
        await _insert_grant_full(
            write_harness.factory,
            as_user=_OWNER,
            row_id="active",
            user_sub=_OWNER,
            principal_model="User",
            perm_bits=1,
            expired_at_ms=None,
            created_at_ms=t0 - 1,
            updated_at_ms=t0 - 1,
            **key,
        )
        monkeypatch.setattr(_clock_module, "now_ms", lambda: t0)
        set_current_user_id(owner.user_id)
        try:
            row = await write_harness.service.modify_permission_bits(
                owner, add_bits=2, **key
            )
        finally:
            set_current_user_id(None)
        assert row is not None
        assert row["perm_bits"] == 3, (
            "old_bits must come ONLY from the ACTIVE row (1), never the "
            "lapsed sibling's 15 — (1 | 2) == 3, not (1 | 15 | 2) == 15"
        )

    async def test_e_a_revoke_count_precedes_the_update(
        self, write_harness: Any, sp_pin: Any
    ) -> None:
        """E-A — a PUBLIC row visible to everyone but owned by someone
        else stays unexpirable (owner-only UPDATE ``USING``); the
        revoke's ``visible_matched_count`` must still count it (the
        pre-UPDATE SELECT), never merely ``len(expired_ids)``."""
        await _seed_attacker_resources(write_harness.factory)
        owner = _new_user_context(_OWNER)
        await _insert_grant_full(
            write_harness.factory,
            as_user=_OWNER,
            row_id="a-public",
            user_sub=_OWNER,
            principal_type="public",
            principal_model=None,
            resource_type="agent",
            resource_id="agent-1",
            perm_bits=1,
        )
        await _insert_grant_full(
            write_harness.factory,
            as_user=_ATTACKER,
            row_id="b-public",
            user_sub=_ATTACKER,
            principal_type="public",
            principal_model=None,
            resource_type="agent",
            resource_id="agent-attacker",
            perm_bits=1,
        )
        set_current_user_id(owner.user_id)
        try:
            result = await write_harness.service.revoke_permission(
                owner,
                principal_type="public",
                principal_id=None,
                resource_type="agent",
                resource_id="agent-attacker",
            )
        finally:
            set_current_user_id(None)
        assert result["visible_matched_count"] == 1, (
            "B's public row is VISIBLE (public) but not OWNED by A — "
            "E-A: the count is the pre-UPDATE SELECT, not len(expired_ids)"
        )
        assert result["expired_ids"] == [], "A cannot expire B's row"
        b_pair = await _acl_row_pair(_admin_engine(), write_harness.schema, "b-public")
        assert b_pair[0] is None

    async def test_w1_trace_id_and_stamps_on_modify_insert(
        self, write_harness: Any, sp_pin: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard T + 7.0/X2/X2b/BL-C — modify's NEW row carries the REAL
        trace_id of the active span, and its ``created_at_ms``/
        ``granted_at_ms``/``updated_at_ms`` are the CURRENT stamp —
        never ``None``/``0`` and, more precisely, never INHERITED from
        the superseded row: the clock is frozen at a DIFFERENT,
        well-separated value for the grant vs. the modify, so a bug
        that copied the old row's ``created_at_ms`` cannot hide behind
        two nearly-identical real-wall-clock reads."""
        owner = _new_user_context(_OWNER)
        key = dict(
            principal_type="user",
            principal_id=_VIEWER,
            resource_type="agent",
            resource_id="agent-1",
        )
        t0 = 1_790_000_000_000
        t1 = 1_790_000_500_000
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")
        set_current_user_id(owner.user_id)
        try:
            monkeypatch.setattr(_clock_module, "now_ms", lambda: t0)
            await write_harness.service.grant_permission(owner, perm_bits=1, **key)
            monkeypatch.setattr(_clock_module, "now_ms", lambda: t1)
            with tracer.start_as_current_span("modify"):
                span = trace.get_current_span()
                trace_id_hex = format(span.get_span_context().trace_id, "032x")
                row = await write_harness.service.modify_permission_bits(
                    owner, add_bits=2, **key
                )
        finally:
            set_current_user_id(None)
        assert row is not None
        assert len(trace_id_hex) == 32
        assert row["trace_id"] == trace_id_hex
        assert row["created_at_ms"] == t1, "X2/BL-C — never inherited from the old row"
        assert row["granted_at_ms"] == t1, "X2b"
        assert row["updated_at_ms"] == t1
        admin = _admin_engine()
        try:
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                db_row = conn.execute(
                    text(
                        "SELECT trace_id, created_at_ms, granted_at_ms, updated_at_ms "
                        "FROM console_acl_entries WHERE id = :id"
                    ),
                    {"id": row["id"]},
                ).one()
        finally:
            admin.dispose()
        assert db_row[0] == trace_id_hex
        assert db_row[1] == t1
        assert db_row[2] == t1
        assert db_row[3] == t1

    async def test_w5_modify_denial_row_carries_tenant(
        self, write_harness: Any, sp_pin: Any
    ) -> None:
        """W5-t (ADDENDUM A) — modify's OWN domain-level refusal (a
        soft-deleted resource, mirroring P-3) must carry the caller's
        tenant on the denial row, not ``-``, AND ``bits=new_bits``
        (SF-4: seeding 7, ``remove_bits=4`` gives ``new_bits=3``) —
        never the seeded bits or 0."""
        owner = _new_user_context(_OWNER)
        key = dict(
            principal_type="user",
            principal_id=_VIEWER,
            resource_type="agent",
            resource_id="agent-1",
            tenant_id="t1",
        )
        set_current_user_id(owner.user_id)
        try:
            await write_harness.service.grant_permission(owner, perm_bits=7, **key)
        finally:
            set_current_user_id(None)

        admin = _admin_engine()
        try:
            with admin.begin() as conn:
                conn.execute(text(f'SET search_path TO "{write_harness.schema}"'))
                conn.execute(
                    text(
                        "UPDATE console_agents SET deleted_at_ms = 1 "
                        "WHERE agent_id = 'agent-1'"
                    )
                )

            set_current_user_id(owner.user_id)
            try:
                with pytest.raises(AclWriteRefused):
                    await write_harness.service.modify_permission_bits(
                        owner, remove_bits=4, **key
                    )
            finally:
                set_current_user_id(None)

            rows = _interactions_rows(
                admin, write_harness.schema, resource_id="agent-1"
            )
            denial_rows = [
                r
                for r in rows
                if r["status"] == "failed"
                and r["question"].startswith("op=modifyPermissionBits")
            ]
            assert len(denial_rows) == 1
            assert denial_rows[0]["question"].endswith("tenant=t1"), denial_rows[0][
                "question"
            ]
            fields = _parse_question(denial_rows[0]["question"])
            assert fields == {
                "op": "modifyPermissionBits",
                "principal_type": "user",
                "principal_id": _VIEWER,
                "resource_type": "agent",
                "resource_id": "agent-1",
                "bits": "3",
                "tenant": "t1",
            }
            error_detail = json.loads(denial_rows[0]["error_detail"])
            assert error_detail["predicate_or_attempted_row"] == {
                "principal_type": "user",
                "principal_id": _VIEWER,
                "resource_type": "agent",
                "resource_id": "agent-1",
                "tenant_id": "t1",
                "add_bits": None,
                "remove_bits": 4,
            }
        finally:
            admin.dispose()
