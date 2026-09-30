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

from scripts.neuter import lock as lockmod
from scripts.neuter.pg import start_container, stop_container
from scripts.neuter.pytest_run import run_pytest

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
    repo_dir: Path,
    sha: str,  # noqa: ARG001 - kept in the signature; callers pass the pinned sha for clarity/logging
    scope_files: list[str],
    python: str,
    *,
    lock_path: Path | None,
    already_locked: bool,
    fake_db: bool | None = None,
) -> set[str]:
    """Run ``--collect-only`` once over ``scope_files`` at ``sha`` (checked
    out in ``repo_dir``) and return the collected node ids -- through the
    SAME chokepoint (:func:`scripts.neuter.pytest_run.run_pytest`) every
    other phase (neuter/baseline/drift/arbitrate) uses (review round 2:
    collect is one of the five phases the chokepoint requirement names).

    ``-o addopts=""`` overrides whatever the TARGET repo's own
    ``pyproject.toml``/``pytest.ini`` sets (a ``-v`` there switches
    ``--collect-only``'s rendering from the flat ``file.py::test`` list this
    parses to a verbose ``<Dir>/<Module>/<Function>`` tree instead) --
    portable regardless of which repo is being neutered.

    ``PYTHONPATH`` is pinned to ``<repo_dir>/src`` (the chokepoint's own
    rule): without it, an editable install of the SAME package in
    ``python``'s own environment (e.g. the harness's own dev venv, if it
    happens to be reused to collect against a DIFFERENT checkout) can
    resolve import collisions from ITS OWN site-packages ``.pth`` entry
    instead of ``repo_dir``'s tree, collecting nothing there was to collect
    and failing every id as ``uncollected`` (exit 3) until this is pinned.

    ``fake_db`` (review round 3 blocker 1, opt-in): ``--collect-only``
    still IMPORTS every test module to introspect it, and an import-time
    side effect in a mapped test's module can start a durable product
    container exactly like a full run can. ``None`` (the default -- every
    existing unit test on the toy fixture, which never touches Postgres at
    all) skips container start-up entirely, unchanged from before this
    round. The production CLI commands (``run``/``arbitrate``/``report``)
    pass the real ``--no-db`` value explicitly, so collect gets a real (or
    fake) DSN too.
    """
    resolved_lock = lockmod.resolve_lock_path(str(lock_path) if lock_path else None)
    pg_handle = None
    if fake_db is not None:
        pg_handle = start_container(
            "collect", 0, fake=fake_db, fake_dir=repo_dir / ".neuter_collect_fake_pg"
        )
    try:
        result = run_pytest(
            workdir=repo_dir,
            pytest_args=list(scope_files),
            python=python,
            lock_path=resolved_lock,
            src_root=str(repo_dir / "src"),
            collect_only=True,
            already_locked=already_locked,
            pg_handle=pg_handle,
        )
    finally:
        if pg_handle is not None:
            try:
                stop_container(pg_handle)
            except Exception:  # noqa: BLE001 - cleanup must not itself crash
                pass
    stdout = result.collect_stdout or ""
    ids = {line.strip() for line in stdout.splitlines() if "::" in line}
    return ids


def load_neuter_spec(
    path: Path,
    *,
    repo_dir: Path,
    python: str,
    collected_ids: set[str] | None = None,
    lock_path: Path | None = None,
    already_locked: bool = False,
    fake_db: bool | None = None,
) -> NeuterSpecFile:
    """Load and fail-closed validate a neuter spec file (§3).

    ``collected_ids``, when given, is used instead of running
    ``--collect-only`` again (the pool runs it once, in worker 1's
    worktree, per §3).

    ``lock_path``/``already_locked``: threaded straight through to the
    collect phase's chokepoint call (review round 2). A caller that has
    already taken the heavy-cap lock itself (the ``run``/``arbitrate``/
    ``report`` CLI commands) passes ``already_locked=True`` so collect only
    ASSERTS the lock, never re-acquires it (which would self-conflict on a
    second file descriptor to the same lock file). A standalone caller
    (most existing unit tests, or a direct script) leaves the default
    ``already_locked=False``: collect acquires+releases ``lock_path`` (or
    the process-wide default) itself around the ``--collect-only`` call.
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
        # `.get()` throughout, never direct indexing: a malformed entry
        # (missing "id" or "row") must always fail closed as a
        # SpecLoadError (exit 3), never escape as a raw KeyError -- found
        # when a neuter that skips the "row" check went on to raise
        # KeyError on `entry["row"]` instead of the expected exit 3.
        gid = entry.get("id")
        if not gid:
            raise SpecLoadError("guard_tests_id", f"entry without id: {entry!r}")
        row = entry.get("row")
        if not row:
            raise SpecLoadError("guard_tests_row", f"entry without row: {gid!r}")
        guard_tests.append(GuardTestEntry(id=gid, row=row))

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
        collected_ids = _collect_ids(
            repo_dir,
            sha,
            scope_files,
            python,
            lock_path=lock_path,
            already_locked=already_locked,
            fake_db=fake_db,
        )
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
