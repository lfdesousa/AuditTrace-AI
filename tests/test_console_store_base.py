"""Base-level invariant tests for ``services/console_store`` (WU-A).

Every invariant the base OWNS is proven here ONCE, against BOTH shared
implementations (``MockConsoleStore`` and ``PostgresConsoleStore`` on a
real aiosqlite engine), through the same parametrized ``sut`` fixture. A
domain migrated onto the base inherits these guarantees; it does not
re-prove them.

**Side-effect discipline** (``feedback_vacuous_neuter_test_antipattern``):
every isolation assertion is made against ``raw_rows()`` — a read that
BYPASSES the guarded API and returns every row of every user — never only
against the API under test. "bob got ``None``" is paired with "alice's
row is still alice's, unchanged".

Neuter map (each base invariant → the test that goes RED when it is removed):

| invariant (where)                                   | RED test                                                   |
|-----------------------------------------------------|------------------------------------------------------------|
| RLS predicate in ``_scoped_select`` / ``_scoped_rows`` | ``TestRlsReadsAreUserScoped`` + ``TestRlsWritesAreUserScoped`` (7 tests) |
| user-scoped COUNT (``_scoped_count`` / ``len(_scoped_rows)``) | ``test_cap_is_per_user_not_global``, ``test_count_is_user_scoped`` |
| trace_id / session_id stamp (``_insert/_update/_delete_values``) | ``TestStamping``                                        |
| D13 tombstone lookup + un-tombstone (``upsert``)    | ``TestSoftDeleteThenRecreate``                             |
| reserved-column refusal (``_validated_*``)          | ``TestCallerMetadataRejected``                             |
| ContextVar cross-check (``resolve_user_sub``)       | ``TestScopeAnchoring``                                     |
| serialize-inside-session (``_snapshot`` before close/commit) | ``TestSessionScopeDiscipline``                       |
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

import pytest
import pytest_asyncio
from opentelemetry.sdk.trace import TracerProvider

from audittrace.db.rls import set_current_user_id
from audittrace.identity import UserContext
from audittrace.services.console_store import (
    ConsoleDomain,
    ConsoleStoreBase,
    ConsoleStoreCapExceededError,
    ConsoleStoreDomainError,
    ConsoleStoreForbiddenFieldError,
    ConsoleStoreScopeError,
    MockConsoleStore,
    PostgresConsoleStore,
    bind_session_id,
    current_trace_id_hex,
    decode_cursor,
    encode_cursor,
)
from audittrace.services.console_store._cursor import coercer_for, row_after_cursor
from tests.console_store_fixture_domain import (
    CappedWidgetDomain,
    PinnedOnlyWidgetDomain,
    SqliteHarness,
    WidgetDomain,
    WidgetRow,
)

KEY_A = {"kind": "tool", "name": "web-search"}
KEY_B = {"kind": "tool", "name": "calculator"}
KEY_C = {"kind": "skill", "name": "summarize"}


@dataclass
class Sut:
    """A store built on one backend + a raw (unguarded) reader for it."""

    backend: str
    store: ConsoleStoreBase[dict[str, Any]]
    raw_rows: Callable[[], Awaitable[list[dict[str, Any]]]]
    make: Callable[[ConsoleDomain[dict[str, Any]]], Sut]


def _mock_sut(domain: ConsoleDomain[dict[str, Any]]) -> Sut:
    store: MockConsoleStore[dict[str, Any]] = MockConsoleStore(domain)

    async def raw() -> list[dict[str, Any]]:
        return [dict(r) for r in store._rows]

    return Sut("mock", store, raw, _mock_sut)


@pytest_asyncio.fixture(params=["mock", "sqlite"])
async def sut(request: pytest.FixtureRequest) -> Any:
    if request.param == "mock":
        yield _mock_sut(WidgetDomain())
        return
    harness = SqliteHarness()
    await harness.create()

    def make(domain: ConsoleDomain[dict[str, Any]]) -> Sut:
        return Sut(
            "sqlite",
            PostgresConsoleStore(domain, harness.factory),
            harness.raw_rows,
            make,
        )

    try:
        yield make(WidgetDomain())
    finally:
        await harness.dispose()


@pytest.fixture
def alice(user_context: UserContext) -> UserContext:
    return replace(user_context, user_id="alice-sub", is_admin=False)


@pytest.fixture
def bob(user_context: UserContext) -> UserContext:
    return replace(user_context, user_id="bob-sub", is_admin=False)


@pytest.fixture(autouse=True)
def _clear_request_context() -> Any:
    set_current_user_id(None)
    bind_session_id(None)
    yield
    set_current_user_id(None)
    bind_session_id(None)


# ── round trip ────────────────────────────────────────────────────────────


class TestRoundTrip:
    async def test_upsert_get_list_round_trip(
        self, sut: Sut, alice: UserContext
    ) -> None:
        created = await sut.store.upsert(alice, KEY_A, {"payload": "p1", "priority": 7})
        assert created["kind"] == "tool" and created["name"] == "web-search"
        assert created["user_sub"] == "alice-sub"
        assert created["payload"] == "p1" and created["priority"] == 7
        assert created["deleted_at_ms"] is None
        assert created["created_at_ms"] == created["updated_at_ms"]

        got = await sut.store.get(alice, KEY_A)
        assert got == created
        items, cursor = await sut.store.list(alice)
        assert items == [created] and cursor is None
        assert await sut.store.count(alice) == 1

    async def test_defaults_hook_fills_omitted_value_columns(
        self, sut: Sut, alice: UserContext
    ) -> None:
        created = await sut.store.upsert(alice, KEY_A, {})
        assert created["payload"] is None and created["priority"] == 0

    async def test_upsert_existing_updates_same_row(
        self, sut: Sut, alice: UserContext
    ) -> None:
        first = await sut.store.upsert(alice, KEY_A, {"payload": "p1"})
        second = await sut.store.upsert(alice, KEY_A, {"priority": 3})
        assert second["id"] == first["id"]
        assert second["created_at_ms"] == first["created_at_ms"]
        assert second["updated_at_ms"] >= first["updated_at_ms"]
        assert second["payload"] == "p1" and second["priority"] == 3
        assert len(await sut.raw_rows()) == 1

    async def test_batch_get_aligns_to_input_order(
        self, sut: Sut, alice: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {})
        await sut.store.upsert(alice, KEY_C, {})
        got = await sut.store.batch_get(alice, [KEY_C, KEY_B, KEY_A])
        assert [None if g is None else g["name"] for g in got] == [
            "summarize",
            None,
            "web-search",
        ]
        assert await sut.store.batch_get(alice, []) == []

    async def test_domain_property_exposes_descriptor(self, sut: Sut) -> None:
        assert isinstance(sut.store.domain, WidgetDomain)


# ── RLS reads ─────────────────────────────────────────────────────────────


class TestRlsReadsAreUserScoped:
    async def test_get_never_returns_another_users_row(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {"payload": "alice-secret"})
        assert await sut.store.get(bob, KEY_A) is None, (
            "bob read alice's row — the user_sub predicate is missing/neutered"
        )
        raw = await sut.raw_rows()
        assert [(r["user_sub"], r["payload"]) for r in raw] == [
            ("alice-sub", "alice-secret")
        ]

    async def test_list_never_includes_another_users_row(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {})
        await sut.store.upsert(alice, KEY_B, {})
        items, _ = await sut.store.list(bob)
        assert items == [], "bob's list included alice's rows"
        alice_items, _ = await sut.store.list(alice)
        assert len(alice_items) == 2

    async def test_batch_get_never_includes_another_users_row(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {})
        assert await sut.store.batch_get(bob, [KEY_A]) == [None]
        assert (await sut.store.batch_get(alice, [KEY_A]))[0] is not None

    async def test_count_is_user_scoped(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {})
        await sut.store.upsert(alice, KEY_B, {})
        assert await sut.store.count(bob) == 0, "bob's COUNT included alice's rows"
        assert await sut.store.count(alice) == 2


# ── RLS writes ────────────────────────────────────────────────────────────


class TestRlsWritesAreUserScoped:
    async def test_upsert_same_key_creates_own_row_never_touches_other_users(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {"payload": "alice"})
        bob_row = await sut.store.upsert(bob, KEY_A, {"payload": "bob"})
        assert bob_row["user_sub"] == "bob-sub"
        raw = sorted(await sut.raw_rows(), key=lambda r: r["user_sub"])
        assert [(r["user_sub"], r["payload"]) for r in raw] == [
            ("alice-sub", "alice"),
            ("bob-sub", "bob"),
        ], "bob's upsert overwrote alice's row (existence lookup unscoped)"

    async def test_delete_never_tombstones_another_users_row(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {})
        assert await sut.store.delete(bob, KEY_A) is False
        raw = await sut.raw_rows()
        assert raw[0]["user_sub"] == "alice-sub" and raw[0]["deleted_at_ms"] is None, (
            "bob soft-deleted alice's row"
        )

    async def test_upsert_never_resurrects_another_users_tombstone(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        alice_row = await sut.store.upsert(alice, KEY_A, {"payload": "alice"})
        assert await sut.store.delete(alice, KEY_A) is True
        bob_row = await sut.store.upsert(bob, KEY_A, {"payload": "bob"})
        assert bob_row["id"] != alice_row["id"]
        raw = {r["id"]: r for r in await sut.raw_rows()}
        assert raw[alice_row["id"]]["deleted_at_ms"] is not None, (
            "bob's upsert un-tombstoned ALICE's row (tombstone lookup unscoped)"
        )
        assert raw[alice_row["id"]]["payload"] == "alice"
        assert raw[bob_row["id"]]["user_sub"] == "bob-sub"


# ── D13 soft-delete then re-create ────────────────────────────────────────


class TestSoftDeleteThenRecreate:
    async def test_delete_then_upsert_untombstones_the_same_row(
        self, sut: Sut, alice: UserContext
    ) -> None:
        first = await sut.store.upsert(alice, KEY_A, {"payload": "v1"})
        assert await sut.store.delete(alice, KEY_A) is True
        assert await sut.store.get(alice, KEY_A) is None
        assert await sut.store.count(alice) == 0

        again = await sut.store.upsert(alice, KEY_A, {"priority": 9})
        assert again["id"] == first["id"], "re-create must reuse the tombstoned row"
        assert again["deleted_at_ms"] is None, "tombstone was not cleared (D13)"
        assert again["payload"] == "v1" and again["priority"] == 9
        assert len(await sut.raw_rows()) == 1
        assert await sut.store.count(alice) == 1

    async def test_deleted_row_invisible_everywhere(
        self, sut: Sut, alice: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {})
        await sut.store.delete(alice, KEY_A)
        assert await sut.store.get(alice, KEY_A) is None
        assert (await sut.store.list(alice))[0] == []
        assert await sut.store.batch_get(alice, [KEY_A]) == [None]
        assert await sut.store.count(alice) == 0

    async def test_delete_is_idempotent(self, sut: Sut, alice: UserContext) -> None:
        assert await sut.store.delete(alice, KEY_A) is False
        await sut.store.upsert(alice, KEY_A, {})
        assert await sut.store.delete(alice, KEY_A) is True
        assert await sut.store.delete(alice, KEY_A) is False


# ── cap hook ──────────────────────────────────────────────────────────────


class TestCapHook:
    async def test_cap_blocks_transition_into_active_at_limit(
        self, sut: Sut, alice: UserContext
    ) -> None:
        capped = sut.make(CappedWidgetDomain())
        for i in range(3):
            await capped.store.upsert(alice, {"kind": "tool", "name": f"n{i}"}, {})
        with pytest.raises(ConsoleStoreCapExceededError) as excinfo:
            await capped.store.upsert(alice, {"kind": "tool", "name": "n3"}, {})
        assert excinfo.value.cap == 3
        assert excinfo.value.domain == "console_store_test_widgets_capped"
        assert len(await capped.raw_rows()) == 3

    async def test_reaffirm_active_row_at_cap_is_allowed(
        self, sut: Sut, alice: UserContext
    ) -> None:
        capped = sut.make(CappedWidgetDomain())
        for i in range(3):
            await capped.store.upsert(alice, {"kind": "tool", "name": f"n{i}"}, {})
        updated = await capped.store.upsert(
            alice, {"kind": "tool", "name": "n0"}, {"priority": 5}
        )
        assert updated["priority"] == 5

    async def test_cap_is_per_user_not_global(
        self, sut: Sut, alice: UserContext, bob: UserContext
    ) -> None:
        capped = sut.make(CappedWidgetDomain())
        for i in range(3):
            await capped.store.upsert(alice, {"kind": "tool", "name": f"n{i}"}, {})
        try:
            created = await capped.store.upsert(bob, {"kind": "tool", "name": "b0"}, {})
        except ConsoleStoreCapExceededError as exc:
            pytest.fail(
                "bob's FIRST upsert was refused because ALICE is at the cap — "
                f"the cap COUNT is not user-scoped: {exc}"
            )
        assert created["user_sub"] == "bob-sub"
        assert await capped.store.count(bob) == 1

    async def test_tombstoned_rows_do_not_count_but_resurrect_is_capped(
        self, sut: Sut, alice: UserContext
    ) -> None:
        capped = sut.make(CappedWidgetDomain())
        await capped.store.upsert(alice, {"kind": "tool", "name": "gone"}, {})
        await capped.store.delete(alice, {"kind": "tool", "name": "gone"})
        for i in range(3):
            await capped.store.upsert(alice, {"kind": "tool", "name": f"n{i}"}, {})
        with pytest.raises(ConsoleStoreCapExceededError):
            await capped.store.upsert(alice, {"kind": "tool", "name": "gone"}, {})


# ── M5 stamping ───────────────────────────────────────────────────────────


def _trace_hex(span: Any) -> str:
    return format(span.get_span_context().trace_id, "032x")


class TestStamping:
    async def test_trace_id_stamped_on_insert_update_and_delete(
        self, sut: Sut, alice: UserContext
    ) -> None:
        tracer = TracerProvider().get_tracer("console-store-tests")
        seen: list[str] = []
        with tracer.start_as_current_span("insert") as span:
            await sut.store.upsert(alice, KEY_A, {})
            seen.append(_trace_hex(span))
        raw = await sut.raw_rows()
        assert raw[0]["trace_id"] == seen[0], "insert did not stamp the active trace_id"

        with tracer.start_as_current_span("update") as span:
            await sut.store.upsert(alice, KEY_A, {"priority": 1})
            seen.append(_trace_hex(span))
        raw = await sut.raw_rows()
        assert raw[0]["trace_id"] == seen[1] != seen[0], "update did not re-stamp"

        with tracer.start_as_current_span("delete") as span:
            await sut.store.delete(alice, KEY_A)
            seen.append(_trace_hex(span))
        raw = await sut.raw_rows()
        assert raw[0]["trace_id"] == seen[2] != seen[1], "delete did not re-stamp"

    async def test_trace_id_is_none_when_no_span_is_active_during_the_write(
        self, sut: Sut, alice: UserContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The M5 "never fabricated" property at the STORE level.

        Root-cause note (full-suite run 2026-09-13): two unrelated tests
        install a global SDK ``TracerProvider`` and leave ``telemetry.
        _tracer`` initialised, so the base's own ``@log_call`` aspect opens
        a REAL span around ``upsert`` and the base (correctly) stamps its
        id. That is the observability environment, not the guard. Here the
        aspect's tracer is neutralised and the INVALID span is made current,
        so NO span is active during the write — and the row MUST carry
        ``None``. Neuter ``build_write_stamp``'s trace source (or the
        stamp) and this stays green only if the base fabricates an id,
        which the probe test below would then catch."""
        from opentelemetry import trace

        from audittrace import telemetry

        monkeypatch.setattr(telemetry, "_tracer", None)
        with trace.use_span(trace.INVALID_SPAN, end_on_exit=False):
            await sut.store.upsert(alice, KEY_A, {})
        assert (await sut.raw_rows())[0]["trace_id"] is None, (
            "a trace_id was stamped with no span active — fabricated ids "
            "look reconstructable and are not"
        )

    async def test_stamped_trace_id_is_exactly_the_one_active_during_the_write(
        self, sut: Sut, alice: UserContext
    ) -> None:
        """Deterministic in EVERY telemetry state: a probe hook records what
        ``current_trace_id_hex()`` reports INSIDE the write; the stored
        value must equal it (None when nothing is active, the active span's
        id otherwise) — never a different or invented id."""
        seen: list[str | None] = []

        class ProbeDomain(WidgetDomain):
            name = "trace_probe"

            def defaults(self, key: Any) -> Any:
                seen.append(current_trace_id_hex())
                return {"payload": None, "priority": 0}

        probe = sut.make(ProbeDomain())
        await probe.store.upsert(alice, KEY_A, {})
        assert len(seen) == 1
        assert (await probe.raw_rows())[0]["trace_id"] == seen[0]

    def test_current_trace_id_hex_is_none_without_a_valid_span(self) -> None:
        """Deterministic regardless of whether another test installed a
        global SDK TracerProvider: with the INVALID span current there is
        nothing to stamp, so the source reports ``None`` (the laptop
        telemetry-no-op default)."""
        from opentelemetry import trace

        with trace.use_span(trace.INVALID_SPAN, end_on_exit=False):
            assert current_trace_id_hex() is None

    async def test_session_id_stamped_from_request_context(
        self, sut: Sut, alice: UserContext
    ) -> None:
        bind_session_id("run-42")
        await sut.store.upsert(alice, KEY_A, {})
        assert (await sut.raw_rows())[0]["session_id"] == "run-42"
        bind_session_id("run-43")
        await sut.store.upsert(alice, KEY_A, {"priority": 2})
        assert (await sut.raw_rows())[0]["session_id"] == "run-43"
        bind_session_id("run-44")
        await sut.store.delete(alice, KEY_A)
        assert (await sut.raw_rows())[0]["session_id"] == "run-44", (
            "delete did not re-stamp session_id"
        )
        bind_session_id(None)
        await sut.store.upsert(alice, KEY_B, {})
        assert (await sut.raw_rows())[1]["session_id"] is None


