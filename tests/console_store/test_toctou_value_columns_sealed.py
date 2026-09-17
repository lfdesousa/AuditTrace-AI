"""SPEC ADDENDUM D (fix round 3, 2026-09-17) — F6: sealing MUTATION is not
sealing VALUE. Answers
``review-verdict-consolestorebase-wu-a-fixround2-20260917.outcome-reject``
F6: a domain whose ``value_columns`` is a stateful ``property`` (or
``__getattribute__`` override) answers ``validate_domain()``'s
reserved-column check with a SAFE tuple and a LATER read, inside
``_insert_values``'s row-assignment loop, with a HOSTILE one — no
monkeypatch, no ``object.__setattr__``, no metaclass swap; the sanctioned
extension point (an ordinary domain subclass) is enough. See ``_domain.py``'s
module docstring, "Fifth hop", for the full account and the fix
(:class:`~audittrace.services.console_store._domain.DomainContract`: every
declarative surface is read EXACTLY ONCE by ``validate_domain()`` and
cached on the store; nothing downstream ever re-reads the live domain for
those fields again).

Two independent tests per variant (Addendum B Req 1 — one neuter per run):

* ``test_*_stamp_survives_the_fixed_code`` — GREEN under the current code:
  construction consumes the ONE safe read; every subsequent read the
  property/``__getattribute__`` answers is hostile, and the write is
  unaffected because nothing downstream reads it again.
* ``test_neutering_the_read_once_fix_reopens_*`` — non-vacuity: restores
  ``_insert_values`` to its PRE-ADDENDUM-D shape (reading
  ``self._domain.value_columns`` live, exactly as it did before this fix),
  reproduces the ORIGINAL F1 harm (raw DB witness: ``trace_id`` nulled),
  then restores the sealed member and re-confirms the fixed behaviour on a
  FRESH store (the counter-based fixtures are stateful, so reuse across
  the RED/GREEN halves would conflate two different reads).

The four siblings (``key_columns``, ``model``, ``order_by``,
``has_session_id``) are audited in
``test_toctou_declarative_surfaces_sealed.py``.
"""

from __future__ import annotations

import uuid
from typing import Any

from opentelemetry.sdk.trace import TracerProvider

from audittrace.identity import UserContext
from audittrace.services.console_store import PostgresConsoleStore
from audittrace.services.console_store import _base as _base_module
from audittrace.services.console_store._context import WriteStamp
from tests.console_store.support import KEY_A, _raw
from tests.console_store_fixture_domain import SqliteHarness, WidgetDomain


def _span_trace_id(span: Any) -> str:
    return format(span.get_span_context().trace_id, "032x")


# ── the two hostile domains: property, then __getattribute__ ───────────────


class TocTouPropertyDomain(WidgetDomain):
    """SPEC ADDENDUM D's own example, verbatim in spirit: safe on the
    FIRST read (``validate_domain()``'s, at construction), hostile on
    EVERY read after — the minimal shape that defeats a validate-then-use
    check without ever mutating anything.

    ``_n`` is INSTANCE state, set via ``object.__setattr__`` in
    ``__init__`` (the same disclosed bypass ``ConsoleDomain``'s own module
    docstring names for a domain's ONE-TIME initialization) — a
    CLASS-level counter would leak across every test that instantiates
    this fixture in the same process, since ``ConsoleDomain.__setattr__``
    refuses the ordinary ``self._n = 0`` an instance attribute would need."""

    name = "toctou_property_widget"

    def __init__(self) -> None:
        object.__setattr__(self, "_n", 0)

    @property
    def value_columns(self) -> tuple[str, ...]:  # type: ignore[override]
        n = object.__getattribute__(self, "_n")
        object.__setattr__(self, "_n", n + 1)
        return ("payload", "priority") if n < 1 else ("payload", "priority", "trace_id")


