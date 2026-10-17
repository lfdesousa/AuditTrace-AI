"""Domain-DESCRIPTOR sealing probes for ``services/console_store`` — fix
round 1 (2026-09-15), answering
``review-verdict-consolestorebase-wu-a-20260915.outcome-reject`` F1. Split
from ``test_console_store_hostile.py`` (A3 — that file exceeded the
PYTHON-ENGINEERING §11 500-LOC trigger), then split AGAIN (fix round 4,
2026-09-17, F7 advisory item 5 — this file had grown back to 533 LOC) into
three guard-scoped files, this one covering Guards A and D. Guards B/B'
(the ``_domain``/``_sessions`` POINTER write-once check) moved to
``test_domain_reassignment_sealed.py``; Guard C (the reserved-column
refusal at point of use) moved to
``test_domain_reserved_column_guard_sealed.py``. No test body changed —
only the file each class lives in.

**The defect (see ``_domain.py`` module docstring for the full account):**
``ConsoleStoreBase.domain`` hands out the validated :class:`ConsoleDomain`
instance BY REFERENCE. Before this round, one line of ordinary Python —
``store.domain.value_columns = (*store.domain.value_columns, "trace_id")``
— reassigned the descriptor's own attribute in place (no ``_domain``
pointer swap, no dunder, no ``__dict__`` write) and the next insert
silently nulled the base-stamped ``trace_id`` (M5 / EU AI Act Art 12).

**Two independent guards, proven separately** (Addendum B Req 1 — one
neuter per run, never a batch; a batch neuter proves only that *at
least one* guard is load-bearing, F-C1's mistake):

* **Guard A** — ``ConsoleDomain.__setattr__``/``__delattr__``: the
  descriptor INSTANCE itself refuses every attribute set/delete. This is
  what closes the ORIGINAL exploit line, completely — proven below
  (``TestDomainDescriptorSealed``).
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
from audittrace.services.console_store import ConsoleDomain, PostgresConsoleStore
from audittrace.services.console_store._errors import ConsoleStoreSealedError
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

    def test_neutering_the_seal_lets_the_mutation_through(self) -> None:
        """Non-vacuity for Guard A: with ``ConsoleDomain.__setattr__``
        neutered (mirroring exactly how F1 was originally found — no other
        guard stands in front of a bare descriptor that was never wired
        into a store), the SAME mutation the test above refuses instead
        succeeds and changes LIVE state — a concrete, checkable side
        effect (the tuple itself changes), not merely "no exception was
        raised" (A1's own complaint about a weaker proof shape).

        SPEC ADDENDUM C R2 (fix round 2) sealed ``__setattr__`` itself as a
        ``_SEALED_DOMAIN_CLASS_ATTRS`` member, so an ordinary
        ``monkeypatch.setattr(ConsoleDomain, "__setattr__", ...)`` (which
        dispatches through ``_DomainMeta.__setattr__``) is now ITSELF
        refused — proof the class-level seal covers its own hook. Reaching
        the neuter this test needs therefore requires the same disclosed
        ``type.__setattr__`` bypass every other SEALED_STORE_MEMBERS-style
        neuter in this suite uses, restored in ``finally`` regardless of
        outcome so this test cannot leak state into any other."""
        original = ConsoleDomain.__dict__["__setattr__"]
        type.__setattr__(ConsoleDomain, "__setattr__", object.__setattr__)
        try:
            domain = WidgetDomain()
            domain.value_columns = (*domain.value_columns, "trace_id")
            assert domain.value_columns == ("payload", "priority", "trace_id"), (
                "neutering the seal should have let the mutation through"
            )
        finally:
            type.__setattr__(ConsoleDomain, "__setattr__", original)


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
