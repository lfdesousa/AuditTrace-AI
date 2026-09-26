"""Unit tests for the sovereign ACL write path — **ACL 2b-core-A1**
(``2026-09-26-SPEC-acl-2b-core-A-write-path.md`` + ADDENDA U/V/W/X).

Runs ``grant_permission``/``bulk_write_acl_entries`` against BOTH
implementations — ``PostgresConsoleAclEntriesService`` (aiosqlite via
``InMemoryPostgresFactory``, wired by the standard ``client`` fixture)
and ``MockConsoleAclEntriesService`` (fresh instance, sharing the SAME
wired ``postgres_factory`` the ``client`` fixture registers — spec §7:
"a mock write with no wired factory raises"). Audit-row assertions go
through the REAL ``/interactions`` HTTP route
(``feedback_test_through_real_http_route``), never a bare service call.

**The real-Postgres-only proofs — shape derivation (rollback/pg_stat_
activity pairs), O-3/Q-4 DDL neuters, R-8's constraint-redundancy
finding, the savepoint pin, P-4 — live in the NEW harness file
``tests/test_acl_write_path_rls.py`` (spec §8), which this file does
NOT duplicate.** This file's job is functional correctness + the
mock/aiosqlite half of the neuters spec §7 scopes to BOTH
implementations (#1, #2, #4).
"""

from __future__ import annotations

import dataclasses
import json
import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from audittrace import dependencies
from audittrace.db.models import ConsoleAclEntry
from audittrace.services.console_acl import (
    MAX_PERM_BITS,
    PERMISSION_BIT_DELETE,
    PERMISSION_BIT_EDIT,
    PERMISSION_BIT_SHARE,
    PERMISSION_BIT_VIEW,
    AclGrantOp,
    ConsoleAclEntriesService,
    MockConsoleAclEntriesService,
    _audit,
)
from audittrace.services.console_acl._errors import (
    AclBulkRolledBackError,
    AclPastExpiryError,
    AclPrincipalTypeRefused,
    AclWriteRefused,
)

pytestmark = pytest.mark.usefixtures("client")


def _interactions(client, *, event_class: str = _audit.EVENT_CLASS_ACL_AUTHZ):
    resp = client.get(
        "/interactions", params={"event_class": event_class, "limit": 1000}
    )
    assert resp.status_code == 200
    return resp.json()["interactions"]


def _match_one_by_resource(rows: list[dict], resource_id: str) -> dict:
    """Match an ``/interactions`` row by the ``resource_id`` embedded in
    ``question`` — the interaction row's OWN ``id`` (an autoincrement
    integer) is a different id from the ACL entry's id that
    ``grant_permission`` returns; matching on the latter would always
    return zero rows."""
    matches = [r for r in rows if f":{resource_id}" in r["question"]]
    assert len(matches) == 1, "a 200 with no matched row is a FAIL (spec §10)"
    return matches[0]


async def _old_row_expired_at_ms(
    service: ConsoleAclEntriesService, row_id: str
) -> int | None:
    """Read a SPECIFIC row's ``expired_at_ms`` directly — the read-path
    service methods filter expired rows out by design (ruling 2), so
    they can never show whether the OLD row was actually expired versus
    simply outranked; this is the only way to see it. Works on both
    implementations: the mock never persists ACL rows to a DB (only its
    audit rows do), so it is read from ``service._entries`` directly."""
    if isinstance(service, MockConsoleAclEntriesService):
        row = next(e for e in service._entries if e.id == row_id)
        return row.expired_at_ms
    pg = dependencies.get_postgres_factory()
    async with pg.get_session_factory()() as db:
        result = await db.execute(
            sa.select(ConsoleAclEntry.expired_at_ms).where(ConsoleAclEntry.id == row_id)
        )
        return result.scalar_one()


@pytest_asyncio.fixture
def pg_service(client) -> ConsoleAclEntriesService:
    """The container ``client`` wires (``create_test_container``) already
    carries a ``PostgresConsoleAclEntriesService`` bound to the SAME
    ``InMemoryPostgresFactory`` registered as ``postgres_factory`` —
    reuse it verbatim so ``grant_permission``'s internal audit-row
    write lands in the table ``/interactions`` reads."""
    return dependencies.get_console_acl_service()


