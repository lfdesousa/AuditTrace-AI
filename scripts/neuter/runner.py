"""CLI entry point (SPEC v3 §13): ``run`` / ``arbitrate`` / ``report`` /
``hold-shared`` / ``assert-idle``.

::

    .venv/bin/python -m scripts.neuter.runner run --sha <sha> --neuters <priv>/neuters.json \\
        [--guard-tests <priv>/guard_tests.json] --evidence <priv>/evidence/<date>-<wu>/ --workers 5 \\
        [--sample 0.10] [--timeout 900] [--tmpfs] [--resume]
    .venv/bin/python -m scripts.neuter.runner arbitrate --ids <id,...> --evidence <dir>
    .venv/bin/python -m scripts.neuter.runner report [--verify] --evidence <dir>
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
from scripts.neuter.junit import parse_junit
from scripts.neuter.pool import (
    append_jsonl,
    apply_edits,
    git_diff_quiet,
    py_compile_ok,
    restore,
    run_pool,
)
from scripts.neuter.report import verify_report, write_report
from scripts.neuter.spec import EXIT_SPEC_ERROR, SpecLoadError, load_neuter_spec

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


def _cmd_run(args: argparse.Namespace) -> int:
    repo_dir = Path(args.repo_dir).resolve()
    evidence_dir = Path(args.evidence).resolve()
    python = args.python or f"{repo_dir}/.venv/bin/python"
    try:
        spec = load_neuter_spec(Path(args.neuters), repo_dir=repo_dir, python=python)
    except SpecLoadError as exc:
        print(f"spec load error [{exc.reason}]: {exc.detail}", file=sys.stderr)
        return EXIT_SPEC_ERROR

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
    )


def _cmd_arbitrate(args: argparse.Namespace) -> int:
    repo_dir = Path(args.repo_dir).resolve()
    evidence_dir = Path(args.evidence).resolve()
    python = args.python or f"{repo_dir}/.venv/bin/python"
    spec = load_neuter_spec(Path(args.neuters), repo_dir=repo_dir, python=python)
    neuters_by_id = {n.id: n for n in spec.neuters}
    ids = args.ids.split(",")

    results_by_id = {}
    for path in evidence_dir.glob("neuter_results_w*.jsonl"):
        with path.open() as fh:
            for line in fh:
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
            evidence_dir / "junit" / f"arbitration_{neuter_id.replace('/', '_')}.xml"
        )
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        exit_code = None
        junit_result = None
        try:
            if not nocompile:
                # sequential, alone, over the FULL scope_files (§12) -- the
                # authoritative tie-break, never the mapped-test shortcut.
                cmd = [
                    python,
                    "-m",
                    "pytest",
                    *spec.scope_files,
                    "-q",
                    "--no-cov",
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
                junit_result = parse_junit(
                    junit_path if junit_path.exists() else None, entry.tests
                )
        finally:
            restore(repo_dir, entry)
        verdict = classify(
            entry.tests,
            junit_result,
            tests_expected=len(entry.tests),
            nocompile=nocompile,
            pathcheck_ok=True,
            db_leak=False,
            timed_out=False,
            exit_code=exit_code,
        )
        harness_verdict = results_by_id.get(neuter_id, {}).get("verdict")
        append_jsonl(
            out_path,
            {
                "id": neuter_id,
                "harness_verdict": harness_verdict,
                "authoritative_verdict": verdict.verdict,
                "defect_ref": None,
            },
        )
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    repo_dir = Path(args.repo_dir).resolve()
    evidence_dir = Path(args.evidence).resolve()
    python = args.python or f"{repo_dir}/.venv/bin/python"
    spec = load_neuter_spec(Path(args.neuters), repo_dir=repo_dir, python=python)
    reviewer_guard_tests = None
    if args.guard_tests:
        reviewer_spec = json.loads(Path(args.guard_tests).read_text())
        from scripts.neuter.spec import GuardTestEntry

        reviewer_guard_tests = [
            GuardTestEntry(id=e["id"], row=e["row"]) for e in reviewer_spec
        ]
    if args.verify:
        return verify_report(
            evidence_dir, spec, reviewer_guard_tests=reviewer_guard_tests
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
    run_p.add_argument("--repo-dir", default=common["repo_dir"])
    run_p.add_argument("--python", default=common["python"])
    run_p.set_defaults(func=_cmd_run)

    arb_p = sub.add_parser("arbitrate")
    arb_p.add_argument("--ids", required=True)
    arb_p.add_argument("--evidence", required=True)
    arb_p.add_argument("--neuters", required=True)
    arb_p.add_argument("--timeout", type=int, default=900)
    arb_p.add_argument("--repo-dir", default=common["repo_dir"])
    arb_p.add_argument("--python", default=common["python"])
    arb_p.set_defaults(func=_cmd_arbitrate)

    rep_p = sub.add_parser("report")
    rep_p.add_argument("--evidence", required=True)
    rep_p.add_argument("--neuters", required=True)
    rep_p.add_argument("--guard-tests")
    rep_p.add_argument("--verify", action="store_true")
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
