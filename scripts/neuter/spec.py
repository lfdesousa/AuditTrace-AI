"""Neuter spec format, loading, and fail-closed validation (SPEC v3 §3, §5).

Load fails closed (exit 3) before any worktree or container, against
``git show <sha>:<file>`` -- the committed tree, never a worktree someone
can edit (X6). Every failure mode raises :class:`SpecLoadError` with a
stable ``reason`` and the offending ids, so a caller (``runner.py``) can
print them and exit 3 without a stack trace.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

EXIT_SPEC_ERROR = 3


class SpecLoadError(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class GuardTestEntry:
    id: str
    row: str


@dataclass(frozen=True)
class Edit:
    old: str
    new: str


@dataclass(frozen=True)
class NeuterEntry:
    id: str
    file: str
    edits: list[Edit]
    tests: list[str]
    engines: list[str]
    guard: str
    clause: str
    expect: str = "RED"


@dataclass(frozen=True)
class NeuterSpecFile:
    schema: int
    sha: str
    scope_files: list[str]
    guard_tests: list[GuardTestEntry]
    neuters: list[NeuterEntry]
    path: Path = field(repr=False)


_KNOWN_ENGINES = frozenset({"postgres", "mock"})


def _git_show(repo_dir: Path, sha: str, file: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{sha}:{file}"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SpecLoadError(
            "untracked_file", f"{file} not found at {sha}: {result.stderr.strip()}"
        )
    return result.stdout


def _collect_ids(
    repo_dir: Path, sha: str, scope_files: list[str], python: str
) -> set[str]:
    """Run ``--collect-only`` once over ``scope_files`` at ``sha`` (checked
    out in ``repo_dir``) and return the collected node ids."""
    result = subprocess.run(
        [python, "-m", "pytest", "--collect-only", "-q", *scope_files],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    ids = {line.strip() for line in result.stdout.splitlines() if "::" in line}
    return ids


def load_neuter_spec(
    path: Path,
    *,
    repo_dir: Path,
    python: str,
    collected_ids: set[str] | None = None,
) -> NeuterSpecFile:
    """Load and fail-closed validate a neuter spec file (§3).

    ``collected_ids``, when given, is used instead of running
    ``--collect-only`` again (the pool runs it once, in worker 1's
    worktree, per §3).
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecLoadError("unreadable", str(exc)) from exc

    schema = raw.get("schema")
    if schema != 3:
        raise SpecLoadError("schema", f"expected schema 3, got {schema!r}")

    sha = raw.get("sha")
    if not sha:
        raise SpecLoadError("sha", "missing sha")

    scope_files = raw.get("scope_files") or []
    if not scope_files:
        raise SpecLoadError("scope_files", "empty scope_files")

    raw_guard_tests = raw.get("guard_tests") or []
    if not raw_guard_tests:
        raise SpecLoadError("guard_tests", "empty guard_tests")
    guard_tests: list[GuardTestEntry] = []
    for entry in raw_guard_tests:
        if "row" not in entry or not entry["row"]:
            raise SpecLoadError(
                "guard_tests_row", f"entry without row: {entry.get('id')!r}"
            )
        guard_tests.append(GuardTestEntry(id=entry["id"], row=entry["row"]))

    raw_neuters = raw.get("neuters") or []
    if not raw_neuters:
        raise SpecLoadError("neuters", "empty neuters")

    seen_ids: set[str] = set()
    neuters: list[NeuterEntry] = []
    file_cache: dict[str, str] = {}
    all_tests: set[str] = set()

    for entry in raw_neuters:
        nid = entry.get("id")
        if not nid:
            raise SpecLoadError("neuter_id", "neuter entry without id")
        if nid in seen_ids:
            raise SpecLoadError("duplicate_id", nid)
        seen_ids.add(nid)

        if entry.get("expect", "RED") != "RED":
            raise SpecLoadError("expect", f"{nid}: expect != RED")

        file = entry.get("file")
        if not file:
            raise SpecLoadError("neuter_file", f"{nid}: missing file")

        edits_raw = entry.get("edits") or []
        if not edits_raw:
            raise SpecLoadError("edits", f"{nid}: empty edits")
        edits = [Edit(old=e["old"], new=e["new"]) for e in edits_raw]
        for edit in edits:
            if edit.old == edit.new:
                raise SpecLoadError("old_eq_new", f"{nid}: old == new")

        tests = entry.get("tests") or []
        if not tests:
            raise SpecLoadError("tests", f"{nid}: empty tests")
        all_tests.update(tests)

        engines = entry.get("engines") or []
        if not engines or any(e not in _KNOWN_ENGINES for e in engines):
            raise SpecLoadError("engines", f"{nid}: unknown engines {engines!r}")

        if file not in file_cache:
            file_cache[file] = _git_show(repo_dir, sha, file)
        source = file_cache[file]
        for edit in edits:
            count = source.count(edit.old)
            if count != 1:
                raise SpecLoadError(
                    "match_count", f"{nid}: {count} matches for `old` (want 1)"
                )

        neuters.append(
            NeuterEntry(
                id=nid,
                file=file,
                edits=edits,
                tests=tests,
                engines=engines,
                guard=entry.get("guard", ""),
                clause=entry.get("clause", ""),
                expect="RED",
            )
        )

    guard_test_ids = {g.id for g in guard_tests}
    if guard_test_ids != all_tests:
        missing = sorted(all_tests - guard_test_ids)
        extra = sorted(guard_test_ids - all_tests)
        raise SpecLoadError(
            "closure",
            f"guard_tests != union(tests): missing={missing[:5]} extra={extra[:5]}",
        )

    if collected_ids is None:
        collected_ids = _collect_ids(repo_dir, sha, scope_files, python)
    for test_id in sorted(all_tests):
        if test_id not in collected_ids:
            raise SpecLoadError("uncollected", test_id)

    return NeuterSpecFile(
        schema=3,
        sha=sha,
        scope_files=scope_files,
        guard_tests=guard_tests,
        neuters=neuters,
        path=path,
    )
