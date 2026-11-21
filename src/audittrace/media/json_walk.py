"""Iterative walks over a JSON-shaped value (dicts, lists, strings), linear and depth-free.

The request body is caller data and ``json.loads`` accepts nesting thousands of levels
deep, far beyond the interpreter's recursion limit: a recursive walk raised
``RecursionError`` (the route answered 500 and wrote no audit row). These walks use an
explicit stack, so they cover EVERY level and never raise on depth; there is no depth cap
(a cap would hide a nested link from the link guard).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def any_string(value: Any, predicate: Callable[[str], bool]) -> bool:
    """True when ``predicate`` holds for any string leaf (dict values and list items)."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if predicate(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def map_strings(value: Any, fn: Callable[[str], str]) -> Any:
    """A copy of ``value`` with ``fn`` applied to every string leaf (keys are kept)."""
    if isinstance(value, str):
        return fn(value)
    if not isinstance(value, dict | list):
        return value
    root: Any = {} if isinstance(value, dict) else []
    stack: list[tuple[Any, Any]] = [(value, root)]
    while stack:
        source, target = stack.pop()
        pairs = source.items() if isinstance(source, dict) else enumerate(source)
        for key, item in pairs:
            if isinstance(item, str):
                copy: Any = fn(item)
            elif isinstance(item, dict | list):
                copy = {} if isinstance(item, dict) else []
                stack.append((item, copy))
            else:
                copy = item
            if isinstance(target, dict):
                target[key] = copy
            else:
                target.append(copy)
    return root
