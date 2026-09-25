"""Unit tests for ``services/console_acl/_audit.py`` — the sovereign ACL
audit writer (ACL 2b-core-B,
``2026-09-25-SPEC-acl-2b-core-B-audit-writer-CONSOLIDATED-v2.md``).

Runs against the ``aiosqlite`` param (``InMemoryPostgresFactory`` via the
standard ``client``/``user_context`` fixtures) — the REAL-Postgres half of
the same guards (N3's stronger RLS-based abort, N5's append-only trigger,
N6's cross-subject-read limitation pin, N8's forged-user_id-refused-on-PG
counterpart) live in ``tests/test_acl_ownership_rls.py`` per §9's "aiosqlite
param AND real PG" instruction and §2's additions-only exception.

**Per-guard neuter table (see the build record for the run log; PG-side
rows live in the sibling file's own table):**

* **N1** — ``TestRecordSuccess`` — audit row on a successful write, full
  payload + content_hash coverage asserted.
* **N2** — ``TestRecordDenial`` — audit row on a denied write, separately.
* **N3** (aiosqlite half; PG half in ``test_acl_ownership_rls.py``) —
  ``TestDenialRowSurvivesRollback``.
* **N4** (the writer's half — no operation exists yet, see the module
  docstring) — ``TestFailClosed``.
* **N7** — ``TestTraceIdDerivation`` — the §4.3 NULL-trace-trap proof.
* **N8** (VALUE assertion + aiosqlite half of the forged-identity
  asymmetry; PG half in ``test_acl_ownership_rls.py``) —
  ``TestUserIdAndGrantedByDerivation``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from audittrace import integrity
from audittrace.db.models import ConsoleAclEntry, InteractionRecord
from audittrace.dependencies import get_postgres_factory
from audittrace.services.console_acl import _audit
from audittrace.services.console_acl._audit import (
    ACL_DENIAL_FAILURE_CLASSES,
    EVENT_CLASS_ACL_AUTHZ,
    FAILURE_CLASS_ACL_DENIED_POLICY,
    record,
    record_denial,
)


def _assert_trace_id_is_valid_hex(value: str | None) -> None:
    """§4.3's ordering: assert 32-char hex BEFORE any reconstruction match
    is attempted. Raises ``AssertionError``/``ValueError`` on ``None`` or
    anything not real 32-char hex — the guard the NULL-trace trap needs."""
    assert value is not None, "trace_id must not be None to be matchable"
    assert isinstance(value, str)
    assert len(value) == 32, f"expected 32-char hex, got {value!r}"
    int(value, 16)  # raises ValueError if not hex


async def _open_db():
    pg = get_postgres_factory()
    return pg.get_session_factory()


class TestRecordSuccess:
    """N1 — audit row on a successful write; row exists, payload asserted,
    content_hash coverage asserted."""

    async def test_record_persists_a_full_payload_row(
        self, client, user_context
    ) -> None:
        session_factory = await _open_db()
        async with session_factory() as db:
            row = await record(
                db,
                user_context=user_context,
                op="grantPermission",
                principal_type="user",
                principal_id="principal-1",
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=3,  # VIEW | EDIT
                acl_entry_ids=["acl-entry-1"],
                expired_ids=[],
                visible_matched_count=1,
                expired_at_ms=None,
                tenant_id="tenant-1",
            )
            await db.commit()
            row_id = row.id

        resp = client.get(
            "/interactions",
            params={"event_class": EVENT_CLASS_ACL_AUTHZ, "limit": 1000},
        )
        assert resp.status_code == 200
        matches = [r for r in resp.json()["interactions"] if r["id"] == row_id]
        assert len(matches) == 1, "a 200 without a match is a FAIL (§4.1)"
        stored = matches[0]

        assert stored["project"] == "console-acl"
        assert stored["source"] == "console-acl"
        assert stored["event_class"] == EVENT_CLASS_ACL_AUTHZ
        assert stored["status"] == "success"
        assert stored["failure_class"] is None
        assert stored["user_id"] == user_context.user_id
        assert stored["question"] == (
            "op=grantPermission principal=user:principal-1 "
            "resource=agent:agent-1 bits=3 tenant=tenant-1"
        )

        answer = json.loads(stored["answer"])
        assert answer == {
            "acl_entry_ids": ["acl-entry-1"],
            "expired_ids": [],
            "visible_matched_count": 1,
            "perm_bits": 3,
            "bits": {"VIEW": True, "EDIT": True, "DELETE": False, "SHARE": False},
            "granted_by": user_context.user_id,
            "expired_at_ms": None,
        }

        # content_hash coverage (§3.1): every field lives in a
        # _CONTENT_FIELDS column, recomputation must match the stored hash.
        recomputed = integrity.content_hash(
            {k: stored.get(k) for k in integrity._CONTENT_FIELDS}
        )
        assert stored["content_hash"] == recomputed

    async def test_record_does_not_commit_the_callers_session(
        self, client, user_context
    ) -> None:
        """§1/§5 — record() stages the row on the CALLER's session; it is
        invisible to a fresh session until the caller commits (proves the
        row rides the caller's own transaction, never its own)."""
        session_factory = await _open_db()
        async with session_factory() as db:
            await record(
                db,
                user_context=user_context,
                op="grantPermission",
                principal_type="user",
                principal_id="p1",
                resource_type="agent",
                resource_id="agent-uncommitted",
                perm_bits=1,
                acl_entry_ids=["acl-x"],
            )
            # Deliberately NOT committed yet — check visibility from a
            # fresh, independent session.
            async with session_factory() as fresh:
                rows = (
                    (
                        await fresh.execute(
                            select(InteractionRecord).where(
                                InteractionRecord.event_class == EVENT_CLASS_ACL_AUTHZ
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert rows == [], (
                    "record() must not commit — the row must ride the "
                    "caller's own transaction"
                )
            await db.rollback()


class TestRecordDenial:
    """N2 — audit row on a DENIED write, separately from N1."""

    async def test_record_denial_persists_a_failed_row(
        self, client, user_context
    ) -> None:
        row = await record_denial(
            user_context=user_context,
            op="grantPermission",
            principal_type="user",
            principal_id="principal-1",
            resource_type="agent",
            resource_id="agent-999",
            perm_bits=1,
            failure_class=FAILURE_CLASS_ACL_DENIED_POLICY,
            predicate_or_attempted_row={"resource_id": "agent-999"},
            db_error_class="IntegrityError",
        )

        resp = client.get(
            "/interactions",
            params={"event_class": EVENT_CLASS_ACL_AUTHZ, "limit": 1000},
        )
        matches = [r for r in resp.json()["interactions"] if r["id"] == row.id]
        assert len(matches) == 1
        stored = matches[0]

        assert stored["status"] == "failed"
        assert stored["failure_class"] == FAILURE_CLASS_ACL_DENIED_POLICY
        assert stored["answer"] == "{}"
        detail = json.loads(stored["error_detail"])
        assert detail == {
            "predicate_or_attempted_row": {"resource_id": "agent-999"},
            "db_error_class": "IntegrityError",
        }
        recomputed = integrity.content_hash(
            {k: stored.get(k) for k in integrity._CONTENT_FIELDS}
        )
        assert stored["content_hash"] == recomputed

    async def test_unknown_failure_class_is_refused(self, client, user_context) -> None:
        with pytest.raises(ValueError, match="not in the closed ACL denial set"):
            await record_denial(
                user_context=user_context,
                op="grantPermission",
                principal_type="user",
                principal_id="p1",
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
                failure_class="not_a_real_failure_class",
                predicate_or_attempted_row={},
            )

    def test_closed_set_has_exactly_the_five_pinned_values(self) -> None:
        assert ACL_DENIAL_FAILURE_CLASSES == {
            "acl_denied_policy",
            "acl_denied_principal_type",
            "acl_denied_past_expiry",
            "acl_denied_bulk_rollback",
            "acl_audit_write_failed",
        }


class TestDenialRowSurvivesRollback:
    """N3 (aiosqlite half — the ``031:97-100`` CHECK works on both
    dialects). §5.2: a DB-level refusal rolls back the transaction it is
    in, taking any in-transaction row with it; the denial row, written in
    its OWN independent transaction, must NOT be taken down with it."""

    async def test_denial_row_survives_the_aborted_transaction_it_documents(
        self, client, user_context
    ) -> None:
        session_factory = await _open_db()

        with pytest.raises(IntegrityError):
            async with session_factory() as db:
                db.add(
                    ConsoleAclEntry(
                        id=str(uuid.uuid4()),
                        user_sub=user_context.user_id,
                        principal_type="user",
                        principal_id="p1",
                        principal_model="User",
                        resource_type="agent",
                        resource_id="agent-check-violation",
                        # Violates ck_console_acl_entries_perm_bits_range
                        # (031:97-100) on BOTH dialects.
                        perm_bits=99,
                        granted_at_ms=0,
                        created_at_ms=0,
                        updated_at_ms=0,
                    )
                )
                await db.commit()

        # Written in its OWN transaction, independent of the aborted one
        # above — must survive.
        denial = await record_denial(
            user_context=user_context,
            op="grantPermission",
            principal_type="user",
            principal_id="p1",
            resource_type="agent",
            resource_id="agent-check-violation",
            perm_bits=99,
            failure_class=FAILURE_CLASS_ACL_DENIED_POLICY,
            predicate_or_attempted_row={"perm_bits": 99},
            db_error_class="IntegrityError",
        )

        async with session_factory() as fresh:
            acl_rows = (
                (
                    await fresh.execute(
                        select(ConsoleAclEntry).where(
                            ConsoleAclEntry.resource_id == "agent-check-violation"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert acl_rows == [], "the aborted ACL insert must not have landed"

            denial_rows = (
                (
                    await fresh.execute(
                        select(InteractionRecord).where(
                            InteractionRecord.id == denial.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(denial_rows) == 1, "the denial row must survive the rollback"


class TestFailClosed:
    """N4 — the WRITER's half of fail-closed: break the audit write and
    the writer raises rather than swallowing. The CALLER's half (a future
    2b-core-A write method must not catch and discard this either) has no
    operation to test against yet — logged as a forward obligation."""

    async def test_record_propagates_a_broken_content_hash(
        self, client, user_context, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError("content_hash broken for this test")

        monkeypatch.setattr(_audit, "_content_hash", _raise)
        session_factory = await _open_db()
        with pytest.raises(RuntimeError, match="content_hash broken"):
            async with session_factory() as db:
                await record(
                    db,
                    user_context=user_context,
                    op="grantPermission",
                    principal_type="user",
                    principal_id="p1",
                    resource_type="agent",
                    resource_id="agent-1",
                    perm_bits=1,
                    acl_entry_ids=["acl-1"],
                )

    async def test_record_denial_propagates_a_broken_content_hash(
        self, client, user_context, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError("content_hash broken for this test")

        monkeypatch.setattr(_audit, "_content_hash", _raise)
        with pytest.raises(RuntimeError, match="content_hash broken"):
            await record_denial(
                user_context=user_context,
                op="grantPermission",
                principal_type="user",
                principal_id="p1",
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
                failure_class=FAILURE_CLASS_ACL_DENIED_POLICY,
                predicate_or_attempted_row={},
            )

    async def test_record_denial_propagates_a_broken_postgres_factory(
        self, client, user_context, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A more realistic "the write itself failed" case — the DB layer
        is unavailable — still raises, never silently returns."""

        def _raise() -> object:
            raise RuntimeError("postgres factory unavailable for this test")

        monkeypatch.setattr(_audit, "get_postgres_factory", _raise)
        with pytest.raises(RuntimeError, match="postgres factory unavailable"):
            await record_denial(
                user_context=user_context,
                op="grantPermission",
                principal_type="user",
                principal_id="p1",
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
                failure_class=FAILURE_CLASS_ACL_DENIED_POLICY,
                predicate_or_attempted_row={},
            )


class TestTraceIdDerivation:
    """N7 — trace_id derivation is choke-stamped, never test-supplied.
    §4.3: the write runs inside a real span, the captured value is
    asserted 32-char hex BEFORE any match, and a NULL match is a FAIL."""

    async def test_trace_id_is_the_active_spans_id_and_matches_on_reconstruction(
        self, client, user_context
    ) -> None:
        tracer = TracerProvider().get_tracer("acl-audit-writer-tests")
        session_factory = await _open_db()
        with tracer.start_as_current_span("acl-write") as span:
            captured = format(span.get_span_context().trace_id, "032x")
            _assert_trace_id_is_valid_hex(captured)  # BEFORE any match
            async with session_factory() as db:
                row = await record(
                    db,
                    user_context=user_context,
                    op="grantPermission",
                    principal_type="user",
                    principal_id="p1",
                    resource_type="agent",
                    resource_id="agent-trace",
                    perm_bits=1,
                    acl_entry_ids=["acl-trace"],
                )
                await db.commit()
                row_id = row.id

        resp = client.get(
            "/interactions",
            params={"event_class": EVENT_CLASS_ACL_AUTHZ, "limit": 1000},
        )
        rows = resp.json()["interactions"]
        by_id = [r for r in rows if r["id"] == row_id]
        assert len(by_id) == 1
        assert by_id[0]["trace_id"] == captured

        by_trace = [r for r in rows if r["trace_id"] == captured]
        assert len(by_trace) == 1, "a 200 without a trace_id match is a FAIL (§4.1)"

    async def test_null_trace_is_never_a_valid_match_the_null_trace_trap(
        self, client, user_context
    ) -> None:
        """§4.3 — with no active span, ``current_trace_id_hex()`` returns
        ``None``. A naive recipe that matches on ``None == None`` would
        find every NULL-trace row and call it proof. The REQUIRED guard
        (``_assert_trace_id_is_valid_hex``) refuses ``None`` BEFORE any
        match is even attempted."""
        session_factory = await _open_db()
        with otel_trace.use_span(otel_trace.INVALID_SPAN, end_on_exit=False):
            captured = _audit.current_trace_id_hex()
            assert captured is None
            async with session_factory() as db:
                row = await record(
                    db,
                    user_context=user_context,
                    op="grantPermission",
                    principal_type="user",
                    principal_id="p1",
                    resource_type="agent",
                    resource_id="agent-null-trace",
                    perm_bits=1,
                    acl_entry_ids=["acl-null-trace"],
                )
                await db.commit()
                row_id = row.id

        resp = client.get(
            "/interactions",
            params={"event_class": EVENT_CLASS_ACL_AUTHZ, "limit": 1000},
        )
        stored = next(r for r in resp.json()["interactions"] if r["id"] == row_id)
        assert stored["trace_id"] is None, (
            "no span active — trace_id must not be fabricated"
        )

        # The REQUIRED ordering: assert-valid-hex BEFORE matching. A NULL
        # capture must fail this assertion, so reconstruction can never
        # proceed to a (meaningless) None==None match.
        with pytest.raises(AssertionError):
            _assert_trace_id_is_valid_hex(captured)


class TestUserIdAndGrantedByDerivation:
    """N8 — VALUE assertion that ``user_id``/``granted_by`` derive from
    the request-resolved ``UserContext``, never a parameter, plus the
    aiosqlite half of the forged-identity asymmetry (PG half in
    ``test_acl_ownership_rls.py``)."""

    async def test_user_id_and_granted_by_equal_the_context_subject(
        self, client, user_context
    ) -> None:
        session_factory = await _open_db()
        async with session_factory() as db:
            row = await record(
                db,
                user_context=user_context,
                op="grantPermission",
                principal_type="user",
                principal_id="p1",
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
                acl_entry_ids=["acl-1"],
            )
            await db.commit()

        assert row.user_id == user_context.user_id
        answer = json.loads(row.answer)
        assert answer["granted_by"] == user_context.user_id

    async def test_neutering_the_derivation_flips_the_value_assertion_red(
        self, client, user_context, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Replace the derivation with a hard-coded wrong subject — the
        value assertion above must now fail, proving it actually depends
        on the derivation rather than being vacuously true."""

        def _wrong_user_id(_ctx: object) -> str:
            return "wrong-hardcoded-subject"

        monkeypatch.setattr(_audit, "_require_user_id", _wrong_user_id)
        session_factory = await _open_db()
        async with session_factory() as db:
            row = await record(
                db,
                user_context=user_context,
                op="grantPermission",
                principal_type="user",
                principal_id="p1",
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
                acl_entry_ids=["acl-1"],
            )
            await db.commit()

        assert row.user_id != user_context.user_id
        assert row.user_id == "wrong-hardcoded-subject"

    async def test_empty_user_id_is_refused(self, client, user_context) -> None:
        empty = replace(user_context, user_id="")
        session_factory = await _open_db()
        with pytest.raises(ValueError, match="refusing to persist"):
            async with session_factory() as db:
                await record(
                    db,
                    user_context=empty,
                    op="grantPermission",
                    principal_type="user",
                    principal_id="p1",
                    resource_type="agent",
                    resource_id="agent-1",
                    perm_bits=1,
                    acl_entry_ids=["acl-1"],
                )
        with pytest.raises(ValueError, match="refusing to persist"):
            await record_denial(
                user_context=empty,
                op="grantPermission",
                principal_type="user",
                principal_id="p1",
                resource_type="agent",
                resource_id="agent-1",
                perm_bits=1,
                failure_class=FAILURE_CLASS_ACL_DENIED_POLICY,
                predicate_or_attempted_row={},
            )

    async def test_a_forged_user_id_bypassing_the_writer_succeeds_silently_on_aiosqlite(
        self, client, user_context
    ) -> None:
        """N8's asymmetry note, aiosqlite half: ``_audit.py`` itself takes
        no ``user_id`` parameter, so the ONLY way to "forge" one is to
        bypass the writer entirely with a raw INSERT. On aiosqlite (no
        RLS), that raw INSERT succeeds regardless of any ambient identity
        — the structural rule (no parameter on the writer's own API), not
        RLS, is what protects this path. The Postgres counterpart in
        ``test_acl_ownership_rls.py`` proves the SAME raw INSERT is
        refused there by migration 005's RLS ``WITH CHECK``."""
        session_factory = await _open_db()
        async with session_factory() as db:
            db.add(
                InteractionRecord(
                    project="console-acl",
                    source="console-acl",
                    question="raw forged insert",
                    answer="{}",
                    prompt_tokens=0,
                    completion_tokens=0,
                    timestamp="2026-09-25T00:00:00+00:00",
                    session_id=None,
                    model=None,
                    user_id="attacker-forged-subject",
                    status="success",
                    event_class=EVENT_CLASS_ACL_AUTHZ,
                )
            )
            await db.commit()  # succeeds — no RLS on aiosqlite

        async with session_factory() as fresh:
            rows = (
                (
                    await fresh.execute(
                        select(InteractionRecord).where(
                            InteractionRecord.user_id == "attacker-forged-subject"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1, (
                "aiosqlite enforces no RLS — the forged row lands; the "
                "writer's own no-parameter API is the real protection"
            )
