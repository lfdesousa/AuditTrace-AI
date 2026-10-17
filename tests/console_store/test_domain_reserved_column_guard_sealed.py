"""Guard C — refusing a reserved-column collision AT THE POINT OF USE, not
merely at construction. Split out of ``test_domain_descriptor_sealed.py``
(fix round 4, 2026-09-17, F7 advisory item 5 — that file had grown to 533
LOC, past the PYTHON-ENGINEERING §11 500-LOC trigger) into its own
guard-scoped file; no test body changed, only the file it lives in. See
``test_domain_descriptor_sealed.py`` for Guards A/D (the descriptor
itself) and ``test_domain_reassignment_sealed.py`` for Guards B/B' (the
``_domain``/``_sessions`` pointer write-once check).

``ConsoleStoreBase._refuse_reserved_value_columns`` (F1 item 2):
LOAD-BEARING, not defence in depth (SPEC ADDENDUM C R5) — refuses,
LOUDLY, at the point every write path TRUSTS ``value_columns``, whatever
route got a bad descriptor there. Before
``tests/console_store/test_extension_point_sealed.py`` closed the "third
hop" (an ordinary domain subclass overriding ``__setattr__``), THIS guard
was the SOLE barrier reachable with zero monkeypatch. Even now, a
construction path that skips ``validate_domain()`` reaches it too —
simulated below via monkeypatch (proven below,
``TestReservedColumnRefusedAtPointOfUse``).
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider

from audittrace.identity import UserContext
from audittrace.services.console_store import (
    ConsoleDomain,
    ConsoleStoreForbiddenFieldError,
    DomainContract,
    PostgresConsoleStore,
)
from audittrace.services.console_store import _base as _base_module
from audittrace.services.console_store._context import REQUIRED_MODEL_COLUMNS
from tests.console_store.support import KEY_A, _raw
from tests.console_store_fixture_domain import SqliteHarness, WidgetDomain


def _span_trace_id(span: Any) -> str:
    return format(span.get_span_context().trace_id, "032x")


def _contract_skipping_the_reserved_column_check(
    domain: ConsoleDomain[Any],
) -> DomainContract:
    """SPEC ADDENDUM D, fix round 3: ``ConsoleStoreBase.__init__`` now
    caches a :class:`DomainContract` (``validate_domain()``'s return
    value) instead of trusting a live ``self._domain`` read — see
    ``_domain.py``'s module docstring, "Fifth hop". The pre-ADDENDUM-D
    simulation here (``monkeypatch.setattr(_base_module, "validate_domain",
    lambda domain: None)``) no longer models the SAME future-construction-
    path threat: with a cached contract, ``lambda domain: None`` makes
    ``self._contract`` ``None`` and every helper crashes with
    ``AttributeError`` before Guard C ever runs — a different failure than
    the one this test is about. This builds a ``DomainContract`` the SAME
    way ``validate_domain()`` does, EXCEPT it skips the reserved-column
    check, so it reaches Guard C exactly as the pre-ADDENDUM-D no-op did —
    same threat model (a future path that builds/caches a contract without
    validating it), adapted to the new plumbing."""
    keys = tuple(domain.key_columns)
    values = tuple(domain.value_columns)
    has_session_id = hasattr(domain.model, "session_id")
    reserved_prefix = tuple(REQUIRED_MODEL_COLUMNS) + (
        ("session_id",) if has_session_id else ()
    )
    return DomainContract(
        name=domain.name,
        model=domain.model,
        key_columns=keys,
        value_columns=values,
        order_by=tuple(domain.order_by),
        default_list_limit=domain.default_list_limit,
        max_list_limit=domain.max_list_limit,
        has_session_id=has_session_id,
        snapshot_columns=reserved_prefix + keys + values,
        order_columns=tuple(c for c, _ in domain.order_by),
        order_directions=tuple(d for _, d in domain.order_by),
    )


class _ReservedValueColumnDomain(WidgetDomain):
    """Declares ``trace_id`` (nullable, reserved) as a value column — the
    exact shape ``validate_domain()`` refuses at construction. Used ONLY
    with ``validate_domain`` monkeypatched (via
    ``_contract_skipping_the_reserved_column_check`` above) to skip its
    reserved-column check, simulating a FUTURE construction path that
    builds/caches a :class:`DomainContract` without it (Guard A closes the
    only OTHER route to this shape — runtime mutation — completely; see
    ``test_domain_descriptor_sealed.py::TestDomainDescriptorSealed``)."""

    name = "hostile_reserved_value_column"
    value_columns = ("payload", "priority", "trace_id")


class TestReservedColumnRefusedAtPointOfUse:
    """Guard C — LOAD-BEARING (SPEC ADDENDUM C R5), not defence in depth.
    Both tests below simulate ONE route to its protected code: a
    construction path that skips ``validate_domain()`` — the FUTURE path
    the spec calls out ("a domain that declares the collision from the
    start ... via a future construction path that skips
    ``validate_domain()``"), via monkeypatch, since no such construction
    path exists today. A SEPARATE route needs no monkeypatch at all: an
    ordinary domain subclass overriding ``__setattr__``/``__delattr__``
    (the "third hop" — see ``test_extension_point_sealed.py``, which also
    closes it); before that fix, this guard was the SOLE barrier reachable
    that way with zero monkeypatch."""

    async def test_refuses_a_domain_that_bypassed_construction_validation(
        self, harness: SqliteHarness, bob: UserContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            _base_module,
            "validate_domain",
            _contract_skipping_the_reserved_column_check,
        )
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
        monkeypatch.setattr(
            _base_module,
            "validate_domain",
            _contract_skipping_the_reserved_column_check,
        )
        # ``_refuse_reserved_value_columns`` is a SEALED_STORE_MEMBERS entry
        # — ``ConsoleStoreBase``'s own metaclass refuses an ordinary
        # ``setattr`` on it (proven in ``test_sealed_classes.py``), so
        # ``monkeypatch.setattr`` cannot reach it either. Bypass the SAME
        # way the sealed-classes tests do to restore a sealed member:
        # ``type.__setattr__`` directly.
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