# ── caller-metadata rejection ─────────────────────────────────────────────


class TestCallerMetadataRejected:
    @pytest.mark.parametrize(
        "field",
        ["user_sub", "trace_id", "session_id", "deleted_at_ms", "id", "created_at_ms"],
    )
    async def test_reserved_column_in_values_is_refused_before_any_write(
        self, sut: Sut, alice: UserContext, field: str
    ) -> None:
        with pytest.raises(ConsoleStoreForbiddenFieldError):
            await sut.store.upsert(alice, KEY_A, {field: "forged"})
        assert await sut.raw_rows() == [], "a refused body still wrote a row"

    async def test_reserved_column_in_key_is_refused(
        self, sut: Sut, alice: UserContext
    ) -> None:
        with pytest.raises(ConsoleStoreForbiddenFieldError):
            await sut.store.get(alice, {**KEY_A, "user_sub": "bob-sub"})
        with pytest.raises(ConsoleStoreForbiddenFieldError):
            await sut.store.delete(alice, {**KEY_A, "user_sub": "bob-sub"})

    async def test_unknown_value_column_is_refused(
        self, sut: Sut, alice: UserContext
    ) -> None:
        with pytest.raises(ValueError, match="unknown value column"):
            await sut.store.upsert(alice, KEY_A, {"colour": "red"})

    async def test_key_column_cannot_be_changed_through_values(
        self, sut: Sut, alice: UserContext
    ) -> None:
        with pytest.raises(ValueError, match="unknown value column"):
            await sut.store.upsert(alice, KEY_A, {"name": "other"})

    @pytest.mark.parametrize(
        "bad_key",
        [
            {"kind": "tool"},
            {"kind": "tool", "name": None},
            {"kind": "t", "name": "n", "x": 1},
        ],
    )
    async def test_malformed_key_is_refused(
        self, sut: Sut, alice: UserContext, bad_key: dict[str, Any]
    ) -> None:
        with pytest.raises(ValueError):
            await sut.store.get(alice, bad_key)

    async def test_non_mapping_key_and_values_are_refused(
        self, sut: Sut, alice: UserContext
    ) -> None:
        with pytest.raises(ValueError):
            await sut.store.get(alice, ["tool", "web-search"])  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await sut.store.upsert(alice, KEY_A, ["p"])  # type: ignore[arg-type]

    async def test_batch_get_is_bounded(self, sut: Sut, alice: UserContext) -> None:
        too_many = [{"kind": "tool", "name": f"n{i}"} for i in range(51)]
        with pytest.raises(ValueError, match="at most 50"):
            await sut.store.batch_get(alice, too_many)


