"""Guard: every third-party GitHub Action is pinned to a full commit SHA.

Spec CI-ACTIONS-SHA-PIN (2026-11-24) + ADDENDUM-1 (B2, S1). ``actions/checkout@v4``
is a moving pointer the action owner controls; ``actions/checkout@<40-hex>`` is
immutable. The tag stays as a trailing comment (``# v6``) so Dependabot's
``github-actions`` ecosystem (``.github/dependabot.yml``) bumps SHA and comment
together, and a reader sees the version.

Scope: ``jobs.<id>.uses`` (reusable workflows) and ``jobs.<id>.steps[].uses`` in
every ``.github/workflows/*.yml|yaml``, plus ``runs.steps[].uses`` in every
composite action ``.github/actions/**/action.yml|yaml``. Exempt by rule: local
actions (``./...``) and ``docker://`` images. Not covered: GitHub's default-setup
CodeQL (no workflow file), ``services:`` / ``container:`` image tags, and whether a
40-hex value is a real commit of that tag (the reviewer's ``git ls-remote``, AC-4).

Two instruments, compared as ORDERED LISTS (B2): PyYAML gives the ``uses:`` refs
(the rule is evaluated on this list); a line scan gives the line number and the
trailing comment. Any difference between the two lists is itself a failure: a
flow-style step, a ``uses:`` line inside a ``run:`` block or a ``with:`` input
literally named ``uses`` cannot balance each other away. The cost: a ``uses:`` line
inside a ``run: |`` block is a false positive (fails closed).

Neuters, each alone RED: N1 one SHA pin reverted to a tag; N2 one trailing comment
removed; N3 the classifier accepts a 7-hex ref (planted sample flips); N4 the list
comparison weakened to a length check (fixtures A and L go green, their tests red);
N5 the comment check accepts any text (fixture G ``# TODO`` passes, its test red).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
COMPOSITES = sorted((ROOT / ".github" / "actions").glob("**/action.y*ml"))
FILES = WORKFLOWS + COMPOSITES

SHA40 = re.compile(r"^[0-9a-f]{40}$")
#: the tag the pin replaced: ``v6``, ``v1.14.0`` (every shape measured at d35f8261); never free text.
TAG_COMMENT = re.compile(r"^v\d+(\.\d+)*$")
USES_LINE = re.compile(
    r"""^\s*(?:-\s*)?uses:\s*(?P<ref>"[^"]*"|'[^']*'|\S+)(?:\s+#\s*(?P<comment>\S.*?))?\s*$"""
)


def is_exempt(ref: str) -> bool:
    """Local actions and docker images carry no GitHub tag to pin."""
    return ref.startswith("./") or ref.startswith("docker://")


def is_sha_pinned(ref: str) -> bool:
    """``owner/repo[/path]@<40-hex>``; a shorter hex prefix is NOT a pin."""
    _owner_repo, sep, version = ref.partition("@")
    return bool(sep) and SHA40.fullmatch(version) is not None


