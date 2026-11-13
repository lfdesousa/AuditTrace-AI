"""Shape pins for the ADR-049 ``evidence-check`` workflow.

The job lives in its OWN workflow so an ``edited`` PR-body event does not
re-run all of CI, and it is Draft-aware (report mode on Drafts, the hard
gate otherwise). These tests pin the trigger types, the draft branch, the
job name branch protection matches, least-privilege permissions, and that
ci.yml no longer carries a duplicate.
"""

from __future__ import annotations

import re
import subprocess
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


def _checker_steps() -> list[dict[str, Any]]:
    steps = _load(_WF)["jobs"]["evidence-check"]["steps"]
    return [s for s in steps if "adr049_evidence_check.py" in s.get("run", "")]


def _walk_keys(node: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            keys.append(str(k))
            keys.extend(_walk_keys(v))
    elif isinstance(node, list):
        for item in node:
            keys.extend(_walk_keys(item))
    return keys


def test_one_checker_step_reads_draft_state_live() -> None:
    """B1: the mode comes from `gh pr view --json isDraft` at RUN time, in the
    SAME step that runs the checker, never from the (replayable) event payload."""
    steps = _checker_steps()
    assert len(steps) == 1
    run = steps[0]["run"]
    assert "gh pr view" in run and "--json isDraft" in run
    assert "--draft" in run
    assert "github.event.pull_request.draft" not in _WF.read_text(encoding="utf-8")


def test_checker_step_runs_on_every_event() -> None:
    """B2: no `if:` on the checker step or the job can skip the check."""
    doc = _load(_WF)
    job = doc["jobs"]["evidence-check"]
    assert "if" not in job
    assert "if" not in _checker_steps()[0]
    assert "if" not in doc


def test_no_continue_on_error_anywhere() -> None:
    """B3: continue-on-error would turn the merge gate advisory."""
    assert "continue-on-error" not in _walk_keys(_load(_WF))
    assert "continue-on-error" not in _WF.read_text(encoding="utf-8")


def test_concurrency_group_per_pr_cancels_stale_runs() -> None:
    conc = _load(_WF)["concurrency"]
    assert "github.event.pull_request.number" in conc["group"]
    assert conc["cancel-in-progress"] is True


def _run_checker_step(
    tmp_path: Path, gh_mode: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Execute the real step script with stub `gh` and `python3` on PATH and
    return (result, the argv the checker was invoked with)."""
    stub = tmp_path / "bin"
    stub.mkdir()
    argv_log = tmp_path / "argv.txt"
    gh = stub / "gh"
    gh.write_text(
        "#!/bin/bash\n"
        + {
            "true": "echo true\n",
            "false": "echo false\n",
            "TRUE": "echo TRUE\n",
            "empty": "echo ''\n",
            "garbage": "echo 'maybe'\n",
            "fail": "echo boom >&2; exit 1\n",
        }[gh_mode]
    )
    gh.chmod(0o755)
    py = stub / "python3"
    py.write_text(f'#!/bin/bash\necho "$@" > {argv_log}\nexit 0\n')
    py.chmod(0o755)
    env = {
        "PATH": f"{stub}:/usr/bin:/bin",
        "PR_NUMBER": "7",
        "REPO": "o/r",
        "GH_TOKEN": "x",
    }
    res = subprocess.run(
        ["bash", "-e", "-c", _checker_steps()[0]["run"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    return res, argv_log.read_text() if argv_log.exists() else ""


def test_step_uses_draft_mode_only_when_live_value_is_exactly_true(
    tmp_path: Path,
) -> None:
    res, argv = _run_checker_step(tmp_path, "true")
    assert res.returncode == 0
    assert argv.split()[0] == "scripts/adr049_evidence_check.py"
    assert "--draft" in argv


def test_step_is_hard_gate_for_anything_but_exactly_true(tmp_path: Path) -> None:
    for i, mode in enumerate(("false", "TRUE", "empty", "garbage", "fail")):
        sub = tmp_path / f"m{i}"
        sub.mkdir()
        res, argv = _run_checker_step(sub, mode)
        assert res.returncode == 0, (mode, res.stderr)
        assert "scripts/adr049_evidence_check.py" in argv, mode
        assert "--draft" not in argv, mode


def test_actions_are_pinned_like_ci() -> None:
    steps = _load(_WF)["jobs"]["evidence-check"]["steps"]
    uses = {s["uses"] for s in steps if "uses" in s}
    assert uses == {"actions/checkout@v6", "actions/setup-python@v6"}


def test_ci_yml_has_no_duplicate_evidence_job() -> None:
    assert "evidence-check" not in _load(_CI)["jobs"]
    assert "adr049_evidence_check" not in _CI.read_text(encoding="utf-8")


def _recipe(mk: str, target: str) -> str:
    m = re.search(rf"^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n)+)", mk, re.M)
    assert m, target
    return m.group(1)


def test_make_hard_target_has_no_draft_flag_and_draft_target_has() -> None:
    """B4: `make check-pr-body` stays the hard gate."""
    mk = (_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "--draft" not in _recipe(mk, "check-pr-body")
    assert "adr049_evidence_check.py" in _recipe(mk, "check-pr-body")
    assert "--draft" in _recipe(mk, "check-pr-body-draft")
