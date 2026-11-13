"""Shape pins for the ADR-049 ``evidence-check`` workflow.

The job lives in its OWN workflow so an ``edited`` PR-body event does not
re-run all of CI, and it is Draft-aware (report mode on Drafts, the hard
gate otherwise). These tests pin the trigger types, the draft branch, the
job name branch protection matches, least-privilege permissions, and that
ci.yml no longer carries a duplicate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_WF = _ROOT / ".github" / "workflows" / "evidence-check.yml"
_CI = _ROOT / ".github" / "workflows" / "ci.yml"

_TYPES = {
    "opened",
    "synchronize",
    "reopened",
    "edited",
    "ready_for_review",
    "converted_to_draft",
}


def _load(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML parses the bare key ``on`` as boolean True.
    return doc.get("on", doc.get(True))


def test_triggers_pin_activity_types_and_branches() -> None:
    pr = _triggers(_load(_WF))["pull_request"]
    assert set(pr["types"]) == _TYPES
    ci_pr = _triggers(_load(_CI))["pull_request"]
    assert pr["branches"] == ci_pr["branches"]


def test_job_name_is_evidence_check() -> None:
    jobs = _load(_WF)["jobs"]
    assert list(jobs) == ["evidence-check"]


def test_least_privilege_permissions() -> None:
    job = _load(_WF)["jobs"]["evidence-check"]
    assert job["permissions"] == {"contents": "read", "pull-requests": "read"}


def test_draft_branch_uses_flag_not_env_bypass() -> None:
    text = _WF.read_text(encoding="utf-8")
    assert "github.event.pull_request.draft" in text
    steps = _load(_WF)["jobs"]["evidence-check"]["steps"]
    runs = [s.get("run", "") for s in steps]
    checker = [r for r in runs if "adr049_evidence_check.py" in r]
    assert checker, "the shared checker must be invoked"
    joined = "\n".join(checker)
    assert "--draft" in joined
    # The hard gate (no flag) must remain the non-draft path.
    assert any("--draft" not in r for r in checker) or "else" in joined


def test_checker_steps_split_on_draft_state() -> None:
    steps = _load(_WF)["jobs"]["evidence-check"]["steps"]
    by_flag = {
        ("--draft" in s["run"]): s.get("if", "")
        for s in steps
        if "adr049_evidence_check.py" in s.get("run", "")
    }
    assert "github.event.pull_request.draft == true" in by_flag[True]
    assert "github.event.pull_request.draft != true" in by_flag[False]


def test_actions_are_pinned_like_ci() -> None:
    steps = _load(_WF)["jobs"]["evidence-check"]["steps"]
    uses = {s["uses"] for s in steps if "uses" in s}
    assert uses == {"actions/checkout@v6", "actions/setup-python@v6"}


def test_ci_yml_has_no_duplicate_evidence_job() -> None:
    assert "evidence-check" not in _load(_CI)["jobs"]
    assert "adr049_evidence_check" not in _CI.read_text(encoding="utf-8")


def test_makefile_has_hard_and_draft_targets() -> None:
    mk = (_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "check-pr-body-draft:" in mk
    assert "--draft" in mk