@pytest_asyncio.fixture
def mock_service(client) -> MockConsoleAclEntriesService:
    """A fresh mock instance. The ``client`` fixture's container has
    already registered ``postgres_factory`` (the same aiosqlite
    factory), which is all :class:`MockConsoleAclEntriesService`'s
    write methods need (spec §7)."""
    return MockConsoleAclEntriesService()


@pytest.fixture(params=["postgres", "mock"])
def service(request, pg_service, mock_service) -> ConsoleAclEntriesService:
    return {"postgres": pg_service, "mock": mock_service}[request.param]


# ── AclGrantOp — frozen, exact fields (spec §3) ──────────────────────────


class TestAclGrantOp:
    def test_is_frozen(self) -> None:
        op = AclGrantOp(
            principal_type="user",
            principal_id="p1",
            resource_type="agent",
            resource_id="a1",
            perm_bits=1,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            op.perm_bits = 2  # type: ignore[misc]

    def test_defaults(self) -> None:
        op = AclGrantOp(
            principal_type="user",
            principal_id="p1",
            resource_type="agent",
            resource_id="a1",
            perm_bits=1,
        )
        assert op.role_id is None
        assert op.expired_at_ms is None
        assert op.tenant_id is None


# ── The closed error hierarchy ────────────────────────────────────────────


class TestErrorHierarchy:
    @pytest.mark.parametrize(
        "error_cls",
        [AclPastExpiryError, AclPrincipalTypeRefused, AclBulkRolledBackError],
    )
    def test_subclasses_the_base(self, error_cls: type[AclWriteRefused]) -> None:
        assert issubclass(error_cls, AclWriteRefused)

    def test_carries_failure_class_and_db_error_class(self) -> None:
        exc = AclWriteRefused(
            "refused", failure_class="acl_denied_policy", db_error_class="X"
        )
        assert exc.failure_class == "acl_denied_policy"
        assert exc.db_error_class == "X"


# ── A2-scope methods ship as a REAL, disclosed gap (ADDENDUM U-6) ───────


class TestA2ScopeIsNotImplemented:
    async def test_revoke_permission_raises(self, service, user_context) -> None:
        with pytest.raises(NotImplementedError, match="2b-core-A2"):
            await service.revoke_permission(
                user_context,
                principal_type="user",
                principal_id="p1",
                resource_type="agent",
                resource_id="a1",
            )

    async def test_modify_permission_bits_raises(self, service, user_context) -> None:
        with pytest.raises(NotImplementedError, match="2b-core-A2"):
            await service.modify_permission_bits(
                user_context,
                principal_type="user",
                principal_id="p1",
                resource_type="agent",
                resource_id="a1",
                add_bits=1,
            )

    async def test_delete_acl_entries_raises(self, service, user_context) -> None:
        with pytest.raises(NotImplementedError, match="2b-core-A2"):
            await service.delete_acl_entries(
                user_context, [{"principal_type": "user", "principal_id": "p1"}]
            )


# ── #1 — audit row per successful write (grant) ──────────────────────────


class TestGrantPermissionSuccessAuditRow:
    async def test_produces_an_audit_row(self, service, client, user_context) -> None:
        row = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="principal-1",
            resource_type="agent",
            resource_id="agent-grant-1",
            perm_bits=3,
        )
        stored = _match_one_by_resource(_interactions(client), "agent-grant-1")
        assert stored["status"] == "success"
        assert stored["failure_class"] is None
        answer = json.loads(stored["answer"])
        assert answer["acl_entry_ids"] == [row["id"]]
        assert stored["question"].startswith("op=grantPermission ")

    async def test_new_row_is_server_stamped(
        self, service, user_context, _now_ms=__import__("time").time
    ) -> None:
        t0 = int(__import__("time").time() * 1000)
        row = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="p-stamp",
            resource_type="agent",
            resource_id="agent-stamp",
            perm_bits=1,
        )
        t1 = int(__import__("time").time() * 1000)
        # #8a — granted_at_ms is server-stamped, never a caller value or a
        # model default (0 would be a model-default pin, barred).
        assert t0 <= row["granted_at_ms"] <= t1
        # S-2 — user_sub / granted_by are token-derived, never a
        # caller-suppliable parameter (there is none on the signature).
        assert row["user_sub"] == user_context.user_id
        assert row["granted_by"] == user_context.user_id


