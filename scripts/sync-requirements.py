#!/usr/bin/env python3
"""Regenerate requirements.txt from pyproject.toml.

Single source of truth for dependencies is pyproject.toml. This script
renders the runtime + dev dependency lists into requirements.txt
deterministically, so the Docker image build path
(``pip install -r requirements.txt``) cannot silently diverge from the
local-dev install path (``pip install -e ".[dev]"``).

Why this exists: 2026-05-07 tier-A live-evidence capture (PR #42)
discovered that ``pyhanko`` + ``pyhanko-certvalidator`` had been added
to pyproject.toml but not to requirements.txt. ``make test`` passed
locally; the deployed image silently shipped without the deps; the
PAdES signature validator fell through to ``check_unavailable`` on
every chunk. The bug was a build-pipeline drift, not a code bug — and
exactly the failure mode this script eliminates.

Usage::

    python scripts/sync-requirements.py            # write requirements.txt
    python scripts/sync-requirements.py --check    # exit 1 on drift, with diff

Wired into the workflow at three layers:

* ``make sync-requirements`` (regenerate)
* ``pre-commit`` hook ``requirements-sync`` (--check, blocks commit)
* CI job ``requirements-sync`` in ``.github/workflows/ci.yml``
  (--check, blocks merge)
"""

from __future__ import annotations

import difflib
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
REQUIREMENTS = ROOT / "requirements.txt"

HEADER = """\
# DO NOT EDIT — regenerated from pyproject.toml by scripts/sync-requirements.py.
#
# Source of truth: pyproject.toml ([project].dependencies +
# [project.optional-dependencies].dev). Run `make sync-requirements`
# after touching dependencies. The pre-commit hook `requirements-sync`
# and the CI job of the same name refuse to land drifted state.
#
# Why this file exists at all (we ship from a lockfile, not pyproject):
#   - Docker image build uses `pip install -r requirements.txt`.
#   - 2026-05-07 tier-A live-evidence capture caught pyhanko +
#     pyhanko-certvalidator missing here, producing a false
#     `signature_status="check_unavailable"` audit signal because the
#     deployed image lacked the deps. The drift-guard closes that class.

"""


def _render_section(title: str, deps: list[str]) -> list[str]:
    return [f"# {title}\n", *(f"{d}\n" for d in deps), "\n"]


def render_requirements() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    runtime: list[str] = data["project"]["dependencies"]
    dev: list[str] = data["project"]["optional-dependencies"]["dev"]

    lines: list[str] = [HEADER]
    lines += _render_section("Runtime dependencies", runtime)
    lines += _render_section("Dev dependencies (extras = [dev])", dev)
    rendered = "".join(lines)
    # Trim trailing blank lines down to a single newline.
    return rendered.rstrip() + "\n"


def main(argv: list[str]) -> int:
    rendered = render_requirements()

    if "--check" in argv:
        current = (
            REQUIREMENTS.read_text(encoding="utf-8") if REQUIREMENTS.exists() else ""
        )
        if current == rendered:
            return 0
        sys.stderr.write(
            "ERROR: requirements.txt is out of sync with pyproject.toml.\n"
            "Run `make sync-requirements` and commit the result.\n\n"
        )
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile="requirements.txt (current)",
            tofile="requirements.txt (regenerated)",
        )
        sys.stderr.writelines(diff)
        return 1

    REQUIREMENTS.write_text(rendered, encoding="utf-8")
    line_count = rendered.count("\n")
    print(f"Wrote {REQUIREMENTS.relative_to(ROOT)} ({line_count} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
