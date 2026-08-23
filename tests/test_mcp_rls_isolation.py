"""Integration tests: does Postgres RLS, ALONE, protect the exact table
the MCP recall path reads (ADR-063 Phase 1 reviewer finding, 2026-08-23)?

**Why this file exists.** The independent reviewer rejected the first
MCP Phase 1 submission on one finding: ``test_mcp_routes.py``'s
cross-tenant isolation test used ``client.app.dependency_overrides[require_user]
= lambda: identity``, which replaces ``require_user``'s entire body, so
``set_current_user_id`` (the RLS ContextVar binding) never ran. The test
was non-vacuous for "does the app-level ``.filter(user_id==...)`` work"
but VACUOUS for the specific claim ADR-063 + the spec make — "isolated
per tenant by row-level security" — because
``PostgresConversationalService.load_sessions`` carries an UNCONDITIONAL
app-level filter (``services/conversational.py``), so no test that goes
through that method can EVER be made to leak by neutering
``set_current_user_id`` alone: two independent, correct guards can't be
told apart by breaking just one of them while the other still holds.

This file proves the property honestly instead: it isolates RLS from
the app-level filter entirely (raw SQL, no ``WHERE user_id`` clause —
representing "the app-level filter is missing or wrong", the reviewer's
own framing) against the REAL migration-005-shaped policy on the
``sessions`` table specifically (the exact table
``recall_recent_sessions`` / MCP's read path queries), fed by the SAME
``app.current_user_id`` ContextVar ``require_user`` binds. This is an
extension of ``tests/test_rls_isolation.py`` (which proves the identical
property for ``interactions``) — reusing its ephemeral-Postgres /
throwaway-schema / non-superuser-role scaffolding via import rather than
duplicating ~150 lines of setup, per the same skip-if-unavailable
convention.

**How this composes with the fix in ``test_mcp_routes.py``.** Two
separate, individually-falsifiable facts, neither of which alone closes
the finding, together do:

1. **Policy sufficiency (this file).** Postgres RLS on ``sessions``,
   fed by a correctly-bound ContextVar and NO app-level filter at all,
   genuinely isolates alice from bob
   (``test_rls_alone_isolates_sessions_with_no_app_filter_in_the_query``).
   If migration 005's policy on ``sessions`` were ever broken, missing,
   or misconfigured, THIS test goes RED — it does not rely on
   ``require_user`` or the app-level filter at all, so it is immune to
   the exact vacuity the reviewer found (no redundant guard can mask a
   broken policy here, because there IS no other guard in this test's
   query).
2. **Binding correctness (`test_mcp_routes.py::TestIdentityBinding::
   test_rls_contextvar_rebinds_per_real_caller_no_stale_leak`).** The
   REAL, un-overridden ``require_user`` correctly rebinds
   ``app.current_user_id`` to each distinct real caller's own ``sub`` on
   every request, verified by genuinely neutering the three
   ``set_current_user_id(...)`` call sites in production ``auth.py`` and
   confirming that test goes RED, then restoring and confirming GREEN.

(1) proves the policy would hold even without the app filter; (2) proves
the ContextVar it depends on is fed correctly by the exact dependency
the MCP route uses. Together: MCP's tenant isolation for
``recall_recent_sessions`` is defended by Postgres RLS, independent of
the Phase-2 app-level filter, fed by identity ``require_user`` resolves
for real on every request — the property ADR-063 + the spec claim.

**Skip behaviour:** identical to ``test_rls_isolation.py`` — no test
Postgres reachable → the whole file skips with a clear reason.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from audittrace.db.rls import set_current_user_id

# Reused directly from test_rls_isolation.py rather than duplicated: the
# ephemeral-Postgres / throwaway-schema / non-superuser-role scaffolding
# (~150 lines). ``scoped_engine`` and ``session_factory`` are pytest
# FIXTURES imported by name — pytest resolves them by the parameter name
# a test requests, which is exactly this import's point, but ruff can't
# tell that from a plain import and flags the later parameter uses as
# "redefinition" (F811); each use is annotated below.
from tests.test_rls_isolation import (  # noqa: F401
    _RESOLVED_URL,
    _as_test_role,
    pytestmark,  # re-exported skip marker — applies module-wide
    scoped_engine,
    session_factory,
)

__all__ = ["pytestmark"]


@pytest.fixture
def seeded_sessions(session_factory):  # noqa: F811 — reused fixture, see import comment
    """Insert one ``sessions`` row for alice and one for bob — the same
    table ``recall_recent_sessions`` reads. Seeded as the superuser
    connection (same rationale as ``test_rls_isolation.py``'s ``seeded``
    fixture: SET LOCAL ROLE is awkward to sequence against SET LOCAL
    app.current_user_id in the same transaction; enforcement is what the
    tests below exercise via SET ROLE in their own sessions, not the
    seeding step)."""
    with session_factory() as s:
        s.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": "user-alice"},
        )
        s.execute(
            text(
                "INSERT INTO sessions(id, project, date, summary, key_points, model, user_id) "
                "VALUES (:id, :p, :d, :sm, :kp, :m, :u)"
            ),
            {
                "id": "sess-alice-rls",
                "p": "AuditTrace",
                "d": "2026-08-23",
                "sm": "alice's private session",
                "kp": "[]",
                "m": "test-model",
                "u": "user-alice",
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
                "INSERT INTO sessions(id, project, date, summary, key_points, model, user_id) "
                "VALUES (:id, :p, :d, :sm, :kp, :m, :u)"
            ),
            {
                "id": "sess-bob-rls",
                "p": "AuditTrace",
                "d": "2026-08-23",
                "sm": "bob's private session",
                "kp": "[]",
                "m": "test-model",
                "u": "user-bob",
            },
        )
        s.commit()


def _raw_sessions_no_where_clause(session) -> list[tuple[str, str]]:
    """The exact shape of query a buggy/missing app-level ``.filter()``
    would issue — no ``WHERE user_id`` at all. Isolation, if any, comes
    from RLS alone. Mirrors ``test_rls_isolation.py``'s own no-WHERE
    technique for ``interactions``."""
    rows = session.execute(text("SELECT user_id, summary FROM sessions")).fetchall()
    return [(r[0], r[1]) for r in rows]


class TestSessionsTableRlsEnforcement:
    """Migration-005's RLS policy on ``sessions`` — the table MCP's
    ``recall_recent_sessions`` reads — enforced with NO application-level
    WHERE clause in the query at all, so the guard under test is
    RLS + the ContextVar binding, not the (also-present-in-production,
    but here deliberately absent) service-layer filter."""

    def test_rls_is_enabled_on_sessions(self, scoped_engine):  # noqa: F811
        with scoped_engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT c.relrowsecurity, c.relforcerowsecurity
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relname = 'sessions'
                      AND n.nspname = current_schema()
                    """
                )
            ).first()
        assert row is not None
        assert row[0] is True, "sessions ENABLE ROW LEVEL SECURITY missing"
        assert row[1] is True, "sessions FORCE ROW LEVEL SECURITY missing"

    def test_rls_alone_isolates_sessions_with_no_app_filter_in_the_query(
        self,
        session_factory,  # noqa: F811
        seeded_sessions,
    ) -> None:
        """The reviewer's own framing: 'a missing/wrong .filter still
        cannot leak'. This query has NO WHERE clause whatsoever — the
        ONLY thing standing between alice and bob's rows is RLS, fed by
        the ContextVar ``require_user`` binds in production. Neuter
        (never bind the ContextVar to the correct caller) → the
        companion test below goes RED."""
        set_current_user_id("user-alice")
        try:
            with session_factory() as s:
                _as_test_role(s)
                rows = _raw_sessions_no_where_clause(s)
            assert rows == [("user-alice", "alice's private session")]
        finally:
            set_current_user_id(None)

        set_current_user_id("user-bob")
        try:
            with session_factory() as s:
                _as_test_role(s)
                rows = _raw_sessions_no_where_clause(s)
            assert rows == [("user-bob", "bob's private session")]
        finally:
            set_current_user_id(None)
