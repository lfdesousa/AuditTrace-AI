"""SPEC ADDENDUM D (fix round 3, 2026-09-17), R1/R3 — the FOUR siblings
audited on the SAME axis as ``value_columns`` (F6, see
``test_toctou_value_columns_sealed.py``): ``key_columns``, ``model``,
``order_by``, ``has_session_id`` (derived from ``model``). R1's own
instruction: "the reviewer found no cross-user read through them; that is
not the same as proving them safe."

**DONE, not merely disclosed** — all five surfaces are pulled into the
SAME :class:`~audittrace.services.console_store._domain.DomainContract`
by the SAME single-read mechanism inside ``validate_domain()`` (see
``_domain.py``'s module docstring, "Fifth hop"); this file proves that for
each of the four siblings here with a live, stateful descriptor: safe on
the ONE read ``validate_domain()`` makes, hostile on every read after, and
a real store operation through the public API still behaves CORRECTLY
because nothing downstream of construction ever reads the live domain for
these fields again.

Not independently neuter-and-restore proven per sibling: unlike
``value_columns`` (F6, the reviewer's own reproduction target, proven with
a full neuter/restore cycle for both the ``property`` and
``__getattribute__`` variants), these four go through the EXACT SAME
``validate_domain()`` → ``DomainContract`` → ``self._contract`` path, not
four independent guards — neutering that ONE mechanism four separate
times would prove the same thing four times over (Addendum B Req 1 is
about not batching INDEPENDENT guards into one neuter, not about
re-proving one mechanism per field it protects). What is verified here,
per sibling, is that the STORE actually uses the cached value under a
live attack shape, with a real assertion on the write's outcome — not a
bare equality check on the domain's own attribute.

**CORRECTED (fix round 4, SPEC F7, 2026-09-17): the paragraph above was
checked and found FALSE for ``has_session_id`` — proven and retracted,
not merely reworded.** ``has_session_id`` has its OWN distinct downstream
consumer: ``_insert_values`` / ``_update_values`` / ``_delete_values``
each branch on ``self._contract.has_session_id`` to decide whether
``session_id`` is stamped AT ALL, a separate call site from the one
``value_columns`` feeds. Neutering the SHARED ``validate_domain()``
capture mechanism via ``value_columns`` therefore proves nothing about
whether ``has_session_id`` is independently load-bearing at ITS OWN use
site. Worse: the assertion this file originally shipped for it
(``"session_id" in raw[row["id"]]``) was a dict-key-presence tautology —
``SqliteHarness.raw_rows()`` builds every row from the fixture's
``WIDGET_COLUMNS``, which names ``"session_id"`` unconditionally, so the
key is present whether or not a live re-read of ``model`` would have
nulled the STAMPED VALUE — and could not have gone RED for the claimed
reason regardless of what code it ran against
(``feedback_vacuous_neuter_test_antipattern``).
``TestHasSessionIdNonVacuous`` below now proves it directly: a per-field
method-neuter (mirroring ``test_toctou_value_columns_sealed.py``'s
pattern — ``value_columns`` stays cached, only ``has_session_id`` is
served live, one neuter per run per Addendum B Req 1) reproduces the SAME
harm CLASS as F6 — a silently dropped M5 / EU AI Act Art 12 traceability
stamp, ``session_id`` this time instead of ``trace_id`` — with a raw-DB
value witness, RED under the neuter, GREEN restored on a fresh store.

``key_columns``, ``model`` and ``order_by`` are UNAFFECTED by this
correction: their existing assertions already compare a real WRITTEN
value (``raw[...]["name"] == "web-search"``, ``raw[...]["trace_id"] ==
expected_trace``, an insertion-order-derived list ordering) rather than
attribute presence, so the "one shared capture mechanism, verified per
sibling by a real outcome assertion" claim continues to hold for those
three.
"""

from __future__ import annotations

import uuid
from typing import Any

from opentelemetry.sdk.trace import TracerProvider

from audittrace.identity import UserContext
from audittrace.services.console_store import PostgresConsoleStore, bind_session_id
from audittrace.services.console_store import _base as _base_module
from audittrace.services.console_store._context import WriteStamp
from tests.console_store.support import KEY_A, _raw
from tests.console_store_fixture_domain import SqliteHarness, WidgetDomain, WidgetRow


def _span_trace_id(span: Any) -> str:
    return format(span.get_span_context().trace_id, "032x")


def _instance_counter(obj: Any) -> int:
    return int(object.__getattribute__(obj, "_n"))


def _bump_instance_counter(obj: Any) -> int:
    n = _instance_counter(obj)
    object.__setattr__(obj, "_n", n + 1)
    return n


# ── key_columns ──────────────────────────────────────────────────────────


