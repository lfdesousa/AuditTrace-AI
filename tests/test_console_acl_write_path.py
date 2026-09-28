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
import time
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


def _match_one_op(rows: list[dict], op: str, resource_id: str) -> dict:
    """Like ``_match_one_by_resource`` but ALSO anchors on the ``op=``
    prefix — A2 tests grant THEN revoke/modify/delete at the SAME
    resource_id, so a bare ``:resource_id`` substring match returns
    more than one row."""
    matches = [
        r
        for r in rows
        if f":{resource_id}" in r["question"] and r["question"].startswith(f"op={op} ")
    ]
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
        special case that would skip the O-3 index / audit trail.

        **Strengthened, Y round (gate-AF should-fix).** The ONLY
        assertion this test used to carry was ``second["id"] !=
        first["id"]`` — which a "same-bits special case" bug (skip
        ``_expire_active``/the mock's inline expire loop whenever the
        re-grant's ``perm_bits`` equals the currently-active row's, but
        still insert a new row) would sail straight through: a new row
        with a different id still lands, the OLD row is just left
        active too. The AC-T-HIST clock-seeded grid (``tests/
        test_acl_write_path_lapsed_clock.py``) can never catch that
        class either — its history always CHANGES bits between writes
        (15 -> 7 -> 3) by construction, so it never exercises a
        same-bits re-grant. This is the ONE test in the whole build
        that does, and the load-bearing assertion is the OLD row's
        ``expired_at_ms``. A one-line source change at the mock's
        grant-path expire predicate (skip a row whose bits already
        match the re-grant) reddens this test on ``[mock]`` — measured
        as a source-level neuter and captured in the round's evidence,
        not shipped as an in-repo tautology."""
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
        old_expired_at_ms = await _old_row_expired_at_ms(service, first["id"])
        assert old_expired_at_ms is not None, (
            "the OLD (bits=1) row must be expired even though the NEW "
            "row's bits are identical — a same-bits special case would "
            "leave this None while still producing a fresh id above"
        )


# ── Y-T1 / Y-T1n / Y-T2 (spec Y-3, kept per ADDENDUM AE-3) — the
# supersession of a time-limited predecessor proven through the READ
# path (``get_effective_permissions``/``has_permission``), which
# AC-T-HIST (the clock-seeded grid, ``tests/
# test_acl_write_path_lapsed_clock.py``) does not exercise — it reads
# emitted rows, never the authorization decision itself. Run on BOTH
# implementations via the ``service`` fixture; the real-Postgres-harness
# siblings live in ``tests/test_acl_write_path_rls.py``. ──────────────


class TestYT1TimeLimitedGrantSupersededThroughTheReadPath:
    async def test_y_t1_downgrade_over_a_still_future_predecessor_is_effective(
        self, service, user_context
    ) -> None:
        """Y-T1 — a downgrade re-grant (15 -> 1) over a predecessor that
        is STILL FUTURE (``expired_at_ms = now+1h``) must be effective
        through the read path, exactly as the NULL-expiry sibling
        (Y-T1n, below) already is — the defect ADDENDUM Y-0 measured was
        this exact case reading 15/True instead of 1/False."""
        resource_id = "agent-y-t1"
        first = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id=resource_id,
            perm_bits=15,
            expired_at_ms=int(time.time() * 1000) + 3_600_000,
        )
        second = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id=resource_id,
            perm_bits=1,
        )
        assert second["id"] != first["id"]
        effective = await service.get_effective_permissions(
            user_context, "agent", resource_id
        )
        has_bit8 = await service.has_permission(user_context, "agent", resource_id, 8)
        assert effective == 1, (
            "RED under the pre-Y-1 predicate: a still-future predecessor "
            "(bits=15) would OR into the effective mask and make this 15"
        )
        assert has_bit8 is False
        old_expired_at_ms = await _old_row_expired_at_ms(service, first["id"])
        assert old_expired_at_ms is not None, "the OLD row must be superseded"
        assert old_expired_at_ms <= int(time.time() * 1000), (
            "superseded AT the re-grant, strictly before its originally "
            "scheduled future expiry"
        )

    async def test_y_t1n_downgrade_over_a_null_expiry_predecessor_is_effective(
        self, service, user_context
    ) -> None:
        """Y-T1n — the NULL-predecessor sibling B2's original test
        already covered (functionally identical to
        ``test_downgrade_regrant_expires_the_old_row_and_new_bits_are_exact``
        above); kept under this name so it has its own row in the
        per-guard table alongside Y-T1/Y-T2, per spec Y-3's three-row
        requirement. Stays GREEN under the pre-Y-1 predicate — that is
        the point: this sibling alone cannot see Y-0's defect, only
        Y-T1/Y-T2 (a STILL-FUTURE predecessor) can."""
        resource_id = "agent-y-t1n"
        first = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id=resource_id,
            perm_bits=15,
        )
        second = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id=resource_id,
            perm_bits=1,
        )
        assert second["id"] != first["id"]
        effective = await service.get_effective_permissions(
            user_context, "agent", resource_id
        )
        has_bit8 = await service.has_permission(user_context, "agent", resource_id, 8)
        assert effective == 1
        assert has_bit8 is False
        old_expired_at_ms = await _old_row_expired_at_ms(service, first["id"])
        assert old_expired_at_ms is not None

    async def test_y_t2_bulk_downgrade_over_a_still_future_predecessor_is_effective(
        self, service, user_context
    ) -> None:
        """Y-T2 — Y-T1's BULK-site sibling: one ``AclGrantOp`` of 1 over
        a direct grant of 15 with a still-future expiry, same key."""
        resource_id = "agent-y-t2"
        first = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id=resource_id,
            perm_bits=15,
            expired_at_ms=int(time.time() * 1000) + 3_600_000,
        )
        result = await service.bulk_write_acl_entries(
            user_context,
            [
                AclGrantOp(
                    principal_type="user",
                    principal_id=user_context.user_id,
                    resource_type="agent",
                    resource_id=resource_id,
                    perm_bits=1,
                )
            ],
        )
        assert result["acl_entry_ids"][0] != first["id"]
        effective = await service.get_effective_permissions(
            user_context, "agent", resource_id
        )
        has_bit8 = await service.has_permission(user_context, "agent", resource_id, 8)
        assert effective == 1, (
            "RED under the pre-Y-1 predicate: a still-future predecessor "
            "would OR into the effective mask and make this 15"
        )
        assert has_bit8 is False
        old_expired_at_ms = await _old_row_expired_at_ms(service, first["id"])
        assert old_expired_at_ms is not None
        assert old_expired_at_ms <= int(time.time() * 1000), (
            "superseded AT the bulk write, strictly before its originally "
            "scheduled expiry (Y-3: Y-T2 asserts this the same way Y-T1 does)"
        )


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


class TestYT3MockAuditFailureRestoresTimeLimitedPredecessor:
    """Y-T3 (ADDENDUM Z-1, closing BL-1 / AA-2 / AB-3) — the grant
    path's audit-failure restore must recover a TIME-LIMITED
    predecessor's TRUE prior state (its ORIGINAL scheduled
    ``expired_at_ms`` and its own ``updated_at_ms``), never a
    hard-coded ``None``. A hard-coded restore is correct only for the
    NULL-expiry case (``TestMockFailClosedCallerHalfUndoesExpiry``,
    above) and PERMANENTLY DESTROYS a time-limited grant's scheduled
    expiry when it fires on this one instead (ADDENDUM Z-1's measured
    table: the row reads back ``(15, None)`` instead of
    ``(15, <scheduled>)``)."""

    async def test_broken_audit_write_on_regrant_restores_the_scheduled_expiry(
        self, mock_service, user_context, monkeypatch
    ) -> None:
        scheduled = 9_999_999_999_999  # distinguishable from both None and "now"
        first = await mock_service.grant_permission(
            user_context,
            principal_type="user",
            principal_id=user_context.user_id,
            resource_type="agent",
            resource_id="agent-y-t3-mock",
            perm_bits=15,
            expired_at_ms=scheduled,
        )
        before = next(e for e in mock_service._entries if e.id == first["id"])
        before_updated_at_ms = before.updated_at_ms

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
                resource_id="agent-y-t3-mock",
                perm_bits=1,
            )

        restored = next(e for e in mock_service._entries if e.id == first["id"])
        assert restored.expired_at_ms == scheduled, (
            "RED (Y-T3's target, pre-Z-1): a hard-coded `None` restore "
            "would PERMANENTLY DESTROY the predecessor's scheduled "
            "future expiry instead of recovering it"
        )
        assert restored.updated_at_ms == before_updated_at_ms


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


# ═══════════════════════════════════════════════════════════════════════
# ACL 2b-core-A2 — revoke_permission / modify_permission_bits /
# delete_acl_entries (2026-09-28-SPEC-acl-2b-core-A2-revoke-modify-
# delete-CONSOLIDATED-v4.md). Same discipline as A1's tests above: BOTH
# implementations via the ``service`` fixture (``postgres`` = aiosqlite
# via ``InMemoryPostgresFactory``, ``mock`` = a fresh mock sharing the
# SAME wired factory), audit-row assertions through the REAL
# ``/interactions`` route. Real-Postgres-only proofs (RLS ownership
# scoping — P-1/P-1b/P-1b-F/P-2/P-3, the abort-shape split, D-A2-4r,
# SF-A/B/C) live in ``tests/test_acl_write_path_rls.py``, NOT here.
# ═══════════════════════════════════════════════════════════════════════

from audittrace.identity import UserContext  # noqa: E402
from audittrace.services.console_acl._mock_write import (  # noqa: E402
    _expire_matching as _mock_expire_matching,
)
from audittrace.services.console_store import _context as _clock_module  # noqa: E402

_HOUR = 3_600_000


def _principal_ctx(sub: str) -> UserContext:
    """A UserContext for the PRINCIPAL a grant was made TO — distinct
    from the resource OWNER (``user_context`` fixture). Checking
    ``get_effective_permissions`` as the OWNER would always read 0 for
    a grant made to someone else (the owner is not automatically a
    matching principal on their own grants — the read path's own,
    correct behaviour, not a write-path bug); these tests need the
    PRINCIPAL's view."""
    return UserContext(user_id=sub, username=sub, agent_type="test", scopes=())