# ── ContextVar anchoring ──────────────────────────────────────────────────


class TestScopeAnchoring:
    async def test_mismatch_with_rls_contextvar_is_refused_before_any_write(
        self, sut: Sut, alice: UserContext
    ) -> None:
        set_current_user_id("bob-sub")
        with pytest.raises(ConsoleStoreScopeError):
            await sut.store.upsert(alice, KEY_A, {})
        assert await sut.raw_rows() == [], "a refused identity still wrote a row"

    async def test_mismatch_refuses_reads_too(
        self, sut: Sut, alice: UserContext
    ) -> None:
        await sut.store.upsert(alice, KEY_A, {})
        set_current_user_id("bob-sub")
        for call in (
            lambda: sut.store.get(alice, KEY_A),
            lambda: sut.store.list(alice),
            lambda: sut.store.batch_get(alice, [KEY_A]),
            lambda: sut.store.count(alice),
            lambda: sut.store.delete(alice, KEY_A),
        ):
            with pytest.raises(ConsoleStoreScopeError):
                await call()

    async def test_matching_contextvar_is_allowed(
        self, sut: Sut, alice: UserContext
    ) -> None:
        set_current_user_id("alice-sub")
        await sut.store.upsert(alice, KEY_A, {})
        assert (await sut.store.get(alice, KEY_A)) is not None

    async def test_empty_user_id_is_refused(
        self, sut: Sut, user_context: UserContext
    ) -> None:
        nobody = replace(user_context, user_id="   ")
        with pytest.raises(ConsoleStoreScopeError):
            await sut.store.list(nobody)