class TocTouGetattributeDomain(WidgetDomain):
    """The SAME defect via ``__getattribute__`` instead of ``property`` —
    F6's own scope note: "the defect is read-nonatomicity, not one
    descriptor trick." ``__getattribute__`` is not in
    ``_SEALED_DOMAIN_MEMBERS`` (unlike ``__setattr__``/``__delattr__`` —
    SPEC ADDENDUM C's "third hop"), and this fix does not add it there
    either (see ``_domain.py``'s "Fifth hop" for why): read-once-and-carry
    protects the base regardless of which descriptor protocol a hostile
    domain uses. ``_n`` is INSTANCE state (see :class:`TocTouPropertyDomain`
    for why a class-level counter would leak across tests)."""

    name = "toctou_getattribute_widget"

    def __init__(self) -> None:
        object.__setattr__(self, "_n", 0)

    def __getattribute__(self, name: str) -> Any:
        if name == "value_columns":
            n = object.__getattribute__(self, "_n")
            object.__setattr__(self, "_n", n + 1)
            return (
                ("payload", "priority")
                if n < 1
                else ("payload", "priority", "trace_id")
            )
        return object.__getattribute__(self, name)


def _old_insert_values_reading_live_domain(
    self: Any, stamp: WriteStamp, key: Any, values: Any
) -> dict[str, Any]:
    """Verbatim reproduction of ``ConsoleStoreBase._insert_values`` BEFORE
    SPEC ADDENDUM D (fix round 3): reads ``self._domain.value_columns`` /
    ``self._domain.has_session_id()`` live, not the cached
    ``self._contract``. Used ONLY to prove the fix is load-bearing
    (Addendum B Req 1 non-vacuity) — installed via ``type.__setattr__``
    (the disclosed bypass every sealed-member neuter in this suite uses)
    and restored in ``finally`` regardless of outcome."""
    self._refuse_reserved_value_columns()
    defaults = self._hook_output(self._domain.defaults(dict(key)), hook="defaults")
    row: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "user_sub": stamp.user_sub,
        "created_at_ms": stamp.now_ms,
        "updated_at_ms": stamp.now_ms,
        "deleted_at_ms": None,
        "trace_id": stamp.trace_id,
    }
    if self._domain.has_session_id():
        row["session_id"] = stamp.session_id
    row.update(key)
    for column in self._domain.value_columns:  # the pre-fix, live re-read
        row[column] = values[column] if column in values else defaults.get(column)
    return row


class TestPropertyVariantFixed:
    async def test_stamp_survives_the_fixed_code(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouPropertyDomain(), harness.factory
        )
        # Construction consumed the ONE read validate_domain() makes; the
        # descriptor is already hostile on every read from here on —
        # proven directly, not merely asserted.
        assert store.domain.value_columns == ("payload", "priority", "trace_id"), (
            "the fixture must actually be hostile post-construction, or "
            "this test would pass for the wrong reason"
        )
        tracer = TracerProvider().get_tracer("f6-property-toctou")
        with tracer.start_as_current_span("alice-write") as span:
            row = await store.upsert(alice, KEY_A, {"payload": "alice-secret"})
            expected_trace = _span_trace_id(span)
        raw = {r["id"]: r for r in await _raw(store, harness)}
        assert raw[row["id"]]["trace_id"] == expected_trace, (
            "the TOCTOU property reached the row build and nulled the "
            "trace_id stamp — F6 reproduced despite the fix"
        )

    async def test_neutering_the_read_once_fix_reopens_the_property_toctou(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        original = _base_module.ConsoleStoreBase.__dict__["_insert_values"]
        type.__setattr__(
            _base_module.ConsoleStoreBase,
            "_insert_values",
            _old_insert_values_reading_live_domain,
        )
        try:
            hostile_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
                TocTouPropertyDomain(), harness.factory
            )
            row = await hostile_store.upsert(alice, KEY_A, {"payload": "attack"})
            raw = {r["id"]: r for r in await _raw(hostile_store, harness)}
            assert raw[row["id"]]["trace_id"] is None, (
                "neutering the read-once fix should have let the property "
                "TOCTOU null the trace_id stamp again — the pre-ADDENDUM-D harm"
            )
        finally:
            type.__setattr__(_base_module.ConsoleStoreBase, "_insert_values", original)
        assert _base_module.ConsoleStoreBase.__dict__["_insert_values"] is original

        # Restored: a FRESH store (fresh counter) proves the fix is back.
        fixed_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouPropertyDomain(), harness.factory
        )
        tracer = TracerProvider().get_tracer("f6-property-toctou-restored")
        with tracer.start_as_current_span("alice-write-2") as span:
            row2 = await fixed_store.upsert(alice, KEY_A, {"payload": "alice-secret-2"})
            expected_trace = _span_trace_id(span)
        raw2 = {r["id"]: r for r in await _raw(fixed_store, harness)}
        assert raw2[row2["id"]]["trace_id"] == expected_trace


