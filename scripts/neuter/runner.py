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
import uuid
from pathlib import Path

from scripts.neuter import lock as lockmod
from scripts.neuter import pg
from scripts.neuter.classify import classify
from scripts.neuter.junit import (
    mapped_failed_in_full,
    parse_junit,
    parse_junit_full,
    unmapped_assertion_shaped_failures,
)
from scripts.neuter.pg import start_container, stop_container
from scripts.neuter.pool import (
    append_event,
    append_jsonl,
    apply_edits,
    evidence_dir_is_refused,
    git_diff_quiet,
    py_compile_ok,
    restore,
    run_pool,
)
from scripts.neuter.pytest_run import chokepoint_scope, run_pytest
from scripts.neuter.report import read_jsonl, verify_report, write_report
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
        # Critical regression, found running the definitive `make test`:
        # this module (`runner.py`) imports `scripts.neuter.pytest_run` at
        # its own top, which makes NEUTER_CHOKEPOINT_REQUIRED=1 and
        # PYTEST_PLUGINS=neuter_pathcheck sticky in THIS wrapper process's
        # own `os.environ` the instant `python -m scripts.neuter.runner
        # hold-shared -- ...` starts -- regardless of whether hold-shared
        # is wrapping a real neuter invocation or (as `make test` uses it)
        # the product's own, entirely un-gated top-level pytest run.
        # `subprocess.run(argv)` with no `env=` override would inherit
        # those two vars into the CHILD, whose own `neuter_pathcheck`
        # plugin then auto-loads (PYTEST_PLUGINS) and immediately fails
        # the whole session at `pytest_configure` (REQUIRED is set, but no
        # token was ever recorded for this session) -- before a single
        # test runs. hold-shared's own child must never inherit these.
        child_env = dict(os.environ)
        child_env.pop("NEUTER_CHOKEPOINT_REQUIRED", None)
        child_env.pop("NEUTER_CHOKEPOINT_TOKEN", None)
        child_env.pop("NEUTER_CHOKEPOINT_TOKEN_FILE", None)
        child_env.pop("PYTEST_PLUGINS", None)
        result = subprocess.run(
            argv, env=child_env
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
    with chokepoint_scope():
        repo_dir = Path(args.repo_dir).resolve()
        evidence_dir = Path(args.evidence).resolve()
        python = args.python or f"{repo_dir}/.venv/bin/python"
        lock_path = Path(args.lock_path) if args.lock_path else None

        if evidence_dir_is_refused(evidence_dir):
            print(f"run: evidence dir refused (/tmp): {evidence_dir}", file=sys.stderr)
            return EXIT_SPEC_ERROR

        try:
            # collect (this spec load's own --collect-only) is one of the five
            # chokepoint-gated phases (review round 2): `already_locked=False`
            # (the default) makes it acquire+release the SAME heavy-cap lock
            # itself for the duration of the collect-only call, since `run_pool`
            # below has not yet taken its own (longer-held) lock at this point.
            spec = load_neuter_spec(
                Path(args.neuters),
                repo_dir=repo_dir,
                python=python,
                lock_path=lock_path,
                fake_db=args.no_db,
            )
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
            lock_path=lock_path,
            sample=args.sample,
            i_measured_it=args.i_measured_it,
            ack_drift=args.ack_drift,
        )


