"""LIBRECHAT-IMAGE-SLICE AC-2 -- every chat-path audit write carries ``input_media``.

An equality, not a list: in the two chat route modules, the number of calls that write
an interaction row equals the number of those calls that pass ``input_media=``. A new
write site added without it turns this red. (stdlib ``ast``; the spec's site list comes
from CODE-INDEX, a different instrument.)
"""

from __future__ import annotations

import ast
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[1] / "src" / "audittrace" / "routes"
MODULES = ("chat.py", "_upstream_errors.py")
WRITERS = {"_persist_interaction", "_persist_or_enqueue"}
#: sites on main a1945364 (CODE-INDEX): 15 + 4 in chat.py, 1 in _upstream_errors.py
MIN_SITES = 18


def _sites() -> list[tuple[str, int, bool, bool]]:
    found: list[tuple[str, int, bool, bool]] = []
    for name in MODULES:
        for node in ast.walk(ast.parse((ROUTES / name).read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            callee = (
                func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            )
            if callee not in WRITERS:
                continue
            forwards = any(k.arg is None for k in node.keywords)
            passes = any(k.arg == "input_media" for k in node.keywords)
            found.append((name, node.lineno, forwards, passes))
    return found


def test_the_guard_sees_the_write_sites() -> None:
    explicit = [s for s in _sites() if not s[2]]
    assert len(explicit) >= MIN_SITES, explicit


def test_every_explicit_write_site_passes_input_media() -> None:
    missing = [
        f"{name}:{line}"
        for name, line, forwards, passes in _sites()
        if not forwards and not passes
    ]
    assert missing == []
