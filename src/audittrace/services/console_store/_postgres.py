"""``PostgresConsoleStore[T]`` — the shared SQLAlchemy-async implementation.

Every method follows the same template, in this order:

1. resolve the request context (``_read_sub`` / ``_write_stamp``) and
   validate the caller's key/values — ALL before any I/O, so a refused
   request never opens a session;
2. open a session through ``_GuardedSessions`` — the session factory lives
   in a closure (no ``_session_factory`` attribute on the store) and every
   session it yields is ALREADY scoped at the DB layer via
   ``set_config('app.current_user_id', <sub>, true)`` (``db/rls.py``), so
   even a caller who digs the opener out gets a Postgres session RLS-bound
   to the token-anchored ``sub``;
3. build the query with ``_scoped_select`` — the ONE guarded builder: it
   always starts from ``user_sub == <sub>`` (+ ``deleted_at_ms IS NULL``
   for active reads) and composes the domain's equality filters and the
   key INTO it with AND;
4. serialize INSIDE the session (``_snapshot`` → plain dict) — no ORM row
   outlives its session (#364); the domain's ``to_item`` only ever sees the
   dict copy.

Async model: this store is ``async`` + asyncpg, the same contract as every
``async_sessionmaker`` in ``dependencies.py`` — the console tier is
network-I/O bound (PYTHON-ENGINEERING §3). The factory is process-resident
and injected, never built per call (§2).

Falsifiability of the guarded builder: drop the ``user_sub`` predicate from
``_scoped_select`` and every two-sub test in ``tests/console_store/
test_base_crud.py`` / ``test_base_pagination.py`` / ``test_base_stamping.py``
observes another user's row (list/get/upsert-existence/tombstone/
delete/batch_get/count — each has its own side-effect assertion).
"""

from __future__ import annotations

import builtins
import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, TypeVar, final

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.db.rls import set_rls_user_id
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.services.console_store._base import ConsoleStoreBase
from audittrace.services.console_store._context import resolve_user_sub
from audittrace.services.console_store._cursor import keyset_predicate
from audittrace.services.console_store._domain import ConsoleDomain
from audittrace.services.console_store._errors import (
    ConsoleStoreCapExceededError,
    ConsoleStoreError,
    ConsoleStoreSealedError,
)

logger = logging.getLogger(__name__)

# SPEC ADDENDUM C advisory A-R7 (fix round 2, 2026-09-17): the names
# _GuardedSessions's own class-level seal must protect — mirrors
# SEALED_STORE_MEMBERS / _SEALED_DOMAIN_CLASS_ATTRS in the same package.
_SEALED_GUARDED_SESSIONS_MEMBERS: frozenset[str] = frozenset(
    {"get_session_scoped", "__setattr__", "__delattr__", "__class__"}
)


class _GuardedSessionsMeta(type):
    """Refuse a CLASS-level ``setattr``/``delattr`` naming a guarded-session
    member (SPEC ADDENDUM C advisory A-R7, fix round 2). The INSTANCE-level
    ``__setattr__``/``__delattr__`` below seal every attribute on an
    already-built ``_GuardedSessions`` object, but a class-level write
    (``_GuardedSessions.get_session_scoped = evil``) is dispatched to the
    *metaclass*, not the instance method — plain ``type`` (the metaclass
    ``_GuardedSessions`` used before this fix) does not intercept it, so
    that ordinary one-line reassignment replaced the token-anchored opener
    for every instance built from this class, present and future. Same
    class of asymmetry Guard D closes for the domain descriptor, and
    R2/R4 close for the store/domain metaclasses.

    Falsifiable: neuter this and ``_GuardedSessions.get_session_scoped =
    lambda self, user_context: None`` succeeds and every store's session
    opener returns ``None`` instead of a scoped session.
    """

    def __setattr__(cls, name: str, value: Any) -> None:
        if name in _SEALED_GUARDED_SESSIONS_MEMBERS:
            raise ConsoleStoreSealedError(f"{cls.__qualname__}.{name} is sealed")
        super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:
        if name in _SEALED_GUARDED_SESSIONS_MEMBERS:
            raise ConsoleStoreSealedError(f"{cls.__qualname__}.{name} is sealed")
        super().__delattr__(name)