# ── #2 — denial row per refused write ─────────────────────────────────────


class TestGrantPermissionDenials:
    async def test_past_expiry_is_refused_by_name(
        self, service, client, user_context
    ) -> None:
        now_ms = int(__import__("time").time() * 1000)
        with pytest.raises(AclPastExpiryError) as excinfo:
            await service.grant_permission(
                user_context,
                principal_type="user",
                principal_id="p-past",
                resource_type="agent",
                resource_id="agent-past-expiry",
                perm_bits=1,
                expired_at_ms=now_ms - 1000,
            )
        assert (
            excinfo.value.failure_class == _audit.FAILURE_CLASS_ACL_DENIED_PAST_EXPIRY
        )
        # B5 — pinned on the EMITTED row, not the model: spec §5.6's
        # ratified literal, not type(exc).__name__ ("ValueError").
        assert excinfo.value.db_error_class == "app:past_expiry"
        rows = [
            r
            for r in _interactions(client)
            if r["failure_class"] == _audit.FAILURE_CLASS_ACL_DENIED_PAST_EXPIRY
            and "agent-past-expiry" in r["question"]
        ]
        assert len(rows) == 1
        detail = json.loads(rows[0]["error_detail"])
        assert detail["db_error_class"] == "app:past_expiry"

    async def test_principal_type_outside_allowed_set_is_refused_by_name(
        self, service, client, user_context
    ) -> None:
        with pytest.raises(AclPrincipalTypeRefused) as excinfo:
            await service.grant_permission(
                user_context,
                principal_type="group",
                principal_id="g1",
                resource_type="agent",
                resource_id="agent-group-refused",
                perm_bits=1,
            )
        assert (
            excinfo.value.failure_class
            == _audit.FAILURE_CLASS_ACL_DENIED_PRINCIPAL_TYPE
        )
        assert excinfo.value.db_error_class == "ck_console_acl_entries_principal_type"
        rows = [
            r
            for r in _interactions(client)
            if r["failure_class"] == _audit.FAILURE_CLASS_ACL_DENIED_PRINCIPAL_TYPE
            and "agent-group-refused" in r["question"]
        ]
        assert len(rows) == 1
        detail = json.loads(rows[0]["error_detail"])
        assert detail["db_error_class"] == "ck_console_acl_entries_principal_type"

    async def test_denied_write_never_lands_the_acl_row(
        self, service, user_context
    ) -> None:
        with pytest.raises(AclPrincipalTypeRefused):
            await service.grant_permission(
                user_context,
                principal_type="group",
                principal_id="g2",
                resource_type="agent",
                resource_id="agent-group-refused-absent",
                perm_bits=1,
            )
        effective = await service.get_effective_permissions(
            user_context, "agent", "agent-group-refused-absent"
        )
        assert effective == 0, "a refused grant must never become effective"


# ── #4 — fail-closed caller half: _audit.record raising propagates ───────


class TestFailClosedCallerHalf:
    """A broken FIRST call to ``_content_hash`` (record's own call) must
    propagate the ORIGINAL exception and leave the ACL row absent; the
    SECOND call (the denial writer's own, independent, `acl_audit_write_
    failed` row) is deliberately left UNBROKEN by this fixture's counting
    side effect, so the neuter's "one acl_audit_write_failed row" claim
    is actually observable — a plain always-raise patch (B's own
    _audit.py-only precedent) would break record_denial's OWN content_
    hash call too and could never produce that row."""

    async def test_broken_audit_write_propagates_and_leaves_no_acl_row(
        self, service, client, user_context, monkeypatch
    ) -> None:
        original = _audit._content_hash
        calls = {"n": 0}

        def _flaky(*args: object, **kwargs: object) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("content_hash broken for this test")
            return original(*args, **kwargs)

        monkeypatch.setattr(_audit, "_content_hash", _flaky)

        with pytest.raises(RuntimeError, match="content_hash broken"):
            await service.grant_permission(
                user_context,
                principal_type="user",
                principal_id="p-n4",
                resource_type="agent",
                resource_id="agent-n4-broken-audit",
                perm_bits=1,
            )

        effective = await service.get_effective_permissions(
            user_context, "agent", "agent-n4-broken-audit"
        )
        assert effective == 0, "the ACL row must be absent when the audit write fails"

        rows = [
            r
            for r in _interactions(client)
            if r["failure_class"] == _audit.FAILURE_CLASS_ACL_AUDIT_WRITE_FAILED
            and "agent-n4-broken-audit" in r["question"]
        ]
        assert len(rows) == 1, (
            "'raises' alone is satisfiable while the grant landed — the "
            "absent-row assertion (above) is the load-bearing one; this "
            "one confirms the writer's own denial row still landed"
        )


