"""No-raw-resource-on-the-surface probes for ``services/console_store``
(split from ``test_console_store_hostile.py``, fix round 1 A3 — that file
exceeded the PYTHON-ENGINEERING §11 500-LOC trigger).

Proves the store never exposes a session factory / engine attribute, that
digging out the guarded session opener still anchors to the token-resolved
sub, that a domain never receives the store, and that no public hook
signature exposes a session parameter.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from audittrace.db.rls import set_current_user_id
from audittrace.identity import UserContext
from audittrace.services.console_store import ConsoleDomain, ConsoleStoreScopeError
from audittrace.services.console_store._postgres import (
    PostgresConsoleStore,
    _GuardedSessions,
)
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
