"""Cross-user isolation THROUGH THE REAL HTTP ROUTE on real Postgres
(ACL WU-2c-A, spec §6 + Addenda A/B/C/D1) — the one instrument the
aiosqlite ``client`` cannot be.

**Why this file exists.** The container's aiosqlite factory enforces no
Row-Level Security (2b-core-A2 §8.14), so a write-side isolation
assertion through ``client`` would be green while production refuses.
This harness drives the REAL FastAPI app (``create_app()`` + ``TestClient``)
with:

* the container's ``console_acl`` service and ``postgres_factory`` bound to
  a throwaway-schema session factory connected AS the ``NOSUPERUSER
  NOBYPASSRLS`` app role (migrations 002-017 + 031/032/033 applied by
  running the REAL migration files);
* ``install_rls_listener()`` so the GUC the route's ``require_user``
  binds is the one pushed into every transaction;
* identities through the REAL ``require_user`` cold path (the
  ``_identity`` shim patches only the JWT decode — never
  ``dependency_overrides``);
* a ``NullPool`` engine (S3(a)): no pooled asyncpg connection is ever
  shared across ``TestClient``'s portal loop and the test's loop. The
  fail-closed identity probe (S3(c)) asserts, before the first request,
  that the service really uses THIS factory and that ``current_user`` is
  the NOBYPASSRLS role — a mismatch is a hard failure, never a skip.

Ground truth for "was a row written / expired" is read through an ADMIN
connection (it bypasses RLS), which is stronger than reading as the app
role. Every matrix cell has a CLEAN test and a NEUTER test: the neuter
applies one DDL change to the throwaway schema, repeats the request and
asserts the OPPOSITE observable (the RED target), then restores the
schema and asserts ``pg_policies`` / ``pg_class`` equal their
before-image. Source-level neuters (dropping ``extra="forbid"`` etc.) are
the build record's per-guard table, not tests here.

**Version gap, disclosed:** this harness runs ``postgres:16``; the live
cluster runs PostgreSQL 18.3.

**Limitations these tests prove OF, not controls:** N-6 (denial rows are
RLS-scoped to the subject that caused them; no cross-subject auditor read
exists) and N-10/N-11 (a non-owner's revoke/modify/expire attempt on rows
it can SEE but does not own is recorded as ``status=success`` with empty
``expired_ids``; 032 filters UPDATEs and only INSERTs are refused).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from audittrace import dependencies
from audittrace.db.rls import install_rls_listener
from audittrace.services.console_acl._postgres import (
    PostgresConsoleAclEntriesService,
)
from tests.test_acl_ownership_rls import (
    _ADMIN_URL,
    _APP_PASSWORD,
    _APP_ROLE,
    _ATTACKER,
    _OWNER,
    _SKIP_REASON,
    _drop_schema,
    _run_migration_upgrade,
    _seed_owner_resources,
    _StaticPostgresFactory,
)
from tests.test_acl_write_path_rls import _build_acl_write_schema
from tests.test_console_acl_write_routes import _identity

pytestmark = pytest.mark.skipif(_ADMIN_URL is None, reason=_SKIP_REASON)

_THIRD = "third-sub-0003"
_FULL = "memory:acl:read-own memory:acl:write audittrace:audit"
_SC09 = "audittrace:query audittrace:context memory:conversational:read-own"
_POLICY = "tenant_isolation_console_acl_entries"
_AGENT = "agent-1"  # seeded, owned by _OWNER
_GROUP = "group-1"  # seeded, owned by _OWNER


def _grants(rid: str = _AGENT, rtype: str = "agent") -> str:
    return f"/console/acl/{rtype}/{rid}/grants"


def _body(principal: str, bits: int = 1, **over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "principal_type": "user",
        "principal_id": principal,
        "perm_bits": bits,
    }
    out.update(over)
    return out


class Harness:
    """The throwaway schema + the real app wired to it."""

    def __init__(self, schema: str, factory: Any, client: TestClient) -> None:
        assert _ADMIN_URL is not None
        self.schema = schema
        self.factory = factory
        self.client = client
        self.admin = create_engine(_ADMIN_URL, pool_pre_ping=True)

    # -- the route, as a subject ------------------------------------------

    def call(
        self, sub: str, method: str, path: str, *, scope: str = _FULL, **kw: Any
    ) -> Any:
        with _identity(sub=sub, scope=scope):
            return self.client.request(
                method, path, headers={"Authorization": f"Bearer t-{sub}"}, **kw
            )

    # -- ground truth through the ADMIN connection (bypasses RLS) ---------

    def sql(self, statement: str, **params: Any) -> list[dict[str, Any]]:
        with self.admin.begin() as conn:
            conn.execute(text(f'SET search_path TO "{self.schema}"'))
            result = conn.execute(text(statement), params)
            return [dict(r) for r in result.mappings()] if result.returns_rows else []

    def entries(self, resource_id: str = _AGENT) -> list[dict[str, Any]]:
        return self.sql(
            "SELECT id, user_sub, principal_id, perm_bits, expired_at_ms, trace_id "
            "FROM console_acl_entries WHERE resource_id = :r "
            "ORDER BY created_at_ms, id",
            r=resource_id,
        )

    def audit(self, **filters: str) -> list[dict[str, Any]]:
        where = " AND ".join(
            ["event_class = 'acl_authz'"] + [f"{k} = :{k}" for k in filters]
        )
        return self.sql(
            "SELECT id, user_id, status, failure_class, question, answer, "
            "error_detail, trace_id, session_id FROM interactions "
            f"WHERE {where} ORDER BY id",
            **filters,
        )

    def audit_count(self) -> int:
        return len(self.audit())

    # -- DDL neuters + restore --------------------------------------------

    def policies(self, table: str) -> list[tuple[Any, ...]]:
        rows = self.sql(
            "SELECT policyname, cmd, qual, with_check FROM pg_policies "
            "WHERE schemaname = :s AND tablename = :t ORDER BY policyname",
            s=self.schema,
            t=table,
        )
        return [tuple(r.values()) for r in rows]

    def rls_flags(self, table: str) -> tuple[bool, bool]:
        row = self.sql(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :s AND c.relname = :t",
            s=self.schema,
            t=table,
        )[0]
        return (row["relrowsecurity"], row["relforcerowsecurity"])

    def restore_032(self) -> None:
        with self.admin.begin() as conn:
            conn.execute(text(f'SET search_path TO "{self.schema}"'))
            for suffix in ("select", "insert", "update", "delete"):
                conn.execute(
                    text(
                        f"DROP POLICY IF EXISTS {_POLICY}_{suffix} ON console_acl_entries"
                    )
                )
            _run_migration_upgrade(conn, "032_tighten_console_acl_entries_ownership.py")

    @contextlib.contextmanager
    def neutered_acl_policies(self, *statements: str) -> Iterator[None]:
        """Apply DDL to ``console_acl_entries``'s policies, yield, restore
        by re-running 032, and assert ``pg_policies`` equals the
        before-image (the restore is proven, not assumed)."""
        before = self.policies("console_acl_entries")
        assert len(before) == 4
        with self.admin.begin() as conn:
            conn.execute(text(f'SET search_path TO "{self.schema}"'))
            for statement in statements:
                conn.execute(text(statement))
        try:
            assert self.policies("console_acl_entries") != before, "neuter was a no-op"
            yield
        finally:
            self.restore_032()
        assert self.policies("console_acl_entries") == before

    @contextlib.contextmanager
    def interactions_rls_disabled(self) -> Iterator[None]:
        before = self.rls_flags("interactions")
        assert before == (True, True), "interactions must be ENABLE+FORCE RLS"
        before_pol = self.policies("interactions")
        self.sql("ALTER TABLE interactions DISABLE ROW LEVEL SECURITY")
        try:
            assert self.rls_flags("interactions") == (False, True)
            yield
        finally:
            self.sql("ALTER TABLE interactions ENABLE ROW LEVEL SECURITY")
            self.sql("ALTER TABLE interactions FORCE ROW LEVEL SECURITY")
        assert self.rls_flags("interactions") == before
        assert self.policies("interactions") == before_pol


def _permissive_insert() -> list[str]:
    return [
        f"DROP POLICY {_POLICY}_insert ON console_acl_entries",
        f"CREATE POLICY {_POLICY}_insert ON console_acl_entries "
        "FOR INSERT WITH CHECK (true)",
    ]


def _permissive_update() -> list[str]:
    return [
        f"DROP POLICY {_POLICY}_update ON console_acl_entries",
        f"CREATE POLICY {_POLICY}_update ON console_acl_entries "
        "FOR UPDATE USING (true) WITH CHECK (true)",
    ]


def _permissive_select() -> list[str]:
    return [
        f"DROP POLICY {_POLICY}_select ON console_acl_entries",
        f"CREATE POLICY {_POLICY}_select ON console_acl_entries FOR SELECT USING (true)",
    ]


def _owner_only_select() -> list[str]:
    return [
        f"DROP POLICY {_POLICY}_select ON console_acl_entries",
        f"CREATE POLICY {_POLICY}_select ON console_acl_entries FOR SELECT "
        "USING (user_sub = current_setting('app.current_user_id', true))",
    ]


@pytest_asyncio.fixture
async def h(app: Any) -> Any:
    """The route harness. Binds the container's ``console_acl`` service and
    ``postgres_factory`` to the throwaway schema's NullPool factory."""
    assert _ADMIN_URL is not None
    schema = _build_acl_write_schema()
    admin_url = _ADMIN_URL.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    at = admin_url.index("@")
    app_url = f"postgresql+asyncpg://{_APP_ROLE}:{_APP_PASSWORD}{admin_url[at:]}"
    engine = create_async_engine(
        app_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    install_rls_listener()
    await _seed_owner_resources(factory)
    dependencies.container._instances["postgres_factory"] = _StaticPostgresFactory(
        factory
    )
    dependencies.container._instances["console_acl"] = PostgresConsoleAclEntriesService(
        session_factory=factory
    )
    # Fail-closed identity probe (S3(c)): never a skip.
    assert dependencies.get_console_acl_service()._session_factory is factory  # type: ignore[attr-defined]
    async with factory() as session:
        current = (await session.execute(text("SELECT current_user"))).scalar_one()
    assert current == _APP_ROLE, f"harness connects as {current!r}, not {_APP_ROLE!r}"
    harness = Harness(schema, factory, TestClient(app))
    try:
        yield harness
    finally:
        harness.client.close()
        harness.admin.dispose()
        await engine.dispose()
        _drop_schema(schema)


# ── the harness itself is not vacuous ──────────────────────────────────────


class TestHarnessIdentity:
    async def test_owner_rows_exist_and_policies_are_032s(self, h: Harness) -> None:
        assert [p[0] for p in h.policies("console_acl_entries")] == [
            f"{_POLICY}_delete",
            f"{_POLICY}_insert",
            f"{_POLICY}_select",
            f"{_POLICY}_update",
        ]
        assert h.rls_flags("console_acl_entries")[0] is True

    async def test_listener_pushes_the_route_bound_guc(self, h: Harness) -> None:
        """The route's identity reaches Postgres: the owner's GET shows
        a grant the owner wrote, and a stranger's identical GET does not."""
        r = h.call(_OWNER, "POST", _grants(), json=_body(_THIRD, 1))
        assert r.status_code == 201, r.text
        mine = h.call(_THIRD, "GET", f"/console/acl/agent/{_AGENT}/permissions")
        theirs = h.call(_ATTACKER, "GET", f"/console/acl/agent/{_AGENT}/permissions")
        assert mine.json() == {"perm_bits": 1}
        assert theirs.json() == {"perm_bits": 0}


# ── N-1: B grants on A's resource ──────────────────────────────────────────


class TestN1GrantOnForeignResource:
    async def test_refused_403_policy_no_row_one_denial(self, h: Harness) -> None:
        before = h.audit_count()
        r = h.call(_ATTACKER, "POST", _grants(), json=_body(_ATTACKER, 15))
        assert r.status_code == 403, r.text
        detail = r.json()["detail"]
        assert detail["failure_class"] == "acl_denied_policy"
        assert detail["db_error_class"] == "42501"
        assert h.entries() == [], "no row may exist for the key"
        assert h.audit_count() == before + 1
        denial = h.audit(user_id=_ATTACKER)[-1]
        assert denial["status"] == "failed"
        assert denial["failure_class"] == "acl_denied_policy"
        assert denial["session_id"] is None

    async def test_neuter_permissive_insert_policy_lands_201(self, h: Harness) -> None:
        """RED target: with the INSERT policy permissive the same request
        is a 201 and a row exists. (Dropping the policy alone would leave
        RLS default-deny — the neuter must REPLACE it.)"""
        with h.neutered_acl_policies(*_permissive_insert()):
            r = h.call(_ATTACKER, "POST", _grants(), json=_body(_ATTACKER, 15))
            assert r.status_code == 201
            assert len(h.entries()) == 1


# ── N-2 / N-4: B revokes / expires A's grant ───────────────────────────────


def _seed_grant_to_third(h: Harness) -> str:
    r = h.call(_OWNER, "POST", _grants(), json=_body(_THIRD, 1))
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


class TestN2RevokeForeignGrant:
    async def test_200_empty_and_row_untouched(self, h: Harness) -> None:
        gid = _seed_grant_to_third(h)
        r = h.call(
            _ATTACKER,
            "DELETE",
            _grants(),
            params={"principal_type": "user", "principal_id": _THIRD},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["expired_ids"] == [] and body["visible_matched_count"] == 0
        row = next(e for e in h.entries() if e["id"] == gid)
        assert row["expired_at_ms"] is None

    async def test_neuter_visibility_and_update_open_expires_the_row(
        self, h: Harness
    ) -> None:
        gid = _seed_grant_to_third(h)
        with h.neutered_acl_policies(*_permissive_select(), *_permissive_update()):
            r = h.call(
                _ATTACKER,
                "DELETE",
                _grants(),
                params={"principal_type": "user", "principal_id": _THIRD},
            )
            assert r.json()["expired_ids"] == [gid]
            assert next(e for e in h.entries() if e["id"] == gid)["expired_at_ms"]


class TestN4ExpireForeignResource:
    async def test_200_empty_and_rows_intact(self, h: Harness) -> None:
        gid = _seed_grant_to_third(h)
        r = h.call(
            _ATTACKER,
            "POST",
            "/console/acl/expire",
            json={"predicates": [{"resource_type": "agent", "resource_id": _AGENT}]},
        )
        assert r.status_code == 200
        assert r.json()["expired_ids"] == []
        assert r.json()["visible_matched_count"] == 0
        assert next(e for e in h.entries() if e["id"] == gid)["expired_at_ms"] is None

    async def test_neuter_visibility_and_update_open_expires_the_row(
        self, h: Harness
    ) -> None:
        gid = _seed_grant_to_third(h)
        with h.neutered_acl_policies(*_permissive_select(), *_permissive_update()):
            r = h.call(
                _ATTACKER,
                "POST",
                "/console/acl/expire",
                json={
                    "predicates": [{"resource_type": "agent", "resource_id": _AGENT}]
                },
            )
            assert r.json()["expired_ids"] == [gid]


# ── N-3: B modifies A's grant ──────────────────────────────────────────────


class TestN3ModifyForeignGrant:
    _PATCH = {"principal_type": "user", "principal_id": _THIRD, "add_bits": 2}

    async def test_404_untouched_no_new_row_visible_count_zero(
        self, h: Harness
    ) -> None:
        gid = _seed_grant_to_third(h)
        r = h.call(_ATTACKER, "PATCH", _grants(), json=self._PATCH)
        assert r.status_code == 404
        entries = h.entries()
        assert [e["id"] for e in entries] == [gid], "no new row"
        assert entries[0]["expired_at_ms"] is None
        row = h.audit(user_id=_ATTACKER)[-1]
        assert row["status"] == "success"
        assert json.loads(row["answer"])["visible_matched_count"] == 0

    async def test_neuter_select_true_moves_the_visible_count_not_the_status(
        self, h: Harness
    ) -> None:
        """A2: under a ``USING (true)`` SELECT B's W3 SEES A's row, the
        owner-only UPDATE filters it (expires 0), and the race branch
        answers 404 BOTH ways — so the RED target is the audit row's
        ``visible_matched_count`` (0 clean -> 1 neutered), not the status."""
        _seed_grant_to_third(h)
        with h.neutered_acl_policies(*_permissive_select()):
            r = h.call(_ATTACKER, "PATCH", _grants(), json=self._PATCH)
            assert r.status_code == 404
            row = h.audit(user_id=_ATTACKER)[-1]
            assert json.loads(row["answer"])["visible_matched_count"] == 1


# ── N-5: B bulk-grants on A's resource ─────────────────────────────────────


class TestN5BulkOnForeignResource:
    async def test_403_bulk_rollback_zero_rows_denial_with_op_index(
        self, h: Harness
    ) -> None:
        before = h.audit_count()
        r = h.call(
            _ATTACKER,
            "POST",
            _grants() + "/bulk",
            json={"ops": [_body(_ATTACKER, 1), _body(_THIRD, 2)]},
        )
        assert r.status_code == 403
        assert r.json()["detail"]["failure_class"] == "acl_denied_bulk_rollback"
        assert h.entries() == []
        assert h.audit_count() == before + 1
        denial = h.audit(user_id=_ATTACKER)[-1]
        assert denial["status"] == "failed"
        detail = json.loads(denial["error_detail"])
        assert detail["predicate_or_attempted_row"]["op_index"] == 0

    async def test_neuter_permissive_insert_lands_both_ops(self, h: Harness) -> None:
        with h.neutered_acl_policies(*_permissive_insert()):
            r = h.call(
                _ATTACKER,
                "POST",
                _grants() + "/bulk",
                json={"ops": [_body(_ATTACKER, 1), _body(_THIRD, 2)]},
            )
            assert r.status_code == 200
            assert len(r.json()["acl_entry_ids"]) == 2
            assert len(h.entries()) == 2


# ── N-6: the audit row is visible to its SUBJECT only (limitation proof) ──


class TestN6DenialRowsAreSubjectScoped:
    """A PROOF OF THE Q-3 LIMITATION, not of a control: a denial row is
    RLS-scoped to the subject that caused it, so no auditor (here: the
    resource's owner A) can read B's denial through ``GET /interactions``.
    Runs ONLY on this real-Postgres harness (the aiosqlite visibility
    test is forbidden — it would be vacuous)."""

    def _read(self, h: Harness, sub: str) -> list[dict[str, Any]]:
        r = h.call(sub, "GET", "/interactions", params={"event_class": "acl_authz"})
        assert r.status_code == 200, r.text
        return list(r.json()["interactions"])

    async def test_b_sees_own_denial_a_sees_none_naming_b(self, h: Harness) -> None:
        h.call(_ATTACKER, "POST", _grants(), json=_body(_ATTACKER, 15))
        b_view = self._read(h, _ATTACKER)
        assert len(b_view) == 1 and b_view[0]["user_id"] == _ATTACKER
        assert b_view[0]["status"] == "failed"
        a_view = self._read(h, _OWNER)
        assert [r for r in a_view if r["user_id"] == _ATTACKER] == []
        assert a_view == [], "A has written nothing: A sees zero rows"

    async def test_neuter_interactions_rls_disabled_exposes_b_to_a(
        self, h: Harness
    ) -> None:
        """B4's corrected neuter: ``NO FORCE`` would leave the app role's
        view unchanged (FORCE binds the table OWNER only); the app role is
        subject to RLS whenever it is ENABLED, so the neuter DISABLES it
        (as the admin connection) and restores ENABLE + FORCE."""
        h.call(_ATTACKER, "POST", _grants(), json=_body(_ATTACKER, 15))
        with h.interactions_rls_disabled():
            exposed = self._read(h, _OWNER)
            assert [r["user_id"] for r in exposed] == [_ATTACKER]


# ── N-7: the positive control chain, as the owner ──────────────────────────


class TestN7HappyChain:
    async def test_grant_modify_revoke_and_effective_bits(self, h: Harness) -> None:
        g = h.call(_OWNER, "POST", _grants(), json=_body(_THIRD, 1))
        assert g.status_code == 201
        gid = g.json()["id"]
        eff = lambda: h.call(  # noqa: E731
            _THIRD, "GET", f"/console/acl/agent/{_AGENT}/permissions"
        ).json()["perm_bits"]
        assert eff() == 1
        m = h.call(
            _OWNER,
            "PATCH",
            _grants(),
            json={"principal_type": "user", "principal_id": _THIRD, "add_bits": 8},
        )
        assert m.status_code == 200 and m.json()["perm_bits"] == 9
        assert eff() == 9
        entries = h.entries()
        assert [e["perm_bits"] for e in entries] == [1, 9]
        assert entries[0]["id"] == gid and entries[0]["expired_at_ms"] is not None
        assert entries[1]["expired_at_ms"] is None
        d = h.call(
            _OWNER,
            "DELETE",
            _grants(),
            params={"principal_type": "user", "principal_id": _THIRD},
        )
        assert d.status_code == 200
        assert d.json()["expired_ids"] == [entries[1]["id"]]
        assert eff() == 0

    async def test_exact_audit_row_deltas_on_postgres(self, h: Harness) -> None:
        n0 = h.audit_count()
        h.call(_OWNER, "POST", _grants(), json=_body(_THIRD, 1))
        assert h.audit_count() == n0 + 1
        h.call(
            _OWNER,
            "POST",
            _grants() + "/bulk",
            json={"ops": [_body("p1"), _body("p2"), _body("p3")]},
        )
        assert h.audit_count() == n0 + 1 + 3
        h.call(
            _OWNER,
            "POST",
            "/console/acl/expire",
            json={"predicates": [{"resource_id": _AGENT}, {"resource_id": _GROUP}]},
        )
        assert h.audit_count() == n0 + 1 + 3 + 2
        assert {r["session_id"] for r in h.audit()} == {None}

    async def test_trace_linkage_response_audit_and_entry_agree(
        self, h: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opentelemetry.sdk.trace import TracerProvider

        from audittrace import telemetry

        monkeypatch.setattr(
            telemetry, "_tracer", TracerProvider().get_tracer("n7-trace")
        )
        g = h.call(_OWNER, "POST", _grants(), json=_body(_THIRD, 1))
        trace_id = g.json()["trace_id"]
        assert trace_id is not None and len(trace_id) == 32
        assert h.entries()[0]["trace_id"] == trace_id
        rows = h.call(
            _OWNER, "GET", "/interactions", params={"trace_id": trace_id}
        ).json()["interactions"]
        assert [r["trace_id"] for r in rows] == [trace_id]

    async def test_r7_filter_is_rls_scoped_to_the_caller(
        self, h: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R7 narrows WITHIN the caller's rows: B asking for A's trace id
        gets nothing (RLS), A gets the row."""
        from opentelemetry.sdk.trace import TracerProvider

        from audittrace import telemetry

        monkeypatch.setattr(telemetry, "_tracer", TracerProvider().get_tracer("r7"))
        first = h.call(_OWNER, "POST", _grants(), json=_body(_THIRD, 1)).json()
        second = h.call(_OWNER, "POST", _grants(), json=_body("p-other", 1)).json()
        assert first["trace_id"] != second["trace_id"]
        mine = h.call(
            _OWNER, "GET", "/interactions", params={"trace_id": first["trace_id"]}
        ).json()
        assert mine["total"] == 1, "the filter must narrow: A wrote TWO rows"
        assert [r["trace_id"] for r in mine["interactions"]] == [first["trace_id"]]
        theirs = h.call(
            _ATTACKER, "GET", "/interactions", params={"trace_id": first["trace_id"]}
        )
        assert theirs.json()["total"] == 0


# ── N-8: SC-09 shape — the scope gate, the ACL layer never entered ────────


_SC09_REQUESTS = [
    ("POST", _grants(), {"json": _body(_ATTACKER)}),
    (
        "DELETE",
        _grants(),
        {"params": {"principal_type": "user", "principal_id": _THIRD}},
    ),
    (
        "PATCH",
        _grants(),
        {"json": {"principal_type": "user", "principal_id": _THIRD, "add_bits": 1}},
    ),
    ("POST", _grants() + "/bulk", {"json": {"ops": [_body(_ATTACKER)]}}),
    (
        "POST",
        "/console/acl/expire",
        {"json": {"predicates": [{"resource_id": _AGENT}]}},
    ),
]


class TestN8Sc09ShapeScopeGate:
    @pytest.mark.parametrize("idx", range(5))
    async def test_every_write_route_403_with_no_acl_or_denial_row(
        self, h: Harness, idx: int
    ) -> None:
        method, path, kw = _SC09_REQUESTS[idx]
        before = h.audit_count()
        r = h.call(_OWNER, method, path, scope=_SC09, **kw)
        assert r.status_code == 403
        assert h.audit_count() == before, "the ACL layer must never be entered"
        assert h.entries() == []

    async def test_audit_read_is_also_403_without_the_audit_scope(
        self, h: Harness
    ) -> None:
        assert h.call(_OWNER, "GET", "/interactions", scope=_SC09).status_code == 403

    async def test_neuter_adding_the_write_scope_opens_the_route(
        self, h: Harness
    ) -> None:
        r = h.call(
            _OWNER,
            "POST",
            _grants(),
            scope=_SC09 + " memory:acl:write",
            json=_body(_THIRD),
        )
        assert r.status_code == 201


# ── N-9: hostile body ──────────────────────────────────────────────────────


class TestN9HostileBody:
    @pytest.mark.parametrize(
        "extra",
        [
            {"user_sub": _ATTACKER},
            {"granted_by": _ATTACKER},
            {"trace_id": "0" * 32},
            {"session_id": "s"},
            {"user_id": _ATTACKER},
        ],
    )
    async def test_extra_keys_are_422_not_silently_ignored(
        self, h: Harness, extra: dict[str, Any]
    ) -> None:
        before = h.audit_count()
        r = h.call(_OWNER, "POST", _grants(), json=_body(_THIRD, 1, **extra))
        assert r.status_code == 422
        assert h.entries() == []
        assert h.audit_count() == before


# ── N-10 / N-11: limitation proofs (a non-owner who can SEE the row) ──────


class TestN10N11NonOwnerAttemptsOnVisibleRows:
    """PROOFS OF A LIMITATION (disclosure 14): 032 *filters* UPDATEs by
    ``USING (user_sub = caller)`` and refuses only INSERTs, so a non-owner
    acting on a row that names it is recorded as ``status=success`` with
    empty ``expired_ids`` — an auditor filtering ``status=failed`` sees
    only grant/bulk attempts."""

    def _seed_grant_naming_b(self, h: Harness) -> str:
        r = h.call(_OWNER, "POST", _grants(), json=_body(_ATTACKER, 1))
        assert r.status_code == 201
        return str(r.json()["id"])

    async def test_n10_modify_404_one_success_row_visible_one_expired_none(
        self, h: Harness
    ) -> None:
        gid = self._seed_grant_naming_b(h)
        before = h.audit_count()
        r = h.call(
            _ATTACKER,
            "PATCH",
            _grants(),
            json={"principal_type": "user", "principal_id": _ATTACKER, "add_bits": 2},
        )
        assert r.status_code == 404
        assert h.audit_count() == before + 1
        row = h.audit(user_id=_ATTACKER)[-1]
        assert row["status"] == "success"
        answer = json.loads(row["answer"])
        assert answer["visible_matched_count"] == 1 and answer["expired_ids"] == []
        entries = h.entries()
        assert [e["id"] for e in entries] == [gid] and entries[0][
            "expired_at_ms"
        ] is None

    async def test_n11_revoke_200_one_success_row_visible_one_expired_none(
        self, h: Harness
    ) -> None:
        gid = self._seed_grant_naming_b(h)
        before = h.audit_count()
        r = h.call(
            _ATTACKER,
            "DELETE",
            _grants(),
            params={"principal_type": "user", "principal_id": _ATTACKER},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["expired_ids"] == [] and body["visible_matched_count"] == 1
        assert h.audit_count() == before + 1
        assert h.audit(user_id=_ATTACKER)[-1]["status"] == "success"
        assert next(e for e in h.entries() if e["id"] == gid)["expired_at_ms"] is None

    @pytest.mark.parametrize("which", ["modify", "revoke"])
    async def test_neuter_owner_only_select_moves_the_visible_integer(
        self, h: Harness, which: str
    ) -> None:
        """RED target = the audit row's integer: with an owner-only SELECT
        policy B can no longer see the row naming it (1 -> 0)."""
        self._seed_grant_naming_b(h)
        with h.neutered_acl_policies(*_owner_only_select()):
            if which == "modify":
                h.call(
                    _ATTACKER,
                    "PATCH",
                    _grants(),
                    json={
                        "principal_type": "user",
                        "principal_id": _ATTACKER,
                        "add_bits": 2,
                    },
                )
            else:
                h.call(
                    _ATTACKER,
                    "DELETE",
                    _grants(),
                    params={"principal_type": "user", "principal_id": _ATTACKER},
                )
            row = h.audit(user_id=_ATTACKER)[-1]
            assert json.loads(row["answer"])["visible_matched_count"] == 0


# ── the DB, not the route, is the control for out-of-range bits ───────────


class TestDatabaseIsTheControl:
    async def test_policy_denial_is_403_and_leaves_no_other_trace(
        self, h: Harness
    ) -> None:
        """A second owner-only check on the other resource type: B against
        A's prompt group is refused the same way (``promptGroup`` resolver
        in the 032 subquery)."""
        r = h.call(
            _ATTACKER, "POST", _grants(_GROUP, "promptGroup"), json=_body(_ATTACKER, 1)
        )
        assert r.status_code == 403
        assert r.json()["detail"]["failure_class"] == "acl_denied_policy"
        assert h.entries(_GROUP) == []

    async def test_unsupported_resource_types_are_refused_for_everyone(
        self, h: Harness
    ) -> None:
        """No resolver => 032 refuses every row, even the 'owner' (the four
        resource types without a sovereign store)."""
        r = h.call(_OWNER, "POST", _grants("x", "skill"), json=_body(_THIRD, 1))
        assert r.status_code == 403
