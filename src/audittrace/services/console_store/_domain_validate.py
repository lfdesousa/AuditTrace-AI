"""Validating a :class:`~audittrace.services.console_store._domain.
ConsoleDomain` into an immutable :class:`DomainContract` — split out of
``_domain.py`` (SPEC ADDENDUM D, fix round 3, 2026-09-17) once that module
crossed the PYTHON-ENGINEERING §11 500-LOC review trigger; ``ConsoleDomain``
(what a domain IS) and this module (how a domain gets VALIDATED into the
contract a store caches) are one concern split across two files, not two
concerns forced into one.

See ``_domain.py``'s module docstring, "Fifth hop", for the full account
of the defect this module's :class:`DomainContract` closes (F6:
``value_columns`` — and, audited on the same axis, ``key_columns``,
``model``, ``order_by``, ``has_session_id`` — read MORE THAN ONCE after
validation, so a stateful ``property``/``__getattribute__`` override can
answer :func:`validate_domain` with a safe value and a later consumer with
a hostile one). :func:`validate_domain` is the ONLY place any of those
five surfaces is read off a live domain; everything else in the package
reads the :class:`DomainContract` it returns.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from audittrace.services.console_store._context import (
    REQUIRED_MODEL_COLUMNS,
    RESERVED_COLUMNS,
)
from audittrace.services.console_store._cursor import Direction, coercer_for
from audittrace.services.console_store._domain import ORDERABLE_RESERVED, ConsoleDomain
from audittrace.services.console_store._errors import ConsoleStoreDomainError


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConsoleStoreDomainError(message)


@dataclass(frozen=True)
class DomainContract:
    """Every DECLARATIVE surface a domain supplies, read EXACTLY ONCE by
    :func:`validate_domain` and carried from then on (SPEC ADDENDUM D R1) —
    the ``ConsoleDomain``-level analogue of :class:`~audittrace.services.
    console_store._context.WriteStamp`. A ``ConsoleStoreBase`` caches this
    on ``self._contract`` at construction and reads ONLY these fields for
    ``name``/``model``/``key_columns``/``value_columns``/``order_by`` and
    their derived shapes from then on — never ``self._domain.<same name>``
    again. This is what closes the read-nonatomicity a stateful
    ``property``/``__getattribute__`` override could otherwise exploit
    between validation and use: see ``ConsoleDomain``'s module docstring,
    "Fifth hop", for the full account and the four-sibling audit.

    Deliberately NOT here: ``cap()``, ``defaults()``, ``merge()``,
    ``equality_filters()``, ``to_item()`` — those are HOOKS, meant to vary
    per call, and stay live-invoked on ``self._domain`` (validated on every
    call by :func:`validate_cap` / :func:`validate_equality_filters`,
    against THIS contract's ``key_columns``/``value_columns``, never a
    fresh read of the domain's own)."""

    name: str
    model: type[Any]
    key_columns: tuple[str, ...]
    value_columns: tuple[str, ...]
    order_by: tuple[tuple[str, Direction], ...]
    default_list_limit: int
    max_list_limit: int
    has_session_id: bool
    snapshot_columns: tuple[str, ...]
    order_columns: tuple[str, ...]
    order_directions: tuple[Direction, ...]


def validate_domain(domain: ConsoleDomain[Any]) -> DomainContract:
    """Refuse an invalid descriptor BEFORE the store does any I/O, and
    return the validated, immutable :class:`DomainContract` the store
    caches for the rest of its lifetime.

    SPEC ADDENDUM D R1: every declarative attribute below (``name``,
    ``model``, ``key_columns``, ``value_columns``, ``order_by``,
    ``default_list_limit``, ``max_list_limit``) is read via ``getattr``
    into a local EXACTLY ONCE and every check below (and the returned
    contract) uses that SAME local — never a second ``getattr`` of the
    same attribute — so a domain whose declarative surface is a stateful
    ``property``/``__getattribute__`` cannot present one value to this
    validator and a different one to a later consumer (F6).

    Falsifiable: neuter the reserved-column check and a hostile domain that
    declares ``value_columns=("user_sub",)`` gets to write another user's
    ``user_sub`` (``tests/console_store/test_hostile_domain_hooks.py``).
    """
    _require(isinstance(domain, ConsoleDomain), "domain must be a ConsoleDomain")
    name = getattr(domain, "name", None)
    _require(isinstance(name, str) and bool(name.strip()), "domain.name must be set")
    assert isinstance(name, str)  # narrows for mypy; _require just enforced this
    model = getattr(domain, "model", None)
    _require(isinstance(model, type), f"{name}: domain.model must be an ORM class")
    assert isinstance(model, type)  # narrows for mypy; _require just enforced this

    keys = tuple(getattr(domain, "key_columns", ()))
    values = tuple(getattr(domain, "value_columns", ()))
    _require(bool(keys), f"{name}: key_columns must name at least one column")
    _require(
        all(isinstance(c, str) and c for c in keys + values),
        f"{name}: column names must be non-empty strings",
    )
    reserved_hit = sorted((set(keys) | set(values)) & RESERVED_COLUMNS)
    _require(
        not reserved_hit,
        f"{name}: reserved column(s) may not be key/value columns: {reserved_hit}",
    )
    _require(
        not (set(keys) & set(values)),
        f"{name}: key_columns and value_columns must be disjoint",
    )
    _require(len(set(keys)) == len(keys), f"{name}: duplicate key column")
    _require(len(set(values)) == len(values), f"{name}: duplicate value column")

    missing = [
        c for c in REQUIRED_MODEL_COLUMNS + keys + values if not hasattr(model, c)
    ]
    _require(not missing, f"{name}: ORM model lacks column(s): {missing}")

    order_by = tuple(getattr(domain, "order_by", ()))
    _require(bool(order_by), f"{name}: order_by must name at least one column")
    orderable = ORDERABLE_RESERVED | set(keys)
    for entry in order_by:
        _require(
            isinstance(entry, tuple) and len(entry) == 2,
            f"{name}: order_by entries must be (column, direction)",
        )
        column, direction = entry
        _require(column in orderable, f"{name}: cannot order by {column!r}")
        _require(direction in ("asc", "desc"), f"{name}: bad direction {direction!r}")
        try:
            coercer_for(getattr(model, column).type.python_type)
        except (AttributeError, NotImplementedError, TypeError) as exc:
            raise ConsoleStoreDomainError(
                f"{name}: ordering column {column!r} has an unsupported type"
            ) from exc
    ordered = {column for column, _ in order_by}
    _require(
        "id" in ordered or set(keys) <= ordered,
        f"{name}: order_by must include 'id' or every key column (total order)",
    )

    default_limit = getattr(domain, "default_list_limit", 0)
    max_limit = getattr(domain, "max_list_limit", 0)
    _require(
        isinstance(default_limit, int) and isinstance(max_limit, int),
        f"{name}: list limits must be ints",
    )
    _require(1 <= default_limit <= max_limit, f"{name}: 1 <= default <= max limit")

    # SPEC ADDENDUM D R1: has_session_id/snapshot_columns/order_columns/
    # order_directions are DERIVED from the locals above, never from
    # domain.has_session_id() / domain.snapshot_columns() / ... (those
    # sealed template methods re-read self.key_columns/self.value_columns/
    # self.model themselves — calling them here would reopen exactly the
    # gap this function exists to close).
    has_session_id = hasattr(model, "session_id")
    reserved_prefix = tuple(REQUIRED_MODEL_COLUMNS) + (
        ("session_id",) if has_session_id else ()
    )
    snapshot_columns = reserved_prefix + keys + values
    order_columns = tuple(column for column, _ in order_by)
    order_directions = tuple(direction for _, direction in order_by)

    validate_cap(domain, name=name)
    validate_equality_filters(domain, name=name, key_columns=keys, value_columns=values)

    return DomainContract(
        name=name,
        model=model,
        key_columns=keys,
        value_columns=values,
        order_by=order_by,
        default_list_limit=default_limit,
        max_list_limit=max_limit,
        has_session_id=has_session_id,
        snapshot_columns=snapshot_columns,
        order_columns=order_columns,
        order_directions=order_directions,
    )


def validate_cap(domain: ConsoleDomain[Any], *, name: str | None = None) -> int | None:
    """``cap()`` must return ``None`` or a positive int — checked on EVERY
    call, not only at construction, since a hook is a live method.

    ``name`` defaults to a live ``domain.name`` read for callers outside a
    store (e.g. tests exercising a bare domain); ``ConsoleStoreBase._cap()``
    passes the CACHED ``self._contract.name`` instead, so an error message
    never depends on a second, un-cached read of a declarative surface."""
    cap = domain.cap()
    resolved_name = domain.name if name is None else name
    _require(
        cap is None
        or (isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1),
        f"{resolved_name}: cap() must return None or a positive int",
    )
    return cap


def validate_equality_filters(
    domain: ConsoleDomain[Any],
    *,
    name: str | None = None,
    key_columns: tuple[str, ...] | None = None,
    value_columns: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """``equality_filters()`` may only name key/value columns — checked on
    EVERY call. A reserved column here would be an attempt to touch the
    ``user_sub`` predicate; refused.

    SPEC ADDENDUM D R1: ``key_columns``/``value_columns`` default to a live
    read of the domain (for callers outside a store, e.g.
    :func:`validate_domain` itself, which passes its OWN already-captured
    locals) but ``ConsoleStoreBase._filters()`` always passes the CACHED
    ``self._contract.key_columns``/``value_columns`` — the allow-list a
    hostile ``equality_filters()`` is checked against is the SAME one
    every other consumer in this call actually uses, never a fresh,
    independently-re-readable one."""
    filters = domain.equality_filters()
    resolved_name = domain.name if name is None else name
    _require(
        isinstance(filters, Mapping), f"{resolved_name}: equality_filters must map"
    )
    resolved_keys = domain.key_columns if key_columns is None else key_columns
    resolved_values = domain.value_columns if value_columns is None else value_columns
    allowed = set(resolved_keys) | set(resolved_values)
    bad = sorted(set(filters) - allowed)
    _require(not bad, f"{resolved_name}: equality_filters may not name {bad}")
    return dict(filters)