class _GuardedSessions(metaclass=_GuardedSessionsMeta):
    """Owns the injected session factory and yields ONLY DB-scoped sessions.

    The factory is captured by the closure in ``__init__`` and is not an
    attribute (``__slots__`` fixes the attribute set). What remains
    reachable is ``get_session_scoped(user_context)``, which re-resolves the
    token-derived ``sub`` (cross-checked against the RLS ContextVar) and pushes
    it as the Postgres RLS GUC before yielding — on Postgres with a NOBYPASSRLS app
    role the DB itself then hides every other user's row. On SQLite (unit
    tests only) there is no RLS; the app-level ``_scoped_select`` predicate
    is the load-bearing guard there, which is why the RLS assertions also
    run against a real Postgres (``tests/test_console_store_rls_postgres.py``).

    **F1-round self-attack finding (2026-09-17), same class as the domain
    descriptor's "second hop": ``__slots__`` limits WHICH attributes may
    exist, it does not make an existing one write-once.** ``store._sessions``
    is already a disclosed-reachable object (``test_no_raw_resource.py``
    proves the opener stays token-anchored even when dug out); before this
    round, nothing stopped ``store._sessions._open = evil`` — an ordinary,
    single-line reassignment of the slot holding the ENTIRE guarded-opener
    closure, replacing token-anchoring and RLS-GUC-pushing with anything the
    caller likes. Reproduced directly before this fix (see the build
    record). ``__setattr__``/``__delattr__`` below refuse every ORDINARY
    attribute set/delete on an INSTANCE, mirroring :class:`~audittrace.
    services.console_store._domain.ConsoleDomain`'s instance seal;
    ``_GuardedSessionsMeta`` above closes the matching CLASS-level hop
    (SPEC ADDENDUM C A-R7); ``__init__`` uses ``object.__setattr__`` once,
    the same bypass every seal in this package uses for its OWN one-time
    initialization.

    Residual, disclosed: ``__closure__`` introspection on the stored
    function can recover the factory, and direct ``object.__setattr__`` /
    ``type.__setattr__`` / ``__dict__``-shaped access still writes —
    deliberate circumvention, not a hurry-mode path.
    """

    __slots__ = ("_open",)

    # Class-level annotation (not a class-level VALUE — no default is
    # given) so mypy knows the slot exists: __init__ below assigns it via
    # ``object.__setattr__``, not ``self._open = ...``, which is otherwise
    # mypy's only signal that a slotted attribute is defined.
    _open: Callable[[UserContext], AbstractAsyncContextManager[AsyncSession]]

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        @asynccontextmanager
        async def _open(user_context: UserContext) -> AsyncIterator[AsyncSession]:
            # Anchored: the sub is re-resolved from the token-derived
            # identity (and cross-checked against the RLS ContextVar) HERE,
            # so the opener cannot be pointed at an arbitrary sub string.
            user_sub = resolve_user_sub(user_context)
            async with session_factory() as session:
                await set_rls_user_id(session, user_sub)
                yield session

        object.__setattr__(self, "_open", _open)

    def __setattr__(self, name: str, value: Any) -> None:
        raise ConsoleStoreSealedError(
            f"{type(self).__qualname__}.{name} is set once at construction "
            "and cannot be reassigned"
        )

    def __delattr__(self, name: str) -> None:
        raise ConsoleStoreSealedError(
            f"{type(self).__qualname__}.{name} cannot be deleted"
        )

    def get_session_scoped(
        self, user_context: UserContext
    ) -> AbstractAsyncContextManager[AsyncSession]:
        return self._open(user_context)


T = TypeVar("T")