# ── pagination ────────────────────────────────────────────────────────────


class TestPagination:
    async def test_pages_walk_every_row_once(
        self, sut: Sut, alice: UserContext
    ) -> None:
        for i in range(5):
            await sut.store.upsert(alice, {"kind": "tool", "name": f"n{i}"}, {})
        seen: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            items, cursor = await sut.store.list(alice, cursor=cursor, limit=2)
            seen += [i["name"] for i in items]
            pages += 1
            if cursor is None:
                break
        assert pages == 3 and sorted(seen) == [f"n{i}" for i in range(5)]
        assert len(seen) == len(set(seen))

    async def test_order_is_newest_first_with_key_tiebreak(
        self, sut: Sut, alice: UserContext
    ) -> None:
        for name in ("a", "b", "c"):
            await sut.store.upsert(alice, {"kind": "tool", "name": name}, {})
        items, _ = await sut.store.list(alice)
        stamps = [(i["updated_at_ms"], i["kind"], i["name"]) for i in items]
        assert stamps == sorted(stamps, reverse=True)

    @pytest.mark.parametrize(
        "cursor",
        [
            "garbage!!",
            encode_cursor([1]),
            encode_cursor([1, "tool"]),
            encode_cursor(["1", "tool", "n"]),
            encode_cursor([None, "tool", "n"]),
            encode_cursor([True, "tool", "n"]),
            encode_cursor([1, 2, "n"]),
        ],
    )
    async def test_invalid_cursor_raises_value_error(
        self, sut: Sut, alice: UserContext, cursor: str
    ) -> None:
        with pytest.raises(ValueError, match="invalid cursor"):
            await sut.store.list(alice, cursor=cursor)

    async def test_limit_is_clamped(self, sut: Sut, alice: UserContext) -> None:
        for i in range(3):
            await sut.store.upsert(alice, {"kind": "tool", "name": f"n{i}"}, {})
        items, cursor = await sut.store.list(alice, limit=0)
        assert len(items) == 1 and cursor is not None
        items, cursor = await sut.store.list(alice, limit=9999)
        assert len(items) == 3 and cursor is None


