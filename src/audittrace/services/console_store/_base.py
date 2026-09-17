"""``ConsoleStoreBase[T]`` — the store contract plus the SEALED template
helpers both implementations share.

The public API (``get`` / ``list`` / ``upsert`` / ``delete`` /
``batch_get`` / ``count``) is abstract here and concrete in
:class:`PostgresConsoleStore` / :class:`MockConsoleStore`. Everything that
makes those methods safe is CONCRETE here and sealed:

* ``_read_sub`` / ``_write_stamp`` — the ONLY sources of ``user_sub``,
  ``trace_id`` and ``session_id`` (request context, never a body).
* ``_validated_key`` / ``_validated_values`` / ``_validated_keys`` — refuse
  reserved (server-stamped) columns and unknown columns BEFORE any I/O.
* ``_insert_values`` / ``_update_values`` / ``_delete_values`` — the only
  builders of column assignments; they stamp the reserved columns by
  direct assignment (never ``setdefault``) and iterate the CACHED
  ``self._contract.value_columns``, never a live re-read (F6, SPEC
  ADDENDUM D — see ``_domain.py``'s "Fifth hop").
* ``_page`` / ``_cursor_values`` — the shared keyset pagination.

Sealing (``_sealing.seal_subclass``) refuses any subclass defined outside
this package and any in-package redefinition of the members listed in
``SEALED_STORE_MEMBERS``. ``@final`` covers the type checker; the seal
covers runtime. Every OTHER instance attribute (``_domain``, and on
:class:`PostgresConsoleStore`, ``_sessions``) is write-once: set exactly
once in ``__init__`` and refused on any later reassignment (``__setattr__``
below) — ``_domain``'s check was found + closed during the WU-A ADDENDUM A
pass; ``_sessions`` (and a since-removed ``_model`` cache — see
``tests/console_store/test_no_raw_resource.py``) was found during this fix
round's F1 "enumerate reachable MUTATION" pass (same class of hole, not
previously enumerated). All are exercised by
``tests/console_store/test_sealed_classes.py`` +
``tests/console_store/test_domain_descriptor_sealed.py``.

**F1 (fix round 1, 2026-09-15): the domain DESCRIPTOR is now also sealed**
(``_domain.py::ConsoleDomain.__setattr__``) — closing the pointer here was
not enough, because ``ConsoleStoreBase.domain`` hands out the descriptor
BY REFERENCE and a read-only property protects only the binding, never the
referent. See ``_domain.py`` for the defect and the fix.
``_refuse_reserved_value_columns`` (below) is a SECOND, LOAD-BEARING guard
at the point every write path trusts ``value_columns`` (now ``self.
_contract.value_columns`` — SPEC ADDENDUM D) — see its own docstring for
why it is not "defence in depth" (SPEC ADDENDUM C R5: a fix-round-1
reviewer reached its protected code with ZERO monkeypatch, via the "third
hop" ``_domain.py`` documents; the label this module carried before that
round mislabelled it, inviting its own deletion — THE INVERSE).

**SPEC ADDENDUM C (fix round 2, 2026-09-17), R2: this metaclass's own
hooks are now sealed too.** ``_SealedMeta`` guarded ``SEALED_STORE_MEMBERS``
but not ``__setattr__``, ``__delattr__`` or ``__class__`` themselves, so
``PostgresConsoleStore.__setattr__ = object.__setattr__`` (an ordinary,
un-enumerated one-line class attribute write) disabled the metaclass, and
``store._scoped_select = unscoped`` then reached another user's row
THROUGH THE PUBLIC API — same for ``PostgresConsoleStore.__class__ =
ABCMeta``, which removes ``_SealedMeta`` from dispatch entirely. Both
routes are now refused by adding those three names to
``SEALED_STORE_MEMBERS`` itself, so the same check that guards every other
sealed member guards the guard.

**SPEC ADDENDUM D (fix round 3, 2026-09-17), R1/R3: MUTATION vs VALUE, a
second axis.** Every guard above answers "can this be MUTATED?" (axis 1).
F6 (``_domain.py``, "Fifth hop") showed that is half the question for a
domain's declarative surfaces: axis 2 is "does a read always return the
SAME value, and what PINS that?" :func:`~audittrace.services.console_store.
_domain_validate.validate_domain` reads each one EXACTLY ONCE into a
:class:`~audittrace.services.console_store._domain_validate.
DomainContract`; ``__init__`` caches it as ``self._contract`` (write-once,
same guard as ``self._domain``/``self._sessions``), and every sealed
helper below reads ``self._contract.X``, never ``self._domain.X`` again.
Axis 1's guards stay independently load-bearing for OTHER consumers of
``store.domain`` (introspection, a future caller). Full per-surface table
(all seven fields, both axes): the fix-round-3 evidence file — not
duplicated here to keep this module under the PYTHON-ENGINEERING §11
500-LOC trigger.
"""

