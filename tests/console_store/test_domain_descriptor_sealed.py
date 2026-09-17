"""Domain-DESCRIPTOR sealing probes for ``services/console_store`` — fix
round 1 (2026-09-15), answering
``review-verdict-consolestorebase-wu-a-20260915.outcome-reject`` F1. Split
from ``test_console_store_hostile.py`` (A3 — that file exceeded the
PYTHON-ENGINEERING §11 500-LOC trigger).

**The defect (see ``_domain.py`` module docstring for the full account):**
``ConsoleStoreBase.domain`` hands out the validated :class:`ConsoleDomain`
instance BY REFERENCE. Before this round, one line of ordinary Python —
``store.domain.value_columns = (*store.domain.value_columns, "trace_id")``
— reassigned the descriptor's own attribute in place (no ``_domain``
pointer swap, no dunder, no ``__dict__`` write) and the next insert
silently nulled the base-stamped ``trace_id`` (M5 / EU AI Act Art 12).

**Five independent guards close it (four from the fix round, plus one
found while enumerating THIS round's own new seal — the "attack your own
fix" pass the spec demands), each proven separately** (Addendum B Req 1 —
one neuter per run, never a batch; a batch neuter proves only that *at
least one* guard is load-bearing, F-C1's mistake):

* **Guard A** — ``ConsoleDomain.__setattr__``/``__delattr__``: the
  descriptor INSTANCE itself refuses every attribute set/delete. This is
  what closes the ORIGINAL exploit line, completely — proven below
  (``TestDomainDescriptorSealed``).
* **Guard B** — ``ConsoleStoreBase.__setattr__``'s write-once check: the
  ``_domain`` POINTER cannot be reassigned to a descriptor that never
  passed ``validate_domain()`` (WU-A ADDENDUM A finding, generalised this
  round — see ``_base.py``). Proven below (``TestDomainReassignmentSealed``,
  the A1 fix).
* **Guard B'** — the SAME generalised write-once check, for
  :class:`~audittrace.services.console_store._postgres.PostgresConsoleStore`'s
  OTHER instance attribute, ``_sessions`` (``_model`` was removed
  entirely, see ``test_no_raw_resource.py``). Proven below
  (``TestSessionsWriteOnceSealed``) — this closes the PARTIAL disclosed in
  the fix-round-1 evidence file ("NO dedicated regression test this
  session").
* **Guard C** — ``ConsoleStoreBase._refuse_reserved_value_columns`` (F1
  item 2, defence in depth): refuses, LOUDLY, at the point every write path
  TRUSTS ``value_columns``, whatever route got a bad descriptor there. With
  Guard A closing the runtime-mutation path entirely, the only way to reach
  Guard C's protected code in THIS codebase is a construction path that
  skips ``validate_domain()`` — simulated below via monkeypatch, since no
  such path exists today (proven below, ``TestReservedColumnRefusedAtPointOfUse``).
* **Guard D** — ``_DomainMeta.__setattr__``/``__delattr__`` (the SECOND
  HOP, found this round: Guard A is an INSTANCE method and does not fire
  for ``WidgetDomain.value_columns = (...)``, a CLASS-level ClassVar
  reassignment — see the ``_domain.py`` module docstring for the full
  account of why this is a distinct mutation surface from Guard A's, not
  a duplicate of it). Proven below (``TestDomainClassLevelSealed``).
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider

from audittrace.identity import UserContext
from audittrace.services.console_store import (
    ConsoleDomain,
    ConsoleStoreForbiddenFieldError,
    ConsoleStoreSealedError,
    PostgresConsoleStore,
)
from audittrace.services.console_store import _base as _base_module
from tests.console_store.support import KEY_A, _raw
from tests.console_store_fixture_domain import SqliteHarness, WidgetDomain


def _span_trace_id(span: Any) -> str:
    return format(span.get_span_context().trace_id, "032x")


# ── Guard A: the descriptor itself refuses every attribute set/delete ───────


class TestDomainDescriptorSealed:
    async def test_the_original_exploit_line_is_refused_verbatim(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        """THE F1 reproduction, verbatim from the spec: this exact line
        used to reach ``_base.py``'s insert loop with a descriptor that
        never passed ``validate_domain()``. It no longer does."""
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        original = store.domain.value_columns
        with pytest.raises(ConsoleStoreSealedError):
            store.domain.value_columns = (*store.domain.value_columns, "trace_id")
        assert store.domain.value_columns == original, (
            "the blocked mutation attempt still took effect"
        )
        # Business continues normally afterwards: a real write still
        # stamps trace_id correctly (raw DB read as witness, not merely
        # "an exception was raised" — A1's own lesson applied here too).
        tracer = TracerProvider().get_tracer("f1-descriptor-mutation")
        with tracer.start_as_current_span("alice-write") as span:
            row = await store.upsert(alice, KEY_A, {"payload": "alice-secret"})
            expected_trace = _span_trace_id(span)
        raw = {r["id"]: r for r in await _raw(store, harness)}
        assert raw[row["id"]]["trace_id"] == expected_trace

    def test_deleting_a_descriptor_attribute_is_refused(
        self, harness: SqliteHarness
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        with pytest.raises(ConsoleStoreSealedError):
            del store.domain.value_columns
        assert store.domain.value_columns == ("payload", "priority")

    def test_neutering_the_seal_lets_the_mutation_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-vacuity for Guard A: with ``ConsoleDomain.__setattr__``
        neutered (mirroring exactly how F1 was originally found — no other
        guard stands in front of a bare descriptor that was never wired
        into a store), the SAME mutation the test above refuses instead
        succeeds and changes LIVE state — a concrete, checkable side
        effect (the tuple itself changes), not merely "no exception was
        raised" (A1's own complaint about a weaker proof shape)."""
        monkeypatch.setattr(ConsoleDomain, "__setattr__", object.__setattr__)
        domain = WidgetDomain()
        domain.value_columns = (*domain.value_columns, "trace_id")
        assert domain.value_columns == ("payload", "priority", "trace_id"), (
            "neutering the seal should have let the mutation through"
        )


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
    also been reachable at that same point (see the class docstring above).
    """

    name = "hostile_domain_swap"
    value_columns = ("payload", "priority", "trace_id")


class TestDomainReassignmentSealed:
    """Guard B — generalised this round from a ``_domain``-only check to
    every ``ConsoleStoreBase`` instance attribute (``_base.py``)."""

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
        self, harness: SqliteHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-vacuity: reproduces the ORIGINAL (pre-fix-round-1) shape of
        ``ConsoleStoreBase.__setattr__`` — ONLY ``_domain`` was
        write-once-checked, exactly as WU-A shipped it — and shows the
        SAME class of mutation the test above now refuses
        (``store._sessions = evil``) instead succeeding under that
        narrower, pre-fix check. A concrete side effect (``is evil``), not
        merely "no exception was raised"."""

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

        monkeypatch.setattr(
            _base_module.ConsoleStoreBase, "__setattr__", _pre_fix_setattr
        )
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        evil = object()
        store._sessions = evil  # type: ignore[assignment]
        assert store._sessions is evil, (
            "the pre-fix (domain-only) write-once check should have let "
            "the _sessions swap through"
        )


