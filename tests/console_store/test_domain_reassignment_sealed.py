"""Guards B and B' — the ``ConsoleStoreBase`` INSTANCE attribute write-once
check (``_domain`` and ``_sessions``). Split out of
``test_domain_descriptor_sealed.py`` (fix round 4, 2026-09-17, F7 advisory
item 5 — that file had grown to 533 LOC, past the PYTHON-ENGINEERING §11
500-LOC trigger) into its own guard-scoped file; no test body changed,
only the file it lives in. See ``test_domain_descriptor_sealed.py`` for
Guards A/D (the descriptor itself) and
``test_domain_reserved_column_guard_sealed.py`` for Guard C (the
reserved-column refusal at point of use).

* **Guard B** — ``ConsoleStoreBase.__setattr__``'s write-once check: the
  ``_domain`` POINTER cannot be reassigned to a descriptor that never
  passed ``validate_domain()`` (WU-A ADDENDUM A finding, generalised the
  original fix round — see ``_base.py``). Proven below
  (``TestDomainReassignmentSealed``, the A1 fix).
* **Guard B'** — the SAME generalised write-once check, for
  :class:`~audittrace.services.console_store._postgres.PostgresConsoleStore`'s
  OTHER instance attribute, ``_sessions`` (``_model`` was removed
  entirely, see ``test_no_raw_resource.py``). Proven below
  (``TestSessionsWriteOnceSealed``) — this closes the PARTIAL disclosed in
  the fix-round-1 evidence file ("NO dedicated regression test this
  session").
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider

from audittrace.identity import UserContext
from audittrace.services.console_store import PostgresConsoleStore
from audittrace.services.console_store import _base as _base_module
from audittrace.services.console_store._errors import ConsoleStoreSealedError
from tests.console_store.support import KEY_A, _raw
from tests.console_store_fixture_domain import SqliteHarness, WidgetDomain


def _span_trace_id(span: Any) -> str:
    return format(span.get_span_context().trace_id, "032x")


# ── Guard B: the ``_domain`` POINTER cannot be swapped post-construction ───


class ReservedAsValueColumnDomain(WidgetDomain):
    """A descriptor that declares a NULLABLE reserved column (``trace_id``)
    as writable — exactly what ``validate_domain()`` refuses at
    construction. Only reachable by swapping ``_domain`` in AFTER
    construction (Guard B's job), since a store built with this domain
    directly is refused at construction.

    A1 (fix round 1): the original version of this fixture declared
    ``user_sub`` (NOT NULL) instead. Under Guard B neutered, the swap
    succeeded and the subsequent insert then died on a NOT NULL constraint
    BEFORE the asserted side effect (a clobbered stamp) ever executed — the
    test REDded on "did not raise", never on the harm it claimed to guard
    against (the review's own A1 finding). ``trace_id`` is nullable, so the
    harm this fixture models — a silently nulled traceability stamp — is
    now something a test could actually observe in the DB, had Guard C not
    also been reachable at that same point (see
    ``test_domain_reserved_column_guard_sealed.py``).
    """

    name = "hostile_domain_swap"
    value_columns = ("payload", "priority", "trace_id")


class TestDomainReassignmentSealed:
    """Guard B — generalised the original fix round from a ``_domain``-only
    check to every ``ConsoleStoreBase`` instance attribute (``_base.py``)."""

    async def test_reassigning_domain_after_construction_is_refused(
        self, harness: SqliteHarness
    ) -> None:
        domain = WidgetDomain()
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            domain, harness.factory
        )
        with pytest.raises(ConsoleStoreSealedError, match="cannot be reassigned"):
            store._domain = WidgetDomain()  # type: ignore[misc]
        assert store.domain is domain, "the reassignment attempt still took effect"

    async def test_domain_swap_cannot_smuggle_a_reserved_column_past_the_one_time_check(
        self, harness: SqliteHarness, alice: UserContext, bob: UserContext
    ) -> None:
        """A1 (fixed): the swap itself is refused (Guard B), proven with a
        REAL span so the witness is the DB row's ``trace_id``, not merely
        "an exception was raised" — the exact upgrade A1 demanded."""
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        tracer = TracerProvider().get_tracer("f1-domain-swap")
        with tracer.start_as_current_span("alice-write") as span:
            alice_row = await store.upsert(alice, KEY_A, {"payload": "alice-secret"})
            alice_trace = _span_trace_id(span)
        with pytest.raises(ConsoleStoreSealedError):
            store._domain = ReservedAsValueColumnDomain()  # type: ignore[misc]
        other = {"kind": "tool", "name": "other"}
        with tracer.start_as_current_span("bob-write") as span:
            bob_row = await store.upsert(bob, other, {"priority": 1})
            bob_trace = _span_trace_id(span)
        raw = {r["id"]: r for r in await _raw(store, harness)}
        assert raw[alice_row["id"]]["trace_id"] == alice_trace
        assert bob_row["user_sub"] == "bob-sub"
        assert raw[bob_row["id"]]["trace_id"] == bob_trace, (
            "the blocked swap still let a row get written with a nulled "
            "trace_id — traceability stamp lost (M5 / EU AI Act Art 12)"
        )


# ── Guard B': the SAME write-once check, for PostgresConsoleStore's OTHER ──
# ── instance attribute, ``_sessions`` (closes a fix-round-1 PARTIAL) ────────


class TestSessionsWriteOnceSealed:
    """The fix-round-1 evidence file disclosed this as a GAP: the
    generalisation from a ``_domain``-only write-once check to "every
    instance attribute" was demonstrated manually at the REPL for
    ``_sessions``/``_model`` but had NO dedicated, automated regression
    test. ``_model`` was removed entirely (``test_no_raw_resource.py``),
    so ``_sessions`` is the only remaining Postgres-specific instance
    attribute needing its own proof."""

    def test_reassigning_sessions_after_construction_is_refused(
        self, harness: SqliteHarness
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        original = store._sessions
        with pytest.raises(ConsoleStoreSealedError, match="cannot be reassigned"):
            store._sessions = object()  # type: ignore[assignment]
        assert store._sessions is original, "the blocked swap still took effect"

    def test_neutering_the_generalised_check_lets_sessions_be_swapped(
        self, harness: SqliteHarness
    ) -> None:
        """Non-vacuity: reproduces the ORIGINAL (pre-fix-round-1) shape of
        ``ConsoleStoreBase.__setattr__`` — ONLY ``_domain`` was
        write-once-checked, exactly as WU-A shipped it — and shows the
        SAME class of mutation the test above now refuses
        (``store._sessions = evil``) instead succeeding under that
        narrower, pre-fix check. A concrete side effect (``is evil``), not
        merely "no exception was raised".

        SPEC ADDENDUM C R2 (fix round 2) sealed ``__setattr__`` itself as a
        ``SEALED_STORE_MEMBERS`` member, so an ordinary
        ``monkeypatch.setattr(ConsoleStoreBase, "__setattr__", ...)`` (which
        dispatches through ``_SealedMeta.__setattr__``) is now ITSELF
        refused — proof the class-level seal covers its own hook. Reaching
        the neuter this test needs therefore requires the same disclosed
        ``type.__setattr__`` bypass every other class-level neuter in this
        suite uses, restored in ``finally`` regardless of outcome."""

        def _pre_fix_setattr(self: Any, name: str, value: Any) -> None:
            if name in _base_module.SEALED_STORE_MEMBERS:
                raise ConsoleStoreSealedError(
                    f"{type(self).__qualname__}.{name} is sealed"
                )
            if name == "_domain" and "_domain" in self.__dict__:
                raise ConsoleStoreSealedError(
                    f"{type(self).__qualname__}.{name} is set once at "
                    "construction and cannot be reassigned"
                )
            object.__setattr__(self, name, value)

        original = _base_module.ConsoleStoreBase.__dict__["__setattr__"]
        type.__setattr__(_base_module.ConsoleStoreBase, "__setattr__", _pre_fix_setattr)
        try:
            store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
                WidgetDomain(), harness.factory
            )
            evil = object()
            store._sessions = evil  # type: ignore[assignment]
            assert store._sessions is evil, (
                "the pre-fix (domain-only) write-once check should have let "
                "the _sessions swap through"
            )
        finally:
            type.__setattr__(_base_module.ConsoleStoreBase, "__setattr__", original)