# ── domain equality filters ───────────────────────────────────────────────


class TestEqualityFilters:
    async def test_domain_filter_narrows_every_read(
        self, sut: Sut, alice: UserContext
    ) -> None:
        pinned = sut.make(PinnedOnlyWidgetDomain())
        await pinned.store.upsert(alice, {"kind": "pinned", "name": "p"}, {})
        await pinned.store.upsert(alice, {"kind": "tool", "name": "t"}, {})
        items, _ = await pinned.store.list(alice)
        assert [i["kind"] for i in items] == ["pinned"]
        assert await pinned.store.count(alice) == 1
        assert await pinned.store.get(alice, {"kind": "tool", "name": "t"}) is None
        assert len(await pinned.raw_rows()) == 2


# ── #364 session scope ────────────────────────────────────────────────────


class TestSessionScopeDiscipline:
    async def test_store_survives_attribute_expiry_on_commit(
        self, alice: UserContext
    ) -> None:
        """With ``expire_on_commit=True`` every ORM attribute is expired after
        ``commit()``; reading one outside the session (or after commit in an
        async session) fails. The base serializes INSIDE the session and
        BEFORE commit, so it does not depend on ``expire_on_commit=False``."""
        harness = SqliteHarness(expire_on_commit=True)
        await harness.create()
        try:
            store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
                WidgetDomain(), harness.factory
            )
            created = await store.upsert(alice, KEY_A, {"payload": "x"})
            assert created["payload"] == "x"
            updated = await store.upsert(alice, KEY_A, {"priority": 4})
            assert updated["priority"] == 4
            assert (await store.get(alice, KEY_A)) == updated
            assert (await store.list(alice))[0] == [updated]
            assert await store.delete(alice, KEY_A) is True
        finally:
            await harness.dispose()

    async def test_upsert_failure_rolls_back_and_wraps(
        self, alice: UserContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = SqliteHarness()
        await harness.create()
        try:
            store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
                WidgetDomain(), harness.factory
            )

            async def _boom(*_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("boom")

            monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncSession.commit", _boom)
            with pytest.raises(
                RuntimeError, match=r"console_store_test_widgets\.upsert\(.*failed"
            ):
                await store.upsert(alice, KEY_A, {})
            monkeypatch.undo()
            assert await harness.raw_rows() == []
        finally:
            await harness.dispose()


# ── descriptor validation ─────────────────────────────────────────────────


def _domain(**overrides: Any) -> ConsoleDomain[dict[str, Any]]:
    attrs: dict[str, Any] = {
        "name": "d",
        "model": WidgetRow,
        "key_columns": ("kind", "name"),
        "value_columns": ("payload", "priority"),
        "order_by": (("created_at_ms", "asc"), ("id", "asc")),
        "to_item": lambda self, row: dict(row),
    }
    attrs.update(overrides)
    cls = type("D", (ConsoleDomain,), attrs)
    return cls()  # type: ignore[no-any-return]


class TestDomainValidation:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"name": ""},
            {"model": "not-a-class"},
            {"key_columns": ()},
            {"key_columns": ("kind", "")},
            {"key_columns": ("kind", "user_sub")},
            {"value_columns": ("payload", "trace_id")},
            {"value_columns": ("kind",)},
            {"key_columns": ("kind", "kind")},
            {"value_columns": ("payload", "payload")},
            {"value_columns": ("nope",)},
            {"order_by": ()},
            {"order_by": ("created_at_ms",)},
            {"order_by": (("payload", "asc"), ("id", "asc"))},
            {"order_by": (("id", "sideways"),)},
            {"order_by": (("created_at_ms", "asc"),)},
            {"default_list_limit": 0},
            {"default_list_limit": "25"},
            {"max_list_limit": 10, "default_list_limit": 25},
            {"cap": lambda self: 0},
            {"cap": lambda self: True},
            {"cap": lambda self: "3"},
            {"equality_filters": lambda self: {"user_sub": "x"}},
            {"equality_filters": lambda self: ["kind"]},
        ],
    )
    def test_invalid_descriptor_is_refused_at_construction(
        self, overrides: dict[str, Any]
    ) -> None:
        with pytest.raises(ConsoleStoreDomainError):
            MockConsoleStore(_domain(**overrides))

    async def test_default_hooks_apply_when_a_domain_overrides_none(
        self, alice: UserContext
    ) -> None:
        store: MockConsoleStore[dict[str, Any]] = MockConsoleStore(_domain())
        created = await store.upsert(alice, KEY_A, {"payload": "p"})
        assert created["payload"] == "p" and created["priority"] is None
        assert await store.count(alice) == 1

    def test_non_domain_is_refused(self) -> None:
        with pytest.raises(ConsoleStoreDomainError):
            MockConsoleStore(object())  # type: ignore[arg-type]

    def test_unsupported_ordering_type_is_refused(self) -> None:
        class Odd:
            id = WidgetRow.id
            user_sub = WidgetRow.user_sub
            created_at_ms = WidgetRow.created_at_ms
            updated_at_ms = WidgetRow.updated_at_ms
            deleted_at_ms = WidgetRow.deleted_at_ms
            trace_id = WidgetRow.trace_id
            kind = WidgetRow.kind
            name = WidgetRow.name
            payload = WidgetRow.payload
            priority = WidgetRow.priority
            when = object()

        with pytest.raises(ConsoleStoreDomainError, match="unsupported type"):
            MockConsoleStore(
                _domain(
                    model=Odd, key_columns=("kind", "when"), order_by=(("when", "asc"),)
                )
            )


# ── cursor helpers ────────────────────────────────────────────────────────


class TestCursorHelpers:
    def test_round_trip(self) -> None:
        values = [1700000000000, "tool", "web-search"]
        assert decode_cursor(encode_cursor(values), [int, str, str]) == values

    def test_row_after_cursor_honours_directions(self) -> None:
        assert row_after_cursor([5, "b"], [5, "a"], ["desc", "desc"]) is False
        assert row_after_cursor([5, "a"], [5, "b"], ["desc", "desc"]) is True
        assert row_after_cursor([4, "z"], [5, "a"], ["desc", "asc"]) is True
        assert row_after_cursor([5, "a"], [5, "a"], ["asc", "asc"]) is False
        assert row_after_cursor([6], [5], ["asc"]) is True

    def test_coercer_for_rejects_other_types(self) -> None:
        with pytest.raises(TypeError):
            coercer_for(float)
