"""No-raw-resource-on-the-surface probes for ``services/console_store``
(split from ``test_console_store_hostile.py``, fix round 1 A3 — that file
exceeded the PYTHON-ENGINEERING §11 500-LOC trigger).

Proves the store never exposes a session factory / engine attribute, that
digging out the guarded session opener still anchors to the token-resolved
sub, that a domain never receives the store, and that no public hook
signature exposes a session parameter.

**Fix round 1 self-attack finding (2026-09-17):** ``store._sessions`` is
already disclosed-reachable (the test above proves the opener stays
token-anchored even when dug out); ``TestGuardedSessionsOpenerSealed``
below proves the SLOT ITSELF (``_sessions._open``, the closure holding the
entire guarded-opener logic) cannot be reassigned once dug out — closing
a second-hop mutation surface in the exact shape F1 and Guard D closed for
the domain descriptor (``__slots__`` limits WHICH attributes may exist; it
does not, by itself, make an existing one write-once).
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy.ext.asyncio import AsyncSession

from audittrace.db.rls import set_current_user_id
from audittrace.identity import UserContext
from audittrace.services.console_store import (
    ConsoleDomain,
    ConsoleStoreScopeError,
    ConsoleStoreSealedError,
)
from audittrace.services.console_store._postgres import (
    PostgresConsoleStore,
    _GuardedSessions,
)
from tests.console_store.support import KEY_A, _raw
from tests.console_store_fixture_domain import SqliteHarness, WidgetDomain


class TestNoRawResourceOnTheSurface:
    def test_store_holds_no_session_factory_or_engine_attribute(
        self, harness: SqliteHarness
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        exposed = {k for k in vars(store)} | set(dir(store))
        for needle in (
            "session_factory",
            "_session_factory",
            "engine",
            "_engine",
            "session",
        ):
            assert needle not in exposed, (
                f"{needle!r} is attribute-reachable on the store"
            )
        # F1 (fix round 1): the cached ``_model`` attribute was found to be
        # reassignable (``store._model = Evil`` swapped the ORM class every
        # query is built against — the exact "narrow the seam" defect F1
        # exists to close) and removed entirely; every read now goes
        # through ``self._domain.model`` (already sealed).
        assert not hasattr(store, "_model"), (
            "the redundant, previously-reassignable _model cache is back"
        )
        assert _GuardedSessions.__slots__ == ("_open",)
        assert not hasattr(store._sessions, "__dict__")

    async def test_even_the_guarded_opener_is_anchored_to_the_token(
        self, harness: SqliteHarness, alice: UserContext, bob: UserContext
    ) -> None:
        """Whoever digs out ``_sessions`` still cannot open a session for a
        sub other than the request's: the opener re-resolves the sub from
        the UserContext and cross-checks the RLS ContextVar."""
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        set_current_user_id("bob-sub")
        with pytest.raises(ConsoleStoreScopeError):
            async with store._sessions.get_session_scoped(alice):
                pass
        async with store._sessions.get_session_scoped(bob) as session:
            assert isinstance(session, AsyncSession)

    def test_domain_never_receives_the_store(self, harness: SqliteHarness) -> None:
        domain = WidgetDomain()
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            domain, harness.factory
        )
        assert vars(domain) == {}
        assert store.domain is domain

    def test_no_public_hook_exposes_a_session(self) -> None:
        import inspect

        for name in ("cap", "defaults", "merge", "equality_filters", "to_item"):
            params = inspect.signature(getattr(ConsoleDomain, name)).parameters
            for p in params.values():
                assert "session" not in p.name.lower()


class TestGuardedSessionsOpenerSealed:
    """Guard E (fix round 1, 2026-09-17 self-attack finding). Independent
    of Guard B (``store._sessions = evil`` — the store-level write-once
    check): this is about the slot INSIDE the already-dug-out
    ``_GuardedSessions`` object."""

    async def test_reassigning_the_opener_slot_is_refused(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        original = store._sessions._open
        with pytest.raises(ConsoleStoreSealedError, match="cannot be reassigned"):
            store._sessions._open = lambda user_context: None  # type: ignore[assignment]
        assert store._sessions._open is original, (
            "the blocked reassignment attempt still took effect"
        )
        # Business continues normally: a real write still opens a properly
        # token-anchored, RLS-scoped session (raw DB read as witness).
        tracer = TracerProvider().get_tracer("guard-e-opener-reassignment")
        with tracer.start_as_current_span("alice-write") as span:
            row = await store.upsert(alice, KEY_A, {"payload": "alice-secret"})
            expected_trace = format(span.get_span_context().trace_id, "032x")
        raw = {r["id"]: r for r in await _raw(store, harness)}
        assert raw[row["id"]]["trace_id"] == expected_trace

    async def test_deleting_the_opener_slot_is_refused(
        self, harness: SqliteHarness, alice: UserContext
    ) -> None:
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )
        with pytest.raises(ConsoleStoreSealedError, match="cannot be deleted"):
            del store._sessions._open
        async with store._sessions.get_session_scoped(alice) as session:
            assert isinstance(session, AsyncSession)

    async def test_neutering_the_opener_seal_lets_it_be_swapped(
        self,
        harness: SqliteHarness,
        alice: UserContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-vacuity: with ``_GuardedSessions.__setattr__`` neutered to
        plain ``object.__setattr__`` (the ORIGINAL, pre-Guard-E shape), the
        SAME reassignment the tests above refuse instead succeeds and
        replaces the ENTIRE token-anchored, RLS-scoped opener with
        whatever the caller supplies — a concrete, checkable side effect: a
        session that never pushes the RLS GUC and returns None instead of
        a real ``AsyncSession``."""
        monkeypatch.setattr(_GuardedSessions, "__setattr__", object.__setattr__)
        store: PostgresConsoleStore[dict[str, Any]] = PostgresConsoleStore(
            WidgetDomain(), harness.factory
        )

        async def _evil_open(user_context: UserContext) -> Any:
            yield None  # never resolves a sub, never touches RLS

        from contextlib import asynccontextmanager

        store._sessions._open = asynccontextmanager(_evil_open)  # type: ignore[assignment]
        async with store._sessions.get_session_scoped(alice) as session:
            assert session is None, (
                "neutering the seal should have let the opener be replaced"
            )