def _cmd_arbitrate(args: argparse.Namespace) -> int:
    with chokepoint_scope():
        repo_dir = Path(args.repo_dir).resolve()
        evidence_dir = Path(args.evidence).resolve()
        python = args.python or f"{repo_dir}/.venv/bin/python"

        # arbitrate takes the SAME heavy-cap lock as the pool -- BEFORE the
        # spec load's own collect phase, so collect is ALSO covered by this
        # one, long-held acquisition (review round 2: collect is one of the
        # five chokepoint-gated phases). It applies edits and runs pytest
        # against repo_dir directly, exactly the class of operation `make
        # test`'s hold-shared must never race with.
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

        # Review round 4 should-fix (same class as round 3's "collect"
        # fix): a literal "arbitrate" run-id container name collides with
        # any leftover container from an earlier interrupted/crashed
        # arbitrate call (docker run exit 125, name already in use) --
        # found live, re-running this exact command. Unique per call.
        pg_handle = start_container(
            f"arbitrate-{uuid.uuid4().hex[:8]}",
            0,
            tmpfs=getattr(args, "tmpfs", False),
            fake=getattr(args, "no_db", False),
            fake_dir=evidence_dir / "fake_pg"
            if getattr(args, "no_db", False)
            else None,
        )
        try:
            spec = load_neuter_spec(
                Path(args.neuters),
                repo_dir=repo_dir,
                python=python,
                lock_path=resolved_lock,
                already_locked=True,
                fake_db=getattr(args, "no_db", False),
            )
            neuters_by_id = {n.id: n for n in spec.neuters}
            ids = args.ids.split(",")

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
                    raise RuntimeError(
                        f"{repo_dir} dirty before arbitrating {neuter_id}"
                    )
                apply_edits(repo_dir, entry)
                nocompile = not py_compile_ok(repo_dir, entry, python)
                junit_path = (
                    evidence_dir
                    / "junit"
                    / f"arbitration_{neuter_id.replace('/', '_')}.xml"
                )
                exit_code = None
                junit_result = None
                full = None
                unmapped_red: list[str] = []
                foreign_pg_container = False
                chokepoint_marker_ok = True
                watch_attached = True
                try:
                    if not nocompile:
                        # THE chokepoint (review round 2): sequential, alone,
                        # over the FULL scope_files (§12) -- the authoritative
                        # tie-break, never the mapped-test shortcut. Pins the
                        # SAME PYTHONPATH/DSN/container-watch every other phase
                        # gets -- blockers 1/2/5's root cause was arbitrate
                        # blindly importing the venv's editable install instead
                        # of repo_dir's edited tree.
                        pytest_result = run_pytest(
                            workdir=repo_dir,
                            pytest_args=list(spec.scope_files),
                            python=python,
                            lock_path=resolved_lock,
                            timeout_s=args.timeout,
                            junit_path=junit_path,
                            pg_handle=pg_handle,
                            already_locked=True,
                        )
                        exit_code = pytest_result.exit_code
                        foreign_pg_container = pytest_result.foreign_pg_container
                        chokepoint_marker_ok = pytest_result.chokepoint_marker_ok
                        watch_attached = pytest_result.watch_attached
                        full = parse_junit_full(
                            junit_path if junit_path.exists() else None
                        )
                        junit_result = parse_junit(
                            junit_path if junit_path.exists() else None, entry.tests
                        )
                        unmapped_red = unmapped_assertion_shaped_failures(
                            full, entry.tests
                        )
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
                    if junit_result is not None
                    and junit_result.tests_collected is not None
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
                    foreign_pg_container=foreign_pg_container,
                    chokepoint_marker_ok=chokepoint_marker_ok,
                    watch_attached=watch_attached,
                )
                authoritative_verdict = verdict.verdict
                authoritative_error_reason = verdict.error_reason
                harness_verdict = results_by_id.get(neuter_id, {}).get("verdict")
                if (
                    harness_verdict == "RED"
                    and full is not None
                    and not mapped_failed_in_full(full, entry.tests)
                ):
                    # SPEC v3 §5 `not_reproduced` (review round 2, blocker 1/2):
                    # the harness's own targeted run was RED, but NONE of its
                    # mapped tests show as failed in the FULL-scope run --
                    # the full-scope run never even reproduced the edit (a
                    # blind PYTHONPATH, a dirty restore, ...). This is an ERROR,
                    # never a "clean" or "confirmed" GREEN.
                    authoritative_verdict = "ERROR"
                    authoritative_error_reason = "not_reproduced"
                elif authoritative_verdict == "GREEN" and unmapped_red:
                    # Blocker 3: a mapped-only GREEN whose full-scope run shows
                    # OTHER assertion-shaped failures is drift, not a confirmed
                    # GREEN -- never reported as an "authoritative GREEN".
                    authoritative_verdict = "DRIFT"
                append_jsonl(
                    out_path,
                    {
                        "id": neuter_id,
                        "harness_verdict": harness_verdict,
                        "authoritative_verdict": authoritative_verdict,
                        "authoritative_error_reason": authoritative_error_reason,
                        "unmapped_red": unmapped_red,
                        "defect_ref": None,
                    },
                )
            return 0
        finally:
            try:
                stop_container(pg_handle)
            except Exception:  # noqa: BLE001 - cleanup must not itself crash
                pass
            os.close(lock_fd)