# ── #6 — bits, PER BIT (grant side; A1's half of the row) ────────────────


class TestGrantPermissionBits:
    @pytest.mark.parametrize(
        "bit",
        [
            PERMISSION_BIT_VIEW,
            PERMISSION_BIT_EDIT,
            PERMISSION_BIT_DELETE,
            PERMISSION_BIT_SHARE,
        ],
    )
    async def test_each_bit_is_set_individually(
        self, service, user_context, bit: int
    ) -> None:
        resource_id = f"agent-bit-{bit}"
        row = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id=resource_id,
            perm_bits=bit,
        )
        assert row["perm_bits"] == bit
        has_bit = await service.has_permission(user_context, "agent", resource_id, bit)
        assert has_bit is True
        for other_bit in (
            PERMISSION_BIT_VIEW,
            PERMISSION_BIT_EDIT,
            PERMISSION_BIT_DELETE,
            PERMISSION_BIT_SHARE,
        ):
            if other_bit == bit:
                continue
            assert (
                await service.has_permission(
                    user_context, "agent", resource_id, other_bit
                )
                is False
            ), "a batched perm_bits assertion cannot see one dead bit"

    async def test_max_perm_bits_holds_all_four(self, service, user_context) -> None:
        row = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id="agent-max-bits",
            perm_bits=MAX_PERM_BITS,
        )
        assert row["perm_bits"] == MAX_PERM_BITS


# ── O-6 — expire-and-insert, never in-place mutation ─────────────────────


class TestExpireAndInsert:
    async def test_regrant_expires_the_old_row_and_inserts_a_new_one(
        self, service, user_context
    ) -> None:
        first = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id="agent-regrant",
            perm_bits=1,
        )
        second = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id="agent-regrant",
            perm_bits=5,
        )
        assert second["id"] != first["id"], "expire-and-insert, never UPDATE in place"
        effective = await service.get_effective_permissions(
            user_context, "agent", "agent-regrant"
        )
        assert effective == 5, "only the NEW row's bits are effective"
        old_expired_at_ms = await _old_row_expired_at_ms(service, first["id"])
        assert old_expired_at_ms is not None, (
            "the OLD row must actually be expired, not merely outranked — "
            "5 is a SUPERSET of 1, so effective==5 alone cannot tell a "
            "genuinely-expired old row from one _expire_active silently "
            "skipped (B2 — 1-then-5 is invisible to a batched assertion)"
        )

    async def test_downgrade_regrant_expires_the_old_row_and_new_bits_are_exact(
        self, service, user_context
    ) -> None:
        """B2 — the closing test a superset re-grant (1 then 5) cannot
        provide: a DOWNGRADE re-grant (15 then 1). If ``_expire_active``
        were skipped, or matched nothing, the OLD row (bits=15) would
        remain active and OR into the effective mask, making
        effective == 15 (or 15|1 == 15) instead of the new grant's own
        1 — and the old row's own ``expired_at_ms`` would still be
        ``None``. Both are asserted directly, not inferred from a
        superset."""
        first = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id="agent-downgrade-regrant",
            perm_bits=15,
        )
        second = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id="agent-downgrade-regrant",
            perm_bits=1,
        )
        assert second["id"] != first["id"]
        effective = await service.get_effective_permissions(
            user_context, "agent", "agent-downgrade-regrant"
        )
        assert effective == 1, (
            "a leftover active bits=15 row would make this 15, not 1 — "
            "the load-bearing assertion this guard actually needs"
        )
        old_expired_at_ms = await _old_row_expired_at_ms(service, first["id"])
        assert old_expired_at_ms is not None, "the OLD (bits=15) row must be expired"

    async def test_identical_bits_still_expires_and_inserts(
        self, service, user_context
    ) -> None:
        """5.1 — a re-grant with IDENTICAL bits still expires+inserts; no
        special case that would skip the O-3 index / audit trail."""
        first = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id="agent-idempotent-regrant",
            perm_bits=1,
        )
        second = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id="agent-idempotent-regrant",
            perm_bits=1,
        )
        assert second["id"] != first["id"]


