"""Shared FIXTURE domain for the console-store base tests (not a test module).

A synthetic ``WidgetRow`` ORM model on its OWN ``DeclarativeBase`` (so it
never leaks into ``audittrace.db.models.Base`` / the Alembic chain), plus
three descriptors over it:

* :class:`WidgetDomain` — plain, uncapped, unfiltered;
* :class:`CappedWidgetDomain` — ``cap() == 3`` (the tool-favorites cap
  pattern, small enough to hit in a test);
* :class:`PinnedOnlyWidgetDomain` — ``equality_filters() == {"kind":
  "pinned"}`` (a narrowing domain filter).

The model carries BOTH ``trace_id`` and ``session_id`` so the base's M5
stamping of both columns is provable here even before the M5 retrofit adds
``session_id`` to the real console tables.

:class:`SqliteHarness` gives the Postgres implementation a real aiosqlite
engine on a temp file and, crucially, ``raw_rows()`` — a read that BYPASSES
the guarded API and returns every row of every user, so side-effect
assertions ("alice's row is still alice's", "no row was written") are made
against the truth, not against the API under test.
"""

from __future__ import annotations

import atexit
import os
import tempfile
from collections.abc import Mapping
from typing import Any

from sqlalchemy import BigInteger, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from audittrace.services.console_store import ConsoleDomain


class FixtureBase(DeclarativeBase):
    """Own metadata — deliberately NOT ``audittrace.db.models.Base``."""


class WidgetRow(FixtureBase):
    __tablename__ = "console_store_test_widgets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_sub: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deleted_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_sub", "kind", "name", name="uq_widgets_user_kind_name"),
    )


WIDGET_COLUMNS: tuple[str, ...] = (
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


class WidgetDomain(ConsoleDomain[dict[str, Any]]):
    name = "console_store_test_widgets"
    model = WidgetRow
    key_columns = ("kind", "name")
    value_columns = ("payload", "priority")
    order_by = (("updated_at_ms", "desc"), ("kind", "desc"), ("name", "desc"))
    default_list_limit = 25
    max_list_limit = 50

    def defaults(self, key: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"payload": None, "priority": 0}

    def to_item(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return dict(row)


class CappedWidgetDomain(WidgetDomain):
    name = "console_store_test_widgets_capped"

    def cap(self) -> int | None:
        return 3


class PinnedOnlyWidgetDomain(WidgetDomain):
    name = "console_store_test_widgets_pinned"

    def equality_filters(self) -> Mapping[str, Any]:
        return {"kind": "pinned"}


class SqliteHarness:
    """A real aiosqlite engine + the fixture table, owned by the TEST."""

    def __init__(self, *, expire_on_commit: bool = False) -> None:
        tmp = tempfile.NamedTemporaryFile(
            prefix="console_store_test_", suffix=".sqlite", delete=False
        )
        tmp.close()
        self._path = tmp.name
        atexit.register(self._unlink)
        self.engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{self._path}",
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )
        self.factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self.engine, expire_on_commit=expire_on_commit
        )

    def _unlink(self) -> None:
        try:
            os.unlink(self._path)
        except OSError:
            pass

    async def create(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(FixtureBase.metadata.create_all)

    async def raw_rows(self) -> list[dict[str, Any]]:
        """EVERY row of EVERY user — bypasses the guarded API on purpose."""
        async with self.factory() as session:
            rows = (
                (
                    await session.execute(
                        select(WidgetRow).order_by(
                            WidgetRow.created_at_ms, WidgetRow.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [{c: getattr(r, c) for c in WIDGET_COLUMNS} for r in rows]

    async def dispose(self) -> None:
        await self.engine.dispose()
        self._unlink()
