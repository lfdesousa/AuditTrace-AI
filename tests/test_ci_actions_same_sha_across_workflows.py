"""Guard: an action used by both ``ci.yml`` and ``evidence-check.yml`` is the SAME commit.

SPEC CI-ACTIONS-SHA-PIN, ADDENDUM-2 advisory A1. ``evidence-check.yml`` mirrors the
``ci.yml`` evidence job; before the SHA pin, an exact-tag test kept the two in step.
Ruling (b) moved that test to owner/repo only (so a Dependabot bump never edits a
test), which would silently drop the coupling. This test keeps it: for every action
that appears in both files, the set of pinned SHAs over both files is exactly one.
It survives a grouped Dependabot bump (both files move together) and goes RED when
a bump, or a hand edit, moves only one.

Neuter: change one SHA in one file only -> ``test_planted_divergence_is_reported``
shows the detector fires and ``test_ci_and_evidence_check_agree_on_shared_actions``
goes RED on the real files.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
EVIDENCE = WORKFLOWS / "evidence-check.yml"


def _uses(path: Path) -> list[str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        out += [
            str(s["uses"])
            for s in job.get("steps") or []
            if isinstance(s, dict) and "uses" in s
        ]
    return out


def _by_action(refs: Iterable[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for ref in refs:
        action, _sep, version = ref.partition("@")
        out.setdefault(action, set()).add(version)
    return out


def divergent_shared_actions(left: Path, right: Path) -> dict[str, set[str]]:
    """Actions present in both files whose refs (over both files) are not exactly one."""
    a, b = _by_action(_uses(left)), _by_action(_uses(right))
    return {
        action: a[action] | b[action]
        for action in sorted(a.keys() & b.keys())
        if len(a[action] | b[action]) != 1
    }


def test_ci_and_evidence_check_agree_on_shared_actions() -> None:
    shared = _by_action(_uses(CI)).keys() & _by_action(_uses(EVIDENCE)).keys()
    assert shared, "no shared action: the coupling check would be vacuous"
    assert divergent_shared_actions(CI, EVIDENCE) == {}


def test_planted_divergence_is_reported(tmp_path: Path) -> None:
    sha_a, sha_b = "a" * 40, "b" * 40
    body = "on: push\njobs:\n  j:\n    runs-on: x\n    steps:\n      - uses: actions/checkout@{ref}\n"
    left, right, same = (tmp_path / n for n in ("l.yml", "r.yml", "s.yml"))
    left.write_text(body.format(ref=sha_a), encoding="utf-8")
    right.write_text(body.format(ref=sha_b), encoding="utf-8")
    same.write_text(body.format(ref=sha_a), encoding="utf-8")
    assert divergent_shared_actions(left, right) == {"actions/checkout": {sha_a, sha_b}}
    assert divergent_shared_actions(left, same) == {}