class TocTouKeyColumnsDomain(WidgetDomain):
    """Safe ``("kind", "name")`` on the ONE ``validate_domain()`` read;
    every read after drops ``"name"`` — the exact shape ``_validated_key``
    would reject if it re-read the live domain instead of the cached
    ``DomainContract``."""

    name = "toctou_key_columns_widget"

    def __init__(self) -> None:
        object.__setattr__(self, "_n", 0)

    @property
    def key_columns(self) -> tuple[str, ...]:  # type: ignore[override]
        n = _bump_instance_counter(self)
        return ("kind", "name") if n < 1 else ("kind",)


class TestKeyColumnsPinned:
    async def test_a_hostile_post_construction_key_columns_does_not_reach_the_query(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouKeyColumnsDomain(), harness.factory
        )
        # Confirm the fixture is genuinely hostile from here on — proven,
        # not assumed.
        assert store.domain.key_columns == ("kind",), (
            "the fixture must actually be hostile post-construction"
        )
        row = await store.upsert(alice, KEY_A, {"payload": "alice-secret"})
        raw = {r["id"]: r for r in await _raw(store, harness)}
        assert raw[row["id"]]["kind"] == "tool"
        assert raw[row["id"]]["name"] == "web-search", (
            "a live re-read of key_columns would have accepted a key "
            "naming only 'kind' and silently dropped 'name'"
        )


# ── model ────────────────────────────────────────────────────────────────


class TocTouModelDomain(WidgetDomain):
    """Safe ``WidgetRow`` on the ONE ``validate_domain()`` read; every
    read after answers with a non-ORM placeholder that would blow up
    ``select(model)`` / ``model.user_sub`` if the store ever re-read it."""

    name = "toctou_model_widget"

    def __init__(self) -> None:
        object.__setattr__(self, "_n", 0)

    @property
    def model(self) -> type[Any]:  # type: ignore[override]
        n = _bump_instance_counter(self)
        return WidgetRow if n < 1 else object


class TestModelPinned:
    async def test_a_hostile_post_construction_model_does_not_reach_the_query(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouModelDomain(), harness.factory
        )
        assert store.domain.model is object, (
            "the fixture must actually be hostile post-construction"
        )
        tracer = TracerProvider().get_tracer("toctou-model")
        with tracer.start_as_current_span("alice-write") as span:
            row = await store.upsert(alice, KEY_A, {"payload": "alice-secret"})
            expected_trace = _span_trace_id(span)
        raw = {r["id"]: r for r in await _raw(store, harness)}
        assert raw[row["id"]]["trace_id"] == expected_trace, (
            "a live re-read of model would have raised building the query "
            "against a class with no mapped columns, not written a row"
        )


# ── order_by (+ order_columns/order_directions derived from it) ────────────


class TocTouOrderByDomain(WidgetDomain):
    """Safe ordering on the ONE ``validate_domain()`` read (needed so
    construction's total-order check passes); every read after answers
    with an ordering ``validate_domain()`` would have refused (orders by
    a VALUE column, which is nullable — total order is undefined)."""

    name = "toctou_order_by_widget"

    def __init__(self) -> None:
        object.__setattr__(self, "_n", 0)

    @property
    def order_by(self) -> tuple[tuple[str, str], ...]:  # type: ignore[override]
        n = _bump_instance_counter(self)
        if n < 1:
            return (("updated_at_ms", "desc"), ("kind", "desc"), ("name", "desc"))
        return (("payload", "asc"),)


class TestOrderByPinned:
    async def test_a_hostile_post_construction_order_by_does_not_reach_the_query(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouOrderByDomain(), harness.factory
        )
        assert store.domain.order_by == (("payload", "asc"),), (
            "the fixture must actually be hostile post-construction"
        )
        await store.upsert(alice, {"kind": "tool", "name": "a"}, {"payload": "1"})
        await store.upsert(alice, {"kind": "tool", "name": "b"}, {"payload": "2"})
        # If list() ever re-read the live order_by, it would order by the
        # (nullable) "payload" column instead of the cached, validated
        # ("updated_at_ms" desc, ...) — a materially different result the
        # test below distinguishes by insertion order (b was inserted
        # after a, so "updated_at_ms desc" puts b first).
        items, _ = await store.list(alice)
        assert [item["name"] for item in items] == ["b", "a"], (
            "a live re-read of order_by would have ordered by 'payload' "
            "instead of the cached, validated ordering"
        )


# ── has_session_id (derived from model) ─────────────────────────────────


class TocTouSessionIdDomain(WidgetDomain):
    """``has_session_id()`` is itself a SEALED template member (redefining
    it is refused at class-creation) — but it DERIVES from ``model``, so a
    hostile ``model`` (see :class:`TocTouModelDomain` above) is the same
    attack surface. This fixture isolates that ONE derived field: safe
    ``WidgetRow`` (``session_id`` column present) on the ONE
    ``validate_domain()`` read, a model WITHOUT ``session_id`` on every
    read after."""

    name = "toctou_has_session_id_widget"

    def __init__(self) -> None:
        object.__setattr__(self, "_n", 0)

    @property
    def model(self) -> type[Any]:  # type: ignore[override]
        n = _bump_instance_counter(self)
        if n < 1:
            return WidgetRow

        class _NoSessionIdRow:
            user_sub = None

        return _NoSessionIdRow


