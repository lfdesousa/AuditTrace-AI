"""Pins the `.gitignore` guard that stops a PR-body file from ever being
staged into this PUBLIC repo (#397, marker
ADR049-PRBODY-AUTOENFORCE-20260803).

PR bodies are PRIVATE (they live in
``~/work/audittrace-private/pr-bodies/`` and are only ever pointed to via
the ``PR_BODY`` env var — see ``.githooks/pre-push``). The guard is a set
of `.gitignore` globs; this test makes it non-vacuous by actually
shelling out to ``git check-ignore`` (the same mechanism `git add` /
`git status` use) rather than re-implementing glob matching in Python,
so a regression in the real `.gitignore` file is what fails the test —
not a parallel model of it.

Regression pinned: the ORIGINAL guard (`/PR-BODY-*.md`, `/*.pr-body.md`)
missed the bare, documented convention basename `PR-BODY.md`
(`feedback_gh_pr_body_from_file`) and the lowercase spelling
`pr-body.md` — caught by independent review 2026-08-03. `git
check-ignore -v` proved both slipped through unignored.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Shapes the guard MUST catch: the exact documented convention basename,
# a case variant, a suffixed variant, and a prefixed variant — the four
# concrete examples from the review that found the original guard too
# narrow.
_MUST_BE_IGNORED = (
    "PR-BODY.md",
    "pr-body.md",
    "PR-BODY-393.md",
    "my-PR-BODY-thing.md",
)

# A subdirectory case — the guard is deliberately NOT rooted (`/`-prefixed)
# so a stray body anywhere in the tree is caught, not just at repo root.
_MUST_BE_IGNORED_NESTED = "docs/backlog/PR-BODY.md"

# Sanity control: an ordinary markdown file must NOT be swept up by the
# guard (it would be a silent-drop bug, not a security win).
_MUST_NOT_BE_IGNORED = "some-normal-file.md"


def _git_check_ignore(relative_path: str) -> int:
    """Return the real ``git check-ignore -q`` exit code for a path
    relative to the repo root — 0 = ignored, 1 = not ignored, anything
    else is a hard failure (e.g. bad git invocation)."""
    result = subprocess.run(  # noqa: S603, S607 — git is PATH-resolved and required for this repo to exist at all
        ["git", "check-ignore", "-q", relative_path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 1), (
        f"git check-ignore returned an unexpected code "
        f"{result.returncode} for {relative_path!r}: {result.stderr}"
    )
    return result.returncode


@pytest.mark.parametrize("filename", _MUST_BE_IGNORED)
def test_pr_body_shape_is_ignored(filename: str) -> None:
    assert _git_check_ignore(filename) == 0, (
        f"{filename!r} should be ignored by .gitignore (PR-body guard, "
        f"#397) but git check-ignore reported it is NOT — a real PR body "
        f"named like this could be committed to the public repo."
    )


def test_nested_pr_body_shape_is_ignored() -> None:
    """The guard is not rooted to the repo top level — a stray body under
    any subdirectory must be caught too."""
    assert _git_check_ignore(_MUST_BE_IGNORED_NESTED) == 0


def test_ordinary_markdown_file_is_not_ignored() -> None:
    """Negative control: the guard must not be so broad it silently drops
    unrelated markdown files from ever being staged."""
    assert _git_check_ignore(_MUST_NOT_BE_IGNORED) == 1


def test_gitignore_declares_the_pr_body_guard_patterns() -> None:
    """Belt + braces: the specific glob lines this test relies on are
    actually present in `.gitignore` (catches someone deleting the
    guard's *lines* while leaving unrelated `.gitignore` entries alone —
    `git check-ignore` alone wouldn't distinguish that from an entirely
    different guard that happens to still match these fixtures)."""
    gitignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*PR-BODY*.md" in gitignore_text
    assert "*pr-body*.md" in gitignore_text