async def _raw_insert(
    service: ConsoleAclEntriesService,
    *,
    user_sub: str,
    principal_type: str,
    principal_id: str | None,
    principal_model: str | None,
    resource_type: str,
    resource_id: str,
    perm_bits: int,
    tenant_id: str | None = None,
    role_id: str | None = None,
    granted_by: str | None = None,
    granted_at_ms: int = 0,
    expired_at_ms: int | None = None,
    created_at_ms: int = 0,
    updated_at_ms: int = 0,
) -> str:
    """Seed a row bypassing every guard (grant_permission's own O-6
    expire-and-insert would supersede a pre-existing row at the SAME
    key — some A2 fixtures need TWO simultaneously-active rows at one
    key, e.g. KSEL's ``firstrow`` scenario, which only a raw insert can
    construct). Mirrors ``tests/test_acl_write_path_rls.py``'s own "raw
    INSERT bypassing the service" precedent."""
    if isinstance(service, MockConsoleAclEntriesService):
        row = service.seed_entry(
            user_sub=user_sub,
            principal_type=principal_type,
            principal_id=principal_id,
            principal_model=principal_model,
            resource_type=resource_type,
            resource_id=resource_id,
            perm_bits=perm_bits,
            tenant_id=tenant_id,
            role_id=role_id,
            granted_by=granted_by,
            granted_at_ms=granted_at_ms,
            expired_at_ms=expired_at_ms,
            created_at_ms=created_at_ms,
            updated_at_ms=updated_at_ms,
        )
        return str(row["id"])
    pg = dependencies.get_postgres_factory()
    entry_id = str(uuid.uuid4())
    async with pg.get_session_factory()() as db:
        db.add(
            ConsoleAclEntry(
                id=entry_id,
                user_sub=user_sub,
                principal_type=principal_type,
                principal_id=principal_id,
                principal_model=principal_model,
                resource_type=resource_type,
                resource_id=resource_id,
                perm_bits=perm_bits,
                tenant_id=tenant_id,
                role_id=role_id,
                granted_by=granted_by,
                granted_at_ms=granted_at_ms,
                expired_at_ms=expired_at_ms,
                created_at_ms=created_at_ms,
                updated_at_ms=updated_at_ms,
            )
        )
        await db.commit()
    return entry_id


