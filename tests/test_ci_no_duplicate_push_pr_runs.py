"""Guard: a PR branch must not run CI twice (push AND pull_request).

Defect (backlog CI-DUPLICATE-PUSH-AND-PR-RUNS, issue #18): ``ci.yml`` and
``e2e.yml`` triggered on ``push`` to ``feat/**`` / ``fix/**`` AND on
``pull_request``, so every push to a PR branch ran the whole workflow twice.

Rule pinned here: in any workflow that also has a ``pull_request`` trigger,
``push.branches`` may only be the literal ``main`` (the merge commit, which
is not a duplicate of a PR run). Tags are unaffected. The tests also pin
what must NOT change: the main and tag triggers, and the job names that
branch protection matches by name.

Unit tests over parsed YAML; nothing is mocked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_WF_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"
_WORKFLOWS = sorted(p.name for p in _WF_DIR.glob("*.yml"))

# Job ids/names of the workflows touched by this change. Branch protection
# matches required checks by name, so these must never be renamed.
_JOBS = {
    "ci.yml": ["test", "lock-check", "helm-lint", "release-version-check"],
    "e2e.yml": ["e2e"],
}


def _triggers(name: str) -> dict[str, Any]:
    doc = yaml.safe_load((_WF_DIR / name).read_text(encoding="utf-8"))
    # PyYAML parses the bare key ``on`` as boolean True.
    return doc.get("on", doc.get(True)) or {}


@pytest.mark.parametrize("name", _WORKFLOWS)
def test_push_branches_do_not_duplicate_pull_request(name: str) -> None:
    trig = _triggers(name)
    if "pull_request" not in trig or "push" not in trig:
        return
    branches = (trig["push"] or {}).get("branches", [])
    assert set(branches) <= {"main"}, (
        f"{name}: push.branches {branches} duplicates the pull_request run"
    )


@pytest.mark.parametrize("name", ["ci.yml", "e2e.yml"])
def test_pull_request_trigger_kept(name: str) -> None:
    pr = _triggers(name)["pull_request"]
    assert pr["branches"] == ["main", "feat/**"]


@pytest.mark.parametrize("name", ["ci.yml", "e2e.yml"])
def test_main_push_kept(name: str) -> None:
    assert "main" in _triggers(name)["push"]["branches"]


def test_ci_tag_trigger_kept() -> None:
    assert _triggers("ci.yml")["push"]["tags"] == ["v*"]


def test_publish_tag_trigger_untouched() -> None:
    assert _triggers("publish.yml")["push"]["tags"] == ["v*.*.*"]


@pytest.mark.parametrize("name", sorted(_JOBS))
def test_required_check_names_unchanged(name: str) -> None:
    doc = yaml.safe_load((_WF_DIR / name).read_text(encoding="utf-8"))
    assert list(doc["jobs"]) == _JOBS[name]
    assert all("name" not in job for job in doc["jobs"].values())