from __future__ import annotations

import builtins
import logging
import uuid
from abc import ABC, ABCMeta, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, Generic, TypeVar, final

from audittrace.identity import UserContext
from audittrace.services.console_store._context import (
    RESERVED_COLUMNS,
    WriteStamp,
    build_write_stamp,
    resolve_user_sub,
)
from audittrace.services.console_store._cursor import (
    coercer_for,
    decode_cursor,
    encode_cursor,
)
from audittrace.services.console_store._domain import ConsoleDomain
from audittrace.services.console_store._domain_validate import (
    validate_cap,
    validate_domain,
    validate_equality_filters,
)
from audittrace.services.console_store._errors import (
    ConsoleStoreDomainError,
    ConsoleStoreForbiddenFieldError,
    ConsoleStoreSealedError,
)
from audittrace.services.console_store._sealing import seal_subclass

logger = logging.getLogger(__name__)

SEALED_STORE_MEMBERS: frozenset[str] = frozenset(
    {
        "_read_sub",
        "_write_stamp",
        "_validated_key",
        "_validated_keys",
        "_validated_values",
        "_hook_output",
        "_refuse_reserved_value_columns",
        "_insert_values",
        "_update_values",
        "_delete_values",
        "_clamp_limit",
        "_cursor_values",
        "_page",
        "_to_item",
        "_cap",
        "_filters",
        # implementation-level guarded builders (sealed even in-package)
        "_scoped_select",
        "_scoped_count",
        "_scoped_rows",
        "_snapshot",
        "_assign",
        # SPEC ADDENDUM C R2 (fix round 2): the seal's OWN hooks — a guard
        # whose hooks can be reassigned is not a guard, it is a default.
        # Sealing these closes both the class-level metaclass-swap route
        # (``Store.__class__ = ABCMeta``) and the seal-dunder-reassignment
        # route (``Store.__setattr__ = object.__setattr__``), each of
        # which produced a cross-user read through the public API.
        "__setattr__",
        "__delattr__",
        "__class__",
    }
)


class _SealedMeta(ABCMeta):
    """Refuse ``StoreClass._sealed_member = ...`` at the CLASS level — the
    runtime monkeypatch route around ``@final``. Also refuses reassigning
    the seal's OWN hooks (``__setattr__``, ``__delattr__``, ``__class__`` —
    SPEC ADDENDUM C R2, fix round 2): before that fix, ``StoreClass.
    __setattr__ = object.__setattr__`` or ``StoreClass.__class__ = ABCMeta``
    disabled this metaclass in one line and let a subsequent
    ``store._scoped_select = unscoped`` reach another user's row through
    the public API. (``type.__setattr__(cls, ...)`` called directly still
    works; disclosed residual.)"""

    def __setattr__(cls, name: str, value: Any) -> None:
        if name in SEALED_STORE_MEMBERS:
            raise ConsoleStoreSealedError(f"{cls.__qualname__}.{name} is sealed")
        super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:
        if name in SEALED_STORE_MEMBERS:
            raise ConsoleStoreSealedError(f"{cls.__qualname__}.{name} is sealed")
        super().__delattr__(name)


T = TypeVar("T")


