#!/usr/bin/env python3
"""TOKEN-GUARD mechanical drift guard (ties #429 / TH-D #434).

Fails any AGENT DEF — the markdown files that teach an LLM agent its own
bash usage — whose taught bash would put a raw JWT into that agent's own
transcript:

  * ``audittrace-login --show`` used WITHOUT nearby acknowledgement that the
    default is now redacted (no "redacted"/"fingerprint"/"token-guard"
    marker word within the surrounding text window). Since TOKEN-GUARD
    (2026-08-11, ``scripts/audittrace-login``), plain ``--show`` prints a
    REDACTED fingerprint by default, so a bare, unmarked mention reads as
    the STALE pre-fix teaching (raw-print) rather than the current safe
    default — exactly the "prose, not a mechanical gate" failure this guard
    exists to close (`feedback_policies_must_be_mechanically_inviolable`).
    An agent def correctly describing the new default states so nearby
    (see the release-agent / memory-curator defs for the pattern); one that
    doesn't is treated as drifted/ambiguous and flagged fail-closed.
    ``--show-unsafe`` is the explicit, deliberately-reviewed escape hatch
    for the rare script that needs the raw value and is intentionally NOT
    flagged by the raw-show pattern — an agent def should still never be
    TAUGHT it, but that is a manual-review concern, not something this
    mechanical pattern can distinguish from a legitimate non-agent-def
    script reference.
  * a direct ``cat``/``jq`` read of the raw token STORE (``tokens.json``),
    bypassing the redaction (and the sanctioned in-process resolver)
    entirely.

This is LAYER 3 (defense-in-depth) of the TOKEN-GUARD spec
(``spec-token-guard-kill-show-20260811.md``). Layer 1 — killing the leak at
its source in ``scripts/audittrace-login`` — is the REAL fix; this guard is
best-effort. Bash-pattern-matching is inherently porous (an agent def could
obfuscate the pattern in ways a regex misses), which is *why* layer 1 is
primary and this guard is a drift catcher, not a security boundary on its
own.

Scans the known agent-def locations:
  * ``$AUDITTRACE_REPO_ROOT/.claude/agents/*.md``       (public; ``.claude/``
    is gitignored, so this is the OPERATOR's real checkout, not necessarily
    ``git``'s notion of "this repo" — see the MECHANICAL ENFORCEMENT note
    below for why that distinction matters)
  * ``$AUDITTRACE_REPO_ROOT/.claude/settings.local.json``  (the Bash
    allow/deny permission arrays — also gitignored, also real-checkout-only)
  * ``$AUDITTRACE_PRIVATE_DIR/sdlc/agents/**/*.md``     (private SDLC fleet
    defs, recursive — covers the ``opencode/`` variant too; default
    ``~/work/audittrace-private``; SKIPPED gracefully, not a failure, when
    the directory doesn't exist — e.g. a CI runner with no private checkout)

``AUDITTRACE_REPO_ROOT`` defaults to ``/home/lfdesousa/work/AuditTrace-AI``
(the laptop's real checkout — the Portability invariant's "laptop default"
for this value; override for another machine). This is deliberately an env
var with an absolute default, NOT ``Path(__file__).resolve().parent.parent``
— this script is invoked from ARBITRARY git worktrees / CI checkouts of this
repo (every worktree has its own copy of ``scripts/``), but the agent defs
that matter are the operator's ONE real, gitignored ``.claude/`` directory.
Deriving the target from ``__file__`` would silently scan whatever worktree
happened to run the guard (usually empty — ``.claude/`` isn't copied into a
worktree) instead of the real defs, which is exactly how a worktree-scoped
build could "pass" this guard while the real, operator-facing defs stayed
dirty (the failure mode this REJECT identified, 2026-08-11).

MECHANICAL ENFORCEMENT: both the public and private locations are OUTSIDE
what ``git`` tracks for THIS repo (``.claude/`` is gitignored; the private
dir is a separate repo entirely), so this cannot be a git-staged-diff
pre-commit hook the way ``scripts/check-no-private-content.sh`` is — no
``git diff --cached`` for this repo ever lists either path. Instead it is
wired as an ``always_run: true, pass_filenames: false`` local hook in
``.pre-commit-config.yaml`` (id ``token-guard``) — it runs unconditionally on
every commit, ignores whatever files are staged, and scans the FIXED
absolute/config locations above. Also exposed as ``make token-guard`` for a
standalone run. A guard that only a human remembers to run by hand is prose,
not a gate (`feedback_policies_must_be_mechanically_inviolable`) — this is
what closes that gap.

Usage:
    scripts/check-agent-def-token-guard.py                 # scan real locations
    scripts/check-agent-def-token-guard.py --paths a.md b.md   # scan given files
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Matches "audittrace-login ... --show", allowing the two tokens to be
# separated by up to 60 chars/lines (covers prose wrapped across a bullet),
# but NOT "--show-unsafe" (negative lookahead on the suffix).
RAW_SHOW_PATTERN = re.compile(r"audittrace-login(?:[^\n]|\n){0,60}?--show(?!-unsafe)\b")

# A match is treated as the CURRENT safe default (not a violation) only if
# one of these marker words appears in the text window around it — i.e. the
# agent def explicitly acknowledges the redacted-by-default behaviour rather
# than silently repeating the stale pre-TOKEN-GUARD raw-print teaching.
_SAFE_SHOW_MARKERS = ("redacted", "fingerprint", "token-guard", "token_fingerprint")
_SAFE_MARKER_WINDOW_BEFORE = 40
_SAFE_MARKER_WINDOW_AFTER = 320

# Matches a direct shell read of the raw token STORE: `cat ...tokens.json`,
# `jq ... tokens.json`, or a Python `open(...tokens.json...)` call. Deliberately
# narrow (real command/call shapes only) so it does not fire on ordinary prose
# like "reads the token from ~/.config/audittrace/tokens.json inside the
# process" (the sanctioned in-process helper's own docstring language).
TOKENS_JSON_READ_PATTERN = re.compile(
    r"\b(?:cat|jq)\b[^\n]{0,80}tokens\.json|open\([^)\n]{0,80}tokens\.json",
    re.IGNORECASE,
)

DEFAULT_PRIVATE_DIR = Path(
    os.environ.get(
        "AUDITTRACE_PRIVATE_DIR", str(Path.home() / "work" / "audittrace-private")
    )
)

# The operator's real, gitignored checkout — NOT derived from ``__file__``
# (see the module docstring's MECHANICAL ENFORCEMENT note for why: this
# script runs from arbitrary worktrees/CI clones, but the agent defs that
# matter live in the ONE real checkout). Laptop default per the Portability
# invariant; override via env for another machine.
DEFAULT_REPO_ROOT = Path(
    os.environ.get("AUDITTRACE_REPO_ROOT", "/home/lfdesousa/work/AuditTrace-AI")
)


def _line_at(text: str, offset: int) -> tuple[int, str]:
    """Return ``(1-based line number, stripped line text)`` for a char offset."""
    lineno = text.count("\n", 0, offset) + 1
    lines = text.splitlines()
    line = lines[lineno - 1].strip() if 0 <= lineno - 1 < len(lines) else ""
    return lineno, line


def _has_safe_marker_nearby(text: str, start: int, end: int) -> bool:
    window = text[
        max(0, start - _SAFE_MARKER_WINDOW_BEFORE) : min(
            len(text), end + _SAFE_MARKER_WINDOW_AFTER
        )
    ].lower()
    return any(marker in window for marker in _SAFE_SHOW_MARKERS)


def find_violations(text: str) -> list[str]:
    """Return human-readable violation descriptions found in ``text``.

    Pure function — no filesystem access — so it is unit-testable against
    synthetic fixtures without depending on the real (gitignored / private)
    agent-def locations existing on the machine running the test.
    """
    violations: list[str] = []
    for match in RAW_SHOW_PATTERN.finditer(text):
        if _has_safe_marker_nearby(text, match.start(), match.end()):
            continue  # explicitly documented as the current redacted default
        lineno, line = _line_at(text, match.start())
        violations.append(
            f"line {lineno}: teaches the raw `audittrace-login --show` form "
            "with no nearby acknowledgement that the default is now redacted "
            "(stale since TOKEN-GUARD; migrate to the in-process "
            "scripts/deploy/memory.py helper, the redacted `--show` default, "
            f"or `--ensure` for a bare session-liveness check): {line!r}"
        )
    for match in TOKENS_JSON_READ_PATTERN.finditer(text):
        lineno, line = _line_at(text, match.start())
        violations.append(
            f"line {lineno}: reads the raw token store directly (tokens.json) "
            f"instead of the in-process resolver: {line!r}"
        )
    return violations


def scan_paths(paths: list[Path]) -> dict[Path, list[str]]:
    """Scan each path in ``paths``; return ``{path: violations}`` for hits only."""
    results: dict[Path, list[str]] = {}
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        violations = find_violations(text)
        if violations:
            results[path] = violations
    return results


def default_scan_targets(
    *,
    public_agents_dir: Path | None = None,
    private_dir: Path | None = None,
    settings_file: Path | None = None,
) -> list[Path]:
    """The real agent-def locations this guard protects, best-effort.

    All three are optional-existence: a machine without the private repo
    (e.g. a fresh CI runner) simply contributes zero targets from that side
    rather than failing — this guard's job is to catch drift where the files
    exist, not to assert they must exist.

    ``public_agents_dir`` and ``private_dir`` default from
    :data:`DEFAULT_REPO_ROOT` / :data:`DEFAULT_PRIVATE_DIR` (env-overridable
    absolute paths, never ``__file__``-relative — see the module docstring).
    ``settings_file`` defaults to ``settings.local.json`` alongside whichever
    ``public_agents_dir`` resolved to (its parent, i.e. the ``.claude/`` dir)
    so an explicit ``public_agents_dir`` override (as tests use) stays
    self-contained instead of also picking up the real machine's file.
    """
    targets: list[Path] = []
    resolved_public = public_agents_dir or (DEFAULT_REPO_ROOT / ".claude" / "agents")
    if resolved_public.is_dir():
        targets.extend(sorted(resolved_public.glob("*.md")))
    resolved_settings = settings_file or (
        resolved_public.parent / "settings.local.json"
    )
    if resolved_settings.is_file():
        targets.append(resolved_settings)
    resolved_private = private_dir or DEFAULT_PRIVATE_DIR
    private_agents_dir = resolved_private / "sdlc" / "agents"
    if private_agents_dir.is_dir():
        targets.extend(sorted(private_agents_dir.rglob("*.md")))
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paths",
        nargs="*",
        type=Path,
        default=None,
        help="explicit files to scan (default: the known agent-def locations)",
    )
    args = parser.parse_args(argv)

    targets = args.paths if args.paths else default_scan_targets()
    if not targets:
        print(
            "[token-guard] no agent-def files found to scan (neither "
            ".claude/agents/ nor the private sdlc/agents/ dir is present on "
            "this machine) — best-effort, not a failure."
        )
        return 0

    results = scan_paths(targets)
    if not results:
        print(f"[token-guard] clean — scanned {len(targets)} agent-def file(s).")
        return 0

    print(f"[token-guard] {len(results)} agent-def file(s) with violations:")
    for path, violations in results.items():
        print(f"\n{path}:")
        for v in violations:
            print(f"  - {v}")
    print(
        "\n[token-guard] FAIL — an agent def teaches the raw `--show` form or "
        "a direct tokens.json read. Migrate to the in-process "
        "scripts/deploy/memory.py helper (see the CREDENTIAL RULE in any "
        "agent def), the redacted `--show` default, or `--ensure`. Never "
        "teach `--show-unsafe` to an agent def."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
