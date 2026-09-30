"""CLI entry point (SPEC v3 §13): ``run`` / ``arbitrate`` / ``report`` /
``hold-shared`` / ``assert-idle``.

::

    .venv/bin/python -m scripts.neuter.runner run --sha <sha> --neuters <priv>/neuters.json \\
        [--guard-tests <priv>/guard_tests.json] --evidence <priv>/evidence/<date>-<wu>/ --workers 5 \\
        [--sample 0.10] [--timeout 900] [--tmpfs] [--resume] [--i-measured-it]
    .venv/bin/python -m scripts.neuter.runner arbitrate --ids <id,...> --evidence <dir> --neuters <priv>/neuters.json
    .venv/bin/python -m scripts.neuter.runner report [--verify] [--ack-errors] --evidence <dir> --neuters <priv>/neuters.json
    .venv/bin/python -m scripts.neuter.runner hold-shared -- <pytest argv>
    .venv/bin/python -m scripts.neuter.runner assert-idle
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.neuter import lock as lockmod
from scripts.neuter.classify import classify
from scripts.neuter.junit import (
    parse_junit,
    parse_junit_full,
    unmapped_assertion_shaped_failures,
)
from scripts.neuter.pool import (
    append_jsonl,
    apply_edits,
    evidence_dir_is_refused,
    git_diff_quiet,
    py_compile_ok,
    restore,
    run_pool,
)
from scripts.neuter.report import verify_report, write_report
from scripts.neuter.spec import (
    EXIT_SPEC_ERROR,
    GuardTestEntry,
    SpecLoadError,
    load_neuter_spec,
)

EXIT_LOCK_HELD = 8


def _cmd_hold_shared(argv: list[str]) -> int:
    """Hold ``LOCK_SH`` for the child's whole lifetime (§10, FNH2-BL-1)."""
    if not argv:
        print(
            "hold-shared: no child argv given (expected `-- <argv>`)", file=sys.stderr
        )
        return 2
    path = lockmod.resolve_lock_path()
    fd = lockmod.open_lock_file(path)
    try:
        lockmod.try_flock(fd, fcntl.LOCK_SH)
    except lockmod.LockHeldError:
        print(
            f"hold-shared: lock held (a neuter pool is running) at {path}",
            file=sys.stderr,
        )
        return EXIT_LOCK_HELD
    try:
        result = subprocess.run(
            argv
        )  # fork+exec as a CHILD -- never os.exec*, or the fd (and lock) would drop.
        return result.returncode
    finally:
        os.close(fd)


def _cmd_assert_idle() -> int:
    """§10: file absent -> 0; held EX -> 8; unreadable -> 0 + warning (SF-E)."""
    path = lockmod.resolve_lock_path()
    fd = lockmod.open_existing_lock_file(path)
    if fd is None:
        return 0
    try:
        try:
            lockmod.try_flock(fd, fcntl.LOCK_SH)
        except lockmod.LockHeldError:
            print(f"assert-idle: pool lock held at {path}", file=sys.stderr)
            return EXIT_LOCK_HELD
        fcntl.flock(fd, fcntl.LOCK_UN)
        return 0
    finally:
        os.close(fd)


def _load_guard_tests_file(path: Path) -> list[GuardTestEntry]:
    raw = json.loads(path.read_text())
    return [GuardTestEntry(id=e["id"], row=e["row"]) for e in raw]


def _cmd_run(args: argparse.Namespace) -> int:
    repo_dir = Path(args.repo_dir).resolve()
    evidence_dir = Path(args.evidence).resolve()
    python = args.python or f"{repo_dir}/.venv/bin/python"

    if evidence_dir_is_refused(evidence_dir):
        print(f"run: evidence dir refused (/tmp): {evidence_dir}", file=sys.stderr)
        return EXIT_SPEC_ERROR

    try:
        spec = load_neuter_spec(Path(args.neuters), repo_dir=repo_dir, python=python)
    except SpecLoadError as exc:
        print(f"spec load error [{exc.reason}]: {exc.detail}", file=sys.stderr)
        return EXIT_SPEC_ERROR

    # --sha is HONOURED, not merely accepted: the caller's belief about
    # which commit it is neutering must match what the spec file itself
    # was validated against, or the run is refused before anything starts.
    if args.sha != spec.sha:
        print(
            f"run: --sha {args.sha} does not match the spec's own sha {spec.sha}",
            file=sys.stderr,
        )
        return EXIT_SPEC_ERROR

    if args.guard_tests:
        # --guard-tests is HONOURED: an alternate/reviewer-authored mapping
        # must close over the SAME union(tests) as the spec's own, or the
        # run is refused (SF-C's authorship-independence check, extended to
        # `run` instead of only `report`). Persisted into the evidence dir
        # so `report` picks it up automatically afterwards.
        reviewer_guard_tests = _load_guard_tests_file(Path(args.guard_tests))
        reviewer_ids = {g.id for g in reviewer_guard_tests}
        union_tests: set[str] = set()
        for n in spec.neuters:
            union_tests.update(n.tests)
        if reviewer_ids != union_tests:
            missing = sorted(union_tests - reviewer_ids)
            extra = sorted(reviewer_ids - union_tests)
            print(
                f"run: --guard-tests does not close over union(tests): "
                f"missing={missing[:5]} extra={extra[:5]}",
                file=sys.stderr,
            )
            return EXIT_SPEC_ERROR
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "guard_tests_reviewer.json").write_text(
            Path(args.guard_tests).read_text()
        )

    return run_pool(
        spec,
        repo_dir=repo_dir,
        parent_worktree_dir=repo_dir.parent,
        evidence_dir=evidence_dir,
        workers=args.workers,
        python=python,
        timeout_s=args.timeout,
        tmpfs=args.tmpfs,
        resume=args.resume,
        pathcheck_module=args.pathcheck_module,
        src_root_relative=args.src_root_relative,
        fake_db=args.no_db,
        lock_path=Path(args.lock_path) if args.lock_path else None,
        sample=args.sample,
        i_measured_it=args.i_measured_it,
    )


