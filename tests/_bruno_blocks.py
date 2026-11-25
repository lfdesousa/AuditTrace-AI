"""Shared readers for Bruno ``.bru`` request files (block-scoped, comment-stripped)."""

from __future__ import annotations

import re
from pathlib import Path


def _block(text: str, name: str) -> str:
    """Return the inner text of the top-level ``name { ... }`` block ('' if absent)."""
    m = re.search(rf"^{re.escape(name)} \{{\n", text, re.M)
    if not m:
        return ""
    depth, i = 1, m.end()
    while i < len(text) and depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end() : i - 1]


def _strip_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return re.sub(r"//[^\n]*", "", code)


def _code_lines(path: Path, block: str = "tests") -> list[str]:
    inner = _strip_comments(_block(path.read_text(), block))
    return [ln.strip() for ln in inner.splitlines() if ln.strip()]
