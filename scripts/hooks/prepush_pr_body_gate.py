#!/usr/bin/env python3
"""Pre-push decision logic for the ADR-049 PR-body evidence gate (#397).

Marker: ADR049-PRBODY-AUTOENFORCE-20260803.

This module is the extracted, unit-testable "brain" behind
``.githooks/pre-push``. The hook shell script does I/O (reads git's
pre-push stdin protocol, invokes gitleaks) and then delegates the
PR-body decision entirely to :func:`run_pr_body_gate` here, so the
decision logic can be exercised by pytest WITHOUT invoking a real
``git push`` (per the spec's R4).

**Single source of truth (#396 invariant).** This module does NOT
re-implement the ADR-049 evidence rule. For the "PR_BODY is set to a
real path" case it calls ``scripts.adr049_evidence_check.main``
directly, so the pass/fail decision AND the printed ``::error::`` lines
are byte-identical to what CI's ``evidence-check`` job and
``make check-pr-body`` already produce — there is exactly one place
that decides what a populated PR-body section looks like.

PR bodies themselves live OUTSIDE this repo (see
``~/work/audittrace-private/pr-bodies/`` in the private sibling repo) —
``PR_BODY`` is expected to be an absolute path into that private tree.
Nothing in this module or the hook ever reads a repo-relative default
path or commits body content into the public repo.

Decision table (``PR_BODY`` env var):

=================  =========================================  ==========
Value              Meaning                                      Behaviour
=================  =========================================  ==========
unset / empty      contributor didn't opt in                   allow; print a non-blocking reminder IFF the push
                                                                 target is not ``main``/``master``
``none`` (any case) explicit, acknowledged skip                 allow silently
any other string   path to a PR-body file to pre-validate       run ``adr049_evidence_check`` on it; BLOCK (exit 1)
                                                                 on failure, allow (exit 0) on pass
=================  =========================================  ==========
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# This module lives in scripts/hooks/ (a real package — see
# scripts/hooks/__init__.py — so pytest-cov can measure it as
# `scripts.hooks.prepush_pr_body_gate`, unlike scripts/adr049_evidence_check.py
# which is a deliberately loose, unmeasured script). adr049_evidence_check.py
# itself is a sibling of scripts/hooks/, one directory up, and is NOT a
# package member — insert its directory onto sys.path so a plain
# `import adr049_evidence_check` resolves it, whether this module is run
# directly (`python3 scripts/hooks/prepush_pr_body_gate.py`) or imported
# normally from a test (`from scripts.hooks import prepush_pr_body_gate`).
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import adr049_evidence_check  # noqa: E402  (see sys.path shim above)

_MAIN_REFS = frozenset({"refs/heads/main", "refs/heads/master"})

_REMINDER = (
    "Reminder: set PR_BODY=<path-to-your-PR-body-file> to pre-validate it "
    "against the ADR-049 evidence gate before you push (or PR_BODY=none to "
    "acknowledge and skip this reminder). See ADR-049."
)

_BLOCK_TAIL = (
    "::error::Push BLOCKED by the pre-push ADR-049 gate (#397) — "
    "fix the body, see ADR-049. No override; PR_BODY=none is the only "
    "acknowledged skip."
)


def classify_pr_body_env(raw: str | None) -> str:
    """Classify the raw ``PR_BODY`` env-var value into one of three modes.

    Returns ``"unset"`` (not set or empty string), ``"skip"`` (the
    literal, case-insensitive value ``none`` — an explicit, acknowledged
    opt-out), or ``"path"`` (anything else — treated as a file path to
    validate).
    """
    if raw is None or raw == "":
        return "unset"
    if raw.strip().lower() == "none":
        return "skip"
    return "path"


def is_protected_ref(remote_ref: str) -> bool:
    """True for the ``main``/``master`` remote ref.

    Feature-branch pushes are where the non-blocking reminder helps (a
    PR is about to be opened); pushes directly to a protected branch
    don't need it — and per R1, unset PR_BODY must never block either
    way, so this only gates whether the REMINDER prints, not exit code.
    """
    return remote_ref in _MAIN_REFS


def run_pr_body_gate(
    pr_body_env: str | None,
    remote_ref: str,
    *,
    adr049_main: Callable[[list[str]], int] = adr049_evidence_check.main,
) -> int:
    """Evaluate the ADR-049 pre-push gate and print the outcome.

    Pure enough to unit-test directly: the only I/O is (a) whatever
    ``adr049_main`` performs internally (real file read, by default —
    injectable for tests) and (b) ``print()`` for user-facing output,
    observable via ``capsys`` exactly like the existing
    ``tests/test_adr049_evidence_check.py`` CLI tests. Returns the
    process exit code the caller (the shell hook) must propagate.
    """
    mode = classify_pr_body_env(pr_body_env)

    if mode == "skip":
        return 0

    if mode == "unset":
        if not is_protected_ref(remote_ref):
            print(_REMINDER)
        return 0

    # mode == "path": delegate ENTIRELY to the single source of truth for
    # both the pass/fail decision and the CI-identical ::error:: output.
    assert pr_body_env is not None  # narrows for mypy; "path" implies non-empty
    rc = adr049_main(["adr049_evidence_check.py", pr_body_env])
    if rc != 0:
        print(_BLOCK_TAIL)
    return rc


def _parse_remote_ref(argv: list[str]) -> str:
    """Extract the destination remote ref from argv, if the shell wrapper
    passed one (git's pre-push stdin protocol line 3). Defaults to the
    empty string (treated as a non-protected ref) when absent."""
    return argv[1] if len(argv) > 1 and argv[1] else ""


def main(argv: list[str] | None = None) -> int:
    """CLI entry point invoked by ``.githooks/pre-push``.

    Usage: ``prepush_pr_body_gate.py [<remote-ref>]`` — ``remote-ref`` is
    the destination ref (e.g. ``refs/heads/feat/123-thing``) the shell
    wrapper parsed off git's pre-push stdin protocol. ``PR_BODY`` is
    read from the environment.
    """
    argv = list(sys.argv if argv is None else argv)
    remote_ref = _parse_remote_ref(argv)
    return run_pr_body_gate(os.environ.get("PR_BODY"), remote_ref)


if __name__ == "__main__":
    sys.exit(main())