# ── bulk_write_acl_entries — success + all-or-nothing (O-4) ──────────────


class TestBulkWriteAclEntries:
    async def test_success_returns_every_id(
        self, service, client, user_context
    ) -> None:
        ops = [
            AclGrantOp(
                principal_type="user",
                principal_id="p-bulk-1",
                resource_type="agent",
                resource_id="agent-bulk-1",
                perm_bits=1,
            ),
            AclGrantOp(
                principal_type="user",
                principal_id="p-bulk-2",
                resource_type="agent",
                resource_id="agent-bulk-2",
                perm_bits=2,
            ),
        ]
        result = await service.bulk_write_acl_entries(user_context, ops)
        assert len(result["acl_entry_ids"]) == 2
        for acl_id in result["acl_entry_ids"]:
            rows = [
                r
                for r in _interactions(client)
                if r.get("answer")
                and acl_id in json.loads(r["answer"]).get("acl_entry_ids", [])
            ]
            assert len(rows) == 1

    async def test_all_or_nothing_rollback_on_one_refused_op(
        self, service, client, user_context
    ) -> None:
        ops = [
            AclGrantOp(
                principal_type="user",
                principal_id="p-bulk-ok",
                resource_type="agent",
                resource_id="agent-bulk-rollback-ok",
                perm_bits=1,
            ),
            AclGrantOp(
                # R-8 refusal — the SAME closed-set check as single-grant.
                principal_type="group",
                principal_id="p-bulk-bad",
                resource_type="agent",
                resource_id="agent-bulk-rollback-bad",
                perm_bits=1,
            ),
        ]
        with pytest.raises(AclBulkRolledBackError) as excinfo:
            await service.bulk_write_acl_entries(user_context, ops)
        assert (
            excinfo.value.failure_class == _audit.FAILURE_CLASS_ACL_DENIED_BULK_ROLLBACK
        )

        # Neither op's ACL row landed — including op 0, which "succeeded"
        # before op 1 was refused.
        for resource_id in ("agent-bulk-rollback-ok", "agent-bulk-rollback-bad"):
            effective = await service.get_effective_permissions(
                user_context, "agent", resource_id
            )
            assert effective == 0

        success_rows = [
            r
            for r in _interactions(client)
            if r["status"] == "success" and "agent-bulk-rollback-ok" in r["question"]
        ]
        assert success_rows == [], (
            "a rolled-back write with a surviving audit row is a false record"
        )
        bulk_denials = [
            r
            for r in _interactions(client)
            if r["failure_class"] == _audit.FAILURE_CLASS_ACL_DENIED_BULK_ROLLBACK
            and "agent-bulk-rollback-bad" in r["question"]
        ]
        assert len(bulk_denials) == 1
        detail = json.loads(bulk_denials[0]["error_detail"])
        attempted = detail["predicate_or_attempted_row"]
        assert attempted["op_index"] == 1
        assert attempted["op_count"] == 2


# ── T — the two-key trace_id link (unit level; real-PG span proof lives
# in the harness file too, this is the mock/aiosqlite half) ──────────────


class TestTraceIdLink:
    async def test_acl_row_and_audit_row_share_a_trace_id(
        self, pg_service, client, user_context
    ) -> None:
        """T — the two-key link. B3's finding: a prior round of this test
        never read ``interactions.trace_id`` at all (only counted
        matching rows), so setting ``_audit.record``'s trace_id to
        ``None`` left it green. Asserts the ACTUAL equality now."""
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("grant"):
            span = trace.get_current_span()
            trace_id_hex = format(span.get_span_context().trace_id, "032x")
            row = await pg_service.grant_permission(
                user_context,
                principal_type="user",
                principal_id="p-trace",
                resource_type="agent",
                resource_id="agent-trace-link",
                perm_bits=1,
            )
        assert len(trace_id_hex) == 32
        int(trace_id_hex, 16)
        assert row["trace_id"] == trace_id_hex

        stored = _match_one_by_resource(_interactions(client), "agent-trace-link")
        assert stored["trace_id"] is not None, "a NULL match is never a valid match"
        assert stored["trace_id"] == trace_id_hex == row["trace_id"]


