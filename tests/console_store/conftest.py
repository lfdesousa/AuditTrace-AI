"""Shared pytest FIXTURES for the ``tests/console_store/`` package (mirrors
the ``tests/bff/`` precedent: a directory-scoped ``conftest.py`` for a
cohesive test family, rather than importing fixture functions into every
module's namespace — the latter works functionally (pytest fixture
resolution honours a name bound anywhere in a module's namespace) but ruff
F811 cannot tell an imported fixture apart from a same-named test-function
PARAMETER and flags every single test signature as a "redefinition"; a real
conftest.py has no such import to collide with, since pytest injects its
fixtures without the test module ever binding the name itself.

``sut`` (parametrized ``mock``/``sqlite``) backs the base-invariant family;
``harness``/``alice``/``bob``/the autouse context-clearing fixtures back
BOTH the base-invariant and hostile-probe families — one definition, no
drift between the two (PYTHON-ENGINEERING §11 decomposition, fix round 1
A3: the two pre-split monoliths exceeded the 500-LOC review trigger).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
import pytest_asyncio

from audittrace.db.rls import set_current_user_id
from audittrace.identity import UserContext
from audittrace.services.console_store import (
    ConsoleDomain,
    MockConsoleStore,
    PostgresConsoleStore,
    bind_session_id,
)
from tests.console_store.support import Sut
from tests.console_store_fixture_domain import SqliteHarness, WidgetDomain


@pytest_asyncio.fixture
async def harness() -> Any:
    h = SqliteHarness()
    await h.create()
    try:
        yield h
    finally:
        await h.dispose()


@pytest.fixture
def alice(user_context: UserContext) -> UserContext:
    return replace(user_context, user_id="alice-sub", is_admin=False)


@pytest.fixture
def bob(user_context: UserContext) -> UserContext:
    return replace(user_context, user_id="bob-sub", is_admin=False)


@pytest.fixture(autouse=True)
def _clear_context() -> Any:
    """Used by the hostile-probe family (only ``user_sub`` matters there)."""
    set_current_user_id(None)
    yield
    set_current_user_id(None)


@pytest.fixture(autouse=True)
def _clear_request_context() -> Any:
    """Used by the base-invariant family (session_id matters there too)."""
    set_current_user_id(None)
    bind_session_id(None)
    yield
    set_current_user_id(None)
    bind_session_id(None)


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