async def _row_pair(
    service: ConsoleAclEntriesService, row_id: str
) -> tuple[int | None, int]:
    """``(expired_at_ms, updated_at_ms)`` for a SPECIFIC row, read from a
    FRESH lookup — same rationale as ``_old_row_expired_at_ms`` above."""
    if isinstance(service, MockConsoleAclEntriesService):
        row = next(e for e in service._entries if e.id == row_id)
        return (row.expired_at_ms, row.updated_at_ms)
    pg = dependencies.get_postgres_factory()
    async with pg.get_session_factory()() as db:
        result = await db.execute(
            sa.select(
                ConsoleAclEntry.expired_at_ms, ConsoleAclEntry.updated_at_ms
            ).where(ConsoleAclEntry.id == row_id)
        )
        row = result.one()
        return (row[0], row[1])


# ── VAL — §3's pre-I/O ValueError shapes, no I/O, no audit row ──────────


class TestA2PreIOValidation:
    @pytest.mark.parametrize(
        "predicates",
        [
            [],
            [{}],
            [{"not_a_real_key": "x"}],
            [{"principal_id": None}],
            [{"resource_type": "agent"}],  # names none of the three anchors
        ],
        ids=["empty-list", "empty-dict", "unknown-key", "none-value", "no-anchor"],
    )
    async def test_delete_rejects_before_any_io(
        self, service, client, user_context, predicates
    ) -> None:
        before = len(_interactions(client))
        with pytest.raises(ValueError):
            await service.delete_acl_entries(user_context, predicates)
        assert len(_interactions(client)) == before, (
            "a caller defect writes NO audit row (D-A2-1)"
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"add_bits": 99},
            {"remove_bits": -1},
            {"add_bits": True},
        ],
        ids=["both-none", "add-out-of-range", "remove-negative", "bool-not-int"],
    )
    async def test_modify_rejects_before_any_io(
        self, service, client, user_context, kwargs
    ) -> None:
        before = len(_interactions(client))
        with pytest.raises(ValueError):
            await service.modify_permission_bits(
                user_context,
                principal_type="user",
                principal_id="p-val",
                resource_type="agent",
                resource_id="agent-val",
                **kwargs,
            )
        assert len(_interactions(client)) == before, (
            "a caller defect writes NO audit row (D-A2-1)"
        )


# ── revoke_permission — happy path, zero match, audit row (#1) ─────────


class TestRevokePermission:
    async def test_expires_and_returns_ids(self, service, client, user_context) -> None:
        row = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-revoke-1",
            resource_type="agent",
            resource_id="agent-revoke-1",
            perm_bits=3,
        )
        result = await service.revoke_permission(
            user_context,
            principal_type="user",
            principal_id="v-revoke-1",
            resource_type="agent",
            resource_id="agent-revoke-1",
        )
        assert result == {"expired_ids": [row["id"]], "visible_matched_count": 1}
        effective = await service.get_effective_permissions(
            user_context, "agent", "agent-revoke-1"
        )
        assert effective == 0

        stored = _match_one_op(
            _interactions(client), "revokePermission", "agent-revoke-1"
        )
        assert stored["status"] == "success"
        assert stored["question"].startswith("op=revokePermission ")
        answer = json.loads(stored["answer"])
        assert answer["acl_entry_ids"] == []
        assert answer["expired_ids"] == [row["id"]]
        assert answer["visible_matched_count"] == 1
        assert answer["expired_at_ms"] is None

    async def test_zero_match_is_a_success_row(
        self, service, client, user_context
    ) -> None:
        result = await service.revoke_permission(
            user_context,
            principal_type="user",
            principal_id="v-revoke-none",
            resource_type="agent",
            resource_id="agent-revoke-none",
        )
        assert result == {"expired_ids": [], "visible_matched_count": 0}
        stored = _match_one_op(
            _interactions(client), "revokePermission", "agent-revoke-none"
        )
        assert stored["status"] == "success"

    async def test_retains_the_row_never_hard_deletes(
        self, service, user_context
    ) -> None:
        """Retention (#13) — the row's other columns survive byte-for-
        byte; only ``expired_at_ms``/``updated_at_ms`` change."""
        row = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-retain",
            resource_type="agent",
            resource_id="agent-retain",
            perm_bits=5,
        )
        await service.revoke_permission(
            user_context,
            principal_type="user",
            principal_id="v-retain",
            resource_type="agent",
            resource_id="agent-retain",
        )
        if isinstance(service, MockConsoleAclEntriesService):
            retained = next(e for e in service._entries if e.id == row["id"])
            assert retained.perm_bits == 5
            assert retained.expired_at_ms is not None
        else:
            pg = dependencies.get_postgres_factory()
            async with pg.get_session_factory()() as db:
                result = await db.execute(
                    sa.select(ConsoleAclEntry).where(ConsoleAclEntry.id == row["id"])
                )
                retained = result.scalar_one()
                assert retained.perm_bits == 5
                assert retained.expired_at_ms is not None