class TestGetattributeVariantFixed:
    async def test_stamp_survives_the_fixed_code(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouGetattributeDomain(), harness.factory
        )
        assert store.domain.value_columns == ("payload", "priority", "trace_id"), (
            "the fixture must actually be hostile post-construction, or "
            "this test would pass for the wrong reason"
        )
        tracer = TracerProvider().get_tracer("f6-getattribute-toctou")
        with tracer.start_as_current_span("alice-write") as span:
            row = await store.upsert(alice, KEY_A, {"payload": "alice-secret"})
            expected_trace = _span_trace_id(span)
        raw = {r["id"]: r for r in await _raw(store, harness)}
        assert raw[row["id"]]["trace_id"] == expected_trace, (
            "the TOCTOU __getattribute__ override reached the row build and "
            "nulled the trace_id stamp — F6 reproduced despite the fix"
        )

    async def test_neutering_the_read_once_fix_reopens_the_getattribute_toctou(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        original = _base_module.ConsoleStoreBase.__dict__["_insert_values"]
        type.__setattr__(
            _base_module.ConsoleStoreBase,
            "_insert_values",
            _old_insert_values_reading_live_domain,
        )
        try:
            hostile_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
                TocTouGetattributeDomain(), harness.factory
            )
            row = await hostile_store.upsert(alice, KEY_A, {"payload": "attack"})
            raw = {r["id"]: r for r in await _raw(hostile_store, harness)}
            assert raw[row["id"]]["trace_id"] is None, (
                "neutering the read-once fix should have let the "
                "__getattribute__ TOCTOU null the trace_id stamp again"
            )
        finally:
            type.__setattr__(_base_module.ConsoleStoreBase, "_insert_values", original)
        assert _base_module.ConsoleStoreBase.__dict__["_insert_values"] is original

        fixed_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouGetattributeDomain(), harness.factory
        )
        tracer = TracerProvider().get_tracer("f6-getattribute-toctou-restored")
        with tracer.start_as_current_span("alice-write-2") as span:
            row2 = await fixed_store.upsert(alice, KEY_A, {"payload": "alice-secret-2"})
            expected_trace = _span_trace_id(span)
        raw2 = {r["id"]: r for r in await _raw(fixed_store, harness)}
        assert raw2[row2["id"]]["trace_id"] == expected_trace


class TestCrossUserEscalationStillFails:
    """SPEC ADDENDUM D's own honest-scope note, pinned as a regression: an
    attempt to escalate F6 to a cross-user READ (a time-varying ``model``
    property remapping which ORM class ``_scoped_select`` queries)
    independently fails, because ``_scoped_select`` binds its compared
    ``user_sub`` from ``_read_sub()`` (token-derived) — and, since this
    fix, from ``self._contract.model`` (captured once), never a live
    ``self._domain.model`` a hostile property could steer per call."""

    async def test_bob_cannot_read_alices_row_through_a_toctou_domain(
        self, harness: SqliteHarness, alice: UserContext, bob: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouPropertyDomain(), harness.factory
        )
        await store.upsert(alice, KEY_A, {"payload": "alice-secret"})
        assert await store.get(bob, KEY_A) is None, "bob read alice's row"