def _cmd_arbitrate(args: argparse.Namespace) -> int:
    repo_dir = Path(args.repo_dir).resolve()
    evidence_dir = Path(args.evidence).resolve()
    python = args.python or f"{repo_dir}/.venv/bin/python"
    spec = load_neuter_spec(Path(args.neuters), repo_dir=repo_dir, python=python)
    neuters_by_id = {n.id: n for n in spec.neuters}
    ids = args.ids.split(",")

    # arbitrate takes the SAME heavy-cap lock as the pool: it applies edits
    # and runs pytest against repo_dir directly, exactly the class of
    # operation `make test`'s hold-shared must never race with.
    resolved_lock = lockmod.resolve_lock_path(
        str(args.lock_path) if args.lock_path else None
    )
    lock_fd = lockmod.open_lock_file(resolved_lock)
    try:
        lockmod.try_flock(lock_fd, fcntl.LOCK_EX)
    except lockmod.LockHeldError:
        print(f"arbitrate: lock held at {resolved_lock}", file=sys.stderr)
        os.close(lock_fd)
        return EXIT_LOCK_HELD

    try:
        results_by_id = {}
        for path in evidence_dir.glob("neuter_results_w*.jsonl"):
            with path.open() as fh:
                for line in fh:
                    row = json.loads(line)
                    results_by_id[row["id"]] = row
        if not results_by_id and (evidence_dir / "neuter_results.jsonl").exists():
            for line in (
                (evidence_dir / "neuter_results.jsonl").read_text().splitlines()
            ):
                row = json.loads(line)
                results_by_id[row["id"]] = row

        out_path = evidence_dir / "arbitration.jsonl"
        for neuter_id in ids:
            entry = neuters_by_id[neuter_id]
            if not git_diff_quiet(repo_dir):
                raise RuntimeError(f"{repo_dir} dirty before arbitrating {neuter_id}")
            apply_edits(repo_dir, entry)
            nocompile = not py_compile_ok(repo_dir, entry, python)
            junit_path = (
                evidence_dir
                / "junit"
                / f"arbitration_{neuter_id.replace('/', '_')}.xml"
            )
            junit_path.parent.mkdir(parents=True, exist_ok=True)
            exit_code = None
            junit_result = None
            unmapped_red: list[str] = []
            try:
                if not nocompile:
                    # sequential, alone, over the FULL scope_files (§12) --
                    # the authoritative tie-break, never the mapped-test
                    # shortcut. Classifies the FULL scope (blocker 3): a
                    # planted wrong-mapping neuter (X6) that leaves OTHER,
                    # unmapped tests failing must never read as an
                    # "authoritative GREEN".
                    cmd = [
                        python,
                        "-m",
                        "pytest",
                        *spec.scope_files,
                        "-q",
                        "--no-cov",
                        "-o",
                        "addopts=",
                        f"--junitxml={junit_path}",
                    ]
                    result = subprocess.run(
                        cmd,
                        cwd=repo_dir,
                        capture_output=True,
                        text=True,
                        timeout=args.timeout,
                    )
                    exit_code = result.returncode
                    full = parse_junit_full(junit_path if junit_path.exists() else None)
                    junit_result = parse_junit(
                        junit_path if junit_path.exists() else None, entry.tests
                    )
                    unmapped_red = unmapped_assertion_shaped_failures(full, entry.tests)
            finally:
                restored_clean = restore(repo_dir, entry)
                if not restored_clean:
                    raise RuntimeError(
                        f"arbitrate: {repo_dir} left dirty after restoring {neuter_id} "
                        "(should-fix: the restore result is no longer ignored)"
                    )
            # Arbitration runs over the FULL scope_files, not just
            # entry.tests -- junit's own `tests=` attribute (the full-scope
            # collected count) is what "collected" must compare against
            # here, or a full-scope run would ALWAYS "mismatch" against the
            # neuter's small mapped-test count and mask every other
            # condition (§4's `collected` fires first). `missing` still
            # checks the SPECIFIC mapped ids independently, at whatever
            # scale.
            tests_expected = (
                junit_result.tests_collected
                if junit_result is not None and junit_result.tests_collected is not None
                else len(entry.tests)
            )
            verdict = classify(
                entry.tests,
                junit_result,
                tests_expected=tests_expected,
                nocompile=nocompile,
                pathcheck_ok=True,
                db_leak=False,
                timed_out=False,
                exit_code=exit_code,
            )
            authoritative_verdict = verdict.verdict
            if authoritative_verdict == "GREEN" and unmapped_red:
                # Blocker 3: a mapped-only GREEN whose full-scope run shows
                # OTHER assertion-shaped failures is drift, not a confirmed
                # GREEN -- never reported as an "authoritative GREEN".
                authoritative_verdict = "DRIFT"
            harness_verdict = results_by_id.get(neuter_id, {}).get("verdict")
            append_jsonl(
                out_path,
                {
                    "id": neuter_id,
                    "harness_verdict": harness_verdict,
                    "authoritative_verdict": authoritative_verdict,
                    "authoritative_error_reason": verdict.error_reason,
                    "unmapped_red": unmapped_red,
                    "defect_ref": None,
                },
            )
        return 0
    finally:
        os.close(lock_fd)