# ── modify_permission_bits — bits recompute, per-bit (#6), no-match
# (D-A2-2), same-bits (7s), never in-place (#7), race branch (S-1) ─────


class TestModifyPermissionBits:
    @pytest.mark.parametrize("bit", [1, 2, 4, 8])
    async def test_each_bit_is_added_individually(
        self, service, user_context, bit: int
    ) -> None:
        resource_id = f"agent-modify-add-{bit}"
        await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-modify",
            resource_type="agent",
            resource_id=resource_id,
            perm_bits=0,
        )
        row = await service.modify_permission_bits(
            user_context,
            principal_type="user",
            principal_id="v-modify",
            resource_type="agent",
            resource_id=resource_id,
            add_bits=bit,
        )
        assert row is not None
        assert row["perm_bits"] == bit
        effective = await service.get_effective_permissions(
            _principal_ctx("v-modify"), "agent", resource_id
        )
        assert (effective & bit) == bit

    @pytest.mark.parametrize("bit", [1, 2, 4, 8])
    async def test_each_bit_is_removed_individually(
        self, service, user_context, bit: int
    ) -> None:
        resource_id = f"agent-modify-remove-{bit}"
        await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-modify",
            resource_type="agent",
            resource_id=resource_id,
            perm_bits=MAX_PERM_BITS,
        )
        row = await service.modify_permission_bits(
            user_context,
            principal_type="user",
            principal_id="v-modify",
            resource_type="agent",
            resource_id=resource_id,
            remove_bits=bit,
        )
        assert row is not None
        assert row["perm_bits"] == MAX_PERM_BITS & ~bit
        effective = await service.get_effective_permissions(
            _principal_ctx("v-modify"), "agent", resource_id
        )
        assert (effective & bit) == 0

    async def test_no_active_row_returns_none_and_writes_one_success_row(
        self, service, client, user_context
    ) -> None:
        row = await service.modify_permission_bits(
            user_context,
            principal_type="user",
            principal_id="v-modify-none",
            resource_type="agent",
            resource_id="agent-modify-none",
            add_bits=2,
            remove_bits=1,
        )
        assert row is None
        stored = _match_one_op(
            _interactions(client), "modifyPermissionBits", "agent-modify-none"
        )
        assert stored["status"] == "success"
        answer = json.loads(stored["answer"])
        assert answer["perm_bits"] == 2  # (0 | 2) & ~1
        assert answer["acl_entry_ids"] == []
        assert answer["expired_ids"] == []
        assert answer["visible_matched_count"] == 0
        assert answer["expired_at_ms"] is None

    async def test_same_bits_still_expires_and_inserts(
        self, service, user_context
    ) -> None:
        """7s — adding a bit already held still expires the old row and
        inserts a NEW one with the same bits and a DIFFERENT id."""
        old = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-samebits",
            resource_type="agent",
            resource_id="agent-samebits",
            perm_bits=5,
        )
        new = await service.modify_permission_bits(
            user_context,
            principal_type="user",
            principal_id="v-samebits",
            resource_type="agent",
            resource_id="agent-samebits",
            add_bits=1,  # already held
        )
        assert new is not None
        assert new["id"] != old["id"]
        assert new["perm_bits"] == 5
        old_pair = await _row_pair(service, old["id"])
        assert old_pair[0] is not None, "the old row must be expired, not left active"

    async def test_never_an_in_place_update_both_rows_on_the_table(
        self, service, user_context
    ) -> None:
        """#7 — old row expired AT the stamp with its ORIGINAL bits
        still on it; new row carries the NEW bits, a different id."""
        old = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-inplace",
            resource_type="agent",
            resource_id="agent-inplace",
            perm_bits=MAX_PERM_BITS,
        )
        new = await service.modify_permission_bits(
            user_context,
            principal_type="user",
            principal_id="v-inplace",
            resource_type="agent",
            resource_id="agent-inplace",
            remove_bits=MAX_PERM_BITS & ~1,
        )
        assert new is not None
        assert new["perm_bits"] == 1
        assert new["id"] != old["id"]
        old_pair = await _row_pair(service, old["id"])
        assert old_pair[0] is not None
        if isinstance(service, MockConsoleAclEntriesService):
            old_row = next(e for e in service._entries if e.id == old["id"])
            assert old_row.perm_bits == MAX_PERM_BITS, (
                "the OLD row's perm_bits must be untouched — modify never "
                "writes UPDATE ... SET perm_bits"
            )

    async def test_inherited_expiry_null_predecessor(
        self, service, user_context
    ) -> None:
        """7i — a NULL-expiry predecessor's inheritance is NULL."""
        await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-inherit-null",
            resource_type="agent",
            resource_id="agent-inherit-null",
            perm_bits=1,
        )
        row = await service.modify_permission_bits(
            user_context,
            principal_type="user",
            principal_id="v-inherit-null",
            resource_type="agent",
            resource_id="agent-inherit-null",
            add_bits=2,
        )
        assert row is not None
        assert row["expired_at_ms"] is None

    async def test_inherited_expiry_time_limited_predecessor(
        self, service, monkeypatch, user_context
    ) -> None:
        """7i — a time-limited predecessor's SCHEDULED expiry is
        inherited — captured BEFORE the UPDATE, never re-read after
        (which would read ``stamp.now_ms`` instead, ADDENDUM 7i(b))."""
        t0 = 1_790_100_000_000
        monkeypatch.setattr(_clock_module, "now_ms", lambda: t0)
        await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-inherit-tl",
            resource_type="agent",
            resource_id="agent-inherit-tl",
            perm_bits=1,
            expired_at_ms=t0 + _HOUR,
        )
        monkeypatch.setattr(_clock_module, "now_ms", lambda: t0 + 1)
        row = await service.modify_permission_bits(
            user_context,
            principal_type="user",
            principal_id="v-inherit-tl",
            resource_type="agent",
            resource_id="agent-inherit-tl",
            add_bits=2,
        )
        assert row is not None
        assert row["expired_at_ms"] == t0 + _HOUR

    async def test_race_branch_expire_returns_nothing_to_insert(
        self, service, client, user_context, monkeypatch
    ) -> None:
        """S-1's injection point: wrap ``_expire_active``/
        ``_expire_matching`` so the SAME key is expired by a SECOND
        caller between the read and the UPDATE — the real call then
        sees ``expired_ids == []`` even though ``active`` was non-empty,
        and must insert NOTHING, write one success row with
        ``perm_bits=new_bits`` (never 0) and return ``None``."""
        await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-race",
            resource_type="agent",
            resource_id="agent-race",
            perm_bits=1,
        )

        if isinstance(service, MockConsoleAclEntriesService):

            def _racing_expire(entries, stamp, match, *, undo_log=None):
                # Someone else expires the row FIRST, then the real call's
                # own attempt matches nothing. _expire_matching is a
                # SYNC function on the mock side — never awaited.
                _mock_expire_matching(entries, stamp, match, undo_log=undo_log)
                return []

            monkeypatch.setattr(
                "audittrace.services.console_acl._mock_write._expire_matching",
                _racing_expire,
            )
        else:
            real_expire_active = _postgres_write._expire_active

            async def _racing_expire_active(db, **kwargs):
                await real_expire_active(db, **kwargs)
                return []

            monkeypatch.setattr(
                _postgres_write, "_expire_active", _racing_expire_active
            )

        row = await service.modify_permission_bits(
            user_context,
            principal_type="user",
            principal_id="v-race",
            resource_type="agent",
            resource_id="agent-race",
            add_bits=2,
        )
        assert row is None, "the race branch inserts nothing and returns None"
        stored = _match_one_op(
            _interactions(client), "modifyPermissionBits", "agent-race"
        )
        assert stored["status"] == "success"
        answer = json.loads(stored["answer"])
        assert answer["perm_bits"] == 3, "new_bits, never 0 (S-1, D-A2-6)"
        assert answer["acl_entry_ids"] == []
        assert answer["expired_ids"] == []
        assert answer["visible_matched_count"] == 1


