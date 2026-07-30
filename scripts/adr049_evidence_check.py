#!/usr/bin/env python3
"""ADR-049 evidence-gate parser — single source of truth (local == CI).

Ported VERBATIM from the inline ``python3 <<'PY'`` heredoc that used to
live in the ``evidence-check`` job of ``.github/workflows/ci.yml``. This
module is now the ONE place the rule is expressed; both the CI job and the
local ``make check-pr-body`` target call it, so a cloud-only blind spot
(the 2026-07-30 inline-backtick regression, #396) cannot re-open.

Required PR-body sections (ADR-049):
    ## Verification (unit)
    ## Validation (end-to-end against deployed image)
    ## Reconstruction (audit artefacts)

Heuristic for "populated": each section must contain AT LEAST ONE of
(a) a checked box ``- [x]`` (case-insensitive), OR (b) a fenced code
block whose content is not purely template placeholders (i.e. contains
characters outside the ``<...>`` placeholder shape). Empty checklists
with all boxes unchecked fail the gate — checking a box is a deliberate,
visible action that confirms the contributor produced the artefact named
by that line. Inline-backtick prose is NOT a code block and does NOT
populate a section (the specific bug #396 guards against).

CLI: reads a PR body from ``sys.argv[1]`` (a file path) or, if no
argument is given, from stdin. Prints the SAME ``::error::``-style lines
CI prints and exits 0 (pass) / 1 (fail).
"""

from __future__ import annotations

import logging
import pathlib
import re
import sys

logger = logging.getLogger(__name__)

# Required sections keyed by (label, heading regex). The regex matches
# against ``## <heading>`` so ``## Verification (unit)`` also matches
# ``^##\s+Verification\b``.
_REQUIRED: list[tuple[str, str]] = [
    ("Verification", r"^##\s+Verification\b"),
    ("Validation", r"^##\s+Validation\b"),
    ("Reconstruction", r"^##\s+Reconstruction\b"),
]


def _split_sections(body: str) -> dict[str, list[str]]:
    """Split the body into a dict of {H2 heading text: [body lines]}."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        h2 = re.match(r"^##\s+(.+)$", line)
        if h2:
            current = h2.group(1).strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def _section_is_populated(lines: list[str]) -> bool:
    """A section counts as populated iff it has a checked box OR a
    non-placeholder fenced code block."""
    # Signal A: at least one checked box.
    for raw in lines:
        if re.match(r"^\s*-\s*\[x\]", raw, flags=re.IGNORECASE):
            return True
    # Signal B: a fenced code block whose content is not purely
    # <placeholder> markers.
    in_fence = False
    fence_buf: list[str] = []
    for raw in lines:
        if raw.strip().startswith("```"):
            if in_fence:
                # End of fence — inspect what we collected.
                collected = "\n".join(fence_buf).strip()
                stripped_placeholders = re.sub(r"<[^>]*>", "", collected).strip()
                if stripped_placeholders:
                    return True
                fence_buf = []
            in_fence = not in_fence
        elif in_fence:
            fence_buf.append(raw)
    return False


def check_evidence(body: str) -> list[str]:
    """Return the list of failure strings for a PR body (empty = pass).

    Pure and importable — no I/O, no exit. The CLI wraps this to print
    the ``::error::`` lines and set the exit code.
    """
    sections = _split_sections(body)
    failures: list[str] = []
    for label, pattern in _REQUIRED:
        matched_key = next(
            (k for k in sections if re.match(pattern, f"## {k}")),
            None,
        )
        if matched_key is None:
            failures.append(f"Missing required section: ## {label}")
            continue
        if not _section_is_populated(sections[matched_key]):
            failures.append(
                f"Section ## {matched_key} has no checked box "
                f"and no populated code block — looks like the "
                f"template is unfilled"
            )
    return failures


def _read_body(argv: list[str]) -> str:
    """Read the PR body from a file arg or stdin."""
    if len(argv) > 1 and argv[1]:
        return pathlib.Path(argv[1]).read_text(encoding="utf-8")
    return sys.stdin.read()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Prints CI-identical ``::error::`` lines, returns
    the process exit code (0 pass / 1 fail)."""
    argv = list(sys.argv if argv is None else argv)
    body = _read_body(argv)
    failures = check_evidence(body)
    if failures:
        print("::error::ADR-049 evidence gate FAILED")
        for f in failures:
            print(f"::error::  - {f}")
        print(
            "::error::See ADR-049 + .github/pull_request_template.md. "
            "Check at least one box per section, OR paste real "
            "command output into a code block. No override available."
        )
        return 1
    print("ADR-049 evidence gate: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
