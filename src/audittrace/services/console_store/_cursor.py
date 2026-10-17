"""Keyset (cursor) pagination shared by BOTH store implementations.

One definition of "the rows after this cursor" — the SQL form for
:class:`PostgresConsoleStore` and the Python form for
:class:`MockConsoleStore` — so the two cannot drift. The cursor is opaque
to callers: base64 of a JSON list holding the ordering columns' values of
the last row served. A malformed, truncated, wrong-arity or wrong-typed
cursor raises ``ValueError`` (the route maps it to 400) — never a silent
"start from the beginning" that would look like an empty result.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from typing import Any, Literal

from sqlalchemy import ColumnElement, and_, or_

Direction = Literal["asc", "desc"]


def encode_cursor(values: Sequence[Any]) -> str:
    raw = json.dumps(list(values), separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _strict_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("cursor component is not an int")
    return int(value)


def _strict_str(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("cursor component is not a str")
    return value


def coercer_for(python_type: type[Any]) -> Callable[[Any], Any]:
    """Strict per-column coercer: an ``int`` column accepts only a JSON int
    (never ``true``/``"1"``), a ``str`` column only a JSON string. Any other
    Python type is refused at domain validation, not here."""
    if python_type is int:
        return _strict_int
    if python_type is str:
        return _strict_str
    raise TypeError(f"unsupported ordering column type: {python_type!r}")


def decode_cursor(cursor: str, coercers: Sequence[Callable[[Any], Any]]) -> list[Any]:
    """Inverse of :func:`encode_cursor`, validated against the ordering
    columns' types. Raises ``ValueError`` for anything that is not exactly a
    JSON list of ``len(coercers)`` correctly-typed components (the strict
    coercers refuse ``null``, booleans and cross-typed values)."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        parsed = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeError) as exc:
        raise ValueError(f"invalid cursor: {cursor!r}") from exc
    if not isinstance(parsed, list) or len(parsed) != len(coercers):
        raise ValueError(f"invalid cursor: {cursor!r}")
    out: list[Any] = []
    for value, coerce in zip(parsed, coercers, strict=True):
        try:
            out.append(coerce(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid cursor: {cursor!r}") from exc
    return out


def row_after_cursor(
    row_values: Sequence[Any],
    cursor_values: Sequence[Any],
    directions: Sequence[Direction],
) -> bool:
    """Python form: is ``row_values`` strictly after ``cursor_values`` under
    the lexicographic ordering with per-column directions?"""
    for row_value, cursor_value, direction in zip(
        row_values, cursor_values, directions, strict=True
    ):
        if row_value == cursor_value:
            continue
        if direction == "asc":
            return bool(row_value > cursor_value)
        return bool(row_value < cursor_value)
    return False


def keyset_predicate(
    columns: Sequence[ColumnElement[Any]],
    directions: Sequence[Direction],
    cursor_values: Sequence[Any],
) -> ColumnElement[bool]:
    """SQL form of :func:`row_after_cursor`: ``OR`` over prefix-equal /
    strict-compare clauses, one per ordering column."""
    clauses: list[ColumnElement[bool]] = []
    for index, (column, direction) in enumerate(zip(columns, directions, strict=True)):
        prefix_equal = [columns[j] == cursor_values[j] for j in range(index)]
        strict = (
            column > cursor_values[index]
            if direction == "asc"
            else column < cursor_values[index]
        )
        clauses.append(and_(*prefix_equal, strict))
    return or_(*clauses)
