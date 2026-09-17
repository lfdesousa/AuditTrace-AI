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
"""

from __future__ import annotations

from typing import Any

from opentelemetry.sdk.trace import TracerProvider

from audittrace.identity import UserContext
from audittrace.services.console_store import PostgresConsoleStore
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
        row = await store.upsert(alice, KEY_A, {"payload": "alice-secret"})
        raw = {r["id"]: r for r in await _raw(store, harness)}
        # snapshot_columns (cached) still names session_id, and the row
        # actually carries the column — a live re-read of model would have
        # flipped has_session_id() to False and stopped stamping it.
        assert "session_id" in raw[row["id"]], (
            "a live re-read of model would have dropped session_id from "
            "the cached snapshot/stamping shape"
        )
        assert row["id"] is not None


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