def _cmd_report(args: argparse.Namespace) -> int:
    with chokepoint_scope():
        repo_dir = Path(args.repo_dir).resolve()
        evidence_dir = Path(args.evidence).resolve()
        python = args.python or f"{repo_dir}/.venv/bin/python"
        lock_path = Path(args.lock_path) if args.lock_path else None
        # collect is chokepoint-gated here too (review round 2): report has not
        # taken any lock of its own, so the default `already_locked=False`
        # makes the spec's own collect-only call acquire+release it.
        spec = load_neuter_spec(
            Path(args.neuters),
            repo_dir=repo_dir,
            python=python,
            lock_path=lock_path,
            fake_db=args.no_db,
        )
        guard_tests_path = args.guard_tests
        if not guard_tests_path:
            default_path = evidence_dir / "guard_tests_reviewer.json"
            if default_path.exists():
                guard_tests_path = str(default_path)
        reviewer_guard_tests = None
        if guard_tests_path:
            reviewer_guard_tests = _load_guard_tests_file(Path(guard_tests_path))
        # Review round 3 should-fix: an ack is never a bare flag -- record the
        # ACTUAL acknowledged ids in events.jsonl too (the report's own
        # ACKNOWLEDGED section + trailer carry the same list; this is the
        # durable, append-only side of the same fact).
        if args.ack_errors or args.ack_drift:
            rows = read_jsonl(evidence_dir / "neuter_results.jsonl")
            acked_error_ids = (
                sorted(r["id"] for r in rows if r.get("verdict") == "ERROR")
                if args.ack_errors
                else []
            )
            acked_drift_ids = (
                sorted(r["id"] for r in rows if r.get("unmapped_red"))
                if args.ack_drift
                else []
            )
            append_event(
                evidence_dir,
                {
                    "event": "ack",
                    "ack_errors": args.ack_errors,
                    "ack_drift": args.ack_drift,
                    "acked_error_ids": acked_error_ids,
                    "acked_drift_ids": acked_drift_ids,
                },
            )
        if args.verify:
            return verify_report(
                evidence_dir,
                spec,
                reviewer_guard_tests=reviewer_guard_tests,
                ack_errors=args.ack_errors,
                ack_drift=args.ack_drift,
            )
        write_report(
            evidence_dir,
            spec,
            reviewer_guard_tests=reviewer_guard_tests,
            ack_errors=args.ack_errors,
            ack_drift=args.ack_drift,
        )
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
    run_p.add_argument(
        "--ack-drift",
        action="store_true",
        help=(
            "acknowledge a non-empty unmapped_red on any row so the pool "
            "may still exit clean (never implicit; review round 2 blocker 4)"
        ),
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
    arb_p.add_argument(
        "--no-db", action="store_true", help="fake Postgres (self-proofs only)"
    )
    arb_p.add_argument("--tmpfs", action="store_true")
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
    rep_p.add_argument(
        "--ack-drift",
        action="store_true",
        help="acknowledge drift_n > 0 so --verify may still pass (never implicit)",
    )
    rep_p.add_argument("--lock-path", default=None)
    rep_p.add_argument(
        "--no-db", action="store_true", help="fake Postgres (self-proofs only)"
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
    # Review round 4 should-fix: a real (non-fake) Postgres start can fail
    # at ANY of the chokepoint-gated phases (collect, baseline, worker,
    # drift, arbitrate) -- this used to surface as a raw, uncaught
    # traceback. One catch at the single CLI dispatch point, a clear
    # message, and the typed exit code -- never a bare stack trace.
    try:
        return args.func(args)
    except pg.DockerUnavailableError as exc:
        print(f"neuter: docker unavailable: {exc}", file=sys.stderr)
        return pg.EXIT_DOCKER_UNAVAILABLE


if __name__ == "__main__":
    raise SystemExit(main())
