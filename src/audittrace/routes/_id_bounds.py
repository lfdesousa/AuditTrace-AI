"""Bounds of an ``interactions`` / ``tool_calls`` integer key as a path parameter.

``interactions.id`` is a 32-bit integer. A larger value is not a row that does not
exist, it is a value PostgreSQL's driver refuses to bind: an unhandled 500. These
helpers answer "not a possible key" BEFORE any query, so the caller sees the same
answer as for an unknown id.
"""

from __future__ import annotations

INT4_MIN = -(2**31)
INT4_MAX = 2**31 - 1


def in_int4_range(value: int) -> bool:
    return INT4_MIN <= value <= INT4_MAX


def possible_interaction_id(raw: str) -> int | None:
    """The key ``raw`` names, or ``None`` when it cannot name a row.

    A row id is a positive 32-bit integer written in ASCII digits: ``abc``, ``1.5``,
    ``0``, ``-1``, a unicode digit and anything above 2147483647 are all ``None``.
    """
    if not (raw.isascii() and raw.isdigit()):
        return None
    value = int(raw)
    if value < 1 or value > INT4_MAX:
        return None
    return value
