"""M5 stamping, caller-metadata rejection and ContextVar-anchoring
invariants for ``services/console_store`` (split from
``test_console_store_base.py``, fix round 1 A3 — that file exceeded the
PYTHON-ENGINEERING §11 500-LOC trigger).

Neuter map (each base invariant → the test that goes RED when it is removed):

| invariant (where)                                             | RED test                       |
|----------------------------------------------------------------|--------------------------------|
| trace_id / session_id stamp (``_insert/_update/_delete_values``) | ``TestStamping``             |
| reserved-column refusal (``_validated_*``)                     | ``TestCallerMetadataRejected`` |
| ContextVar cross-check (``resolve_user_sub``)                  | ``TestScopeAnchoring``         |
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider

from audittrace.db.rls import set_current_user_id
from audittrace.identity import UserContext
from audittrace.services.console_store import (
    ConsoleStoreForbiddenFieldError,
    ConsoleStoreScopeError,
)
from audittrace.services.console_store._context import current_trace_id_hex
from tests.console_store.support import KEY_A, KEY_B, Sut
from tests.console_store_fixture_domain import WidgetDomain


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
        from audittrace.services.console_store import bind_session_id

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