# ── delete_acl_entries — single/multi predicate, S-3 rendering, D-A2-4,
# ordering, retention (#13) ─────────────────────────────────────────────


class TestDeleteAclEntries:
    async def test_single_predicate_expires_and_returns_ids(
        self, service, client, user_context
    ) -> None:
        row = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-del-1",
            resource_type="agent",
            resource_id="agent-del-1",
            perm_bits=1,
        )
        result = await service.delete_acl_entries(
            user_context, [{"principal_id": "v-del-1", "resource_id": "agent-del-1"}]
        )
        assert result == {"expired_ids": [row["id"]], "visible_matched_count": 1}
        stored = _match_one_op(_interactions(client), "deleteAclEntries", "agent-del-1")
        assert stored["status"] == "success"
        assert stored["question"].startswith("op=deleteAclEntries ")

    async def test_absent_keys_render_dash_everywhere(
        self, service, client, user_context
    ) -> None:
        """S-3 — a predicate naming only ``principal_type='public'``
        renders every OTHER field as ``-`` in the denial/success
        rendering, the frozen writer's own ``None`` rendering."""
        await service.grant_permission(
            user_context,
            principal_type="public",
            principal_id=None,
            resource_type="agent",
            resource_id="agent-del-public",
            perm_bits=1,
        )
        await service.delete_acl_entries(user_context, [{"principal_type": "public"}])
        rows = [
            r
            for r in _interactions(client)
            if r["question"].startswith("op=deleteAclEntries ")
            and "principal=public:-" in r["question"]
        ]
        assert len(rows) >= 1
        assert "resource=-:- bits=0 tenant=-" in rows[0]["question"], rows[0][
            "question"
        ]

    async def test_multi_predicate_one_audit_row_each(
        self, service, client, user_context
    ) -> None:
        """D-A2-4 — three predicates, three success rows, each carrying
        its OWN ``visible_matched_count``/``expired_ids``."""
        r1 = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-del-mp-1",
            resource_type="agent",
            resource_id="agent-del-mp",
            perm_bits=1,
        )
        r2 = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-del-mp-2",
            resource_type="agent",
            resource_id="agent-del-mp",
            perm_bits=1,
            tenant_id="t9",
        )
        before = len(_interactions(client))
        result = await service.delete_acl_entries(
            user_context,
            [
                {"principal_id": "v-del-mp-1"},
                {"principal_id": "v-del-mp-2", "tenant_id": "t9"},
                {"principal_id": "v-del-mp-nonexistent"},
            ],
        )
        assert set(result["expired_ids"]) == {r1["id"], r2["id"]}
        assert result["visible_matched_count"] == 2
        after = _interactions(client)
        assert len(after) == before + 3, "one audit row per predicate, incl. zero-match"

    async def test_ordering_ties_broken_by_created_at_ms_then_id(
        self, service, user_context, monkeypatch
    ) -> None:
        """S-a — delete's OWN order: ``(created_at_ms, id)``, never
        RETURNING/insertion order."""
        t0 = 1_790_200_000_000
        monkeypatch.setattr(_clock_module, "now_ms", lambda: t0)
        a = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-order-a",
            resource_type="agent",
            resource_id="agent-order",
            perm_bits=1,
        )
        monkeypatch.setattr(_clock_module, "now_ms", lambda: t0 + 1)
        b = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-order-b",
            resource_type="agent",
            resource_id="agent-order",
            perm_bits=1,
        )
        result = await service.delete_acl_entries(
            user_context, [{"resource_id": "agent-order"}]
        )
        assert result["expired_ids"] == [a["id"], b["id"]]

    async def test_retains_rows_never_hard_deletes(self, service, user_context) -> None:
        """Retention (#13) on delete."""
        row = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-del-retain",
            resource_type="agent",
            resource_id="agent-del-retain",
            perm_bits=2,
        )
        await service.delete_acl_entries(
            user_context, [{"principal_id": "v-del-retain"}]
        )
        if isinstance(service, MockConsoleAclEntriesService):
            retained = next(e for e in service._entries if e.id == row["id"])
        else:
            pg = dependencies.get_postgres_factory()
            async with pg.get_session_factory()() as db:
                result = await db.execute(
                    sa.select(ConsoleAclEntry).where(ConsoleAclEntry.id == row["id"])
                )
                retained = result.scalar_one()
        assert retained.perm_bits == 2
        assert retained.expired_at_ms is not None