# ── _classify — the closed mapping's own branches, direct unit tests ────
# (spec §5.6, corrected by a measurement recorded in _postgres_write.py's
# module docstring: BOTH constraint names classify to
# acl_denied_principal_type; WHICH ONE appears is a real-Postgres-only
# fact — exercised THROUGH grant_permission on real PG by
# tests/test_acl_write_path_rls.py::TestR8GroupPrincipalRefused::
# test_service_refuses_group_principal_on_real_pg (and its joint-neuter
# sibling), exercised HERE at the unit level so this module's own
# branches are covered without needing a live Postgres for every case.)


from audittrace.services.console_acl import _postgres_write  # noqa: E402


class _FakeOrig:
    def __init__(self, sqlstate: str | None = None) -> None:
        self.sqlstate = sqlstate


class _FakeDbExc(Exception):  # noqa: N818 - a fake DB-layer exception, not app-level
    def __init__(self, message: str, orig: object | None = None) -> None:
        super().__init__(message)
        self.orig = orig


class TestClassify:
    def test_recognises_the_named_principal_type_constraint(self) -> None:
        exc = _FakeDbExc(
            'violates check constraint "ck_console_acl_entries_principal_type"'
        )
        failure_class, name = _postgres_write._classify(exc)
        assert failure_class == _audit.FAILURE_CLASS_ACL_DENIED_PRINCIPAL_TYPE
        assert name == "ck_console_acl_entries_principal_type"

    def test_recognises_the_model_matches_type_constraint_too(self) -> None:
        """The measured finding (module docstring): Postgres reports
        THIS constraint, not the one named in spec §5.3's prose, for a
        real group-principal grant — both must classify identically."""
        exc = _FakeDbExc(
            "violates check constraint "
            '"ck_console_acl_entries_principal_model_matches_type"'
        )
        failure_class, name = _postgres_write._classify(exc)
        assert failure_class == _audit.FAILURE_CLASS_ACL_DENIED_PRINCIPAL_TYPE
        assert name == "ck_console_acl_entries_principal_model_matches_type"

    def test_falls_back_to_sqlstate_when_no_named_constraint_matches(self) -> None:
        exc = _FakeDbExc("some other refusal", orig=_FakeOrig(sqlstate="23505"))
        failure_class, name = _postgres_write._classify(exc)
        assert failure_class == _audit.FAILURE_CLASS_ACL_DENIED_POLICY
        assert name == "23505"

    def test_falls_back_to_a_quoted_name_when_no_sqlstate(self) -> None:
        exc = _FakeDbExc('violates constraint "some_other_constraint"')
        failure_class, name = _postgres_write._classify(exc)
        assert failure_class == _audit.FAILURE_CLASS_ACL_DENIED_POLICY
        assert name == "some_other_constraint"

    def test_falls_back_to_the_exception_class_name_as_a_last_resort(self) -> None:
        exc = RuntimeError("nothing recognisable here")
        failure_class, name = _postgres_write._classify(exc)
        assert failure_class == _audit.FAILURE_CLASS_ACL_DENIED_POLICY
        assert name == "RuntimeError"


# ── Stage-1 refusal (the _expire_active/add step) — real-Postgres-only
# in practice (RLS); monkeypatched here so THIS module's own exception-
# handling branch is covered without a live Postgres. The real-Postgres
# behavioural proof (shape, pg_stat_activity) lives in
# tests/test_acl_write_path_rls.py. ─────────────────────────────────────


class TestGrantPermissionStage1Refusal:
    async def test_expire_active_refusal_is_classified_and_denied(
        self, pg_service, client, user_context, monkeypatch
    ) -> None:
        async def _boom(*args: object, **kwargs: object) -> list[str]:
            raise _FakeDbExc(
                'violates check constraint "ck_console_acl_entries_principal_type"'
            )

        monkeypatch.setattr(_postgres_write, "_expire_active", _boom)

        with pytest.raises(AclPrincipalTypeRefused):
            await pg_service.grant_permission(
                user_context,
                principal_type="user",
                principal_id="p-stage1",
                resource_type="agent",
                resource_id="agent-stage1-refusal",
                perm_bits=1,
            )
        denials = [
            r
            for r in _interactions(client)
            if r["failure_class"] == _audit.FAILURE_CLASS_ACL_DENIED_PRINCIPAL_TYPE
            and "agent-stage1-refusal" in r["question"]
        ]
        assert len(denials) == 1


