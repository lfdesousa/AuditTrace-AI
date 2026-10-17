"""Single source of truth for the release version-bearing file set (SPEC D3).

Background (the bug, CONFIRMED at two independent v1.26.0 cuts,
2026-09-10). The Makefile ``release`` target and
:mod:`scripts.release.runner` each hardcoded their OWN list of "files a
version bump touches", and the two lists silently diverged:

* ``make release VERSION=X.Y.Z`` actually bumps 5 files: ``pyproject.toml``,
  ``charts/audittrace/Chart.yaml``, ``docker-compose.yml``, ``.env.ci``,
  ``.env.dev-real-llm.example``.
* ``scripts/release/runner.py::BUMP_FILES`` named a DIFFERENT 5:
  ``pyproject.toml``, ``charts/audittrace/Chart.yaml``,
  ``docs/reference/audittrace/openapi.yaml``,
  ``tests/fixtures/openapi.snapshot.yaml``, ``README.md`` — three of which
  do not change on an ordinary bump (the OpenAPI snapshot is only
  regenerated defensively; it has no embedded version string today) and
  two of the real bump targets (``docker-compose.yml``, the ``.env.*``
  files) were simply missing.

Running the automated runner non-dry with the stale tuple would stage 3
zero-diff files and MISS the 3 that actually changed, leaving them
dirty-but-uncommitted on disk after the branch was pushed.

This module is the ONE place that "which files carry the version" is
listed. Both consumers below import :data:`BUMP_FILES` directly (identity,
never a re-typed copy — a copy is exactly how the two drifted before):

* :mod:`scripts.release.runner` — ``BUMP_FILES = version_files.BUMP_FILES``,
  staged verbatim in R3's ``git add``.
* The Makefile ``release`` target's ``release-bump-files`` helper — prints
  this tuple space-joined for the diff/next-steps guidance, so the
  human-facing hint can never again say something the sed commands don't
  do.

``tests/test_release_bump_files_ssot.py`` is the drift guard: it runs a
REAL ``make release`` bump in a throwaway git worktree and asserts the
files ``git status`` reports dirty afterwards are EXACTLY this set — so a
future edit to either the Makefile's sed commands or this tuple that
leaves the other behind fails loudly, before a human notices at release
time.

Out of scope (ADR-055's territory, untouched here): WHICH files are the
canonical version pin sites (``pyproject.toml`` + ``Chart.yaml::appVersion``
remain the two sources ``tests/test_version_drift.py`` cross-checks) and
the human-gated tag push.
"""

from __future__ import annotations

# The fixed set of files `make release VERSION=...` actually writes to disk
# (Makefile `release` target). Order is the order the Makefile bumps them in;
# consumers treat this as a set, not a sequence, but the order is kept
# readable for humans scanning a diff.
BUMP_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "charts/audittrace/Chart.yaml",
    "docker-compose.yml",
    ".env.ci",
    ".env.dev-real-llm.example",
)