# ── KSEL / KSEL-t / SEL — key/predicate selectivity (A2v2-BL-1,
# A2v3-BL-1, A2-BL-1) ────────────────────────────────────────────────────


class TestKeySelectivity:
    """M(odify)/R(evoke)/D(elete) at a key, plus FIVE siblings each
    differing from the key in exactly ONE of the five columns — a
    selectivity bug (dropping a key column from the WHERE, or using OR
    instead of AND) leaks a sibling's bits into ``old_bits`` / expires a
    sibling it must not touch. ``firstrow`` (a SECOND simultaneously-
    active row at the identical key) is constructed via a raw insert —
    ``grant_permission`` itself cannot produce it (O-6 always supersedes
    the prior active row at the same key first)."""

    async def test_modify_key_selectivity_and_firstrow(
        self, service, user_context, monkeypatch
    ) -> None:
        monkeypatch.setattr(_clock_module, "now_ms", lambda: 1_790_000_000_000)
        owner = user_context.user_id
        key = dict(
            principal_type="user",
            principal_id="viewer-sub-0003",
            resource_type="agent",
            resource_id="agent-1",
            tenant_id=None,
        )
        m_id = await _raw_insert(
            service,
            user_sub=owner,
            principal_type=key["principal_type"],
            principal_id=key["principal_id"],
            principal_model="User",
            resource_type=key["resource_type"],
            resource_id=key["resource_id"],
            perm_bits=7,
            tenant_id=None,
            expired_at_ms=1_790_003_600_000,
            created_at_ms=1,
        )
        # "firstrow" — a second simultaneously-active row at the SAME key.
        m2_id = await _raw_insert(
            service,
            user_sub=owner,
            principal_type=key["principal_type"],
            principal_id=key["principal_id"],
            principal_model="User",
            resource_type=key["resource_type"],
            resource_id=key["resource_id"],
            perm_bits=8,
            tenant_id=None,
            expired_at_ms=1_790_007_200_000,
            created_at_ms=2,
        )
        siblings = {
            "role": await _raw_insert(
                service,
                user_sub=owner,
                principal_type="role",
                principal_id="viewer-sub-0003",
                principal_model="Role",
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=15,
            ),
            "principal": await _raw_insert(
                service,
                user_sub=owner,
                principal_type="user",
                principal_id="other-sub-0009",
                principal_model="User",
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=15,
            ),
            "resource_type": await _raw_insert(
                service,
                user_sub=owner,
                principal_type="user",
                principal_id="viewer-sub-0003",
                principal_model="User",
                resource_type="promptGroup",
                resource_id="agent-1",
                perm_bits=15,
            ),
            "resource_id": await _raw_insert(
                service,
                user_sub=owner,
                principal_type="user",
                principal_id="viewer-sub-0003",
                principal_model="User",
                resource_type="agent",
                resource_id="agent-2",
                perm_bits=15,
            ),
            "tenant": await _raw_insert(
                service,
                user_sub=owner,
                principal_type="user",
                principal_id="viewer-sub-0003",
                principal_model="User",
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=15,
                tenant_id="t1",
            ),
        }
        sibling_pairs_before = {
            name: await _row_pair(service, sid) for name, sid in siblings.items()
        }

        row = await service.modify_permission_bits(user_context, add_bits=1, **key)

        assert row is not None
        assert row["perm_bits"] == 15, "old_bits = OR(7, 8) = 15, never active[0]"
        assert row["expired_at_ms"] == 1_790_007_200_000, "the GREATEST captured expiry"
        for name, sid in siblings.items():
            assert await _row_pair(service, sid) == sibling_pairs_before[name], (
                f"sibling differing only in {name!r} must be untouched"
            )
        assert (await _row_pair(service, m_id))[0] is not None
        assert (await _row_pair(service, m2_id))[0] is not None

    async def test_revoke_key_selectivity(self, service, user_context) -> None:
        owner = user_context.user_id
        key = dict(
            principal_type="user",
            principal_id="viewer-sub-0004",
            resource_type="agent",
            resource_id="agent-9",
            tenant_id=None,
        )
        m_id = await _raw_insert(
            service, user_sub=owner, principal_model="User", perm_bits=7, **key
        )
        siblings = {
            "role": await _raw_insert(
                service,
                user_sub=owner,
                principal_type="role",
                principal_id="viewer-sub-0004",
                principal_model="Role",
                resource_type="agent",
                resource_id="agent-9",
                perm_bits=15,
            ),
            "principal": await _raw_insert(
                service,
                user_sub=owner,
                principal_type="user",
                principal_id="other-sub-0010",
                principal_model="User",
                resource_type="agent",
                resource_id="agent-9",
                perm_bits=15,
            ),
            "resource_type": await _raw_insert(
                service,
                user_sub=owner,
                principal_type="user",
                principal_id="viewer-sub-0004",
                principal_model="User",
                resource_type="promptGroup",
                resource_id="agent-9",
                perm_bits=15,
            ),
            "resource_id": await _raw_insert(
                service,
                user_sub=owner,
                principal_type="user",
                principal_id="viewer-sub-0004",
                principal_model="User",
                resource_type="agent",
                resource_id="agent-10",
                perm_bits=15,
            ),
            "tenant": await _raw_insert(
                service,
                user_sub=owner,
                principal_type="user",
                principal_id="viewer-sub-0004",
                principal_model="User",
                resource_type="agent",
                resource_id="agent-9",
                perm_bits=15,
                tenant_id="t1",
            ),
        }
        sibling_pairs_before = {
            name: await _row_pair(service, sid) for name, sid in siblings.items()
        }

        result = await service.revoke_permission(user_context, **key)

        assert result == {"expired_ids": [m_id], "visible_matched_count": 1}
        for name, sid in siblings.items():
            assert await _row_pair(service, sid) == sibling_pairs_before[name]

    async def test_delete_predicate_selectivity(self, service, user_context) -> None:
        """SEL — same shape, through ``_predicate_clause``. The
        predicate names ALL FIVE key columns explicitly (a NULL tenant
        cannot be named in a predicate at all, D-A2-8 — so M sits at
        tenant ``t1`` here, mirroring KSEL-t's shape, precisely so the
        predicate CAN name every column and each sibling differs in
        exactly one of them)."""
        owner = user_context.user_id
        m_id = await _raw_insert(
            service,
            user_sub=owner,
            principal_type="user",
            principal_id="viewer-sub-0005",
            principal_model="User",
            resource_type="agent",
            resource_id="agent-11",
            perm_bits=7,
            tenant_id="t1",
        )
        siblings = {
            "role": await _raw_insert(
                service,
                user_sub=owner,
                principal_type="role",
                principal_id="viewer-sub-0005",
                principal_model="Role",
                resource_type="agent",
                resource_id="agent-11",
                perm_bits=15,
                tenant_id="t1",
            ),
            "principal": await _raw_insert(
                service,
                user_sub=owner,
                principal_type="user",
                principal_id="other-sub-0011",
                principal_model="User",
                resource_type="agent",
                resource_id="agent-11",
                perm_bits=15,
                tenant_id="t1",
            ),
            "resource_type": await _raw_insert(
                service,
                user_sub=owner,
                principal_type="user",
                principal_id="viewer-sub-0005",
                principal_model="User",
                resource_type="promptGroup",
                resource_id="agent-11",
                perm_bits=15,
                tenant_id="t1",
            ),
            "resource_id": await _raw_insert(
                service,
                user_sub=owner,
                principal_type="user",
                principal_id="viewer-sub-0005",
                principal_model="User",
                resource_type="agent",
                resource_id="agent-12",
                perm_bits=15,
                tenant_id="t1",
            ),
            "tenant": await _raw_insert(
                service,
                user_sub=owner,
                principal_type="user",
                principal_id="viewer-sub-0005",
                principal_model="User",
                resource_type="agent",
                resource_id="agent-11",
                perm_bits=15,
                tenant_id=None,
            ),
        }
        sibling_pairs_before = {
            name: await _row_pair(service, sid) for name, sid in siblings.items()
        }

        result = await service.delete_acl_entries(
            user_context,
            [
                {
                    "principal_type": "user",
                    "principal_id": "viewer-sub-0005",
                    "resource_type": "agent",
                    "resource_id": "agent-11",
                    "tenant_id": "t1",
                }
            ],
        )

        assert result == {"expired_ids": [m_id], "visible_matched_count": 1}
        for name, sid in siblings.items():
            assert await _row_pair(service, sid) == sibling_pairs_before[name]

    async def test_modify_tenant_carriage_ksel_t(
        self, service, user_context, monkeypatch
    ) -> None:
        """KSEL-t (A2v3-BL-1) — the write-side tenant-carriage fix: the
        NEW row's ``tenant_id`` (and every other key column) come from
        THIS call's key, never from a captured/sibling row. A closing
        ``revoke(tenant_id='t1')`` on the NEW row must then actually
        expire it — if the insert had landed at ``tenant_id=None``
        instead, that revoke would silently miss it while the original
        grant stayed live."""
        monkeypatch.setattr(_clock_module, "now_ms", lambda: 1_790_000_000_000)
        owner = user_context.user_id
        key = dict(
            principal_type="user",
            principal_id="viewer-sub-0003",
            resource_type="agent",
            resource_id="agent-1",
            tenant_id="t1",
        )
        m_id = await _raw_insert(
            service,
            user_sub=owner,
            principal_model="User",
            perm_bits=7,
            role_id="r1",
            expired_at_ms=1_790_003_600_001,
            **key,
        )
        null_sibling_id = await _raw_insert(
            service,
            user_sub=owner,
            principal_type="user",
            principal_id="viewer-sub-0003",
            principal_model="User",
            resource_type="agent",
            resource_id="agent-1",
            perm_bits=15,
            tenant_id=None,
        )
        null_sibling_before = await _row_pair(service, null_sibling_id)

        row = await service.modify_permission_bits(user_context, add_bits=0, **key)

        assert row is not None
        assert row["tenant_id"] == "t1"
        assert row["perm_bits"] == 7
        assert row["role_id"] == "r1"
        assert row["expired_at_ms"] == 1_790_003_600_001
        assert row["principal_type"] == "user"
        assert row["principal_id"] == "viewer-sub-0003"
        assert row["resource_type"] == "agent"
        assert row["resource_id"] == "agent-1"
        assert row["user_sub"] == owner
        assert row["granted_by"] == owner
        assert row["principal_model"] == "User"
        assert await _row_pair(service, null_sibling_id) == null_sibling_before

        # The closing proof — a revoke AT tenant t1 must expire the NEW
        # row (never silently miss it, A2v3-BL-1's defect signature).
        closing = await service.revoke_permission(user_context, **key)
        assert closing == {"expired_ids": [row["id"]], "visible_matched_count": 1}
        assert (await _row_pair(service, m_id))[0] is not None


