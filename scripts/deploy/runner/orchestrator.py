"""The ordered P0-P5 phases, the CLI, and the non-self-certifying report.

:class:`DeployRunner` composes :class:`~scripts.deploy.runner.convergence.ConvergenceMixin`
and :class:`~scripts.deploy.runner.helm.HelmMixin` with its own P0/P1/P3/P4/P5
phase methods. Methods defined directly in THIS module call the ``_run`` /
``_sleep`` seams via the PACKAGE namespace (``_runner_pkg._run(...)``), same
as the mixins — see ``_exec.py``'s module docstring.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scripts.deploy import mesh, registry
from scripts.deploy import runner as _runner_pkg
from scripts.deploy.runner._exec import _now_iso
from scripts.deploy.runner.config import (
    _COMPONENT_SELECTOR,
    PHASES,
    PREFLIGHT_SCRIPT,
    VERIFICATION_DEFERRED,
    DeployConfig,
    MeshGateAbortError,
    PhaseRecord,
    PreflightAbortError,
    build_parser,
    config_from_args,
)
from scripts.deploy.runner.convergence import ConvergenceMixin, within_surge_bound
from scripts.deploy.runner.helm import HelmMixin

logger = logging.getLogger("audittrace.deploy.runner")


class DeployRunner(ConvergenceMixin, HelmMixin):
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
        # #456 — default KUBECONFIG to ~/.kube/config when unset and the file
        # exists. A bare k3s `kubectl` otherwise falls back to the root-only
        # /etc/rancher/k3s/k3s.yaml (permission denied) and false-aborts P0.
        # _run() (scripts/deploy/runner/_exec.py) merges os.environ into every
        # subprocess env, so seeding it here propagates to every kubectl/helm
        # exec this runner makes.
        if not os.environ.get("KUBECONFIG"):
            default_kubeconfig = Path.home() / ".kube" / "config"
            if default_kubeconfig.is_file():
                os.environ["KUBECONFIG"] = str(default_kubeconfig)
        # Injected so tests drive it without a cluster. The default gate now ships
        # the WS5 PrivilegedHealer: ``available`` reflects kubectl reachability, so
        # the safe RBAC-tier istiod restart works on ANY reachable cluster; the
        # host-root tier self-gates on the Option-B install and stays fail-closed
        # where the out-of-band units are not present (#384 WS5, decoupled per Luis
        # 2026-08-01). No cluster reachable → unavailable → gate fails closed.
        self.mesh_gate = mesh_gate or mesh.MeshGate(
            mesh.MeshGateConfig(namespace=cfg.namespace),
            healer=mesh.PrivilegedHealer(),
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
        proc = _runner_pkg._run(cmd, env=env)
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
        proc = _runner_pkg._run(cmd)
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
        proc = _runner_pkg._run(
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
        proc = _runner_pkg._run(
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
        _runner_pkg._run(
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
                _runner_pkg._sleep(self.cfg.settle_interval)
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