# PEP 484 Generic, not PEP 695 native `class ConsoleStoreBase[T](...)`: the
# repo's pre-commit mypy hook is pinned to v1.8.0 (predates PEP 695 support)
# and CI has no separate mypy step, so that pinned hook is the only
# mechanically-enforced mypy gate — ruff's pyupgrade (UP046) suggests the
# newer syntax anyway (target-version py312), so this is a disclosed,
# narrow conflict between the two pinned tools, not an oversight.
class ConsoleStoreBase(Generic[T], ABC, metaclass=_SealedMeta):  # noqa: UP046
    """Abstract console store parameterized by a :class:`ConsoleDomain`."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        seal_subclass(cls, sealed_members=SEALED_STORE_MEMBERS)

    @final
    def __setattr__(self, name: str, value: Any) -> None:
        """Refuse shadowing a sealed member on an INSTANCE (``store.
        _scoped_select = evil``), AND refuse reassigning ANY instance
        attribute once it has been set (write-once) — ``store._domain =
        evil``, but equally ``store._sessions = evil`` on
        :class:`~audittrace.services.console_store._postgres.
        PostgresConsoleStore`.

        Generalised (fix round 1, F1 enumeration) from a ``_domain``-only
        check: ``_domain`` is not itself a template helper, so it is not in
        ``SEALED_STORE_MEMBERS`` — but neither was ``_sessions``, and (at the
        time, on a NOW-REMOVED cached ``_model`` attribute — see
        ``tests/console_store/test_no_raw_resource.py``) was found, during
        this round's "enumerate reachable MUTATION, not reachable names"
        pass, to be reassignable exactly like ``_domain`` was (the
        now-removed ``store._model = Evil`` swapped the ORM class
        ``_scoped_select`` queried against — an unscoped read against
        whatever the caller pointed it at; proven by direct reproduction
        before this check was written, then the cache itself was deleted
        rather than kept and merely sealed). A domain/session-factory is
        validated or injected exactly ONCE, in ``__init__``; write-once for
        every instance attribute is the general form of that guarantee,
        closing the whole class rather than one name at a time. Falsifiable:
        neuter this branch and a store whose ``_domain``/``_sessions`` is
        swapped after construction accepts the swap.
        (``object.__setattr__`` / ``__dict__`` writes still work; disclosed
        residual, same family as the sealed-member one.)"""
        if name in SEALED_STORE_MEMBERS:
            raise ConsoleStoreSealedError(f"{type(self).__qualname__}.{name} is sealed")
        if name in self.__dict__:
            raise ConsoleStoreSealedError(
                f"{type(self).__qualname__}.{name} is set once at "
                "construction and cannot be reassigned"
            )
        super().__setattr__(name, value)

    def __init__(self, domain: ConsoleDomain[T]) -> None:
        # SPEC ADDENDUM D R1: self._contract caches the one-time read;
        # self._domain stays only for the DYNAMIC hooks (module docstring).
        self._contract = validate_domain(domain)
        self._domain = domain

    @property
    def domain(self) -> ConsoleDomain[T]:
        """The descriptor this store is parameterized by (read-only)."""
        return self._domain

    # ── public contract ──────────────────────────────────────────────────

    @abstractmethod
    async def get(self, user_context: UserContext, key: Mapping[str, Any]) -> T | None:
        """The caller's OWN active row for ``key``, or ``None`` (absent,
        soft-deleted, or another user's — indistinguishable by design)."""

    @abstractmethod
    async def list(
        self,
        user_context: UserContext,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> tuple[builtins.list[T], str | None]:
        """One page of the caller's OWN active rows in the domain's order,
        plus the opaque cursor for the next page (``None`` when done)."""

    @abstractmethod
    async def upsert(
        self,
        user_context: UserContext,
        key: Mapping[str, Any],
        values: Mapping[str, Any],
    ) -> T:
        """Create or update the caller's OWN row for ``key``. Re-creating a
        soft-deleted key un-tombstones the SAME row (D13). A genuine
        transition into "active" honours the domain cap."""

    @abstractmethod
    async def delete(self, user_context: UserContext, key: Mapping[str, Any]) -> bool:
        """Soft-delete the caller's OWN active row; ``False`` if none."""

    @abstractmethod
    async def batch_get(
        self, user_context: UserContext, keys: Sequence[Mapping[str, Any]]
    ) -> builtins.list[T | None]:
        """Caller's OWN active rows for ``keys``, aligned to input order."""

    @abstractmethod
    async def count(self, user_context: UserContext) -> int:
        """Number of the caller's OWN active rows (the user-scoped
        aggregate a cap is enforced against)."""

    # ── sealed template helpers ──────────────────────────────────────────

    @final
    def _read_sub(self, user_context: UserContext) -> str:
        return resolve_user_sub(user_context)

    @final
    def _write_stamp(self, user_context: UserContext) -> WriteStamp:
        return build_write_stamp(user_context)

    @final
    def _cap(self) -> int | None:
        return validate_cap(self._domain, name=self._contract.name)

    @final
    def _filters(self) -> dict[str, Any]:
        # SPEC ADDENDUM D R1: allow-list is the CACHED contract, not a
        # fresh self._domain.key_columns/value_columns read.
        return validate_equality_filters(
            self._domain,
            name=self._contract.name,
            key_columns=self._contract.key_columns,
            value_columns=self._contract.value_columns,
        )

    @final
    def _validated_key(self, key: Mapping[str, Any]) -> dict[str, Any]:
        """Exactly the domain's key columns, all non-null; reserved names
        are refused as caller metadata."""
        if not isinstance(key, Mapping):
            raise ValueError("key must be a mapping of key columns")
        reserved = sorted(set(key) & RESERVED_COLUMNS)
        if reserved:
            raise ConsoleStoreForbiddenFieldError(
                f"{self._contract.name}: key may not carry server-stamped "
                f"column(s): {reserved}"
            )
        expected = set(self._contract.key_columns)
        if set(key) != expected:
            raise ValueError(
                f"{self._contract.name}: key must name exactly "
                f"{sorted(expected)}, got {sorted(key)}"
            )
        if any(value is None for value in key.values()):
            raise ValueError(f"{self._contract.name}: key columns may not be None")
        return {column: key[column] for column in self._contract.key_columns}

    @final
    def _validated_keys(
        self, keys: Sequence[Mapping[str, Any]]
    ) -> builtins.list[dict[str, Any]]:
        if len(keys) > self._contract.max_list_limit:
            raise ValueError(
                f"{self._contract.name}: batch_get accepts at most "
                f"{self._contract.max_list_limit} keys"
            )
        return [self._validated_key(key) for key in keys]

    @final
    def _validated_values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Only value columns; reserved names are refused as caller
        metadata, key columns are immutable through values."""
        if not isinstance(values, Mapping):
            raise ValueError("values must be a mapping of value columns")
        reserved = sorted(set(values) & RESERVED_COLUMNS)
        if reserved:
            raise ConsoleStoreForbiddenFieldError(
                f"{self._contract.name}: values may not carry server-stamped "
                f"column(s): {reserved}"
            )
        unknown = sorted(set(values) - set(self._contract.value_columns))
        if unknown:
            raise ValueError(
                f"{self._contract.name}: unknown value column(s): {unknown}"
            )
        return dict(values)

    @final
    def _hook_output(self, produced: Any, *, hook: str) -> dict[str, Any]:
        """Validate what a domain hook returned: a mapping over value
        columns only. A reserved column here is a hook trying to stamp
        security/traceability fields — refused."""
        if not isinstance(produced, Mapping):
            raise ConsoleStoreDomainError(
                f"{self._contract.name}: hook {hook}() must return a mapping"
            )
        reserved = sorted(set(produced) & RESERVED_COLUMNS)
        if reserved:
            raise ConsoleStoreForbiddenFieldError(
                f"{self._contract.name}: hook {hook}() may not set server-stamped "
                f"column(s): {reserved}"
            )
        unknown = sorted(set(produced) - set(self._contract.value_columns))
        if unknown:
            raise ConsoleStoreDomainError(
                f"{self._contract.name}: hook {hook}() named unknown column(s): {unknown}"
            )
        return dict(produced)

    @final
    def _refuse_reserved_value_columns(self) -> None:
        """F1 item 2 — LOAD-BEARING, not defence in depth (SPEC ADDENDUM C
        R5, per THE INVERSE: neutering this one alone, before the "third
        hop" in ``_domain.py`` was closed, reached the FULL F1 harm with
        ZERO monkeypatch). Refuses, LOUDLY, if ``value_columns`` collides
        with a base-owned reserved column — whatever route got a bad
        descriptor there: a FUTURE construction path that skips
        ``validate_domain()`` (SPEC ADDENDUM D's disclosed residual) would
        reach this with nothing else in front of it. Checks the CACHED
        ``self._contract.value_columns`` — the SAME tuple ``_insert_
        values``'s row loop below actually uses, never a live
        ``self._domain.value_columns`` that could answer differently
        (F6, ``_domain.py``'s "Fifth hop")."""
        collision = sorted(set(self._contract.value_columns) & RESERVED_COLUMNS)
        if collision:
            raise ConsoleStoreForbiddenFieldError(
                f"{self._contract.name}: value_columns declares reserved "
                f"column(s) {collision} — refusing to let a domain silently "
                "overwrite a base-stamped column"
            )

    @final
    def _insert_values(
        self, stamp: WriteStamp, key: Mapping[str, Any], values: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Full column assignment for a brand-new row. Reserved columns
        come from ``stamp`` by direct assignment — a value or hook cannot
        reach them (both were validated to exclude reserved names)."""
        self._refuse_reserved_value_columns()
        defaults = self._hook_output(self._domain.defaults(dict(key)), hook="defaults")
        row: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "user_sub": stamp.user_sub,
            "created_at_ms": stamp.now_ms,
            "updated_at_ms": stamp.now_ms,
            "deleted_at_ms": None,
            "trace_id": stamp.trace_id,
        }
        if self._contract.has_session_id:
            row["session_id"] = stamp.session_id
        row.update(key)
        # F6: the CACHED tuple, not self._domain.value_columns (a hostile
        # property could answer differently here than at Guard C above).
        for column in self._contract.value_columns:
            row[column] = values[column] if column in values else defaults.get(column)
        return row

    @final
    def _update_values(
        self,
        stamp: WriteStamp,
        current: Mapping[str, Any],
        values: Mapping[str, Any],
        *,
        resurrect: bool,
    ) -> dict[str, Any]:
        """Column assignment for an update (or a D13 un-tombstone when
        ``resurrect``). The domain's ``merge`` hook sees ONLY the value
        columns of the current row."""
        self._refuse_reserved_value_columns()
        current_values = {c: current[c] for c in self._contract.value_columns}
        merged = self._hook_output(
            self._domain.merge(current_values, dict(values)), hook="merge"
        )
        out: dict[str, Any] = dict(merged)
        out["updated_at_ms"] = stamp.now_ms
        out["trace_id"] = stamp.trace_id
        if self._contract.has_session_id:
            out["session_id"] = stamp.session_id
        if resurrect:
            out["deleted_at_ms"] = None
        return out

    @final
    def _delete_values(self, stamp: WriteStamp) -> dict[str, Any]:
        out: dict[str, Any] = {
            "deleted_at_ms": stamp.now_ms,
            "updated_at_ms": stamp.now_ms,
            "trace_id": stamp.trace_id,
        }
        if self._contract.has_session_id:
            out["session_id"] = stamp.session_id
        return out

    @final
    def _clamp_limit(self, limit: int | None) -> int:
        if limit is None:
            return self._contract.default_list_limit
        return max(1, min(int(limit), self._contract.max_list_limit))

    @final
    def _cursor_values(self, cursor: str | None) -> builtins.list[Any] | None:
        if cursor is None:
            return None
        coercers = [
            coercer_for(getattr(self._contract.model, column).type.python_type)
            for column in self._contract.order_columns
        ]
        return decode_cursor(cursor, coercers)

    @final
    def _to_item(self, snapshot: Mapping[str, Any]) -> T:
        """Hand the domain the snapshot — a fresh plain dict built by the
        implementation's ``_snapshot`` (the single copying point), never the
        store's own row or an ORM instance."""
        return self._domain.to_item(snapshot)

    @final
    def _page(
        self, snapshots: Sequence[Mapping[str, Any]], effective_limit: int
    ) -> tuple[builtins.list[T], str | None]:
        has_more = len(snapshots) > effective_limit
        page = list(snapshots[:effective_limit])
        items = [self._to_item(snapshot) for snapshot in page]
        next_cursor = (
            encode_cursor([page[-1][c] for c in self._contract.order_columns])
            if has_more and page
            else None
        )
        return items, next_cursor
