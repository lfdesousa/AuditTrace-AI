"""Guard: a PR branch must not run CI twice (push AND pull_request).

Defect (backlog CI-DUPLICATE-PUSH-AND-PR-RUNS, issue #18): ``ci.yml`` and
``e2e.yml`` triggered on ``push`` to ``feat/**`` / ``fix/**`` AND on
``pull_request``, so every push to a PR branch ran the whole workflow twice.

Rule pinned here: in any workflow that also has a ``pull_request`` trigger,
a ``push`` may only filter to the literal branch ``main`` (the merge commit,
which is not a duplicate of a PR run) and/or tags. A bare ``push:``,
``branches-ignore`` or a paths-only push fire for every branch and fail. The tests also pin
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


def _normalise_on(on: Any) -> dict[str, Any]:
    """Return the ``on:`` block as an event -> filter mapping.

    Handles the shorthand forms ``on: push`` and ``on: [push, pull_request]``.
    """
    if not on:
        return {}
    if isinstance(on, str):
        on = [on]
    if isinstance(on, list):
        return {event: None for event in on}
    return dict(on)


def _triggers(name: str) -> dict[str, Any]:
    doc = yaml.safe_load((_WF_DIR / name).read_text(encoding="utf-8"))
    # PyYAML parses the bare key ``on`` as boolean True.
    return _normalise_on(doc.get("on", doc.get(True)))


def _push_hits_all_branches(push: Any) -> bool:
    """True when a ``push`` trigger can fire for a PR head branch.

    A null/empty push, any ``branches-ignore``, a push with neither a
    ``branches`` nor a ``tags`` filter (including paths-only), and any
    ``branches`` list wider than the literal ``main`` all fire for every
    branch. A tags-only push is not a branch push.
    """
    if not push:
        return True
    if "branches-ignore" in push:
        return True
    if "branches" not in push:
        return "tags" not in push
    return not set(push["branches"] or []) <= {"main"}


@pytest.mark.parametrize("name", _WORKFLOWS)
def test_push_branches_do_not_duplicate_pull_request(name: str) -> None:
    trig = _triggers(name)
    if "pull_request" not in trig or "push" not in trig:
        return
    assert not _push_hits_all_branches(trig["push"]), (
        f"{name}: push trigger {trig['push']!r} duplicates the pull_request run"
    )


@pytest.mark.parametrize(
    ("push", "expected"),
    [
        (None, True),
        ({}, True),
        ({"branches-ignore": ["gh-pages"]}, True),
        ({"tags": ["v*"], "branches-ignore": ["gh-pages"]}, True),
        ({"paths": ["docs/**"]}, True),
        ({"branches": ["main", "fix/**"]}, True),
        ({"branches": ["main"]}, False),
        ({"branches": ["main"], "paths": ["docs/**"]}, False),
        ({"tags": ["v*"]}, False),
        ({"branches": ["main"], "tags": ["v*"]}, False),
    ],
)
def test_push_shape_classifier(push: Any, expected: bool) -> None:
    assert _push_hits_all_branches(push) is expected


@pytest.mark.parametrize(
    ("on", "expected"),
    [
        (None, {}),
        ("push", {"push": None}),
        (["push", "pull_request"], {"push": None, "pull_request": None}),
        ({"push": {"branches": ["main"]}}, {"push": {"branches": ["main"]}}),
    ],
)
def test_normalise_on_shorthand(on: Any, expected: dict[str, Any]) -> None:
    assert _normalise_on(on) == expected


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