class TestBulkPastExpiryRefusal:
    async def test_bulk_op_with_past_expiry_is_refused_and_rolled_back(
        self, service, user_context
    ) -> None:
        now_ms = int(__import__("time").time() * 1000)
        ops = [
            AclGrantOp(
                principal_type="user",
                principal_id=user_context.user_id,
                resource_type="agent",
                resource_id="agent-bulk-past-expiry",
                perm_bits=1,
                expired_at_ms=now_ms - 1000,
            )
        ]
        with pytest.raises(AclBulkRolledBackError):
            await service.bulk_write_acl_entries(user_context, ops)
        effective = await service.get_effective_permissions(
            user_context, "agent", "agent-bulk-past-expiry"
        )
        assert effective == 0


class TestMockBulkUndoRestoresPreExistingExpiry:
    """Mock-only (spec §7's in-memory undo-log — Postgres gets this for
    free from the shared transaction's own rollback; see
    _mock_write.py's module docstring)."""

    async def test_failed_bulk_restores_an_earlier_ops_expired_row(
        self, mock_service, user_context
    ) -> None:
        pre_existing = await mock_service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id="agent-undo-restore",
            perm_bits=1,
        )
        ops = [
            AclGrantOp(
                # Re-grants the SAME key as pre_existing — expires it.
                principal_type="user",
                principal_id=user_context.user_id,
                resource_type="agent",
                resource_id="agent-undo-restore",
                perm_bits=2,
            ),
            AclGrantOp(
                # Refused — forces the whole bulk (including op 0's
                # in-memory expiry above) to roll back.
                principal_type="group",
                principal_id="p-undo-bad",
                resource_type="agent",
                resource_id="agent-undo-restore-bad",
                perm_bits=1,
            ),
        ]
        with pytest.raises(AclBulkRolledBackError):
            await mock_service.bulk_write_acl_entries(user_context, ops)

        restored = next(e for e in mock_service._entries if e.id == pre_existing["id"])
        assert restored.expired_at_ms is None, (
            "the PRE-EXISTING row's expiry must be undone when a LATER "
            "op in the same bulk call is refused"
        )
        effective = await mock_service.get_effective_permissions(
            user_context, "agent", "agent-undo-restore"
        )
        assert effective == 1, "only the original (un-expired) grant is effective"


class TestMockFailClosedCallerHalfUndoesExpiry:
    """Mock-only — the in-memory undo-on-N4-failure path (Postgres gets
    this for free via db.rollback() on the shared transaction)."""

    async def test_broken_audit_write_on_regrant_restores_the_old_row(
        self, mock_service, user_context, monkeypatch
    ) -> None:
        # An unrelated sibling entry so the undo loop's "not this row"
        # branch is exercised too, not only "this is the row to restore".
        await mock_service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id="agent-n4-regrant-undo-sibling",
            perm_bits=1,
        )
        first = await mock_service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id="agent-n4-regrant-undo",
            perm_bits=1,
        )

        original = _audit._content_hash
        calls = {"n": 0}

        def _flaky(*args: object, **kwargs: object) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("content_hash broken for this test")
            return original(*args, **kwargs)

        monkeypatch.setattr(_audit, "_content_hash", _flaky)

        with pytest.raises(RuntimeError, match="content_hash broken"):
            await mock_service.grant_permission(
                user_context,
                principal_type="user",
                principal_id=user_context.user_id,
                resource_type="agent",
                resource_id="agent-n4-regrant-undo",
                perm_bits=5,
            )

        restored = next(e for e in mock_service._entries if e.id == first["id"])
        assert restored.expired_at_ms is None, (
            "a failed audit write must undo the in-memory expiry too"
        )
        effective = await mock_service.get_effective_permissions(
            user_context, "agent", "agent-n4-regrant-undo"
        )
        assert effective == 1, "only the original grant survives the failed re-grant"