# ── Guard D: the domain CLASS itself cannot have a descriptor attribute ────
# ── reassigned after definition — the SECOND HOP of F1, found this round ───


class _ClassLevelSealDomain(WidgetDomain):
    """A throwaway domain subclass — tests mutate ITS class attribute, not
    the shared ``WidgetDomain``'s, so a neuter-and-restore cycle here can
    never leak state into any other test in this file."""

    name = "class_level_seal_probe"


class TestDomainClassLevelSealed:
    """Guard D. ``ConsoleDomain.__setattr__`` (Guard A) is an INSTANCE
    method: Python calls it for ``domain.value_columns = ...`` but NOT for
    ``WidgetDomain.value_columns = ...`` (an attribute set ON THE CLASS
    OBJECT, dispatched to the metaclass). Before this round ``ConsoleDomain``
    used plain ``ABCMeta``, so that second, class-level hop was wide open —
    ordinary Python, no dunder, exactly as "hurry-mode" as the original F1
    line, with WORSE blast radius (it mutates every store built with that
    domain class, not just one held reference). See the ``_domain.py``
    module docstring for the full account, including why the fix is a
    fixed, NAMED set of blocked attributes rather than an unconditional
    block (the latter breaks ABCMeta/typing class-creation machinery,
    verified false directly before this shape was chosen)."""

    def test_class_level_reassignment_is_refused(self) -> None:
        original = _ClassLevelSealDomain.value_columns
        with pytest.raises(ConsoleStoreSealedError):
            _ClassLevelSealDomain.value_columns = (*original, "trace_id")
        assert _ClassLevelSealDomain.value_columns == original, (
            "the blocked class-level mutation attempt still took effect"
        )

    def test_class_level_deletion_is_refused(self) -> None:
        with pytest.raises(ConsoleStoreSealedError):
            del _ClassLevelSealDomain.value_columns
        assert _ClassLevelSealDomain.value_columns == ("payload", "priority")

    def test_non_descriptor_class_attributes_are_still_settable_and_deletable(
        self,
    ) -> None:
        """The block is by EXPLICIT NAME (see the module docstring), not
        "every class attribute" — an ordinary, non-descriptor class
        attribute is unaffected, the same distinction
        ``test_non_sealed_class_attributes_are_still_settable_and_deletable``
        proves for store classes."""
        _ClassLevelSealDomain.scratch_marker = 1  # type: ignore[attr-defined]
        assert _ClassLevelSealDomain.scratch_marker == 1  # type: ignore[attr-defined]
        del _ClassLevelSealDomain.scratch_marker  # type: ignore[attr-defined]
        assert not hasattr(_ClassLevelSealDomain, "scratch_marker")

    async def test_class_level_reassignment_would_have_nulled_trace_id(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        """The refused class-level mutation still leaves an ordinary write
        stamping ``trace_id`` correctly — a raw DB read as witness, not
        merely "an exception was raised" (A1's own upgrade, applied here
        too)."""
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            _ClassLevelSealDomain(), harness.factory
        )
        with pytest.raises(ConsoleStoreSealedError):
            _ClassLevelSealDomain.value_columns = (
                *_ClassLevelSealDomain.value_columns,
                "trace_id",
            )
        tracer = TracerProvider().get_tracer("f1-class-level-mutation")
        with tracer.start_as_current_span("alice-write") as span:
            row = await store.upsert(alice, KEY_A, {"payload": "alice-secret"})
            expected_trace = _span_trace_id(span)
        raw = {r["id"]: r for r in await _raw(store, harness)}
        assert raw[row["id"]]["trace_id"] == expected_trace

    def test_neutering_the_class_level_seal_lets_the_mutation_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-vacuity: with ``_DomainMeta.__setattr__`` neutered to plain
        ``type.__setattr__`` (the ORIGINAL, pre-Guard-D behaviour), the SAME
        mutation the tests above refuse instead succeeds and changes state
        visible to every future instance of the class — a concrete,
        checkable side effect, restored in ``finally`` regardless of
        outcome so this test cannot leak state into any other."""
        from audittrace.services.console_store import _domain as _domain_module

        monkeypatch.setattr(_domain_module._DomainMeta, "__setattr__", type.__setattr__)
        original = _ClassLevelSealDomain.value_columns
        try:
            _ClassLevelSealDomain.value_columns = (*original, "trace_id")
            assert _ClassLevelSealDomain.value_columns == (
                "payload",
                "priority",
                "trace_id",
            ), "neutering the seal should have let the mutation through"
        finally:
            type.__setattr__(_ClassLevelSealDomain, "value_columns", original)


# ── Guard C: refuse a reserved-column collision AT THE POINT OF USE ────────


class _ReservedValueColumnDomain(WidgetDomain):
    """Declares ``trace_id`` (nullable, reserved) as a value column — the
    exact shape ``validate_domain()`` refuses at construction. Used ONLY
    with ``validate_domain`` monkeypatched to a no-op, simulating a FUTURE
    construction path that skips it (Guard A closes the only OTHER route
    to this shape — runtime mutation — completely; see
    ``TestDomainDescriptorSealed`` above)."""

    name = "hostile_reserved_value_column"
    value_columns = ("payload", "priority", "trace_id")


class TestReservedColumnRefusedAtPointOfUse:
    """Guard C — defence in depth. Not reachable today through any real
    code path (Guard A + ``validate_domain()`` between them close every
    known route), so both tests simulate the one FUTURE path the spec
    calls out: "a domain that declares the collision from the start ...
    via a future construction path that skips ``validate_domain()``"."""

    async def test_refuses_a_domain_that_bypassed_construction_validation(
        self, harness: SqliteHarness, bob: UserContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_base_module, "validate_domain", lambda domain: None)
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            _ReservedValueColumnDomain(), harness.factory
        )
        with pytest.raises(ConsoleStoreForbiddenFieldError, match="reserved"):
            await store.upsert(bob, KEY_A, {"priority": 1})
        assert await _raw(store, harness) == [], (
            "Guard C refused the write but a row was written anyway"
        )

    async def test_reproduction_without_guard_c_the_stamp_is_nulled(
        self,
        harness: SqliteHarness,
        alice: UserContext,
        bob: UserContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """THE combined reproduction: with construction validation bypassed
        (as above) AND Guard C ALSO neutered, the write that Guard C
        refused above now SUCCEEDS and silently nulls ``trace_id`` — a raw
        DB read is the witness, alongside an HONEST row (a normal store,
        neither guard touched) that carries the real span id. This is not
        a per-guard non-vacuity proof on its own (two things are neutered
        at once, Addendum B Req 1) — it is the end-to-end reproduction of
        the ORIGINAL headline defect, kept alongside the per-guard proofs
        above rather than instead of them."""
        monkeypatch.setattr(_base_module, "validate_domain", lambda domain: None)
        # ``_refuse_reserved_value_columns`` is a SEALED_STORE_MEMBERS entry
        # — ``ConsoleStoreBase``'s own metaclass refuses an ordinary
        # ``setattr`` on it (proven in ``test_console_store_sealed_
        # classes.py``), so ``monkeypatch.setattr`` cannot reach it either.
        # Bypass the SAME way the sealed-classes tests do to restore a
        # sealed member: ``type.__setattr__`` directly.
        original = _base_module.ConsoleStoreBase.__dict__[
            "_refuse_reserved_value_columns"
        ]
        type.__setattr__(
            _base_module.ConsoleStoreBase,
            "_refuse_reserved_value_columns",
            lambda self: None,
        )
        try:
            honest_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
                WidgetDomain(), harness.factory
            )
            hostile_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
                _ReservedValueColumnDomain(), harness.factory
            )
            tracer = TracerProvider().get_tracer("f1-reproduction")
            with tracer.start_as_current_span("honest-write") as span:
                honest_row = await honest_store.upsert(
                    alice, KEY_A, {"payload": "real"}
                )
                honest_trace = _span_trace_id(span)
            with tracer.start_as_current_span("hostile-write"):
                hostile_row = await hostile_store.upsert(bob, KEY_A, {"priority": 1})
            honest_raw = {r["id"]: r for r in await _raw(honest_store, harness)}
            hostile_raw = {r["id"]: r for r in await _raw(hostile_store, harness)}
        finally:
            type.__setattr__(
                _base_module.ConsoleStoreBase,
                "_refuse_reserved_value_columns",
                original,
            )
        assert honest_raw[honest_row["id"]]["trace_id"] == honest_trace
        assert hostile_raw[hostile_row["id"]]["trace_id"] is None, (
            "expected the unguarded write to null trace_id — if this "
            "fails, a guard that should be neutered here is still active"
        )
