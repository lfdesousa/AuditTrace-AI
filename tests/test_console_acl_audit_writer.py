"""Unit tests for ``services/console_acl/_audit.py`` — the sovereign ACL
audit writer (ACL 2b-core-B,
``2026-09-25-SPEC-acl-2b-core-B-audit-writer-CONSOLIDATED-v2.md``).

Runs against the ``aiosqlite`` param (``InMemoryPostgresFactory`` via the
standard ``client``/``user_context`` fixtures) — the REAL-Postgres half of
the same guards (N3's stronger RLS-based abort, N5's append-only trigger,
N6's cross-subject-read limitation pin, F1's forged-``UserContext`` PG
counterpart) live in ``tests/test_acl_ownership_rls.py`` per §9's
"aiosqlite param AND real PG" instruction and §2's additions-only
exception.

**Fix round 1 (independent review REJECT, 7 findings, F1 a real security
defect).** F1: the first cut's ``_require_user_id`` only checked
``user_context.user_id`` for emptiness — it never cross-checked it
against the ambient RLS ContextVar, so a forged ``UserContext`` passed
straight through both ``record()`` and ``record_denial()``. Fixed by
switching to ``console_store.resolve_user_sub`` (§3.2's derivation choke
already used by every other console-* domain), and this file now
exercises the guard directly (``TestForgedIdentityIsRefused``) rather
than asserting on a bypass that never went through the writer's own API.
F2: N3's two tests called ``record_denial`` AFTER the aborted transaction
had already fully rolled back, so "survives the rollback" held for an
unrelated reason (no overlap ever existed). Fixed round 1 by calling
``record_denial`` WHILE the failed flush's transaction was still open —
**corrected further in round 2, see below.**
F3: nothing exercised ``record_denial``'s own ``trace_id`` derivation —
fixed by ``TestDenialTraceIdDerivation``. F4: nothing pinned
``EVENT_CLASS_ACL_AUTHZ`` — fixed by ``TestEventClassPinning``. F5:
nothing exercised ``session_id`` — fixed by ``TestSessionIdDerivation``.
F6: the only ``content_hash`` checks ran with a NULL ``trace_id`` — fixed
by ``TestContentHashCoversANonNullTraceId``. The self-fulfilling
monkeypatch neuter (``test_neutering_the_derivation_flips_the_value_
assertion_red``, which patched ``_require_user_id`` and then asserted
the EXACT value it had just injected) is REMOVED — replaced by the
forged-``UserContext`` tests below, which land on a raised exception the
neuter did not itself set, plus a manual edit-and-restore neuter of the
real derivation call sites (documented in the build evidence, same
methodology as N7's).

**Fix round 2 (independent review REJECT, narrow — 3 findings, all
"the code is right and the claim about it is wrong").**

* **B1/F6b** — round 1's F6 covered ``record()`` only; the evidence file
  claimed ``record_denial()`` was "indirectly covered by the SAME
  N7/F3 neuters" — FALSE (those neuters change the ``trace_id`` VALUE,
  never what the hash COVERS). Fixed by
  ``test_denial_content_hash_verifies_with_a_real_non_null_trace_id``.
* **B2 correction (round 2's OWN replacement claim was ALSO wrong)** —
  round 2 attributed the observed ``BEGIN``/``ROLLBACK`` to "aiosqlite's
  DBAPI driver" and contrasted it with "Postgres does NOT auto-rollback"
  — a SQLite-versus-Postgres claim. **Both halves were wrong.** A
  connection-level ``event.listen(engine, "rollback", ...)`` probe
  (added below, so this is now instrumented, not asserted) shows the
  rollback fires as a **SQLAlchemy** connection-level event, not
  something the aiosqlite driver does on its own — and a matching probe
  against the real Postgres harness shows the SAME thing happens there:
  **when a flush fails OUTSIDE a savepoint, SQLAlchemy rolls back the
  connection, on ANY database.** The real difference between the two N3
  tests was never SQLite-vs-Postgres — it is **HOW each test aborts**:
  this aiosqlite test aborts via an ORM ``flush()`` with no savepoint
  active (so only the ORM ``Session`` is left "pending rollback" — the
  connection itself was already rolled back by SQLAlchemy), while the
  Postgres N3 test (`tests/test_acl_ownership_rls.py::
  TestAuditWriterRealPostgres::
  test_n3_denial_row_survives_an_rls_aborted_transaction`) aborts via a
  Core-level ``execute(text(...))``, which leaves the transaction
  genuinely OPEN AND ABORTED at the database level (no flush occurs, so
  SQLAlchemy never rolls it back on its own).

* **Fix round 4 correction (round 3's "on ANY database" was ALSO
  overgeneralised — an independent reviewer measured a THIRD shape this
  file did not test).** Round 3 stated the flush-rollback mechanism as
  an unqualified law. It is not: **a flush inside an active
  ``session.begin_nested()`` sends only ``ROLLBACK TO SAVEPOINT``** —
  the OUTER transaction stays open, NOT aborted, and any earlier writes
  in it remain pending and committable. A second subtlety:
  ``begin_nested()`` itself first **autoflushes already-pending
  objects BEFORE opening the savepoint**, so an object ``add()``-ed
  BEFORE ``begin_nested()`` is flushed OUTSIDE the savepoint and still
  triggers a full connection rollback — the outcome depends on where
  the ``add``/flush sits relative to the savepoint boundary. **The
  correct, three-shape statement**, replacing every prior "on ANY
  database" claim in this file: (1) a flush OUTSIDE any savepoint rolls
  back the whole connection, on any dialect; (2) a flush INSIDE an
  active ``begin_nested()`` rolls back only to the savepoint — the
  outer transaction stays open and usable; (3) a Core ``execute()`` with
  no flush at all leaves the transaction open AND aborted (no
  SQLAlchemy-initiated rollback occurs). **This distinction matters for
  2b-core-A**, whose write methods will flush ORM objects and may use
  ``begin_nested()`` for bulk-atomicity (spec O-4,
  ``acl_denied_bulk_rollback``): the forward obligation is to verify
  ``record_denial``'s behaviour against 2b-core-A's ACTUAL abort shape
  (plain flush / savepoint-scoped flush / Core execute), never to
  assume one shape's proof transfers to another — see the build
  record's forward-obligation list for the shape-qualified statement of
  this obligation (not only here, so a future builder reading the build
  record's own "What was NOT done" list hits it directly).
* **B3/F4 correction** — CPython interns identifier-like string
  literals, so ``is`` on ``"acl_authz"`` cannot distinguish an imported
  name from a locally re-typed copy: restoring the EXACT round-1 defect
  (a local ``EVENT_CLASS_ACL_AUTHZ = "acl_authz"`` in this module) left
  ``_audit.EVENT_CLASS_ACL_AUTHZ is memory_scan.EVENT_CLASS_ACL_AUTHZ``
  ``True`` and the whole suite GREEN.
  ``test_audit_module_imports_the_canonical_constant_not_a_copy`` is
  REMOVED (not repaired — no runtime property distinguishes the two
  cases). The structural fix (``_audit.py`` importing the constant
  rather than defining it) stands; a DRIFTED VALUE is still caught by
  ``test_literal_value_is_exactly_acl_authz`` and
  ``TestEventClassValues`` — only the identity-based *test* was the
  problem.

**Per-guard neuter table (see the build record for the run log; PG-side
rows live in the sibling file's own table):**

* **N1** — ``TestRecordSuccess`` — audit row on a successful write, full
  payload + content_hash coverage asserted.
* **N2** — ``TestRecordDenial`` — audit row on a denied write, separately.
* **N3** (aiosqlite half — ORM-session independence only, see
  ``TestDenialRowSurvivesRollback``'s docstring; PG half in
  ``test_acl_ownership_rls.py`` proves genuine DB-level overlap) —
  ``TestDenialRowSurvivesRollback``.
* **N4** (the writer's half — no operation exists yet, see the module
  docstring) — ``TestFailClosed``.
* **N7** — ``TestTraceIdDerivation`` — the §4.3 NULL-trace-trap proof for
  ``record()``.
* **F1/N8** — ``TestForgedIdentityIsRefused`` — a forged ``UserContext``
  THROUGH the writer's own API is refused WHEN THE AMBIENT REQUEST
  CONTEXTVAR IS BOUND (the normal request path); ``TestUserIdAndGranted
  ByDerivation`` keeps the positive VALUE assertion and the
  raw-INSERT-bypass (a DIFFERENT question — the writer's own API was
  never exercised there).
* **F3** — ``TestDenialTraceIdDerivation`` — the §4.3 proof for
  ``record_denial()``.
* **F4** — ``TestEventClassPinning`` (the literal-value pin; the
  identity-based test is REMOVED per B3 above).
* **F5** — ``TestSessionIdDerivation``.
* **F6/F6b** — ``TestContentHashCoversANonNullTraceId`` — now covers
  BOTH ``record()`` and ``record_denial()``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from audittrace import integrity
from audittrace.db.models import ConsoleAclEntry, InteractionRecord
from audittrace.db.rls import set_current_user_id
from audittrace.dependencies import get_postgres_factory
from audittrace.routes import memory_scan
from audittrace.services.console_acl import _audit
from audittrace.services.console_acl._audit import (
    ACL_DENIAL_FAILURE_CLASSES,
    EVENT_CLASS_ACL_AUTHZ,
    FAILURE_CLASS_ACL_DENIED_POLICY,
    record,
    record_denial,
)
from audittrace.services.console_store import ConsoleStoreScopeError, bind_session_id


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
    content_hash coverage asserted (NULL-trace_id variant — the
    non-null-trace_id variant is F6/``TestContentHashCoversANonNullTraceId``)."""

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
    its OWN independent transaction, must NOT be taken down with it.

    Fix round 3 correction (round 2's replacement mechanism was ALSO
    wrong — round 1 said "the transaction is STILL OPEN", round 2 said
    "aiosqlite's DBAPI driver issues the ROLLBACK" and contrasted that
    with "Postgres does NOT auto-rollback"; both are false).
    **Fix round 4 correction (round 3's replacement was ALSO
    overgeneralised — an independent reviewer measured a shape round 3
    never tested).** The correct, THREE-SHAPE statement: (1) a flush
    OUTSIDE any active savepoint rolls back the WHOLE connection, on any
    dialect; (2) a flush INSIDE an active ``session.begin_nested()``
    sends only ``ROLLBACK TO SAVEPOINT`` — the OUTER transaction stays
    open and usable, and ``begin_nested()`` itself first autoflushes any
    already-pending objects OUTSIDE the savepoint, so where an ``add()``
    sits relative to the savepoint boundary changes the outcome; (3) a
    Core ``execute()`` with no flush at all leaves the transaction open
    AND aborted (no SQLAlchemy-initiated rollback). This test exercises
    shape (1) — an ORM ``flush()`` with no savepoint active. Instrumented
    below (not merely asserted) via a connection-level ``"rollback"``
    event listener: this test's failed ``flush()`` fires that event,
    proving the rollback happened, before ``record_denial`` ever runs.
    The REAL difference between this test and the Postgres N3 test is
    HOW EACH ONE ABORTS: this test uses shape (1) (so only the ORM
    ``Session`` object — ``db`` — is left "pending rollback"; the
    CONNECTION itself was already rolled back by SQLAlchemy), while
    ``tests/test_acl_ownership_rls.py::TestAuditWriterRealPostgres::
    test_n3_denial_row_survives_an_rls_aborted_transaction`` uses shape
    (3) (Core ``execute(text(...))``, no flush, transaction genuinely
    OPEN AND ABORTED at the database level). This test proves
    ORM-SESSION-level independence under shape (1) (``record_denial``
    never reuses ``db``'s own ``Session`` object) — the guard is still
    real and still fails when neutered (forcing ``record_denial`` to
    write via ``db`` itself makes both assertions below go RED — see the
    build evidence for the reviewer-measured and builder-reproduced
    transcripts; this file's own committed tests do not include that
    neuter, since it requires an edit to ``_audit.py`` this WU does not
    ship). It does **NOT** prove genuine DB-level transaction overlap
    under shape (3), and says nothing at all about shape (2). See
    ``tests/test_acl_ownership_rls.py``'s
    ``test_n3_variant_aborting_via_orm_flush_the_shape_2b_core_a_will_
    have`` for shape (1) measured directly on Postgres — that test
    states its own finding plainly rather than assuming one, and 2b-core-
    A must independently verify against WHICHEVER shape its own write
    methods actually use (see the build record's forward-obligation
    list, shape-qualified there too)."""

    async def test_denial_row_survives_while_the_orm_session_is_still_pending_rollback(
        self, client, user_context
    ) -> None:
        pg = get_postgres_factory()
        session_factory = pg.get_session_factory()
        sync_engine = pg.get_engine().sync_engine
        rollback_events: list[bool] = []

        def _on_rollback(_conn: object) -> None:
            rollback_events.append(True)

        event.listen(sync_engine, "rollback", _on_rollback)
        try:
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
                with pytest.raises(IntegrityError):
                    await db.flush()

                # INSTRUMENTED, not asserted (fix round 3 — a claim that
                # needs a script should be a test): SQLAlchemy fires a
                # connection-level "rollback" event when the failed
                # flush() above rolls back the connection. This is
                # SQLAlchemy's own behaviour, not something the aiosqlite
                # DBAPI driver does independently. This is shape (1) — a
                # flush with NO active savepoint — of the three shapes
                # the class docstring names (fix round 4: shape (2), a
                # flush INSIDE begin_nested(), rolls back only to the
                # savepoint and does NOT fire this event; shape (1) is
                # not "any dialect" in the sense of "any abort shape",
                # only "any dialect for THIS shape" — a matching probe on
                # the real Postgres harness shows the same event fires
                # there for the SAME shape-(1) flush-based abort — see
                # test_acl_ownership_rls.py::TestAuditWriterRealPostgres::
                # test_n3_variant_aborting_via_orm_flush_the_shape_2b_
                # core_a_will_have). By the time we reach here, the
                # CONNECTION has already been rolled back; only the ORM
                # `Session` object (`db`) is still "pending rollback"
                # from the ORM's own point of view (it will not let us
                # reuse it for further work until we call `db.rollback()`
                # below). `record_denial` opens its OWN, completely
                # independent Session/connection and must succeed
                # regardless of `db`'s pending-rollback state — proving
                # ORM-session-level independence (never reuses `db`
                # itself). This is a DIFFERENT abort shape from the
                # Postgres N3 test, which uses shape (3) — a Core
                # `execute` with no flush — and so leaves its transaction
                # genuinely OPEN AND ABORTED at the database level — see
                # the class docstring.
                assert rollback_events, (
                    "SQLAlchemy must have rolled back the connection "
                    "when the flush failed — this is what makes `db` "
                    "merely ORM-session-pending, not DB-transaction-open"
                )
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

                await db.rollback()  # clean up the ORM session's own pending state
        finally:
            event.remove(sync_engine, "rollback", _on_rollback)

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
            assert len(denial_rows) == 1, (
                "the denial row must survive the caller's still-pending "
                "ORM session state"
            )


class TestFailClosed:
    """N4 — the WRITER's half of fail-closed: break the audit write and
    the writer raises rather than swallowing. The CALLER's half (a future
    2b-core-A write method must not catch and discard this either) has no
    operation to test against yet — logged as a forward obligation.

    Each neuter here breaks something OTHER than the value later
    asserted (`_content_hash`, `get_postgres_factory`) and the assertion
    is that an exception PROPAGATES — a side effect the neuter did not
    itself set, not a self-fulfilling value check."""

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
    """N7 — trace_id derivation is choke-stamped, never test-supplied, for
    ``record()``. §4.3: the write runs inside a real span, the captured
    value is asserted 32-char hex BEFORE any match, and a NULL match is a
    FAIL. (``record_denial``'s own derivation is F3/
    ``TestDenialTraceIdDerivation`` below — the first cut only covered
    ``record()``.)"""

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


class TestDenialTraceIdDerivation:
    """F3 — the SAME §4.3 proof as N7, for ``record_denial()``. The first
    cut's N7 only ever exercised ``record()``'s ``trace_id`` derivation;
    a neuter of ``record_denial``'s call site (hard-code ``trace_id =
    None``) left every existing test GREEN, because none of them looked
    at a denial row's ``trace_id`` under an active span. Denial rows are
    the regulator-facing event (§3.3 requires the trace link on THEM
    specifically, not only on success rows)."""

    async def test_denial_trace_id_is_the_active_spans_id(
        self, client, user_context
    ) -> None:
        tracer = TracerProvider().get_tracer("acl-audit-writer-denial-tests")
        with tracer.start_as_current_span("acl-denial-write") as span:
            captured = format(span.get_span_context().trace_id, "032x")
            _assert_trace_id_is_valid_hex(captured)  # BEFORE any match
            row = await record_denial(
                user_context=user_context,
                op="grantPermission",
                principal_type="user",
                principal_id="p1",
                resource_type="agent",
                resource_id="agent-denial-trace",
                perm_bits=1,
                failure_class=FAILURE_CLASS_ACL_DENIED_POLICY,
                predicate_or_attempted_row={},
            )

        assert row.trace_id == captured

        resp = client.get(
            "/interactions",
            params={"event_class": EVENT_CLASS_ACL_AUTHZ, "limit": 1000},
        )
        by_trace = [r for r in resp.json()["interactions"] if r["trace_id"] == captured]
        assert len(by_trace) == 1, "a 200 without a trace_id match is a FAIL (§4.1)"

    async def test_denial_null_trace_is_never_a_valid_match(
        self, client, user_context
    ) -> None:
        with otel_trace.use_span(otel_trace.INVALID_SPAN, end_on_exit=False):
            captured = _audit.current_trace_id_hex()
            assert captured is None
            row = await record_denial(
                user_context=user_context,
                op="grantPermission",
                principal_type="user",
                principal_id="p1",
                resource_type="agent",
                resource_id="agent-denial-null-trace",
                perm_bits=1,
                failure_class=FAILURE_CLASS_ACL_DENIED_POLICY,
                predicate_or_attempted_row={},
            )
        assert row.trace_id is None, "no span active — trace_id must not be fabricated"
        with pytest.raises(AssertionError):
            _assert_trace_id_is_valid_hex(captured)


class TestEventClassPinning:
    """F4 — nothing pinned ``EVENT_CLASS_ACL_AUTHZ`` before this: a
    neuter of the STRING VALUE (e.g. ``"acl_authx"``) stayed GREEN across
    the whole suite. Pins the literal and its membership in the closed
    set, closing the drift the first cut's false "imported everywhere it
    is registered" docstring claim papered over — ACL WU-1's F1 in new
    clothes.

    Fix round 2 (B3) — a THIRD test used to assert
    ``_audit.EVENT_CLASS_ACL_AUTHZ is memory_scan.EVENT_CLASS_ACL_AUTHZ``
    as "proof" ``_audit.py`` imports rather than re-defines the constant.
    It is REMOVED: CPython interns identifier-like string literals, so a
    local ``EVENT_CLASS_ACL_AUTHZ = "acl_authz"`` re-typed directly in
    ``_audit.py`` (the EXACT round-1 defect) is ``is``-identical to the
    canonical constant anyway — the test cannot tell an import from a
    copy, ever, for this string. No runtime check can distinguish them;
    the structural fix (grep ``_audit.py`` for a bare
    ``EVENT_CLASS_ACL_AUTHZ = "acl_authz"`` assignment, or read the
    import statement) is a code-review property, not a test property.
    ``test_literal_value_is_exactly_acl_authz`` and
    ``TestEventClassValues`` (``tests/test_memory_routes.py``) are what
    actually catch a DRIFTED VALUE, which is the risk that matters."""

    def test_literal_value_is_exactly_acl_authz(self) -> None:
        assert EVENT_CLASS_ACL_AUTHZ == "acl_authz"

    def test_constant_is_a_member_of_the_closed_set(self) -> None:
        assert EVENT_CLASS_ACL_AUTHZ in memory_scan._EVENT_CLASS_VALUES


class TestSessionIdDerivation:
    """F5 — nothing exercised ``session_id`` before this: hard-coding
    ``session_id = None`` in either writer function stayed GREEN across
    the whole suite. Invariant 8 (D-R) ratifies ``session_id = NULL``
    TODAY on the premise that M5 will populate it with NO ACL-side
    change — true only if the writer reads
    ``console_store.current_session_id()`` live, not a hard-coded
    constant."""

    async def test_record_stamps_session_id_from_the_accessor(
        self, client, user_context
    ) -> None:
        bind_session_id("acl-2b-core-b-run-1")
        try:
            session_factory = await _open_db()
            async with session_factory() as db:
                row = await record(
                    db,
                    user_context=user_context,
                    op="grantPermission",
                    principal_type="user",
                    principal_id="p1",
                    resource_type="agent",
                    resource_id="agent-session-id",
                    perm_bits=1,
                    acl_entry_ids=["acl-1"],
                )
                await db.commit()
        finally:
            bind_session_id(None)
        assert row.session_id == "acl-2b-core-b-run-1"

    async def test_record_denial_stamps_session_id_from_the_accessor(
        self, client, user_context
    ) -> None:
        bind_session_id("acl-2b-core-b-run-2")
        try:
            row = await record_denial(
                user_context=user_context,
                op="grantPermission",
                principal_type="user",
                principal_id="p1",
                resource_type="agent",
                resource_id="agent-session-id-denial",
                perm_bits=1,
                failure_class=FAILURE_CLASS_ACL_DENIED_POLICY,
                predicate_or_attempted_row={},
            )
        finally:
            bind_session_id(None)
        assert row.session_id == "acl-2b-core-b-run-2"

    async def test_unbound_session_id_is_null_not_fabricated(
        self, client, user_context
    ) -> None:
        bind_session_id(None)
        session_factory = await _open_db()
        async with session_factory() as db:
            row = await record(
                db,
                user_context=user_context,
                op="grantPermission",
                principal_type="user",
                principal_id="p1",
                resource_type="agent",
                resource_id="agent-session-id-null",
                perm_bits=1,
                acl_entry_ids=["acl-1"],
            )
            await db.commit()
        assert row.session_id is None


class TestContentHashCoversANonNullTraceId:
    """F6 — every existing ``content_hash`` check ran with a NULL
    ``trace_id`` (no active span). Since ``trace_id`` is one of
    ``integrity._CONTENT_FIELDS``, a bug that excluded it from the hashed
    payload while leaving it in the persisted row would be invisible to a
    NULL-only check (``None`` hashes the same either way in practice only
    by coincidence of the field being absent from BOTH branches — this
    test removes that coincidence by exercising a REAL, non-null value).

    Fix round 2 (F6b) — round 1 covered ``record()`` only and the
    evidence file claimed ``record_denial()`` was "indirectly covered by
    the SAME N7/F3 neuters" — FALSE: those neuters change the ``trace_id``
    VALUE; they assert nothing about what the hash COVERS. Forcing
    ``record_denial``'s ``trace_id`` to ``None`` while the persisted row
    keeps a real one left the suite GREEN (55 passed) because no test
    computed ``verify_content_hash`` on a denial row with a non-null
    trace. ``test_denial_content_hash_verifies_with_a_real_non_null_
    trace_id`` below closes that gap directly."""

    async def test_content_hash_verifies_with_a_real_non_null_trace_id(
        self, client, user_context
    ) -> None:
        tracer = TracerProvider().get_tracer("acl-audit-writer-hash-tests")
        session_factory = await _open_db()
        with tracer.start_as_current_span("acl-hash-write") as span:
            captured = format(span.get_span_context().trace_id, "032x")
            async with session_factory() as db:
                row = await record(
                    db,
                    user_context=user_context,
                    op="grantPermission",
                    principal_type="user",
                    principal_id="p1",
                    resource_type="agent",
                    resource_id="agent-hash-trace",
                    perm_bits=1,
                    acl_entry_ids=["acl-1"],
                )
                await db.commit()

        assert row.trace_id == captured
        assert row.trace_id is not None
        assert integrity.verify_content_hash(row) is True

    async def test_denial_content_hash_verifies_with_a_real_non_null_trace_id(
        self, client, user_context
    ) -> None:
        """F6b — the ``record_denial`` half of F6, missing in round 1."""
        tracer = TracerProvider().get_tracer("acl-audit-writer-denial-hash-tests")
        with tracer.start_as_current_span("acl-denial-hash-write") as span:
            captured = format(span.get_span_context().trace_id, "032x")
            row = await record_denial(
                user_context=user_context,
                op="grantPermission",
                principal_type="user",
                principal_id="p1",
                resource_type="agent",
                resource_id="agent-denial-hash-trace",
                perm_bits=1,
                failure_class=FAILURE_CLASS_ACL_DENIED_POLICY,
                predicate_or_attempted_row={},
            )

        assert row.trace_id == captured
        assert row.trace_id is not None
        assert integrity.verify_content_hash(row) is True


class TestForgedIdentityIsRefused:
    """F1 (SECURITY, fix round 1) — the ACTUAL guard: a forged
    ``UserContext`` passed THROUGH the writer's own public API must be
    refused when the ambient RLS identity is bound and disagrees.

    Distinct from ``TestUserIdAndGrantedByDerivation``'s raw-INSERT-bypass
    test, which never calls ``record``/``record_denial`` at all and so
    cannot exercise this guard — that test answers "what happens if you
    skip the writer entirely", not "does the writer's own parameter
    validation work"."""

    async def test_record_refuses_a_forged_user_context(
        self, client, user_context
    ) -> None:
        real_sub = user_context.user_id
        forged = replace(user_context, user_id="attacker-forged-subject")
        set_current_user_id(real_sub)
        try:
            session_factory = await _open_db()
            with pytest.raises(ConsoleStoreScopeError, match="disagrees with the RLS"):
                async with session_factory() as db:
                    await record(
                        db,
                        user_context=forged,
                        op="grantPermission",
                        principal_type="user",
                        principal_id="p1",
                        resource_type="agent",
                        resource_id="agent-forged",
                        perm_bits=1,
                        acl_entry_ids=["acl-1"],
                    )
        finally:
            set_current_user_id(None)

        # And no row was written under the attacker's forged subject.
        session_factory = await _open_db()
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
            assert rows == [], "the forged write must never land"

    async def test_record_denial_refuses_a_forged_user_context(
        self, client, user_context
    ) -> None:
        real_sub = user_context.user_id
        forged = replace(user_context, user_id="attacker-forged-subject-2")
        set_current_user_id(real_sub)
        try:
            with pytest.raises(ConsoleStoreScopeError, match="disagrees with the RLS"):
                await record_denial(
                    user_context=forged,
                    op="grantPermission",
                    principal_type="user",
                    principal_id="p1",
                    resource_type="agent",
                    resource_id="agent-forged-denial",
                    perm_bits=1,
                    failure_class=FAILURE_CLASS_ACL_DENIED_POLICY,
                    predicate_or_attempted_row={},
                )
        finally:
            set_current_user_id(None)

        session_factory = await _open_db()
        async with session_factory() as fresh:
            rows = (
                (
                    await fresh.execute(
                        select(InteractionRecord).where(
                            InteractionRecord.user_id == "attacker-forged-subject-2"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert rows == [], "the forged denial write must never land"

    async def test_matching_user_context_is_not_refused(
        self, client, user_context
    ) -> None:
        """Sanity: the guard is a MISMATCH check, not a blanket refusal —
        a ``UserContext`` that agrees with the ambient identity still
        works."""
        set_current_user_id(user_context.user_id)
        try:
            session_factory = await _open_db()
            async with session_factory() as db:
                row = await record(
                    db,
                    user_context=user_context,
                    op="grantPermission",
                    principal_type="user",
                    principal_id="p1",
                    resource_type="agent",
                    resource_id="agent-matching",
                    perm_bits=1,
                    acl_entry_ids=["acl-1"],
                )
                await db.commit()
        finally:
            set_current_user_id(None)
        assert row.user_id == user_context.user_id


class TestUserIdAndGrantedByDerivation:
    """N8 — VALUE assertion that ``user_id``/``granted_by`` derive from
    the request-resolved ``UserContext``. The forged-identity GUARD
    itself is F1/``TestForgedIdentityIsRefused`` above; this class keeps
    the positive value check and the raw-INSERT-bypass (a DIFFERENT
    question from the guard — no writer call is made there at all)."""

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

    async def test_empty_user_id_is_refused(self, client, user_context) -> None:
        empty = replace(user_context, user_id="")
        session_factory = await _open_db()
        with pytest.raises(ConsoleStoreScopeError, match="is empty"):
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
        with pytest.raises(ConsoleStoreScopeError, match="is empty"):
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

    async def test_a_raw_insert_bypassing_the_writer_succeeds_silently_on_aiosqlite(
        self, client, user_context
    ) -> None:
        """A DIFFERENT question from F1's guard: this test never calls
        ``record``/``record_denial`` at all — it INSERTs directly against
        the ORM model, bypassing the writer's API entirely. On aiosqlite
        (no RLS), that raw INSERT succeeds regardless of any ambient
        identity. The Postgres counterpart in ``test_acl_ownership_rls.py``
        proves the SAME raw INSERT is refused there by migration 005's
        RLS ``WITH CHECK`` — two independent layers (the writer's own
        F1 guard, and RLS underneath it) each close a DIFFERENT bypass."""
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
                    user_id="attacker-forged-subject-raw",
                    status="success",
                    event_class=EVENT_CLASS_ACL_AUTHZ,
                )
            )
            await db.commit()  # succeeds — no RLS on aiosqlite, no writer involved

        async with session_factory() as fresh:
            rows = (
                (
                    await fresh.execute(
                        select(InteractionRecord).where(
                            InteractionRecord.user_id == "attacker-forged-subject-raw"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1, (
                "aiosqlite enforces no RLS and this test never calls the "
                "writer — the row lands because nothing here was asked to "
                "stop it; F1's guard only fires when the writer's own API "
                "is actually used (see TestForgedIdentityIsRefused)"
            )