class TestHasSessionIdPinned:
    async def test_a_hostile_post_construction_model_does_not_flip_has_session_id(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouSessionIdDomain(), harness.factory
        )
        assert not hasattr(store.domain.model, "session_id"), (
            "the fixture must actually be hostile post-construction"
        )
        bind_session_id("sess-honest-0001")
        row = await store.upsert(alice, KEY_A, {"payload": "alice-secret"})
        raw = {r["id"]: r for r in await _raw(store, harness)}
        # A REAL value witness, not dict-key presence (WIDGET_COLUMNS names
        # "session_id" unconditionally, so `"session_id" in raw[...]` is
        # true regardless of what got STAMPED there — see the corrected
        # module docstring above). A live re-read of model would have
        # flipped has_session_id() to False and left the column None.
        assert raw[row["id"]]["session_id"] == "sess-honest-0001", (
            "a live re-read of model would have dropped session_id from "
            "the cached snapshot/stamping shape, leaving the column None"
        )


def _insert_values_reading_has_session_id_live(
    self: Any, stamp: WriteStamp, key: Any, values: Any
) -> dict[str, Any]:
    """``ConsoleStoreBase._insert_values`` with ONLY ``has_session_id``
    read live off ``self._domain`` — ``value_columns`` and every other
    field stay CACHED (``self._contract``), isolating ``has_session_id``
    alone (Addendum B Req 1: one neuter per run, mirroring
    ``test_toctou_value_columns_sealed.py``'s
    ``_old_insert_values_reading_live_domain``, which isolates
    ``value_columns`` the same way)."""
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
    if self._domain.has_session_id():  # the live re-read under test
        row["session_id"] = stamp.session_id
    row.update(key)
    for column in self._contract.value_columns:  # stays cached, not under test
        row[column] = values[column] if column in values else defaults.get(column)
    return row


class TestHasSessionIdNonVacuous:
    """Non-vacuity for ``has_session_id``, answering F7: reproduces the
    SAME harm class F6 proved for ``value_columns`` (a silently dropped
    M5 / EU AI Act Art 12 stamp), one field at a time."""

    async def test_neutering_the_read_once_fix_drops_session_id_silently(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        original = _base_module.ConsoleStoreBase.__dict__["_insert_values"]
        type.__setattr__(
            _base_module.ConsoleStoreBase,
            "_insert_values",
            _insert_values_reading_has_session_id_live,
        )
        try:
            hostile_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
                TocTouSessionIdDomain(), harness.factory
            )
            bind_session_id("sess-hostile-0001")
            row = await hostile_store.upsert(alice, KEY_A, {"payload": "attack"})
            raw = {r["id"]: r for r in await _raw(hostile_store, harness)}
            assert raw[row["id"]]["session_id"] is None, (
                "neutering the read-once fix should have let the model "
                "TOCTOU silently drop the session_id stamp"
            )
        finally:
            type.__setattr__(_base_module.ConsoleStoreBase, "_insert_values", original)
        assert _base_module.ConsoleStoreBase.__dict__["_insert_values"] is original

        # Restored: a FRESH store (fresh counter) proves the fix is back.
        fixed_store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            TocTouSessionIdDomain(), harness.factory
        )
        bind_session_id("sess-honest-0002")
        row2 = await fixed_store.upsert(alice, KEY_A, {"payload": "alice-secret-2"})
        raw2 = {r["id"]: r for r in await _raw(fixed_store, harness)}
        assert raw2[row2["id"]]["session_id"] == "sess-honest-0002"


# ── the sealed template members themselves stay directly testable ─────────


class TestSealedTemplateMembersStillCorrect:
    """``snapshot_columns`` / ``has_session_id`` / ``order_columns`` /
    ``order_directions`` (``_domain.py``, ``@final``, in
    ``_SEALED_DOMAIN_MEMBERS``) are no longer called by
    ``ConsoleStoreBase`` internals after this fix (it reads the CACHED
    ``DomainContract`` fields instead) — but they remain PUBLIC, sealed
    API a domain author or a future caller can invoke directly, and stay
    correct on an ORDINARY (non-hostile) domain."""

    def test_snapshot_columns_orders_reserved_then_key_then_value(self) -> None:
        domain = WidgetDomain()
        assert domain.snapshot_columns() == (
            "id",
            "user_sub",
            "created_at_ms",
            "updated_at_ms",
            "deleted_at_ms",
            "trace_id",
            "session_id",
            "kind",
            "name",
            "payload",
            "priority",
        )

    def test_has_session_id_true_for_a_model_carrying_the_column(self) -> None:
        assert WidgetDomain().has_session_id() is True

    def test_order_columns_and_directions_mirror_order_by(self) -> None:
        domain = WidgetDomain()
        assert domain.order_columns() == ("updated_at_ms", "kind", "name")
        assert domain.order_directions() == ("desc", "desc", "desc")