class PostgresConsoleStore(ConsoleStoreBase[T]):
    """PostgreSQL/SQLAlchemy-async store parameterized by a domain."""

    def __init__(
        self,
        domain: ConsoleDomain[T],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__(domain)
        self._sessions = _GuardedSessions(session_factory)

    # ── the guarded query builder (sealed) ───────────────────────────────

    @final
    def _scoped_select(
        self,
        user_sub: str,
        *,
        key: Mapping[str, Any] | None = None,
        active_only: bool = True,
    ) -> Select[Any]:
        model = self._contract.model
        stmt = select(model).where(model.user_sub == user_sub)
        if active_only:
            stmt = stmt.where(model.deleted_at_ms.is_(None))
        for column, value in self._filters().items():
            stmt = stmt.where(getattr(model, column) == value)
        if key is not None:
            for column, value in key.items():
                stmt = stmt.where(getattr(model, column) == value)
        return stmt

    @final
    def _scoped_count(self, user_sub: str) -> Select[Any]:
        """The user-scoped aggregate — a COUNT over the guarded select's
        own subquery, so it cannot carry a different predicate."""
        return select(func.count()).select_from(
            self._scoped_select(user_sub).subquery()
        )

    @final
    def _snapshot(self, row: Any) -> dict[str, Any]:
        return {
            column: getattr(row, column) for column in self._contract.snapshot_columns
        }

    @final
    def _assign(self, row: Any, assignment: Mapping[str, Any]) -> None:
        for column, value in assignment.items():
            setattr(row, column, value)

    def _ordered(self, stmt: Select[Any]) -> Select[Any]:
        model = self._contract.model
        clauses = [
            getattr(model, column).asc()
            if direction == "asc"
            else getattr(model, column).desc()
            for column, direction in self._contract.order_by
        ]
        return stmt.order_by(*clauses)

    # ── public contract ──────────────────────────────────────────────────

    @log_call(logger=logger)
    async def get(self, user_context: UserContext, key: Mapping[str, Any]) -> T | None:
        user_sub = self._read_sub(user_context)
        validated_key = self._validated_key(key)
        async with self._sessions.get_session_scoped(user_context) as session:
            row = (
                await session.execute(self._scoped_select(user_sub, key=validated_key))
            ).scalar_one_or_none()
            snapshot = None if row is None else self._snapshot(row)
        return None if snapshot is None else self._to_item(snapshot)

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
        stmt = self._scoped_select(user_sub)
        if cursor_values is not None:
            columns = [
                getattr(self._contract.model, c) for c in self._contract.order_columns
            ]
            stmt = stmt.where(
                keyset_predicate(
                    columns, self._contract.order_directions, cursor_values
                )
            )
        stmt = self._ordered(stmt).limit(effective_limit + 1)
        async with self._sessions.get_session_scoped(user_context) as session:
            rows = (await session.execute(stmt)).scalars().all()
            snapshots = [self._snapshot(row) for row in rows]
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
        async with self._sessions.get_session_scoped(user_context) as session:
            try:
                active = (
                    await session.execute(
                        self._scoped_select(stamp.user_sub, key=validated_key)
                    )
                ).scalar_one_or_none()
                if active is not None:
                    self._assign(
                        active,
                        self._update_values(
                            stamp,
                            self._snapshot(active),
                            validated_values,
                            resurrect=False,
                        ),
                    )
                    snapshot = self._snapshot(active)
                    await session.commit()
                else:
                    cap = self._cap()
                    if cap is not None:
                        active_count = (
                            await session.execute(self._scoped_count(stamp.user_sub))
                        ).scalar_one()
                        if active_count >= cap:
                            raise ConsoleStoreCapExceededError(self._contract.name, cap)
                    tombstoned = (
                        await session.execute(
                            self._scoped_select(
                                stamp.user_sub, key=validated_key, active_only=False
                            )
                        )
                    ).scalar_one_or_none()
                    if tombstoned is not None:
                        self._assign(
                            tombstoned,
                            self._update_values(
                                stamp,
                                self._snapshot(tombstoned),
                                validated_values,
                                resurrect=True,
                            ),
                        )
                        row = tombstoned
                    else:
                        row = self._contract.model(
                            **self._insert_values(
                                stamp, validated_key, validated_values
                            )
                        )
                        session.add(row)
                    snapshot = self._snapshot(row)
                    await session.commit()
            except ConsoleStoreError:
                await session.rollback()
                raise
            except Exception as exc:
                await session.rollback()
                raise RuntimeError(
                    f"{self._contract.name}.upsert({validated_key!r}) failed: {exc}"
                ) from exc
        return self._to_item(snapshot)

    @log_call(logger=logger)
    async def delete(self, user_context: UserContext, key: Mapping[str, Any]) -> bool:
        stamp = self._write_stamp(user_context)
        validated_key = self._validated_key(key)
        async with self._sessions.get_session_scoped(user_context) as session:
            row = (
                await session.execute(
                    self._scoped_select(stamp.user_sub, key=validated_key)
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            self._assign(row, self._delete_values(stamp))
            await session.commit()
            return True

    @log_call(logger=logger)
    async def batch_get(
        self, user_context: UserContext, keys: Sequence[Mapping[str, Any]]
    ) -> builtins.list[T | None]:
        user_sub = self._read_sub(user_context)
        validated_keys = self._validated_keys(keys)
        if not validated_keys:
            return []
        model = self._contract.model
        key_columns = self._contract.key_columns
        stmt = self._scoped_select(user_sub).where(
            or_(
                *[
                    and_(*[getattr(model, c) == k[c] for c in key_columns])
                    for k in validated_keys
                ]
            )
        )
        async with self._sessions.get_session_scoped(user_context) as session:
            rows = (await session.execute(stmt)).scalars().all()
            snapshots = [self._snapshot(row) for row in rows]
        index = {tuple(s[c] for c in key_columns): s for s in snapshots}
        out: list[T | None] = []
        for k in validated_keys:
            found = index.get(tuple(k[c] for c in key_columns))
            out.append(None if found is None else self._to_item(found))
        return out

    @log_call(logger=logger)
    async def count(self, user_context: UserContext) -> int:
        user_sub = self._read_sub(user_context)
        async with self._sessions.get_session_scoped(user_context) as session:
            total = (await session.execute(self._scoped_count(user_sub))).scalar_one()
            return int(total)
