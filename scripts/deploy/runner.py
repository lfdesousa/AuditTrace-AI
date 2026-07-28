"""Deterministic, idempotent deploy runner — Component 2 of the CD program.

The instrument the CD Agent executes. Given a target version + a reachable
cluster it always performs the SAME ordered phases and converges to the SAME
end state; re-running on an already-converged cluster is a no-op. There is NO
wall-clock branching in the control flow — timestamps are stamped into evidence
only, never read back to decide anything.

Ordered phases (each logged; each declares the evidence it captures):

* **P0 Preflight**  — ``scripts/deploy-preflight.sh``; ABORTS the run on any
  non-zero exit, surfacing exit 3 (vault injector) / 4 (istiod) explicitly. No
  mutation happens before P0 passes.
* **P1 Resolve**    — resolve ``--target-version`` to a published digest via the
  registry API (:mod:`scripts.deploy.registry`).
* **P2 Chart apply**— ``helm upgrade --install ... --reset-then-reuse-values
  --wait`` (bounded timeout, NO ``--atomic``). The surge-safe strategy lives in
  the chart (WS1); the runner does not re-specify it.
* **P3 Bootstrap**  — ``make k8s-bootstrap-secrets``, GUARDED: SKIPPED when
  ``VAULT_TOKEN`` is unset (already-provisioned assumption). Safe to re-run.
* **P4 Settle**     — wait for the memory-server rollout, sample the pods, and
  ASSERT the peak concurrent Running count never exceeds ``spec.replicas`` — the
  runtime form of the WS1 surge-safe guarantee.
* **P5 Report**     — emit the deploy report (JSON + human summary) + evidence
  bundle.

**The runner NEVER self-certifies success.** It reports what it DID; the
health verdict belongs to the independent Verify Agent (WS3). The report carries
``certified: null`` to make that explicit.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess  # noqa: S404 - the runner is the operator; it shells out to helm/kubectl/make
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.deploy import mesh, registry

logger = logging.getLogger("audittrace.deploy.runner")

# Ordered phase identifiers — the determinism contract fixes this sequence.
PHASES = (
    "P0-preflight",
    "P1-resolve-image",
    "P2-chart-apply",
    "P3-bootstrap",
    "P4-settle",
    "P5-report",
)

# Chart facts (see charts/audittrace/templates/memory-server/deployment.yaml).
MEMORY_SERVER_COMPONENT = "memory-server"
MEMORY_SERVER_CONTAINER = "memory-server"
_COMPONENT_SELECTOR = f"app.kubernetes.io/component={MEMORY_SERVER_COMPONENT}"

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT_SCRIPT = REPO_ROOT / "scripts" / "deploy-preflight.sh"
CHART_DIR = REPO_ROOT / "charts" / "audittrace"

# The runner never marks a deploy healthy. This string is stamped verbatim into
# the report so a reader (and the Verify Agent) sees the hand-off unambiguously.
VERIFICATION_DEFERRED = (
    "deferred to the independent verify runner (WS3); "
    "this runner reports what it did, it does not certify health"
)


class PreflightAbortError(RuntimeError):
    """Raised when P0 preflight fails; aborts the run before any mutation."""

    def __init__(self, exit_code: int, meaning: str) -> None:
        super().__init__(f"preflight aborted (exit {exit_code}): {meaning}")
        self.exit_code = exit_code
        self.meaning = meaning


# Exit code for a P0 abort caused by the mesh-health gate (#384 WS1) — distinct
# from the preflight-script codes 1–5 so a mesh abort is legible in the report.
MESH_UNSAFE_EXIT = 6


class MeshGateAbortError(PreflightAbortError):
    """Raised when the P0 mesh gate is UNSAFE; aborts BEFORE any mutation (#384).

    A subclass of :class:`PreflightAbortError` so :meth:`DeployRunner.run` catches
    it through the existing abort path and still emits a report — the only pod is
    never terminated into a cert-dead mesh (invariant I1).
    """

    def __init__(self, result: mesh.MeshGateResult) -> None:
        super().__init__(MESH_UNSAFE_EXIT, f"mesh unsafe — {result.reason}")
        self.result = result


# ── external-effect indirections (monkeypatched in tests) ────────────────────


def _run(
    cmd: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run a command, capturing output. Sole subprocess entry point."""
    logger.info("exec: %s", " ".join(cmd))
    return subprocess.run(  # noqa: S603 - fixed argv lists, no shell
        cmd,
        env={**os.environ, **(env or {})} if env else None,
        capture_output=True,
        text=True,
        check=False,
    )


def _sleep(seconds: float) -> None:
    """Sleep between pod samples. Not a control-flow branch; mocked to 0 in tests."""
    import time

    time.sleep(seconds)


def _now_iso() -> str:
    """UTC ISO-8601 timestamp — EVIDENCE ONLY, never read back into logic."""
    return datetime.now(UTC).isoformat()


# ── config + records ─────────────────────────────────────────────────────────


def normalize_version(version: str) -> str:
    """Strip a single leading ``v`` — the git TAG is ``vX.Y.Z``, the IMAGE tag is
    ``X.Y.Z``. ``v1.13.0`` and ``1.13.0`` both normalise to ``1.13.0`` (live:
    hub ``1.13.0`` resolves, ``v1.13.0`` 404s)."""
    return (
        version[1:] if version.startswith("v") and version[1:2].isdigit() else version
    )


@dataclass(frozen=True)
class DeployConfig:
    target_version: str
    namespace: str = "audittrace"
    release: str = "audittrace"
    registry: str = "hub"
    dry_run: bool = False
    out_dir: Path = REPO_ROOT / "scripts" / "deploy" / "runs"
    timeout: int = 300
    settle_samples: int = 6
    settle_interval: float = 5.0

    @property
    def deployment(self) -> str:
        return f"{self.release}-memory-server"

    @property
    def image_tag(self) -> str:
        """The IMAGE tag to resolve + deploy (git ``v`` prefix stripped)."""
        return normalize_version(self.target_version)


@dataclass
class PhaseRecord:
    name: str
    status: str  # ok | skipped | noop | planned | flagged | aborted
    command: str | None = None
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    ended_at: str = ""


# ── pure helpers (unit-tested directly) ──────────────────────────────────────


def max_concurrent_running(samples: list[list[str]]) -> int:
    """Peak count of pods in phase ``Running`` across all samples."""
    return max(
        (sum(1 for phase in s if phase == "Running") for s in samples), default=0
    )


def within_surge_bound(samples: list[list[str]], replicas: int) -> tuple[bool, int]:
    """Return ``(ok, peak)`` — the WS1 surge-safe assertion.

    ``ok`` is False when the peak concurrent Running count exceeds
    ``spec.replicas`` (i.e. a surge was observed — two memory-server pods where
    the chart guarantees at most one on a single-node cluster).
    """
    peak = max_concurrent_running(samples)
    return peak <= replicas, peak


def apply_image_tag(cfg: DeployConfig, image_ref: registry.ImageRef) -> str:
    """The value for ``memoryServer.image.tag`` at P2.

    When the digest is resolved (hub) we deploy by the IMMUTABLE digest using the
    canonical ``tag@digest`` form. The chart renders ``repository:tag`` and this
    repo's chart has no ``image.digest`` key, so ``<version>@sha256:...`` renders
    to ``repo:<version>@sha256:...`` — a valid reference where the digest pins
    the pull and the tag stays human-readable. For a local registry whose digest
    was soft-unresolved we keep the plain tag (documented).
    """
    if image_ref.digest:
        return f"{cfg.image_tag}@{image_ref.digest}"
    return cfg.image_tag


def _helm_apply_cmd(cfg: DeployConfig, image_ref: registry.ImageRef) -> list[str]:
    """The exact ``helm upgrade --install`` argv for P2 (surge-safe via chart)."""
    return [
        "helm",
        "upgrade",
        "--install",
        cfg.release,
        str(CHART_DIR),
        "-n",
        cfg.namespace,
        "--reset-then-reuse-values",
        "--set",
        f"memoryServer.image.repository={image_ref.repository}",
        "--set",
        f"memoryServer.image.tag={apply_image_tag(cfg, image_ref)}",
        "--wait",
        "--timeout",
        f"{cfg.timeout}s",
    ]


def extract_digest(image_id: str | None) -> str | None:
    """Pull the ``sha256:...`` component out of a k8s container ``imageID``.

    ``imageID`` is ``repo@sha256:hex`` (or a bare ``sha256:hex``) and reflects
    what is ACTUALLY running, regardless of how the image was specified — the
    right thing to key digest-convergence on.
    """
    if not image_id:
        return None
    _, sep, tail = image_id.partition("@")
    candidate = tail if sep else image_id
    return candidate if candidate.startswith("sha256:") else None


# ── the runner ────────────────────────────────────────────────────────────────


class DeployRunner:
    """Executes the ordered phases and emits the non-self-certifying report."""

    def __init__(
        self, cfg: DeployConfig, mesh_gate: mesh.MeshGate | None = None
    ) -> None:
        self.cfg = cfg
        self.records: list[PhaseRecord] = []
        self.image_ref: registry.ImageRef | None = None
        self.helm_revision: int | None = None
        self.converged: bool = False
        self.surge: dict[str, Any] = {}
        self.aborted: bool = False
        # Injected so tests drive it without a cluster; the default WS1 gate ships
        # the UnconfiguredHealer (fail-closed) until the WS5 privileged healer lands.
        self.mesh_gate = mesh_gate or mesh.MeshGate(
            mesh.MeshGateConfig(namespace=cfg.namespace)
        )

    # -- phase plumbing --

    def _record(self, name: str, status: str, **kw: Any) -> PhaseRecord:
        rec = PhaseRecord(name=name, status=status, **kw)
        self.records.append(rec)
        level = logging.ERROR if status in ("aborted", "flagged") else logging.INFO
        logger.log(level, "[%s] %s %s", name, status, rec.detail)
        return rec

    # -- P0 --

    def phase_preflight(self) -> None:
        cmd = [
            "bash",
            str(PREFLIGHT_SCRIPT),
        ]
        env = {
            "NAMESPACE": self.cfg.namespace,
            "RELEASE": self.cfg.release,
            "TAG": self.cfg.image_tag,
        }
        if self.cfg.dry_run:
            self._record(
                PHASES[0],
                "planned",
                command=" ".join(cmd),
                detail="would run deploy-preflight.sh",
            )
            return
        started = _now_iso()
        proc = _run(cmd, env=env)
        if proc.returncode != 0:
            meaning = {
                1: "environment problem (helm/kubectl missing or cluster unreachable)",
                2: "chart problem (lint / template / apply rejected)",
                3: "vault-injector unhealthy — pods would crash on missing sidecar",
                4: "istiod degraded — workload identity would fail to bootstrap",
                5: "anti-affinity deadlock — pods would hang Pending",
            }.get(proc.returncode, f"preflight failed (exit {proc.returncode})")
            self._record(
                PHASES[0],
                "aborted",
                command=" ".join(cmd),
                detail=meaning,
                evidence={
                    "exit_code": proc.returncode,
                    "stderr_tail": proc.stderr[-2000:],
                },
                started_at=started,
                ended_at=_now_iso(),
            )
            raise PreflightAbortError(proc.returncode, meaning)
        self._record(
            PHASES[0],
            "ok",
            command=" ".join(cmd),
            detail="preflight gates passed",
            started_at=started,
            ended_at=_now_iso(),
        )
        # #384 WS1 — the fail-closed mesh-health gate. Runs AFTER the static
        # preflight gates but BEFORE any P1+ mutation, so with the WS1 surge-safe
        # strategy (maxSurge=0) the only memory-server pod is never terminated
        # into a mesh that cannot re-issue its workload cert (invariant I1). A
        # degraded mesh is auto-healed (bounded) or the deploy aborts fail-closed.
        self._mesh_gate()

    def _mesh_gate(self) -> None:
        """Run the P0 mesh-health gate; ABORT before mutation when UNSAFE (#384)."""
        started = _now_iso()
        result = self.mesh_gate.evaluate()
        if not result.safe:
            self._record(
                PHASES[0],
                "aborted",
                detail=f"MESH UNSAFE — {result.reason}",
                evidence=result.as_dict(),
                started_at=started,
                ended_at=_now_iso(),
            )
            raise MeshGateAbortError(result)
        self._record(
            PHASES[0],
            "ok",
            detail=(
                "mesh healthy; safe to proceed"
                if result.healthy
                else f"mesh auto-healed; safe to proceed ({result.reason})"
            ),
            evidence=result.as_dict(),
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- P1 --

    def phase_resolve(self) -> None:
        # Resolution is a READ-ONLY registry query — safe to run in dry-run too,
        # so the plan can show the real digest-pinned apply line. The version is
        # normalized (git `vX.Y.Z` -> image `X.Y.Z`) BEFORE resolving.
        started = _now_iso()
        try:
            ref = registry.resolve(self.cfg.image_tag, self.cfg.registry)
        except registry.DigestResolutionError as exc:
            if self.cfg.dry_run:
                # Planning offline / registry unreachable: don't fail the plan.
                self.image_ref = registry.ImageRef(
                    repository=registry._BACKENDS[self.cfg.registry][0],
                    tag=self.cfg.image_tag,
                    digest=None,
                    registry=self.cfg.registry,
                )
                self._record(
                    PHASES[1],
                    "planned",
                    detail=f"would resolve {self.cfg.image_tag} on {self.cfg.registry} (digest unresolved in dry-run: {exc})",
                )
                return
            self._record(
                PHASES[1],
                "failed",
                detail=f"could not resolve {self.cfg.image_tag} on {self.cfg.registry}: {exc}",
                evidence={"error": str(exc)},
                started_at=started,
                ended_at=_now_iso(),
            )
            raise  # caught in run(); a report is still emitted
        self.image_ref = ref
        if self.cfg.dry_run:
            status = "planned"
        elif ref.pinned:
            status = "ok"
        else:
            status = "flagged"
        detail = f"resolved {ref.repository}:{ref.tag}" + (
            f" @ {ref.digest}" if ref.pinned else " (digest UNRESOLVED — tag-only pin)"
        )
        self._record(
            PHASES[1],
            status,
            detail=detail,
            evidence={"image": ref.as_dict()},
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- convergence (idempotency) --

    def _running_image(self) -> str | None:
        """The deployment's configured image string (tag-form convergence, local)."""
        jsonpath = (
            '{.spec.template.spec.containers[?(@.name=="'
            + MEMORY_SERVER_CONTAINER
            + '")].image}'
        )
        proc = _run(
            [
                "kubectl",
                "get",
                "deployment",
                self.cfg.deployment,
                "-n",
                self.cfg.namespace,
                "-o",
                f"jsonpath={jsonpath}",
            ]
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def _running_image_digest(self) -> str | None:
        """The ``sha256:...`` actually running, from the live pod ``imageID``."""
        jsonpath = (
            '{.items[*].status.containerStatuses[?(@.name=="'
            + MEMORY_SERVER_CONTAINER
            + '")].imageID}'
        )
        proc = _run(
            [
                "kubectl",
                "get",
                "pods",
                "-l",
                _COMPONENT_SELECTOR,
                "-n",
                self.cfg.namespace,
                "-o",
                f"jsonpath={jsonpath}",
            ]
        )
        if proc.returncode != 0:
            return None
        # Multiple pods -> space-separated imageIDs; take the first with a digest.
        for token in proc.stdout.split():
            digest = extract_digest(token)
            if digest:
                return digest
        return None

    def _is_converged(self) -> tuple[bool, str]:
        """Converged when the LIVE digest equals the resolved digest.

        Keys on the digest (or live pod ``imageID``), never the mutable
        ``repository:tag`` — a re-pushed tag is therefore never falsely seen as
        converged. Falls back to tag-string comparison only when the digest is
        unresolved (local registry, soft-fail).
        """
        assert self.image_ref is not None
        if self.image_ref.digest:
            running = self._running_image_digest()
            return running == self.image_ref.digest, f"digest {self.image_ref.digest}"
        intended = f"{self.image_ref.repository}:{self.cfg.image_tag}"
        return self._running_image() == intended, f"tag {intended}"

    # -- P2 --

    def phase_chart_apply(self) -> None:
        assert self.image_ref is not None
        cmd = _helm_apply_cmd(self.cfg, self.image_ref)
        if self.cfg.dry_run:
            self._record(
                PHASES[2],
                "planned",
                command=" ".join(cmd),
                detail="would helm upgrade --install (surge-safe via chart; no --atomic)",
            )
            return

        converged, basis = self._is_converged()
        if converged:
            self.converged = True
            self.helm_revision = self._helm_revision()
            self._record(
                PHASES[2],
                "noop",
                command=" ".join(cmd),
                detail=f"already converged on {basis}; skipping helm upgrade (idempotent no-op)",
                evidence={"converged_on": basis, "helm_revision": self.helm_revision},
            )
            return

        started = _now_iso()
        proc = _run(cmd)
        if proc.returncode != 0:
            self._record(
                PHASES[2],
                "flagged",
                command=" ".join(cmd),
                detail="helm upgrade returned non-zero (no --atomic → no rollback; Verify Agent decides)",
                evidence={
                    "exit_code": proc.returncode,
                    "stderr_tail": proc.stderr[-2000:],
                },
                started_at=started,
                ended_at=_now_iso(),
            )
            return
        self.helm_revision = self._helm_revision()
        self._record(
            PHASES[2],
            "ok",
            command=" ".join(cmd),
            detail=f"chart applied; helm revision {self.helm_revision}",
            evidence={
                "helm_revision": self.helm_revision,
                "stdout_tail": proc.stdout[-2000:],
            },
            started_at=started,
            ended_at=_now_iso(),
        )

    def _helm_revision(self) -> int | None:
        proc = _run(
            ["helm", "status", self.cfg.release, "-n", self.cfg.namespace, "-o", "json"]
        )
        if proc.returncode != 0:
            return None
        try:
            return int(json.loads(proc.stdout).get("version"))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    # -- P3 --

    def phase_bootstrap(self) -> None:
        cmd = ["make", "k8s-bootstrap-secrets"]
        if self.cfg.dry_run:
            self._record(
                PHASES[3],
                "planned",
                command=" ".join(cmd),
                detail="would run bootstrap IF VAULT_TOKEN set",
            )
            return
        if not os.environ.get("VAULT_TOKEN"):
            self._record(
                PHASES[3],
                "skipped",
                command=" ".join(cmd),
                detail="VAULT_TOKEN unset — assuming already-provisioned (bootstrap is idempotent, safe to re-run later)",
            )
            return
        started = _now_iso()
        proc = _run(cmd)
        status = "ok" if proc.returncode == 0 else "flagged"
        self._record(
            PHASES[3],
            status,
            command=" ".join(cmd),
            detail="vault + keycloak scope bootstrap"
            + ("" if status == "ok" else " returned non-zero"),
            evidence={"exit_code": proc.returncode},
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- P4 --

    def _deployment_replicas(self) -> int:
        proc = _run(
            [
                "kubectl",
                "get",
                "deployment",
                self.cfg.deployment,
                "-n",
                self.cfg.namespace,
                "-o",
                "jsonpath={.spec.replicas}",
            ]
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return 1  # single-node conservative default
        try:
            return int(proc.stdout.strip())
        except ValueError:
            return 1

    def _sample_pod_phases(self) -> list[str]:
        proc = _run(
            [
                "kubectl",
                "get",
                "pods",
                "-l",
                _COMPONENT_SELECTOR,
                "-n",
                self.cfg.namespace,
                "-o",
                "jsonpath={.items[*].status.phase}",
            ]
        )
        if proc.returncode != 0:
            return []
        return proc.stdout.split()

    def phase_settle(self) -> None:
        if self.cfg.dry_run:
            self._record(
                PHASES[4],
                "planned",
                command=f"kubectl rollout status deployment/{self.cfg.deployment} + sample pods x{self.cfg.settle_samples}",
                detail=f"would assert peak concurrent Running <= spec.replicas ({_COMPONENT_SELECTOR})",
            )
            return
        started = _now_iso()
        # Bounded rollout wait (not a control-flow branch on wall-clock).
        _run(
            [
                "kubectl",
                "rollout",
                "status",
                f"deployment/{self.cfg.deployment}",
                "-n",
                self.cfg.namespace,
                f"--timeout={self.cfg.timeout}s",
            ]
        )
        replicas = self._deployment_replicas()
        samples: list[list[str]] = []
        for i in range(self.cfg.settle_samples):
            samples.append(self._sample_pod_phases())
            if i < self.cfg.settle_samples - 1:
                _sleep(self.cfg.settle_interval)
        ok, peak = within_surge_bound(samples, replicas)
        self.surge = {
            "replicas": replicas,
            "peak_running": peak,
            "within_bound": ok,
            "samples": samples,
        }
        self._record(
            PHASES[4],
            "ok" if ok else "flagged",
            detail=(
                f"rollout settled; peak concurrent Running={peak} <= replicas={replicas}"
                if ok
                else f"SURGE OBSERVED: peak Running={peak} EXCEEDED replicas={replicas} (WS1 violation)"
            ),
            evidence=self.surge,
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- P5 --

    def build_report(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "runner": "audittrace-deploy-runner",
            "target_version": self.cfg.target_version,
            "namespace": self.cfg.namespace,
            "release": self.cfg.release,
            "registry": self.cfg.registry,
            "dry_run": self.cfg.dry_run,
            "aborted": self.aborted,
            "converged": self.converged,
            "resolved_image": self.image_ref.as_dict() if self.image_ref else None,
            "helm_revision": self.helm_revision,
            "surge": self.surge,
            "phases": [asdict(r) for r in self.records],
            # ── the non-self-certifying contract ──
            "certified": None,
            "verification": VERIFICATION_DEFERRED,
            "generated_at": _now_iso(),
        }

    def phase_report(self) -> dict[str, Any]:
        """Record P5, build the report (now including P5), and write the bundle."""
        out_dir = Path(self.cfg.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _now_iso().replace(":", "").replace("-", "")
        base = f"deploy-{self.cfg.target_version}-{stamp}"
        json_path = out_dir / f"{base}.json"
        self._record(
            PHASES[5],
            "planned" if self.cfg.dry_run else "ok",
            detail=f"report written to {json_path}",
            evidence={"report_path": str(json_path)},
        )
        report = self.build_report()
        json_path.write_text(json.dumps(report, indent=2, default=str))
        (out_dir / f"{base}.txt").write_text(self.human_summary(report))
        return report

    def human_summary(self, report: dict[str, Any]) -> str:
        lines = [
            "AuditTrace deploy runner — report",
            "=" * 40,
            f"target version : {report['target_version']}",
            f"registry       : {report['registry']}",
            f"namespace      : {report['namespace']}",
            f"dry run        : {report['dry_run']}",
            f"aborted        : {report['aborted']}",
            f"converged      : {report['converged']}",
            f"resolved image : {report['resolved_image']}",
            f"helm revision  : {report['helm_revision']}",
            f"surge          : {report['surge'] or 'n/a'}",
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
            f"certified     : {report['certified']}  (runner NEVER self-certifies)",
            f"verification  : {report['verification']}",
        ]
        return "\n".join(lines) + "\n"

    # -- orchestration --

    def run(self) -> dict[str, Any]:
        try:
            self.phase_preflight()
            self.phase_resolve()
            self.phase_chart_apply()
            self.phase_bootstrap()
            self.phase_settle()
        except (PreflightAbortError, registry.DigestResolutionError):
            # Abort the mutation sequence but ALWAYS still emit a report — a
            # deploy instrument that crashes without a report is useless.
            self.aborted = True
        return self.phase_report()


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.deploy.runner",
        description="Deterministic, idempotent AuditTrace deploy runner (does NOT self-certify).",
    )
    parser.add_argument(
        "--target-version", required=True, help="image tag / version to deploy"
    )
    parser.add_argument("--namespace", default="audittrace")
    parser.add_argument("--release", default="audittrace")
    parser.add_argument("--registry", choices=("hub", "local"), default="hub")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the ordered plan; mutate nothing"
    )
    parser.add_argument("--out-dir", type=Path, default=DeployConfig.out_dir)
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="bounded helm/rollout timeout (seconds)",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> DeployConfig:
    return DeployConfig(
        target_version=args.target_version,
        namespace=args.namespace,
        release=args.release,
        registry=args.registry,
        dry_run=args.dry_run,
        out_dir=args.out_dir,
        timeout=args.timeout,
    )


def print_plan(cfg: DeployConfig, report: dict[str, Any]) -> None:
    print(
        f"deploy plan (dry-run={cfg.dry_run}) — target {cfg.target_version} on {cfg.registry}\n"
    )
    for rec in report["phases"]:
        print(f"  [{rec['name']}] {rec['status']} — {rec['detail']}")
        if rec.get("command"):
            print(f"      $ {rec['command']}")
    print(
        f"\n  certified: {report['certified']}  |  verification: {report['verification']}"
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = build_parser().parse_args(argv)
    cfg = config_from_args(args)
    runner = DeployRunner(cfg)
    report = runner.run()
    print_plan(cfg, report)
    if report["aborted"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
