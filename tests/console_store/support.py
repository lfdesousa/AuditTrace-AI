"""Plain (non-fixture) helpers shared across ``tests/console_store/*``
(NOT a test module; fixtures live in ``conftest.py`` — see that module's
docstring for why the split).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from audittrace.services.console_store import (
    ConsoleDomain,
    ConsoleStoreBase,
    MockConsoleStore,
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


def _builders(harness: Any) -> list[Any]:
    from audittrace.services.console_store import PostgresConsoleStore

    return [
        lambda d: MockConsoleStore(d),
        lambda d: PostgresConsoleStore(d, harness.factory),
    ]


async def _raw(
    store: ConsoleStoreBase[dict[str, Any]], harness: Any
) -> list[dict[str, Any]]:
    if isinstance(store, MockConsoleStore):
        return [dict(r) for r in store._rows]
    return await harness.raw_rows()