# ── #4 — N4 audit-write-failure: fail-closed caller half, per method ────


class TestA2AuditWriteFailure:
    """Same technique as ``TestFailClosedCallerHalf`` above (A1's grant
    version): a broken FIRST ``_content_hash`` call must propagate the
    ORIGINAL exception, leave the ACL row(s) untouched by the FAILED
    write, and still land ONE ``acl_audit_write_failed`` row (the
    denial writer's OWN, independent, content_hash call is left
    unbroken by the counting side effect)."""

    def _flaky_content_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = _audit._content_hash
        calls = {"n": 0}

        def _flaky(*args: object, **kwargs: object) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("content_hash broken for this test")
            return original(*args, **kwargs)

        monkeypatch.setattr(_audit, "_content_hash", _flaky)

    async def test_revoke_propagates_and_leaves_the_row_active(
        self, service, client, user_context, monkeypatch
    ) -> None:
        row = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-n4-revoke",
            resource_type="agent",
            resource_id="agent-n4-revoke",
            perm_bits=1,
        )
        self._flaky_content_hash(monkeypatch)
        with pytest.raises(RuntimeError, match="content_hash broken"):
            await service.revoke_permission(
                user_context,
                principal_type="user",
                principal_id="v-n4-revoke",
                resource_type="agent",
                resource_id="agent-n4-revoke",
            )
        pair = await _row_pair(service, row["id"])
        assert pair[0] is None, "the row must still be ACTIVE — the expiry rolled back"
        rows = [
            r
            for r in _interactions(client)
            if r["failure_class"] == _audit.FAILURE_CLASS_ACL_AUDIT_WRITE_FAILED
            and "agent-n4-revoke" in r["question"]
        ]
        assert len(rows) == 1

    async def test_modify_propagates_and_restores_the_old_row(
        self, service, client, user_context, monkeypatch
    ) -> None:
        row = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-n4-modify",
            resource_type="agent",
            resource_id="agent-n4-modify",
            perm_bits=1,
        )
        self._flaky_content_hash(monkeypatch)
        with pytest.raises(RuntimeError, match="content_hash broken"):
            await service.modify_permission_bits(
                user_context,
                principal_type="user",
                principal_id="v-n4-modify",
                resource_type="agent",
                resource_id="agent-n4-modify",
                add_bits=2,
            )
        pair = await _row_pair(service, row["id"])
        assert pair[0] is None, "the OLD row must still be ACTIVE — no partial mutation"
        rows = [
            r
            for r in _interactions(client)
            if r["failure_class"] == _audit.FAILURE_CLASS_ACL_AUDIT_WRITE_FAILED
            and "agent-n4-modify" in r["question"]
        ]
        assert len(rows) == 1

    async def test_delete_propagates_and_leaves_the_row_active(
        self, service, client, user_context, monkeypatch
    ) -> None:
        row = await service.grant_permission(
            user_context,
            principal_type="user",
            principal_id="v-n4-delete",
            resource_type="agent",
            resource_id="agent-n4-delete",
            perm_bits=1,
        )
        self._flaky_content_hash(monkeypatch)
        with pytest.raises(RuntimeError, match="content_hash broken"):
            await service.delete_acl_entries(
                user_context,
                [{"principal_id": "v-n4-delete", "resource_id": "agent-n4-delete"}],
            )
        pair = await _row_pair(service, row["id"])
        assert pair[0] is None, "the row must still be ACTIVE — the expiry rolled back"
        rows = [
            r
            for r in _interactions(client)
            if r["failure_class"] == _audit.FAILURE_CLASS_ACL_AUDIT_WRITE_FAILED
            and "agent-n4-delete" in r["question"]
        ]
        assert len(rows) == 1


