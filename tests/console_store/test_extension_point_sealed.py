"""SPEC ADDENDUM C (fix round 2, 2026-09-17) probes — answering
``review-verdict-consolestorebase-wu-a-fixround1-20260917.outcome-reject``
F3, F4 and advisory A-R7. New file rather than an addition to
``test_domain_descriptor_sealed.py`` (already at the PYTHON-ENGINEERING
§11 500-LOC review trigger before this round's tests).

**F3 — the sanctioned extension point IS the attack surface.** A domain
subclass legitimately lives OUTSIDE this package; before this round
``__setattr__``/``__delattr__`` were not in ``_SEALED_DOMAIN_MEMBERS``, so
an ORDINARY subclass overriding either escaped Guard A entirely — no
monkeypatch, no ``object.__setattr__`` call, just a ``class`` statement a
domain author could write by accident. ``TestSubclassOverridingSealDunders``
proves both halves the addendum requires: the class statement itself
raises, and (separately) a raw-DB witness shows the original F1 exploit
line does not reach a written row when routed through an ordinary,
would-be-unsealed domain.

**F4 — a guard whose OWN hooks are reassignable is not a guard.**
``_SealedMeta``/``_DomainMeta`` guarded their NAMED member sets but not
``__setattr__``, ``__delattr__`` or ``__class__`` themselves, so three
one-line, un-enumerated class-level writes disabled the metaclasses that
were supposed to refuse exactly that:

* R2 — ``PostgresConsoleStore.__setattr__ = object.__setattr__``
* R3 — ``PostgresConsoleStore.__class__ = ABCMeta``
* R4 — ``WidgetDomain.__class__ = ABCMeta`` (the domain equivalent)

R2 and R3 each produced a CROSS-USER READ through the public API (the
reject's headline finding); each is proven here with an attack-refused
test (side effect: the other user's row is NOT returned) and a
neutered-alone non-vacuity test (side effect: the other user's row IS
now returned — the exact regression, reproduced then restored). R4's
mechanical harm is the SAME nulled-``trace_id`` (M5 / EU AI Act Art 12)
Guard D's OWN non-vacuity test already witnesses via a raw-DB read, not a
second cross-user read — that distinction is stated here rather than
overclaimed.

**A-R7 (advisory) — ``_GuardedSessions`` had no class-level seal.**
``_GuardedSessions.get_session_scoped = <evil>`` succeeded at the class
level even though the instance-level ``__setattr__``/``__delattr__``
refused every instance write; ``TestGuardedSessionsClassLevelSealed``
proves the new ``_GuardedSessionsMeta`` closes it.

Every neuter below restores the mutated object to its ORIGINAL state in a
``finally`` block, using the SAME ``type.__setattr__``/``type.__delattr__``
bypass the rest of this test package uses to reach a sealed member — never
left live for another test (Addendum B Req 1: one neuter per run).
"""

from __future__ import annotations

from abc import ABCMeta
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy import select

from audittrace.identity import UserContext
from audittrace.services.console_store import (
    ConsoleStoreSealedError,
    PostgresConsoleStore,
)
from audittrace.services.console_store import _base as _base_module
from audittrace.services.console_store import _domain as _domain_module
from audittrace.services.console_store._postgres import _GuardedSessions
from tests.console_store.support import KEY_A, _raw
from tests.console_store_fixture_domain import SqliteHarness, WidgetDomain, WidgetRow


def _span_trace_id(span: Any) -> str:
    return format(span.get_span_context().trace_id, "032x")


class _MetaclassSwapDomain(WidgetDomain):
    """A throwaway domain subclass — R4's class-level swap mutates ITS
    class attributes, never the shared ``WidgetDomain``'s, so a
    neuter-and-restore cycle here cannot leak state into any other test."""

    name = "metaclass_swap_probe"


# ── F3: the sanctioned extension point (an ordinary domain subclass) ───────