class TestMockExpireLoopSkipsNonMatchingRows:
    """Exercises the branch where the mock's expiry-mutation loops iterate
    over entries that do NOT match the current key (grant_permission's
    own loop, the bulk per-op loop, and the N4/all-or-nothing undo loop)
    — every other test in this file happens to seed exactly one matching
    row, which never exercises the "skip a sibling" branch."""

    async def test_grant_skips_an_unrelated_pre_existing_entry(
        self, mock_service, user_context
    ) -> None:
        await mock_service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id="agent-unrelated-sibling",
            perm_bits=1,
        )
        row = await mock_service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id="agent-with-sibling",
            perm_bits=2,
        )
        assert row["perm_bits"] == 2
        # The unrelated sibling must be untouched.
        sibling_effective = await mock_service.get_effective_permissions(
            user_context, "agent", "agent-unrelated-sibling"
        )
        assert sibling_effective == 1

    async def test_bulk_expire_and_undo_skip_an_unrelated_sibling(
        self, mock_service, user_context
    ) -> None:
        await mock_service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id="agent-bulk-sibling-untouched",
            perm_bits=1,
        )
        pre_existing = await mock_service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id="agent-bulk-sibling-expired-then-restored",
            perm_bits=1,
        )
        ops = [
            AclGrantOp(
                principal_type="user",
                principal_id=user_context.user_id,
                resource_type="agent",
                resource_id="agent-bulk-sibling-expired-then-restored",
                perm_bits=2,
            ),
            AclGrantOp(
                principal_type="group",
                principal_id="p-sibling-bad",
                resource_type="agent",
                resource_id="agent-bulk-sibling-bad",
                perm_bits=1,
            ),
        ]
        with pytest.raises(AclBulkRolledBackError):
            await mock_service.bulk_write_acl_entries(user_context, ops)

        untouched_effective = await mock_service.get_effective_permissions(
            user_context, "agent", "agent-bulk-sibling-untouched"
        )
        assert untouched_effective == 1
        restored = next(e for e in mock_service._entries if e.id == pre_existing["id"])
        assert restored.expired_at_ms is None


# ── S4 — the module docstring's autoflush-version-independence claim,
# with an in-tree instrument (not just prose) confirming it holds on
# THIS venv's resolved SQLAlchemy (measured 2.1.1 at build time; V-1's
# own table only measured 2.0.51/2.1.0) ──────────────────────────────────


class TestAutoflushIsMeasuredOnThisVenv:
    async def test_expire_active_construct_triggers_autoflush_of_a_pending_row(
        self, user_context
    ) -> None:
        """``_expire_active``'s ORM-enabled ``sa.update(ConsoleAclEntry)``
        must autoflush a PENDING, invalid ``add()``-ed row before its own
        UPDATE executes — the property ADDENDUM V-1 measured on
        2.0.51/2.1.0 and this module's docstring claims "re-verified
        functionally" on 2.1.1. Probed directly: a pending row violating
        ``ck_console_acl_entries_perm_bits_range`` is never explicitly
        flushed; if ``_expire_active`` did NOT autoflush, its own UPDATE
        would run cleanly (0 rows matched) and return an EMPTY list —
        instead it raises, because the pending row's autoflush fires
        first and fails."""
        pg = dependencies.get_postgres_factory()
        async with pg.get_session_factory()() as db:
            db.add(
                ConsoleAclEntry(
                    id=str(uuid.uuid4()),
                    user_sub=user_context.user_id,
                    principal_type="user",
                    principal_id="p-autoflush-probe",
                    principal_model="User",
                    resource_type="agent",
                    resource_id="agent-autoflush-probe",
                    perm_bits=99,  # violates ck_console_acl_entries_perm_bits_range
                    granted_at_ms=0,
                    created_at_ms=0,
                    updated_at_ms=0,
                )
            )
            with pytest.raises(Exception, match="perm_bits_range|CHECK constraint"):
                await _postgres_write._expire_active(
                    db,
                    principal_type="user",
                    principal_id="someone-else-entirely",
                    resource_type="agent",
                    resource_id="agent-autoflush-probe-unrelated-key",
                    tenant_id=None,
                    now_ms=1,
                )
