"""Deterministic, testable release-cut runner (#398).

The instrument the Release Agent executes to CUT a release. Given a target
``--version`` and a clean ``main``, the runner always performs the SAME
ordered phases and either (a) produces the SAME bump + branch + pushed
commit + release-record artefact, or (b) aborts before any mutation. There is
NO wall-clock branching in the control flow — timestamps are stamped into
evidence only (`_now_iso`), never read back to decide anything. Unlike the
CD deploy runner (:mod:`scripts.deploy.runner`), this is a real, occasional
CLI invocation rather than a workflow polling loop, so real ``time``/
timestamps in filenames are fine (SPEC-398).

Ordered phases (each logged; each declares the evidence it captures):

* **R0 Preflight** — hard gates, always evaluated for real (read-only git +
  file reads, never a mutation): current branch is ``main``; the tracked
  working tree is clean (untracked files are ignored); ``--version`` is
  valid MAJOR.MINOR.PATCH semver AND strictly greater than the current
  ``pyproject.toml`` version. ABORTS (no R1+ phase runs) on any failure —
  including under ``--dry-run``, so a dry-run over a dirty tree or an
  already-released version correctly reports the abort rather than a false
  "clean plan".
* **R1 Bump** — shells ``make release VERSION=X.Y.Z`` (ADR-055's single
  source of truth for the pin sites; this runner does NOT re-implement the
  sed).
* **R2 Gate** — shells ``make test``; the run is judged green ONLY when the
  exit code is 0 AND the per-file coverage gate reports PASS AND the
  zero-skip check reports clean AND ``pyproject.toml::version`` ==
  ``Chart.yaml::appVersion`` == ``--version`` (the ADR-055 drift
  assertion, checked directly against the bumped files rather than by
  string-matching pytest's own report). ANY failure STOPS the run before
  R3 — no branch, no commit.
* **R3 Branch + commit** — creates ``chore/release-vX.Y.Z``, stages the
  fixed set of bump-touched files, commits ``chore(release): vX.Y.Z``
  (no ``Co-Authored-By``).
* **R4 Push + record** — pushes the branch, then emits the release-record
  JSON (``scripts/release/runs/release-X.Y.Z-<stamp>.json``): version, base
  commit, the commits bundled since the previous tag, the R2 gate summary,
  the branch name, and the STAGED (never executed) annotated-tag command.
  ``tag_pushed`` is always ``False`` and ``published`` is always ``None`` —
  by construction, not by omission.

**The runner NEVER creates or pushes a git tag.** That is the entire safety
property (SPEC-398 note): the tag → Docker Hub publish step is HUMAN-GATED
(SDLC-ADR-003 §4c) and stays outside this instrument. A dedicated
falsifiability test in ``tests/test_release_runner.py`` asserts no ``git
tag`` / tag-ref ``git push`` invocation ever occurs across a full run.

``--dry-run`` runs R0 for real (it is read-only) and then, on a pass,
records R1-R4 as ``planned`` WITHOUT invoking a single subprocess and
WITHOUT writing any file — "stops without mutating" is interpreted
fail-closed: no release-record artefact is written for a plan that was
never executed.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess  # noqa: S404 - the runner is the operator; it shells out to make/git
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("audittrace.release.runner")

# Ordered phase identifiers — the determinism contract fixes this sequence.
PHASES = (
    "R0-preflight",
    "R1-bump",
    "R2-gate",
    "R3-branch-commit",
    "R4-push-record",
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# The fixed set of files `make release VERSION=...` touches (Makefile
# `release` target + its README-refresh dependency). Staged verbatim in R3 —
# the runner never guesses via `git add -A`, so an accidentally-dirty
# unrelated file can never ride along into the release commit.
BUMP_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "charts/audittrace/Chart.yaml",
    "docs/reference/audittrace/openapi.yaml",
    "tests/fixtures/openapi.snapshot.yaml",
    "README.md",
)

# Strict MAJOR.MINOR.PATCH semver core — this repo's actual version scheme
# (verified against pyproject.toml / Chart.yaml, both plain X.Y.Z, no
# pre-release/build metadata). No leading zeros.
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

# Evidence markers this runner looks for in `make test` output — kept as
# named constants so a test can assert against the SAME string the runner
# checks, rather than a coincidentally-matching duplicate.
_PER_FILE_GATE_PASS_MARKER = "per-file coverage gate: PASS"
_ZERO_SKIP_PASS_MARKER = "No skipped tests"


class PreflightAbortError(RuntimeError):
    """R0 hard-gate failure — raised before any mutation is attempted."""


class BumpFailedError(RuntimeError):
    """R1 ``make release`` returned non-zero — stops before R2."""


class GateFailError(RuntimeError):
    """R2 ``make test`` gate failed — stops before R3 (no branch/commit)."""


class BranchCommitFailedError(RuntimeError):
    """R3 branch creation or commit failed — stops before R4 (no push)."""


class PushFailedError(RuntimeError):
    """R4 ``git push`` failed — the release-record is NOT written."""


# ── external-effect indirection (monkeypatched in tests) ────────────────────


def _run(
    cmd: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing output. Sole subprocess entry point."""
    logger.info("exec: %s", " ".join(cmd))
    return subprocess.run(  # noqa: S603 - fixed argv lists, no shell
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _now_iso() -> str:
    """UTC ISO-8601 timestamp — EVIDENCE ONLY, never read back into logic."""
    return datetime.now(UTC).isoformat()


# ── pure helpers (unit-tested directly) ──────────────────────────────────────


def parse_semver(version: str) -> tuple[int, int, int]:
    """Parse a strict MAJOR.MINOR.PATCH string; raise ``ValueError`` otherwise."""
    match = _SEMVER_RE.match(version)
    if match is None:
        raise ValueError(f"{version!r} is not a valid MAJOR.MINOR.PATCH semver string")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def is_strictly_greater(candidate: str, current: str) -> bool:
    """``True`` iff ``candidate`` is a strictly greater semver than ``current``."""
    return parse_semver(candidate) > parse_semver(current)


def read_pyproject_version(repo_root: Path) -> str:
    """Read ``project.version`` from ``pyproject.toml`` (ADR-055 pin site 1)."""
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def read_chart_app_version(repo_root: Path) -> str:
    """Read ``appVersion`` from ``charts/audittrace/Chart.yaml`` (pin site 2)."""
    chart_yaml = repo_root / "charts" / "audittrace" / "Chart.yaml"
    data = yaml.safe_load(chart_yaml.read_text(encoding="utf-8"))
    return str(data["appVersion"])


def staged_tag_command(version: str) -> str:
    """The annotated-tag command string — STAGED evidence only, NEVER run."""
    return (
        f'git tag -a v{version} -m "chore(release): v{version}" '
        f"&& git push origin v{version}"
    )


# ── config + records ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReleaseConfig:
    version: str
    dry_run: bool = False
    repo_root: Path = REPO_ROOT
    out_dir: Path = REPO_ROOT / "scripts" / "release" / "runs"


@dataclass
class PhaseRecord:
    name: str
    status: str  # ok | planned | aborted
    command: str | None = None
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    ended_at: str = ""


# ── the runner ────────────────────────────────────────────────────────────────


class ReleaseRunner:
    """Executes the ordered phases and (on a successful real run) emits the
    release-record artefact. Never creates or pushes a git tag."""

    def __init__(self, cfg: ReleaseConfig) -> None:
        self.cfg = cfg
        self.records: list[PhaseRecord] = []
        self.aborted: bool = False
        self.base_commit: str | None = None
        self.branch_name: str | None = None
        self.previous_tag: str | None = None
        self.bundled_commits: list[str] = []
        self.gate_summary: dict[str, Any] = {}

    # -- phase plumbing --

    def _record(self, name: str, status: str, **kw: Any) -> PhaseRecord:
        rec = PhaseRecord(name=name, status=status, **kw)
        self.records.append(rec)
        level = logging.ERROR if status == "aborted" else logging.INFO
        logger.log(level, "[%s] %s %s", name, status, rec.detail)
        return rec

    # -- R0 --

    def phase_preflight(self) -> None:
        """Hard gates. ALWAYS evaluated for real (read-only) — even under
        ``--dry-run``, so a dry-run over a dirty tree / bad version / wrong
        branch aborts exactly like a real run would (idempotent-safe, per the
        Determinism contract)."""
        started = _now_iso()

        branch_proc = _run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.cfg.repo_root
        )
        branch = branch_proc.stdout.strip()
        if branch_proc.returncode != 0 or branch != "main":
            reason = f"not on main (current branch: {branch or 'unknown'!r})"
            self._record(
                PHASES[0],
                "aborted",
                detail=reason,
                started_at=started,
                ended_at=_now_iso(),
            )
            raise PreflightAbortError(reason)

        status_proc = _run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=self.cfg.repo_root,
        )
        if status_proc.stdout.strip():
            reason = "working tree has tracked changes:\n" + status_proc.stdout.strip()
            self._record(
                PHASES[0],
                "aborted",
                detail=reason,
                evidence={"porcelain": status_proc.stdout.strip()},
                started_at=started,
                ended_at=_now_iso(),
            )
            raise PreflightAbortError(reason)

        try:
            parse_semver(self.cfg.version)
        except ValueError as exc:
            reason = str(exc)
            self._record(
                PHASES[0],
                "aborted",
                detail=reason,
                started_at=started,
                ended_at=_now_iso(),
            )
            raise PreflightAbortError(reason) from exc

        current_version = read_pyproject_version(self.cfg.repo_root)
        try:
            greater = is_strictly_greater(self.cfg.version, current_version)
        except ValueError as exc:
            reason = f"current pyproject.toml version {current_version!r} is not valid semver: {exc}"
            self._record(
                PHASES[0],
                "aborted",
                detail=reason,
                started_at=started,
                ended_at=_now_iso(),
            )
            raise PreflightAbortError(reason) from exc
        if not greater:
            reason = (
                f"--version {self.cfg.version} is not strictly greater than "
                f"current pyproject.toml version {current_version}"
            )
            self._record(
                PHASES[0],
                "aborted",
                detail=reason,
                evidence={"current_version": current_version},
                started_at=started,
                ended_at=_now_iso(),
            )
            raise PreflightAbortError(reason)

        base_commit_proc = _run(["git", "rev-parse", "HEAD"], cwd=self.cfg.repo_root)
        self.base_commit = base_commit_proc.stdout.strip() or None
        self._record(
            PHASES[0],
            "ok",
            detail=(
                f"main@{(self.base_commit or '?')[:12]} clean; "
                f"{current_version} -> {self.cfg.version}"
            ),
            evidence={
                "current_version": current_version,
                "base_commit": self.base_commit,
            },
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- R1 --

    def phase_bump(self) -> None:
        cmd = ["make", "release", f"VERSION={self.cfg.version}"]
        if self.cfg.dry_run:
            self._record(
                PHASES[1],
                "planned",
                command=" ".join(cmd),
                detail=(
                    "would bump pyproject.toml + Chart.yaml (+ OpenAPI snapshot) "
                    "via `make release` — ADR-055 single source, no re-implemented sed"
                ),
            )
            return
        started = _now_iso()
        proc = _run(cmd, cwd=self.cfg.repo_root)
        if proc.returncode != 0:
            self._record(
                PHASES[1],
                "aborted",
                command=" ".join(cmd),
                detail="make release failed",
                evidence={
                    "exit_code": proc.returncode,
                    "stderr_tail": proc.stderr[-2000:],
                },
                started_at=started,
                ended_at=_now_iso(),
            )
            raise BumpFailedError(
                f"make release VERSION={self.cfg.version} exited {proc.returncode}"
            )
        changed_proc = _run(["git", "status", "--porcelain"], cwd=self.cfg.repo_root)
        changed_files = changed_proc.stdout.strip().splitlines()
        self._record(
            PHASES[1],
            "ok",
            command=" ".join(cmd),
            detail="pyproject.toml + Chart.yaml (+ OpenAPI snapshot if changed) bumped",
            evidence={"changed_files": changed_files},
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- R2 --

    def _version_drift_ok(self) -> bool:
        """``True`` iff pyproject == Chart appVersion == the target version."""
        try:
            pyproject_version = read_pyproject_version(self.cfg.repo_root)
            chart_version = read_chart_app_version(self.cfg.repo_root)
        except (OSError, KeyError, ValueError):
            return False
        return pyproject_version == chart_version == self.cfg.version

    def phase_gate(self) -> None:
        cmd = ["make", "test"]
        if self.cfg.dry_run:
            self._record(
                PHASES[2],
                "planned",
                command=" ".join(cmd),
                detail="would run make test (green + per-file coverage PASS + zero-skip + version-drift)",
            )
            return
        started = _now_iso()
        proc = _run(cmd, cwd=self.cfg.repo_root)
        stdout = proc.stdout
        tests_green = proc.returncode == 0
        per_file_gate_pass = _PER_FILE_GATE_PASS_MARKER in stdout
        zero_skip_pass = _ZERO_SKIP_PASS_MARKER in stdout
        version_drift_pass = self._version_drift_ok()
        ok = (
            tests_green and per_file_gate_pass and zero_skip_pass and version_drift_pass
        )
        self.gate_summary = {
            "returncode": proc.returncode,
            "tests_green": tests_green,
            "per_file_gate_pass": per_file_gate_pass,
            "zero_skip_pass": zero_skip_pass,
            "version_drift_pass": version_drift_pass,
            "ok": ok,
        }
        if ok:
            detail = "make test green; per-file coverage PASS; zero-skip; version-drift consistent"
        else:
            failing = [
                key
                for key, value in self.gate_summary.items()
                if key not in ("returncode", "ok") and not value
            ]
            detail = f"gate FAILED — {', '.join(failing) or 'non-zero exit'}"
        self._record(
            PHASES[2],
            "ok" if ok else "aborted",
            command=" ".join(cmd),
            detail=detail,
            evidence={
                **self.gate_summary,
                "stdout_tail": stdout[-3000:],
                "stderr_tail": proc.stderr[-2000:],
            },
            started_at=started,
            ended_at=_now_iso(),
        )
        if not ok:
            raise GateFailError(detail)

    # -- R3 --

    def phase_branch_commit(self) -> None:
        self.branch_name = f"chore/release-v{self.cfg.version}"
        checkout_cmd = ["git", "checkout", "-b", self.branch_name]
        add_cmd = ["git", "add", *BUMP_FILES]
        commit_msg = f"chore(release): v{self.cfg.version}"
        commit_cmd = ["git", "commit", "-m", commit_msg]
        if self.cfg.dry_run:
            self._record(
                PHASES[3],
                "planned",
                command=f"{' '.join(checkout_cmd)} && {' '.join(add_cmd)} && {' '.join(commit_cmd)}",
                detail="would create the release branch and commit the bumped files (no Co-Authored-By)",
            )
            return
        started = _now_iso()
        checkout_proc = _run(checkout_cmd, cwd=self.cfg.repo_root)
        if checkout_proc.returncode != 0:
            self._record(
                PHASES[3],
                "aborted",
                command=" ".join(checkout_cmd),
                detail="git checkout -b failed",
                evidence={
                    "exit_code": checkout_proc.returncode,
                    "stderr_tail": checkout_proc.stderr[-2000:],
                },
                started_at=started,
                ended_at=_now_iso(),
            )
            raise BranchCommitFailedError(
                f"git checkout -b {self.branch_name} exited {checkout_proc.returncode}"
            )
        add_proc = _run(add_cmd, cwd=self.cfg.repo_root)
        if add_proc.returncode != 0:
            self._record(
                PHASES[3],
                "aborted",
                command=" ".join(add_cmd),
                detail="git add failed",
                evidence={
                    "exit_code": add_proc.returncode,
                    "stderr_tail": add_proc.stderr[-2000:],
                },
                started_at=started,
                ended_at=_now_iso(),
            )
            raise BranchCommitFailedError(f"git add exited {add_proc.returncode}")
        commit_proc = _run(commit_cmd, cwd=self.cfg.repo_root)
        if commit_proc.returncode != 0:
            self._record(
                PHASES[3],
                "aborted",
                command=" ".join(commit_cmd),
                detail="git commit failed",
                evidence={
                    "exit_code": commit_proc.returncode,
                    "stderr_tail": commit_proc.stderr[-2000:],
                },
                started_at=started,
                ended_at=_now_iso(),
            )
            raise BranchCommitFailedError(f"git commit exited {commit_proc.returncode}")
        self._record(
            PHASES[3],
            "ok",
            command=f"{' '.join(checkout_cmd)}; {' '.join(add_cmd)}; {' '.join(commit_cmd)}",
            detail=f"branch {self.branch_name} created; committed {commit_msg!r}",
            evidence={"branch": self.branch_name},
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- R4 --

    def _collect_bundled_commits(self) -> None:
        """The commits on ``main`` since the previous tag — read-only evidence."""
        assert self.base_commit is not None
        describe = _run(
            ["git", "describe", "--tags", "--abbrev=0", self.base_commit],
            cwd=self.cfg.repo_root,
        )
        if describe.returncode == 0 and describe.stdout.strip():
            self.previous_tag = describe.stdout.strip()
            log = _run(
                ["git", "log", f"{self.previous_tag}..{self.base_commit}", "--oneline"],
                cwd=self.cfg.repo_root,
            )
            self.bundled_commits = (
                log.stdout.strip().splitlines() if log.returncode == 0 else []
            )
        else:
            self.previous_tag = None
            self.bundled_commits = []

    def _emit_record_path(self) -> Path:
        out_dir = Path(self.cfg.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _now_iso().replace(":", "").replace("-", "")
        return out_dir / f"release-{self.cfg.version}-{stamp}.json"

    def phase_push_and_record(self) -> dict[str, Any] | None:
        """R4: push the branch, then emit the release-record JSON.

        NEVER creates or pushes a git tag — that boundary is asserted by
        ``tests/test_release_runner.py``'s falsifiability test. The staged
        (never-executed) tag command is carried in the record for the
        Release Agent's human-gated follow-up.
        """
        self.branch_name = self.branch_name or f"chore/release-v{self.cfg.version}"
        push_cmd = ["git", "push", "-u", "origin", self.branch_name]
        if self.cfg.dry_run:
            self._record(
                PHASES[4],
                "planned",
                command=" ".join(push_cmd),
                detail=(
                    "would push the branch and emit "
                    "scripts/release/runs/release-X.Y.Z-<stamp>.json "
                    "(the git TAG is NEVER created/pushed by this runner)"
                ),
            )
            return None
        started = _now_iso()
        proc = _run(push_cmd, cwd=self.cfg.repo_root)
        if proc.returncode != 0:
            self._record(
                PHASES[4],
                "aborted",
                command=" ".join(push_cmd),
                detail="git push failed",
                evidence={
                    "exit_code": proc.returncode,
                    "stderr_tail": proc.stderr[-2000:],
                },
                started_at=started,
                ended_at=_now_iso(),
            )
            raise PushFailedError(
                f"git push -u origin {self.branch_name} exited {proc.returncode}"
            )
        self._collect_bundled_commits()
        record_path = self._emit_record_path()
        self._record(
            PHASES[4],
            "ok",
            command=" ".join(push_cmd),
            detail=f"branch {self.branch_name} pushed; release-record written to {record_path}",
            evidence={
                "bundled_commits": self.bundled_commits,
                "previous_tag": self.previous_tag,
                "record_path": str(record_path),
            },
            started_at=started,
            ended_at=_now_iso(),
        )
        report = self.build_report()
        record_path.write_text(json.dumps(report, indent=2, default=str))
        record_path.with_suffix(".txt").write_text(self.human_summary(report))
        return report

    # -- report --

    def build_report(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "runner": "audittrace-release-runner",
            "version": self.cfg.version,
            "dry_run": self.cfg.dry_run,
            "aborted": self.aborted,
            "base_commit": self.base_commit,
            "previous_tag": self.previous_tag,
            "bundled_commits": self.bundled_commits,
            "branch": self.branch_name,
            "gate_summary": self.gate_summary,
            # ── the human-gated boundary (SPEC-398 note) ──
            "staged_tag_command": staged_tag_command(self.cfg.version),
            "tag_pushed": False,
            "published": None,
            "phases": [asdict(r) for r in self.records],
            "generated_at": _now_iso(),
        }

    def human_summary(self, report: dict[str, Any]) -> str:
        lines = [
            "AuditTrace release runner — report",
            "=" * 40,
            f"version        : {report['version']}",
            f"dry run        : {report['dry_run']}",
            f"aborted        : {report['aborted']}",
            f"base commit    : {report['base_commit']}",
            f"previous tag   : {report['previous_tag']}",
            f"branch         : {report['branch']}",
            f"gate summary   : {report['gate_summary'] or 'n/a'}",
            "",
            "phases:",
        ]
        for rec in report["phases"]:
            cmd = f"  $ {rec['command']}" if rec.get("command") else ""
            lines.append(f"  [{rec['name']}] {rec['status']} — {rec['detail']}")
            if cmd:
                lines.append(cmd)
        lines += [
            "",
            f"staged tag command (NOT executed): {report['staged_tag_command']}",
            f"tag_pushed     : {report['tag_pushed']}  (runner NEVER pushes a tag)",
            f"published      : {report['published']}",
        ]
        return "\n".join(lines) + "\n"

    # -- orchestration --

    def run(self) -> dict[str, Any]:
        try:
            self.phase_preflight()
        except PreflightAbortError:
            self.aborted = True
            return self.build_report()
        try:
            self.phase_bump()
            self.phase_gate()
            self.phase_branch_commit()
            report = self.phase_push_and_record()
        except (
            BumpFailedError,
            GateFailError,
            BranchCommitFailedError,
            PushFailedError,
        ):
            self.aborted = True
            return self.build_report()
        return report if report is not None else self.build_report()


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.release.runner",
        description=(
            "Deterministic AuditTrace release-cut runner (#398): "
            "bump + gate + branch + push. NEVER creates/pushes a git tag."
        ),
    )
    parser.add_argument("--version", required=True, help="semver X.Y.Z to release")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the ordered plan; mutate nothing"
    )
    parser.add_argument("--out-dir", type=Path, default=ReleaseConfig.out_dir)
    return parser


def config_from_args(args: argparse.Namespace) -> ReleaseConfig:
    return ReleaseConfig(
        version=args.version, dry_run=args.dry_run, out_dir=args.out_dir
    )


def print_plan(report: dict[str, Any]) -> None:
    print(f"release plan (dry-run={report['dry_run']}) — version {report['version']}\n")
    for rec in report["phases"]:
        print(f"  [{rec['name']}] {rec['status']} — {rec['detail']}")
        if rec.get("command"):
            print(f"      $ {rec['command']}")
    print(
        f"\n  tag_pushed: {report['tag_pushed']}  |  published: {report['published']}"
        f"\n  staged tag command (NOT executed): {report['staged_tag_command']}"
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = build_parser().parse_args(argv)
    cfg = config_from_args(args)
    runner = ReleaseRunner(cfg)
    report = runner.run()
    print_plan(report)
    if report["aborted"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