# ── §4.0 documentation greps, extended for A2 (spec §10 — 2 / 1 / 0 / 0) ──


class TestA2DocumentationGreps:
    def _grep(self, pattern: str) -> str:
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [
                "grep",
                "-cE",
                pattern,
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
        return result.stdout.strip()

    def test_two_select_statements(self) -> None:
        """``_visible_ids``/``_active_rows`` each hold ONE short
        ``sa.select(...)`` call that ``ruff format`` canonically renders
        on a SINGLE line (unlike ``_expire_where``'s longer chained
        ``sa.update(...)``, which doesn't fit and stays multi-line) — so
        this count is NOT line-anchored, unlike the update check below.
        Verified (this file's own module docstring) that no OTHER
        mention of the literal ``sa.select(`` text exists in
        ``_postgres_write.py`` to inflate this count."""
        assert self._grep(r"sa\.select\(") == "2"

    def test_one_update_statement(self) -> None:
        """Line-anchored — the module docstring mentions
        ``sa.update(ConsoleAclEntry)`` in prose (never at the START of
        its own line, since other words precede it there), so anchoring
        on ``^\\s+sa\\.update\\(`` counts only the real, multi-line
        statement in ``_expire_where``."""
        assert self._grep(r"^\s+sa\.update\(") == "1"

    def test_no_delete_statement(self) -> None:
        """Retention (#13) — A2's write path never issues a Core
        ``sa.delete(...)`` (migration 033's trigger would refuse a real
        DELETE on Postgres regardless; this is the "we never even try"
        half)."""
        assert self._grep(r"^\s+sa\.delete\(") == "0"