def _cmd_report(args: argparse.Namespace) -> int:
    repo_dir = Path(args.repo_dir).resolve()
    evidence_dir = Path(args.evidence).resolve()
    python = args.python or f"{repo_dir}/.venv/bin/python"
    spec = load_neuter_spec(Path(args.neuters), repo_dir=repo_dir, python=python)
    guard_tests_path = args.guard_tests
    if not guard_tests_path:
        default_path = evidence_dir / "guard_tests_reviewer.json"
        if default_path.exists():
            guard_tests_path = str(default_path)
    reviewer_guard_tests = None
    if guard_tests_path:
        reviewer_guard_tests = _load_guard_tests_file(Path(guard_tests_path))
    if args.verify:
        return verify_report(
            evidence_dir,
            spec,
            reviewer_guard_tests=reviewer_guard_tests,
            ack_errors=args.ack_errors,
        )
    write_report(evidence_dir, spec, reviewer_guard_tests=reviewer_guard_tests)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scripts.neuter.runner")
    sub = parser.add_subparsers(dest="command", required=True)

    common = dict(repo_dir=".", python=None)

    run_p = sub.add_parser("run")
    run_p.add_argument("--sha", required=True)
    run_p.add_argument("--neuters", required=True)
    run_p.add_argument("--guard-tests")
    run_p.add_argument("--evidence", required=True)
    run_p.add_argument("--workers", type=int, default=5)
    run_p.add_argument("--sample", type=float, default=0.10)
    run_p.add_argument("--timeout", type=int, default=900)
    run_p.add_argument("--tmpfs", action="store_true")
    run_p.add_argument("--resume", action="store_true")
    run_p.add_argument(
        "--no-db", action="store_true", help="fake Postgres (self-proofs only)"
    )
    run_p.add_argument("--pathcheck-module", default="audittrace")
    run_p.add_argument("--src-root-relative", default="src")
    run_p.add_argument(
        "--lock-path",
        default=None,
        help="override the heavy-cap lock file path (SF-2; also AUDITTRACE_NEUTER_LOCK)",
    )
    run_p.add_argument(
        "--i-measured-it",
        action="store_true",
        help="required to use --workers > 10 (the reason is logged to events.jsonl)",
    )
    run_p.add_argument("--repo-dir", default=common["repo_dir"])
    run_p.add_argument("--python", default=common["python"])
    run_p.set_defaults(func=_cmd_run)

    arb_p = sub.add_parser("arbitrate")
    arb_p.add_argument("--ids", required=True)
    arb_p.add_argument("--evidence", required=True)
    arb_p.add_argument("--neuters", required=True)
    arb_p.add_argument("--timeout", type=int, default=900)
    arb_p.add_argument("--lock-path", default=None)
    arb_p.add_argument("--repo-dir", default=common["repo_dir"])
    arb_p.add_argument("--python", default=common["python"])
    arb_p.set_defaults(func=_cmd_arbitrate)

    rep_p = sub.add_parser("report")
    rep_p.add_argument("--evidence", required=True)
    rep_p.add_argument("--neuters", required=True)
    rep_p.add_argument("--guard-tests")
    rep_p.add_argument("--verify", action="store_true")
    rep_p.add_argument(
        "--ack-errors",
        action="store_true",
        help="acknowledge error_n > 0 so --verify may still pass (never implicit)",
    )
    rep_p.add_argument("--repo-dir", default=common["repo_dir"])
    rep_p.add_argument("--python", default=common["python"])
    rep_p.set_defaults(func=_cmd_report)

    sub.add_parser("hold-shared")
    sub.add_parser("assert-idle")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "hold-shared":
        child = argv[1:]
        if child and child[0] == "--":
            child = child[1:]
        return _cmd_hold_shared(child)
    if argv and argv[0] == "assert-idle":
        return _cmd_assert_idle()

    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
