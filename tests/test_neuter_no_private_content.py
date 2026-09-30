"""No private content leaks into the public harness (SPEC v3 §13).

Greps ``scripts/neuter/**``, ``tests/neuter_fixture/**``, and
``tests/test_neuter_*.py`` for private markers: the private evidence-repo
name, a ``decisions/`` memory-server key, a bare 64-hex-char sha256, an
absolute ``/home/`` path, or an ``at-wt-`` worktree name.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_PRIVATE_PATTERNS = [
    re.compile(r"audittrace-private"),
    re.compile(r"decisions/[0-9a-f]{16}"),
    re.compile(r"\b[0-9a-f]{64}\b"),
    re.compile(r"/home/"),
    re.compile(r"\bat-wt-"),
]


_SELF = Path(__file__).resolve()


def _scan_paths() -> list[Path]:
    paths = list((REPO_ROOT / "scripts" / "neuter").rglob("*.py"))
    paths += list((REPO_ROOT / "tests" / "neuter_fixture").rglob("*"))
    paths += list((REPO_ROOT / "tests").glob("test_neuter_*.py"))
    # Exclude this guard's own source -- its pattern *definitions* legitimately
    # contain the marker substrings they search for (`audittrace-private`,
    # `/home/`, `at-wt-`, ...); scanning itself would be a permanent false
    # positive, not a real leak.
    return [p for p in paths if p.is_file() and p.resolve() != _SELF]


def test_no_private_markers_in_neuter_harness_files():
    offenders: list[str] = []
    for path in _scan_paths():
        text = path.read_text(errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern in _PRIVATE_PATTERNS:
                if pattern.search(line):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{lineno}: {pattern.pattern} -> {line.strip()[:120]}"
                    )
    assert offenders == [], "private markers found:\n" + "\n".join(offenders)


def test_scan_paths_is_non_empty():
    """The scan itself must cover real files -- an empty glob would make the
    guard above vacuously pass."""
    assert len(_scan_paths()) >= 10
