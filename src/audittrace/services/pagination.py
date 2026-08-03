"""Shared, total-order-safe sort + paginate core (backlog #15 R2b).

Extracted from ``routes/memory.py``'s ``_sort_and_paginate`` (the LIST
endpoint, backlog #15 R5) so the recall-tool pagination added for backlog
#15's residual (#375, marker RECALL-PAGINATION-20260803) reuses the exact
same stable-tiebreak + None-coalescing mechanics instead of re-deriving them
(R2b: "mirror the LIST endpoint's ``_sort_and_paginate`` ... reuse/share that
logic — do not duplicate").

Both call sites keep their own domain-specific key functions — dict fields
for the LIST endpoint's manifest rows (``routes/memory.py``), ``Document``
fields for recall's vector-store matches (``services/semantic.py``). Only
the sort/slice mechanic below is shared.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


# ruff (target-version py312) wants PEP 695 `def f[T](...)` syntax here
# (UP047); the repo's pre-commit mypy hook is pinned to v1.8.0
# (.pre-commit-config.yaml), which predates PEP 695 generic-function
# support and fails hard on it. TypeVar is the compatible form until that
# pin moves — noqa is scoped to this one line, not a blanket ignore.
def sort_and_paginate(  # noqa: UP047
    items: list[T],
    *,
    key_fn: Callable[[T], Any],
    tiebreak_fn: Callable[[T], Any],
    reverse: bool,
    limit: int,
    offset: int,
) -> tuple[list[T], int]:
    """Return ``(page, total)`` — a total-order-safe sorted, paginated slice.

    ``total`` is the full pre-limit count of ``items`` so a caller can page.
    Sorting is total-order safe as long as ``key_fn``/``tiebreak_fn``
    coalesce missing values themselves (e.g. ``None`` -> ``0``/``""``) —
    this function never dereferences ``None`` directly, so no comparison
    ever raises.

    ``tiebreak_fn`` runs FIRST as a stable secondary sort, so items with
    equal ``key_fn`` values keep tiebreak-ascending order regardless of
    ``reverse`` — this is what makes paging deterministic across repeated
    calls with the same sort/order (no two calls ever reorder a tied pair
    differently, which would let an item silently vanish between pages).
    """
    ordered = sorted(items, key=tiebreak_fn)
    ordered = sorted(ordered, key=key_fn, reverse=reverse)
    total = len(ordered)
    page = ordered[offset : offset + limit]
    return page, total
