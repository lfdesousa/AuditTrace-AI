"""``MockConsoleStore[T]`` — the shared in-process implementation.

Same domain descriptor, same sealed template helpers, same invariants as
:class:`PostgresConsoleStore`; only the storage differs (a list of plain
row dicts). ``_scoped_rows`` is the Mock's guarded query builder: every
read starts from ``user_sub == <sub>`` and composes the domain's equality
filters and the key INTO it — dropping that predicate makes the same
two-sub tests go RED against the Mock that go RED against Postgres.

The Mock exists so route/unit tests that do not wire a Postgres factory
still run the REAL base logic (stamping, D13, cap, cursor); it is not a
stub that "documents that it doesn't enforce isolation".
"""

from __future__ import annotations

import builtins
import logging
from collections.abc import Mapping, Sequence
from operator import itemgetter
from typing import Any, TypeVar, final

from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.services.console_store._base import ConsoleStoreBase
from audittrace.services.console_store._cursor import row_after_cursor
from audittrace.services.console_store._domain import ConsoleDomain
from audittrace.services.console_store._errors import ConsoleStoreCapExceededError

logger = logging.getLogger(__name__)

T = TypeVar("T")


class MockConsoleStore(ConsoleStoreBase[T]):
    """In-memory store parameterized by a domain (unit tests / bypass mode)."""

    def __init__(self, domain: ConsoleDomain[T]) -> None:
        super().__init__(domain)
        self._rows: list[dict[str, Any]] = []

    def reset(self) -> None:
        self._rows.clear()

    # ── the guarded query builder (sealed) ───────────────────────────────

    @final
    def _scoped_rows(
        self,
        user_sub: str,
        *,
        key: Mapping[str, Any] | None = None,
        active_only: bool = True,
    ) -> builtins.list[dict[str, Any]]:
        filters = self._filters()
        out: list[dict[str, Any]] = []
        for row in self._rows:
            if row["user_sub"] != user_sub:
                continue
            if active_only and row["deleted_at_ms"] is not None:
                continue
            if any(row[c] != v for c, v in filters.items()):
                continue
            if key is not None and any(row[c] != v for c, v in key.items()):
                continue
            out.append(row)
        return out

    @final
    def _snapshot(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {column: row[column] for column in self._domain.snapshot_columns()}

    @final
    def _assign(self, row: dict[str, Any], assignment: Mapping[str, Any]) -> None:
        row.update(assignment)

    def _ordered(self, rows: Sequence[dict[str, Any]]) -> builtins.list[dict[str, Any]]:
        out = list(rows)
        for column, direction in reversed(self._domain.order_by):
            out.sort(key=itemgetter(column), reverse=(direction == "desc"))
        return out

    # ── public contract ──────────────────────────────────────────────────

    @log_call(logger=logger)
    async def get(self, user_context: UserContext, key: Mapping[str, Any]) -> T | None:
        user_sub = self._read_sub(user_context)
        validated_key = self._validated_key(key)
        rows = self._scoped_rows(user_sub, key=validated_key)
        return None if not rows else self._to_item(self._snapshot(rows[0]))

    @log_call(logger=logger)
    async def list(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> tuple[builtins.list[T], str | None]:
        user_sub = self._read_sub(user_context)
        effective_limit = self._clamp_limit(limit)
        cursor_values = self._cursor_values(cursor)
        rows = self._ordered(self._scoped_rows(user_sub))
        if cursor_values is not None:
            columns = self._domain.order_columns()
            directions = self._domain.order_directions()
            rows = [
                row
                for row in rows
                if row_after_cursor(
                    [row[c] for c in columns], cursor_values, directions
                )
            ]
        snapshots = [self._snapshot(row) for row in rows[: effective_limit + 1]]
        return self._page(snapshots, effective_limit)

    @log_call(logger=logger)
    async def upsert(
        self,
        user_context: UserContext,
        key: Mapping[str, Any],
        values: Mapping[str, Any],
    ) -> T:
        stamp = self._write_stamp(user_context)
        validated_key = self._validated_key(key)
        validated_values = self._validated_values(values)
        active = self._scoped_rows(stamp.user_sub, key=validated_key)
        if active:
            row = active[0]
            self._assign(
                row,
                self._update_values(
                    stamp, self._snapshot(row), validated_values, resurrect=False
                ),
            )
            return self._to_item(self._snapshot(row))
        cap = self._cap()
        if cap is not None and len(self._scoped_rows(stamp.user_sub)) >= cap:
            raise ConsoleStoreCapExceededError(self._domain.name, cap)
        tombstoned = self._scoped_rows(
            stamp.user_sub, key=validated_key, active_only=False
        )
        if tombstoned:
            row = tombstoned[0]
            self._assign(
                row,
                self._update_values(
                    stamp, self._snapshot(row), validated_values, resurrect=True
                ),
            )
            return self._to_item(self._snapshot(row))
        row = self._insert_values(stamp, validated_key, validated_values)
        self._rows.append(row)
        return self._to_item(self._snapshot(row))

    @log_call(logger=logger)
    async def delete(self, user_context: UserContext, key: Mapping[str, Any]) -> bool:
        stamp = self._write_stamp(user_context)
        validated_key = self._validated_key(key)
        active = self._scoped_rows(stamp.user_sub, key=validated_key)
        if not active:
            return False
        self._assign(active[0], self._delete_values(stamp))
        return True

    @log_call(logger=logger)
    async def batch_get(
        self, user_context: UserContext, keys: Sequence[Mapping[str, Any]]
    ) -> builtins.list[T | None]:
        user_sub = self._read_sub(user_context)
        validated_keys = self._validated_keys(keys)
        out: list[T | None] = []
        for validated_key in validated_keys:
            rows = self._scoped_rows(user_sub, key=validated_key)
            out.append(None if not rows else self._to_item(self._snapshot(rows[0])))
        return out

    @log_call(logger=logger)
    async def count(self, user_context: UserContext) -> int:
        user_sub = self._read_sub(user_context)
        return len(self._scoped_rows(user_sub))
