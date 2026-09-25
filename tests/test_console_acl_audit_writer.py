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
unrelated reason (no overlap ever existed). Fixed by calling
``record_denial`` WHILE the failed flush's transaction is still open.
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

**Per-guard neuter table (see the build record for the run log; PG-side
rows live in the sibling file's own table):**

* **N1** — ``TestRecordSuccess`` — audit row on a successful write, full
  payload + content_hash coverage asserted.
* **N2** — ``TestRecordDenial`` — audit row on a denied write, separately.
* **N3** (aiosqlite half; PG half in ``test_acl_ownership_rls.py``) —
  ``TestDenialRowSurvivesRollback``, denial written WHILE the aborted
  transaction is still open.
* **N4** (the writer's half — no operation exists yet, see the module
  docstring) — ``TestFailClosed``.
* **N7** — ``TestTraceIdDerivation`` — the §4.3 NULL-trace-trap proof for
  ``record()``.
* **F1/N8** — ``TestForgedIdentityIsRefused`` — a forged ``UserContext``
  THROUGH the writer's own API is refused when the ambient identity is
  bound; ``TestUserIdAndGrantedByDerivation`` keeps the positive VALUE
  assertion and the raw-INSERT-bypass (a DIFFERENT question — the
  writer's own API was never exercised there).
* **F3** — ``TestDenialTraceIdDerivation`` — the §4.3 proof for
  ``record_denial()``.
* **F4** — ``TestEventClassPinning``.
* **F5** — ``TestSessionIdDerivation``.
* **F6** — ``TestContentHashCoversANonNullTraceId``.
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

    Fix round 1 (F2) — the ordering IS the proof: ``record_denial`` runs
    WHILE the aborted transaction (still holding the failed INSERT) has
    NOT yet been rolled back. Calling it only after the caller's
    transaction has already fully ended (the first cut's mistake) proves
    nothing about overlap — the denial row would "survive" for the
    unrelated reason that nothing was ever concurrent."""

    async def test_denial_row_survives_the_still_open_aborted_transaction(
        self, client, user_context
    ) -> None:
        session_factory = await _open_db()
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

            # `db`'s transaction is STILL OPEN here — the failed flush has
            # not been rolled back yet. `record_denial` opens its OWN,
            # completely independent session/transaction and must succeed
            # regardless of `db`'s aborted state. This overlap is the
            # actual guard §5.2 requires; the first cut's tests called
            # `record_denial` only AFTER `db`'s `async with` block had
            # already exited and rolled back, so no overlap ever existed.
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

            await db.rollback()  # now clean up the still-pending failure

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
                "the denial row must survive the STILL-OPEN aborted transaction"
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
    the whole suite. Pins the literal, its membership in the closed set,
    and the object-identity between ``_audit.py``'s import and
    ``memory_scan.py``'s canonical constant (closing the drift the first
    cut's false "imported everywhere it is registered" docstring claim
    papered over — ACL WU-1's F1 in new clothes)."""

    def test_literal_value_is_exactly_acl_authz(self) -> None:
        assert EVENT_CLASS_ACL_AUTHZ == "acl_authz"

    def test_constant_is_a_member_of_the_closed_set(self) -> None:
        assert EVENT_CLASS_ACL_AUTHZ in memory_scan._EVENT_CLASS_VALUES

    def test_audit_module_imports_the_canonical_constant_not_a_copy(self) -> None:
        assert _audit.EVENT_CLASS_ACL_AUTHZ is memory_scan.EVENT_CLASS_ACL_AUTHZ


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
    test removes that coincidence by exercising a REAL, non-null value)."""

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
