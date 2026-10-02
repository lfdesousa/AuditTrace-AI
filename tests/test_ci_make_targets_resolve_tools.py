"""CI parity guard: every make target a CI step runs must resolve its tools.

CI installs into the runner's python and has NO ``.venv``. A recipe that
calls ``.venv/bin/<tool>`` bare passes on every dev machine and fails in CI
with exit 127 (#460 / PR #371: ``make typecheck`` -> ``.venv/bin/mypy``).

Rule: each ``.venv/bin/<tool>`` in a CI-invoked target (and its
prerequisites) must sit inside ``[ -x .venv/bin/<tool> ] && echo
.venv/bin/<tool> || command -v <tool>`` and the recipe must have a fail-closed
branch (``exit 1``) for the not-found case, never a silent skip.

Neuters: (a) revert ``typecheck`` to a bare ``.venv/bin/mypy`` -> the static
guard goes RED; (b) delete the fail-closed branch -> the behavioural test
(no mypy anywhere) goes RED.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
CI = ROOT / ".github/workflows/ci.yml"


def ci_make_targets(ci_text: str) -> set[str]:
    """Targets named by ``make <target>`` in any CI ``run:`` step."""
    targets: set[str] = set()
    data = yaml.safe_load(ci_text)
    for job in (data.get("jobs") or {}).values():
        for step in job.get("steps", []):
            for line in (step.get("run") or "").splitlines():
                m = re.match(r"\s*make\s+([A-Za-z0-9_.-]+)", line)
                if m:
                    targets.add(m.group(1))
    return targets


def parse_makefile(text: str) -> dict[str, tuple[list[str], str]]:
    """target -> (prerequisites, recipe text), tab-indented recipe lines."""
    out: dict[str, tuple[list[str], str]] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^([A-Za-z0-9_.-]+):(?!=)\s*([^#\n]*)", lines[i])
        if m:
            deps = m.group(2).split()
            recipe: list[str] = []
            i += 1
            while i < len(lines) and (
                lines[i].startswith("\t") or not lines[i].strip()
            ):
                recipe.append(lines[i])
                i += 1
            out[m.group(1)] = (deps, "\n".join(recipe))
            continue
        i += 1
    return out


def violations(makefile_text: str, targets: set[str]) -> list[str]:
    rules = parse_makefile(makefile_text)
    seen: set[str] = set()
    todo = list(targets)
    bad: list[str] = []
    while todo:
        t = todo.pop()
        if t in seen or t not in rules:
            continue
        seen.add(t)
        deps, recipe = rules[t]
        todo += deps
        tools = set(re.findall(r"\.venv/bin/([A-Za-z0-9_.-]+)", recipe))
        for tool in sorted(tools):
            pattern = (
                rf"\[ -x \.venv/bin/{re.escape(tool)} \] && echo "
                rf"\.venv/bin/{re.escape(tool)} \|\| command -v {re.escape(tool)}"
            )
            if not re.search(pattern, recipe):
                bad.append(f"{t}: bare .venv/bin/{tool} (no PATH fallback)")
            elif "exit 1" not in recipe:
                bad.append(f"{t}: {tool} lookup has no fail-closed exit 1")
    return bad


def test_ci_invokes_the_targets_we_expect() -> None:
    assert {"typecheck", "security-lint"} <= ci_make_targets(CI.read_text())


def test_every_ci_invoked_make_target_resolves_its_tools() -> None:
    assert violations(MAKEFILE.read_text(), ci_make_targets(CI.read_text())) == []


@pytest.mark.parametrize(
    "recipe",
    [
        "typecheck:\n\t@.venv/bin/mypy src/\n",
        "typecheck:\n\t@[ -x .venv/bin/mypy ] && echo .venv/bin/mypy || command -v mypy\n",
        "typecheck: lint\nlint:\n\t@.venv/bin/ruff check\n",
    ],
    ids=["bare", "fallback-without-fail-closed", "bare-in-prerequisite"],
)
def test_the_guard_flags_a_bare_or_non_failing_lookup(recipe: str) -> None:
    assert violations(recipe, {"typecheck"})


def _run_typecheck(
    tmp_path: Path, path_dirs: list[Path]
) -> subprocess.CompletedProcess[str]:
    """Run the REAL typecheck recipe from a scratch dir that has NO .venv."""
    (tmp_path / "Makefile").write_text(MAKEFILE.read_text())
    (tmp_path / "src").mkdir(exist_ok=True)
    make = shutil.which("make")
    assert make
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    (shim / "make").symlink_to(make)
    env = {"PATH": os.pathsep.join([str(shim), *map(str, path_dirs)])}
    return subprocess.run(  # noqa: S603 - fixed argv
        [str(shim / "make"), "typecheck"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _fake_mypy(directory: Path, exit_code: int) -> Path:
    directory.mkdir(exist_ok=True)
    tool = directory / "mypy"
    tool.write_text(f'#!/bin/sh\necho fake-mypy-ran "$@"\nexit {exit_code}\n')
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return directory


def test_without_a_venv_the_recipe_uses_mypy_from_path(tmp_path: Path) -> None:
    r = _run_typecheck(tmp_path, [_fake_mypy(tmp_path / "bin", 0)])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fake-mypy-ran src/" in r.stdout


def test_a_failing_mypy_from_path_fails_the_target(tmp_path: Path) -> None:
    r = _run_typecheck(tmp_path, [_fake_mypy(tmp_path / "bin", 1)])
    assert r.returncode != 0
    assert "Type checking passed" not in r.stdout


def test_no_mypy_anywhere_fails_closed_with_a_clear_message(tmp_path: Path) -> None:
    """Neuter: remove the ``exit 1`` branch -> the target 'passes' or dies
    with 127 instead of the clear message -> RED."""
    r = _run_typecheck(tmp_path, [])
    assert r.returncode != 0
    assert "mypy not found" in r.stdout
    # It must stop at the lookup, not fall through and die by accident.
    assert "Running type checker" not in r.stdout
    assert "Type checking passed" not in r.stdout
