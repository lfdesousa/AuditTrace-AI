"""SSOT + drift guard for the release version-bearing file set (SPEC D3).

Background (the bug, CONFIRMED at two independent v1.26.0 cuts,
2026-09-10): ``make release`` and ``scripts/release/runner.py::BUMP_FILES``
each hardcoded their OWN list of "files a version bump touches", and the
two lists silently diverged — see ``scripts/release/version_files.py``'s
module docstring for the full incident. The fix is ONE shared source of
truth (:data:`scripts.release.version_files.BUMP_FILES`) consumed by BOTH
the runner (import identity) and the Makefile (`release-bump-files`
helper).

This module proves the fix two ways:

1. ``test_runner_bump_files_is_the_ssot`` — the runner's ``BUMP_FILES``
   IS (identity, not merely equal to) the SSOT tuple, so a future
   hand-typed copy in the runner is structurally impossible, not just
   discouraged.
2. ``test_make_release_dirties_exactly_the_ssot_set`` — the non-vacuous
   proof: runs a REAL ``make release`` bump in a throwaway git worktree
   and asserts the files ``git status`` reports dirty afterwards are
   EXACTLY the SSOT set. This is the "staged-set == dirtied-set" gate the
   spec calls for. Falsifiability: temporarily drop (or add) a file in
   ``version_files.BUMP_FILES`` and this test goes RED, because the files
   that land on disk don't change but the SSOT it's compared against
   does — reproduced during this build (neuter run) and restored before
   commit.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from scripts.release import runner
from scripts.release.version_files import BUMP_FILES

REPO_ROOT = Path(__file__).resolve().parent.parent


def _current_pyproject_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _bump_patch(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def test_runner_bump_files_is_the_ssot() -> None:
    """``runner.BUMP_FILES`` must literally BE ``version_files.BUMP_FILES``
    (identity) — a re-typed copy is exactly how the two drifted before."""
    assert runner.BUMP_FILES is BUMP_FILES


def test_make_release_dirties_exactly_the_ssot_set(tmp_path: Path) -> None:
    """Non-vacuous drift guard: after a REAL ``make release`` bump in a
    throwaway worktree, the set of files ``git status`` reports dirty MUST
    equal ``BUMP_FILES`` exactly — no more, no fewer.
    """
    worktree = tmp_path / "release-dry-bump-worktree"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        # Share the already-installed venv (no reinstall needed) — the
        # Makefile shells out to `.venv/bin/pytest` with a path relative to
        # its own cwd, so the worktree needs its own `.venv` entry.
        (worktree / ".venv").symlink_to((REPO_ROOT / ".venv").resolve())
        bump_version = _bump_patch(_current_pyproject_version())
        result = subprocess.run(
            ["make", "release", f"VERSION={bump_version}"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (
            f"make release VERSION={bump_version} failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        )
        # NOTE: do NOT `.strip()` the whole stdout before splitting — that
        # eats the leading status-column space of the FIRST line only,
        # shifting `line[3:]` by one char for exactly that entry (e.g.
        # ".env.ci" -> "env.ci"). Split first, then drop blank lines.
        dirtied = {line[3:] for line in status.stdout.splitlines() if line.strip()}
        assert dirtied == set(BUMP_FILES), (
            f"`make release` dirtied {dirtied!r} but the SSOT "
            f"(scripts/release/version_files.py::BUMP_FILES) says "
            f"{set(BUMP_FILES)!r} — these MUST match exactly (SPEC D3): "
            "either the Makefile's sed commands or the SSOT tuple drifted."
        )
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