def yaml_uses(path: Path) -> list[str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if path.name.startswith("action."):
        steps = (doc.get("runs") or {}).get("steps") or []
        return [str(s["uses"]) for s in steps if isinstance(s, dict) and "uses" in s]
    out: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        if "uses" in job:
            out.append(str(job["uses"]))
        steps = job.get("steps") or []
        out += [str(s["uses"]) for s in steps if isinstance(s, dict) and "uses" in s]
    return out


def line_uses(path: Path) -> list[tuple[int, str, str | None]]:
    out: list[tuple[int, str, str | None]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = USES_LINE.match(line)
        if m:
            out.append((n, m["ref"].strip("\"'"), m["comment"]))
    return out


def problems(path: Path, root: Path) -> list[tuple[str, str]]:
    """``(kind, message)`` per violation; kinds: ``mismatch``, ``unpinned``, ``comment``."""
    rel = path.relative_to(root)
    refs = yaml_uses(path)
    scanned = line_uses(path)
    scanned_refs = [ref for _n, ref, _c in scanned]
    # ADDENDUM-2 A2: an expression ref (``${{ ... }}``) is mutable by construction; name it.
    expr = [ref for ref in refs if "${{" in ref]
    if expr:
        return [("unpinned", f"{rel}: uses: {expr} is unpinned (expression)")]
    if scanned_refs != refs:
        return [
            (
                "mismatch",
                f"{rel}: yaml uses {refs} != line scan {scanned_refs} "
                "(a flow-style step, a uses: inside a run: block, or a with: input named uses)",
            )
        ]
    out: list[tuple[str, str]] = []
    for (n, _scanned_ref, comment), ref in zip(scanned, refs, strict=True):
        if is_exempt(ref):
            continue
        if not is_sha_pinned(ref):
            out.append(
                ("unpinned", f"{rel}:{n} uses: {ref} is not a 40-hex commit pin")
            )
        elif not (comment and TAG_COMMENT.fullmatch(comment)):
            out.append(
                (
                    "comment",
                    f"{rel}:{n} uses: {ref} needs a trailing '# <tag>' (tag-shaped, e.g. '# v6'); got {comment!r}",
                )
            )
    return out


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def test_workflow_files_exist() -> None:
    assert WORKFLOWS, "no workflows found; the guard would be vacuous"


@pytest.mark.parametrize("path", FILES, ids=_rel)
def test_every_third_party_uses_is_a_full_commit_sha(path: Path) -> None:
    bad = [
        msg for kind, msg in problems(path, ROOT) if kind in ("mismatch", "unpinned")
    ]
    assert bad == []


@pytest.mark.parametrize("path", FILES, ids=_rel)
def test_every_sha_pin_keeps_the_tag_as_a_trailing_comment(path: Path) -> None:
    bad = [msg for kind, msg in problems(path, ROOT) if kind == "comment"]
    assert bad == []


@pytest.mark.parametrize(
    ("ref", "pinned"),
    [
        ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", True),
        ("github/codeql-action/upload-sarif@" + "a" * 40, True),
        ("actions/checkout@v4", False),
        ("actions/checkout@v7.0.1", False),
        ("actions/checkout@main", False),
        ("actions/checkout@3d3c42e", False),
        ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b", False),
        ("actions/checkout", False),
    ],
)
def test_classifier_accepts_only_a_full_sha(ref: str, pinned: bool) -> None:
    assert is_sha_pinned(ref) is pinned


@pytest.mark.parametrize("ref", ["./.github/actions/x", "docker://alpine:3.20"])
def test_exempt_by_rule(ref: str) -> None:
    assert is_exempt(ref) and not is_sha_pinned(ref)


_PIN = "actions/checkout@" + "3d3c42e5aac5ba805825da76410c181273ba90b1"
_FIXTURES: dict[str, tuple[str, set[str]]] = {
    # A clean, pinned workflow: no problem of any kind.
    "ok": (
        f"on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: {_PIN} # v6\n      - run: make test\n",
        set(),
    ),
    # Fixture A (gate B2): flow-style unpinned step + a `uses:` line inside a run: block balance the COUNT.
    "A-flow-style-plus-run-block": (
        f"on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - {{uses: actions/checkout@v4}}\n      - run: |\n          uses: {_PIN} # v4\n",
        {"mismatch"},
    ),
    # Fixture L (gate B2): flow-style unpinned step + a local action whose `with:` input is literally named `uses`.
    "L-flow-style-plus-with-input-named-uses": (
        f"on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - {{uses: actions/checkout@v4}}\n      - uses: ./.github/actions/local\n        with:\n          uses: {_PIN} # v4\n",
        {"mismatch"},
    ),
    # Fixture B: the flow-style unpinned step alone.
    "B-flow-style-alone": (
        "on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - {uses: actions/checkout@v4}\n",
        {"mismatch"},
    ),
    # Fixture G (gate S1): a pin whose trailing comment is free text.
    "G-comment-is-free-text": (
        f"on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: {_PIN} # TODO\n",
        {"comment"},
    ),
    # A pin with no comment at all.
    "no-comment": (
        f"on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: {_PIN}\n",
        {"comment"},
    ),
    # A plain tag.
    "tag": (
        "on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v6\n",
        {"unpinned"},
    ),
    # ADDENDUM-2 A2: an unquoted expression ref is reported as unpinned, not as a mismatch.
    "expression-ref": (
        "on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: ${{ matrix.action }}\n",
        {"unpinned"},
    ),
    # A job-level reusable workflow at a tag.
    "reusable-workflow-tag": (
        "on: push\njobs:\n  j:\n    uses: owner/repo/.github/workflows/x.yml@v1\n",
        {"unpinned"},
    ),
}


@pytest.mark.parametrize("name", sorted(_FIXTURES))
def test_planted_fixtures(tmp_path: Path, name: str) -> None:
    text, expected_kinds = _FIXTURES[name]
    wf = tmp_path / ".github" / "workflows" / "planted.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text(text, encoding="utf-8")
    assert {kind for kind, _msg in problems(wf, tmp_path)} == expected_kinds