class TestSubclassOverridingSealDunders:
    """R1 — a domain subclass redefining ``__setattr__``/``__delattr__`` is
    now a sealed-member redefinition, refused at CLASS-CREATION time by the
    same ``_refuse_redefinition`` mechanism that already sealed the four
    template members. The control already existed; the member list was
    incomplete."""

    def test_overriding_setattr_is_refused_at_class_definition(self) -> None:
        with pytest.raises(ConsoleStoreSealedError, match="__setattr__"):

            class UnsealedDomain(WidgetDomain):  # noqa: F841 - never bound, refused
                def __setattr__(self, name: str, value: Any) -> None:
                    self.__dict__[name] = value

    def test_overriding_delattr_is_refused_at_class_definition(self) -> None:
        with pytest.raises(ConsoleStoreSealedError, match="__delattr__"):

            class UnsealedDomain(WidgetDomain):  # noqa: F841 - never bound, refused
                def __delattr__(self, name: str) -> None:
                    pass

    async def test_the_f1_exploit_no_longer_reaches_a_written_row_via_this_route(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        """The raw-DB half of the addendum's F3 acceptance criterion: since
        the class statement above never produces a class object, there is
        no ``UnsealedDomain`` to build a store with — and an ordinary
        domain (which never attempts the override) still writes correctly,
        with ``trace_id`` intact, exactly as ``TestDomainDescriptorSealed``
        proves for the original exploit line. This is the same "business
        continues normally, not merely an exception" upgrade every other
        guard in this package is held to."""
        with pytest.raises(ConsoleStoreSealedError):

            class UnsealedDomain(WidgetDomain):
                def __setattr__(self, name: str, value: Any) -> None:
                    self.__dict__[name] = value

        assert "UnsealedDomain" not in dir(), "the refused class still exists"
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        tracer = TracerProvider().get_tracer("f3-extension-point")
        with tracer.start_as_current_span("alice-write") as span:
            row = await store.upsert(alice, KEY_A, {"payload": "alice-secret"})
            expected_trace = _span_trace_id(span)
        raw = {r["id"]: r for r in await _raw(store, harness)}
        assert raw[row["id"]]["trace_id"] == expected_trace


# ── F4 R2: the store seal's own __setattr__ hook must itself be sealed ─────


class TestSealDunderReassignmentRefusedR2:
    """``PostgresConsoleStore.__setattr__ = object.__setattr__`` is now
    refused by ``_SealedMeta`` (``__setattr__`` joined
    ``SEALED_STORE_MEMBERS``). Before this fix it disabled the metaclass in
    one line, and a follow-on ``store._scoped_select = unscoped`` then read
    another user's row through the public API — the reject's R2 headline.
    """

    async def test_reassigning_setattr_on_the_class_is_refused(
        self, harness: SqliteHarness, alice: UserContext, bob: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        await store.upsert(alice, KEY_A, {"payload": "ALICE-SECRET"})
        await store.upsert(bob, KEY_A, {"payload": "BOB-SECRET"})
        with pytest.raises(ConsoleStoreSealedError, match="sealed"):
            PostgresConsoleStore.__setattr__ = object.__setattr__  # type: ignore[method-assign]
        assert "__setattr__" not in PostgresConsoleStore.__dict__, (
            "the refused reassignment still took effect"
        )
        items, _ = await store.list(alice)
        assert [item["payload"] for item in items] == ["ALICE-SECRET"], (
            "alice saw a row that is not hers"
        )

    async def test_neutering_the_seal_dunder_check_yields_a_cross_user_read(
        self,
        harness: SqliteHarness,
        alice: UserContext,
        bob: UserContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-vacuity: with ``__setattr__`` removed from
        ``SEALED_STORE_MEMBERS`` (the pre-fix set), the SAME class-level
        reassignment the test above refuses instead succeeds, and the
        follow-on instance-level shadow of ``_scoped_select`` then reads
        another user's row through the PUBLIC API — the exact reject
        headline, reproduced then fully restored so nothing leaks into any
        other test."""
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        await store.upsert(alice, KEY_A, {"payload": "ALICE-SECRET"})
        await store.upsert(bob, KEY_A, {"payload": "BOB-SECRET"})
        monkeypatch.setattr(
            _base_module,
            "SEALED_STORE_MEMBERS",
            frozenset(_base_module.SEALED_STORE_MEMBERS - {"__setattr__"}),
        )
        try:
            PostgresConsoleStore.__setattr__ = object.__setattr__  # type: ignore[method-assign]
            store._scoped_select = lambda *_a, **_k: select(  # type: ignore[method-assign]
                WidgetRow
            )
            items, _ = await store.list(alice)
            payloads = sorted(item["payload"] for item in items)
            assert payloads == ["ALICE-SECRET", "BOB-SECRET"], (
                "neutering the seal-dunder check should have let alice read bob's row"
            )
        finally:
            type.__delattr__(PostgresConsoleStore, "__setattr__")
        assert "__setattr__" not in PostgresConsoleStore.__dict__, (
            "the neuter's class-level shadow was not cleaned up"
        )


# ── F4 R3: the store seal's own metaclass swap must itself be refused ──────


class TestMetaclassSwapRefusedR3:
    """``PostgresConsoleStore.__class__ = ABCMeta`` removes ``_SealedMeta``
    from dispatch entirely — worse than R2, since every SUBSEQUENT
    class-level write (not just one dunder) becomes unguarded. Now refused
    because ``__class__`` joined ``SEALED_STORE_MEMBERS``."""

    async def test_reassigning_the_metaclass_is_refused(
        self, harness: SqliteHarness, alice: UserContext, bob: UserContext
    ) -> None:
        original_metaclass = type(PostgresConsoleStore)
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        await store.upsert(alice, KEY_A, {"payload": "ALICE-SECRET"})
        await store.upsert(bob, KEY_A, {"payload": "BOB-SECRET"})
        with pytest.raises(ConsoleStoreSealedError, match="sealed"):
            PostgresConsoleStore.__class__ = ABCMeta  # type: ignore[assignment]
        assert type(PostgresConsoleStore) is original_metaclass, (
            "the refused metaclass swap still took effect"
        )
        items, _ = await store.list(alice)
        assert [item["payload"] for item in items] == ["ALICE-SECRET"], (
            "alice saw a row that is not hers"
        )

    async def test_neutering_the_class_seal_yields_a_cross_user_read(
        self,
        harness: SqliteHarness,
        alice: UserContext,
        bob: UserContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-vacuity: with ``__class__`` removed from
        ``SEALED_STORE_MEMBERS``, the metaclass swap succeeds, which then
        lets an ORDINARY (post-swap, unguarded) class-level
        ``_scoped_select`` reassignment through — reproducing the R3 cross-
        user read, fully restored afterward via the same ``type.__setattr__``
        bypass every other class-level neuter in this suite uses."""
        original_metaclass = type(PostgresConsoleStore)
        original_scoped_select = PostgresConsoleStore.__dict__["_scoped_select"]
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        await store.upsert(alice, KEY_A, {"payload": "ALICE-SECRET"})
        await store.upsert(bob, KEY_A, {"payload": "BOB-SECRET"})
        monkeypatch.setattr(
            _base_module,
            "SEALED_STORE_MEMBERS",
            frozenset(_base_module.SEALED_STORE_MEMBERS - {"__class__"}),
        )
        try:
            PostgresConsoleStore.__class__ = ABCMeta  # type: ignore[assignment]
            PostgresConsoleStore._scoped_select = (  # type: ignore[method-assign]
                lambda self, user_sub, **_k: select(WidgetRow)
            )
            items, _ = await store.list(alice)
            payloads = sorted(item["payload"] for item in items)
            assert payloads == ["ALICE-SECRET", "BOB-SECRET"], (
                "neutering the class seal should have let alice read bob's row"
            )
        finally:
            type.__setattr__(
                PostgresConsoleStore, "_scoped_select", original_scoped_select
            )
            type.__setattr__(PostgresConsoleStore, "__class__", original_metaclass)
        assert type(PostgresConsoleStore) is original_metaclass
        assert PostgresConsoleStore.__dict__["_scoped_select"] is original_scoped_select


# ── F4 R4: the domain metaclass swap must itself be refused ────────────────


class TestDomainMetaclassSwapRefusedR4:
    """``WidgetDomain.__class__ = ABCMeta`` (here, a throwaway subclass)
    removes ``_DomainMeta`` from dispatch, re-opening exactly the Guard D
    exploit (``value_columns = (*…, "trace_id")``). The witness here is the
    SAME concrete ClassVar-mutation side effect
    ``TestDomainClassLevelSealed.test_neutering_the_class_level_seal_lets_
    the_mutation_through`` uses for Guard D directly (the tuple itself
    changes) — NOT a raw-DB / cross-user-read reproduction, because Guard C
    (``_refuse_reserved_value_columns``, load-bearing per R5) independently
    refuses any WRITE through a domain whose ``value_columns`` collides
    with a reserved column, regardless of which route corrupted it; running
    an upsert here would prove Guard C, not this metaclass seal, and
    conflate two independently-proven guards into one test. R2/R3 above are
    the two class-write routes whose harm is a cross-user READ; this one is
    disclosed as what it mechanically is instead."""

    def test_reassigning_the_domain_metaclass_is_refused(self) -> None:
        original_metaclass = type(_MetaclassSwapDomain)
        with pytest.raises(ConsoleStoreSealedError, match="cannot be reassigned"):
            _MetaclassSwapDomain.__class__ = ABCMeta  # type: ignore[assignment]
        assert type(_MetaclassSwapDomain) is original_metaclass, (
            "the refused metaclass swap still took effect"
        )

    def test_neutering_the_domain_class_seal_reopens_guard_d(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-vacuity: with ``__class__`` removed from
        ``_SEALED_DOMAIN_CLASS_ATTRS``, the domain metaclass swap succeeds,
        which then lets the ORIGINAL Guard D exploit line
        (``value_columns = (*…, "trace_id")``) through — a concrete,
        checkable side effect (the tuple itself changes), fully restored
        afterward regardless of outcome."""
        original_metaclass = type(_MetaclassSwapDomain)
        original_value_columns = _MetaclassSwapDomain.value_columns
        monkeypatch.setattr(
            _domain_module,
            "_SEALED_DOMAIN_CLASS_ATTRS",
            frozenset(_domain_module._SEALED_DOMAIN_CLASS_ATTRS - {"__class__"}),
        )
        try:
            _MetaclassSwapDomain.__class__ = ABCMeta  # type: ignore[assignment]
            _MetaclassSwapDomain.value_columns = (*original_value_columns, "trace_id")
            assert _MetaclassSwapDomain.value_columns == (
                "payload",
                "priority",
                "trace_id",
            ), "neutering the seal should have let the mutation through"
        finally:
            type.__setattr__(
                _MetaclassSwapDomain, "value_columns", original_value_columns
            )
            type.__setattr__(_MetaclassSwapDomain, "__class__", original_metaclass)
        assert type(_MetaclassSwapDomain) is original_metaclass
        assert _MetaclassSwapDomain.value_columns == original_value_columns


# ── A-R7 (advisory): _GuardedSessions had no class-level seal ──────────────


class TestGuardedSessionsClassLevelSealed:
    """``_GuardedSessions.get_session_scoped = <evil>`` succeeded at the
    CLASS level even though the instance-level ``__setattr__``/
    ``__delattr__`` refused every instance write — an asymmetry the
    per-instance seal alone cannot close, since a class-level write is
    dispatched to the metaclass, not the instance's own ``__setattr__``.
    ``_GuardedSessionsMeta`` closes it."""

    def test_non_sealed_class_attributes_are_still_settable_and_deletable(self) -> None:
        """The block is by EXPLICIT NAME (mirroring ``SEALED_STORE_MEMBERS``
        / ``_SEALED_DOMAIN_CLASS_ATTRS``), not "every class attribute" — an
        ordinary, non-sealed class attribute passes through to
        ``type.__setattr__``/``type.__delattr__`` unaffected."""
        _GuardedSessions.scratch_marker = 1  # type: ignore[attr-defined]
        assert _GuardedSessions.scratch_marker == 1  # type: ignore[attr-defined]
        del _GuardedSessions.scratch_marker  # type: ignore[attr-defined]
        assert not hasattr(_GuardedSessions, "scratch_marker")

    def test_reassigning_get_session_scoped_on_the_class_is_refused(self) -> None:
        original = _GuardedSessions.__dict__["get_session_scoped"]
        with pytest.raises(ConsoleStoreSealedError, match="sealed"):
            _GuardedSessions.get_session_scoped = lambda self, user_context: None  # type: ignore[method-assign]
        assert _GuardedSessions.__dict__["get_session_scoped"] is original, (
            "the refused reassignment still took effect"
        )

    def test_deleting_get_session_scoped_on_the_class_is_refused(self) -> None:
        with pytest.raises(ConsoleStoreSealedError, match="sealed"):
            del _GuardedSessions.get_session_scoped
        assert "get_session_scoped" in _GuardedSessions.__dict__, (
            "the refused deletion still took effect"
        )

    def test_neutering_the_class_seal_lets_the_opener_method_be_replaced(self) -> None:
        """Non-vacuity: with plain ``type`` as the metaclass (the ORIGINAL,
        pre-A-R7 shape), the SAME reassignment the test above refuses
        instead succeeds and replaces the opener method for EVERY instance
        of the class, present and future — a concrete, checkable side
        effect, restored in ``finally`` regardless of outcome."""
        original = _GuardedSessions.__dict__["get_session_scoped"]

        def _evil(self: Any, user_context: Any) -> None:
            return None

        type.__setattr__(_GuardedSessions, "get_session_scoped", _evil)
        try:
            assert _GuardedSessions.__dict__["get_session_scoped"] is _evil, (
                "neutering the class seal should have let the method be replaced"
            )
        finally:
            type.__setattr__(_GuardedSessions, "get_session_scoped", original)

    def test_neutering_the_class_seal_lets_the_opener_method_be_deleted(self) -> None:
        """Non-vacuity for ``_GuardedSessionsMeta.__delattr__``: with plain
        ``type`` as the metaclass, the SAME deletion the test above
        refuses instead succeeds and removes the opener method entirely —
        restored in ``finally`` regardless of outcome."""
        original = _GuardedSessions.__dict__["get_session_scoped"]
        type.__delattr__(_GuardedSessions, "get_session_scoped")
        try:
            assert "get_session_scoped" not in _GuardedSessions.__dict__, (
                "neutering the class seal should have let the method be deleted"
            )
        finally:
            type.__setattr__(_GuardedSessions, "get_session_scoped", original)
