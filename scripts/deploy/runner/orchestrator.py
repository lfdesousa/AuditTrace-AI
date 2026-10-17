"""The ordered P0-P5 phases, the CLI, and the non-self-certifying report.

:class:`DeployRunner` composes :class:`~scripts.deploy.runner.convergence.ConvergenceMixin`,
:class:`~scripts.deploy.runner.helm.HelmMixin`, and
:class:`~scripts.deploy.runner.images.FirstPartyImagesMixin` with its own
P0/P1/P3/P4/P5 phase methods. Methods defined directly in THIS module call
the ``_run`` / ``_sleep`` seams via the PACKAGE namespace
(``_runner_pkg._run(...)``), same as the mixins — see ``_exec.py``'s module
docstring.
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
    FIRST_PARTY_MISMATCH_EXIT,
    IMAGE_PIN_LAG_EXIT,
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
from scripts.deploy.runner.images import (
    FIRST_PARTY_PREFIXES,
    FirstPartyImagesMixin,
    _manifest_workload_docs,
    _parse_image_ref,
    _pod_spec_of,
    mismatched_components,
)

logger = logging.getLogger("audittrace.deploy.runner")


def _pinned_tag(mapping: dict[str, Any] | None, *keys: str) -> str | None:
    """Walk a nested ``dict`` by ``keys``, returning the leaf only if it is a
    ``str`` — a pure helper so D6's preflight check is unit-testable without
    a chart file or a cluster."""
    value: Any = mapping
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, str) else None


class DeployRunner(ConvergenceMixin, HelmMixin, FirstPartyImagesMixin):
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
        # BFF-BUMP-1.29.1 D3/Rule B1 — the shared first-party image rows
        # computed in :meth:`phase_settle`, and whether any of them is
        # unequal. Stay empty/False on a dry run or an aborted run (D3 never
        # reads the cluster there) — ``main()`` only returns
        # ``FIRST_PARTY_MISMATCH_EXIT`` when this flips True.
        self.first_party_rows: list[dict[str, Any]] = []
        self.first_party_mismatch: bool = False
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
                detail=f"would run deploy-preflight.sh; {self._image_pin_freshness_note()}",
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
        # D6 (spec 2026-10-07-SPEC-bff-bump-1.29.1-and-stale-override-guard.md)
        # — AFTER the mesh gate, BEFORE any P1+ network/mutation: a release
        # cannot be deployed before its own per-tag re-pin PR lands.
        self._check_image_pin_freshness()

    def _image_pin_lag(self) -> dict[str, str]:
        """Pure (no ``_record``, no mutation, no abort): ``{name: committed
        tag}`` for every per-tag pin D6 tracks (``console.bff.image.tag``,
        ``tests.image.tag``) whose committed tag does NOT equal
        ``--target-version``. Empty when every known pin is already fresh,
        or when ``--registry`` isn't ``hub`` (S3c — ``publish.yml`` only
        ever builds a per-tag BFF/tests image for a hub release push)."""
        if self.cfg.registry != "hub":
            return {}
        chart_values = _runner_pkg._read_chart_values(
            values_files=self.cfg.values_files
        )
        target = self.cfg.image_tag
        return {
            name: tag
            for name, tag in (
                ("bff", _pinned_tag(chart_values, "console", "bff", "image", "tag")),
                ("tests", _pinned_tag(chart_values, "tests", "image", "tag")),
            )
            if tag is not None and tag != target
        }

    def _image_pin_freshness_note(self) -> str:
        """The dry-run plan string for D6 — folded into the SAME single P0
        ``planned`` record ``phase_preflight`` already emits for the
        preflight script, rather than a second phase entry (dry-run never
        calls the cluster or mesh gate either, so a second P0 record here
        would be the only phase the dry-run path ever doubles up)."""
        if self.cfg.registry != "hub":
            return f"image-pin freshness check skipped (registry={self.cfg.registry!r}, D6 applies to hub only)"
        lagging = self._image_pin_lag()
        if not lagging:
            return f"image pins already match target {self.cfg.image_tag}"
        pins = ", ".join(f"{name}={tag}" for name, tag in sorted(lagging.items()))
        return (
            f"would ABORT: chart per-tag pin lags target ({pins}, "
            f"target={self.cfg.image_tag}) — land the re-pin first"
        )

    def _check_image_pin_freshness(self) -> None:
        """D6 preflight abort (non-dry-run) — the chart's per-tag image
        pins must equal the deploy's own target version BEFORE any
        mutation or network call. Records its OWN P0 entry (mirroring the
        mesh gate's own second P0 record, immediately above this call) and
        raises on a lag.

        Falsifiable: deploy a chart whose ``console.bff.image.tag`` is
        still the PRIOR release while ``--target-version`` is the new one —
        this method raises :class:`PreflightAbortError` with
        :data:`IMAGE_PIN_LAG_EXIT` (evidence only; the PROCESS exit for
        every ``PreflightAbortError`` stays 3, Addendum A §3 S2) BEFORE P1
        resolves anything on the registry.
        """
        if self.cfg.registry != "hub":
            self._record(
                PHASES[0],
                "skipped",
                detail=(
                    f"image-pin freshness check skipped (registry={self.cfg.registry!r}; "
                    "D6 applies to hub only, S3c)"
                ),
            )
            return
        lagging = self._image_pin_lag()
        if not lagging:
            self._record(
                PHASES[0],
                "ok",
                detail=f"chart per-tag image pins already match target {self.cfg.image_tag}",
            )
            return
        pins = ", ".join(f"{name}={tag}" for name, tag in sorted(lagging.items()))
        meaning = (
            f"chart per-tag pin lags target ({pins}, target={self.cfg.image_tag}) — "
            "land the re-pin first"
        )
        self._record(
            PHASES[0],
            "aborted",
            detail=meaning,
            evidence={
                "exit_code": IMAGE_PIN_LAG_EXIT,
                "lagging": lagging,
                "target_version": self.cfg.image_tag,
            },
        )
        raise PreflightAbortError(IMAGE_PIN_LAG_EXIT, meaning)

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

    def _wait_rollout(self, kind: str, name: str) -> None:
        """Bounded ``kubectl rollout status`` for one workload (not a
        control-flow branch on wall-clock) — S1(a): every workload D3
        compares gets its rollout awaited, not memory-server alone. A
        timeout is recorded as a non-zero ``_run`` result and swallowed
        here (never raised) — the live digest read right after this will
        itself surface a stale pod as a row mismatch, which IS how a
        timeout gets reported (``flagged``), not a crash."""
        _runner_pkg._run(
            [
                "kubectl",
                "rollout",
                "status",
                f"{kind.lower()}/{name}",
                "-n",
                self.cfg.namespace,
                f"--timeout={self.cfg.timeout}s",
            ]
        )

    def _first_party_workload_names(
        self, manifest_docs: list[dict[str, Any]] | None
    ) -> list[tuple[str, str]]:
        """``(kind, name)`` for every Deployment/StatefulSet/DaemonSet in
        the live manifest carrying at least one first-party container or
        init container — the set S1(a) waits rollout for. Job/CronJob are
        excluded: ``kubectl rollout status`` does not support them."""
        names: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for doc in _manifest_workload_docs(manifest_docs):
            kind = doc.get("kind")
            if kind not in ("Deployment", "StatefulSet", "DaemonSet"):
                continue
            name = (doc.get("metadata") or {}).get("name")
            pod_spec = _pod_spec_of(doc)
            if not name or pod_spec is None or (kind, name) in seen:
                continue
            has_first_party = any(
                isinstance(container, dict)
                and container.get("image")
                and any(
                    _parse_image_ref(container["image"])[0].startswith(prefix)
                    for prefix in FIRST_PARTY_PREFIXES
                )
                for key in ("containers", "initContainers")
                for container in pod_spec.get(key) or []
            )
            if has_first_party:
                seen.add((kind, name))
                names.append((kind, name))
        return names

    def phase_settle(self) -> None:
        if self.cfg.dry_run:
            self._record(
                PHASES[4],
                "planned",
                command=f"kubectl rollout status deployment/{self.cfg.deployment} + sample pods x{self.cfg.settle_samples}",
                detail=(
                    f"would assert peak concurrent Running <= spec.replicas ({_COMPONENT_SELECTOR}); "
                    "would compare every first-party image row (rendered == chart == live)"
                ),
            )
            return
        started = _now_iso()
        manifest_docs = self._live_manifest_docs()
        # S1(a) — wait rollout for EVERY first-party workload D3 compares,
        # not memory-server alone. Memory-server is waited UNCONDITIONALLY
        # first (byte-identical to the pre-fix behaviour, even when the
        # manifest is unreadable); any OTHER first-party workload found in
        # the live manifest (console librechat/bff) is waited too.
        self._wait_rollout("deployment", self.cfg.deployment)
        waited = {("Deployment", self.cfg.deployment)}
        for kind, name in self._first_party_workload_names(manifest_docs):
            if (kind, name) in waited:
                continue
            waited.add((kind, name))
            self._wait_rollout(kind, name)
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
        # D3 — the shared first-party image rows, re-read now that every
        # compared workload's rollout has been awaited (so a Terminating
        # old pod has had its bounded chance to actually finish). Runs on
        # EVERY non-dry-run path, apply or noop (Addendum A Rule B1's
        # "D3 runs on every non-dry-run path") — on noop the rows were
        # already all-equal (that is why it no-op'd); recording them again
        # here keeps P4's evidence symmetric across both paths.
        rows = self._first_party_image_rows(manifest_docs=manifest_docs)
        self.first_party_rows = rows
        mismatched = mismatched_components(rows)
        self.first_party_mismatch = bool(mismatched)
        # B-3 (fix round 1): a report must never say `converged: true` AND
        # `first_party_mismatch: true` at once (Addendum A Rule B1: "a
        # report may say converged: true only when all rows are equal").
        # P2 `noop` sets `self.converged = True` from the PRE-apply
        # convergence check; if THIS (independent, re-read) check finds a
        # mismatch, that earlier verdict is overridden here — covers both
        # a genuinely stale pre-apply read and state that drifted between
        # the P2 check and this P4 recheck. Falsifiable: drop this line and
        # `test_noop_path_d3_recheck_clears_converged_and_exits_eight` goes
        # RED (`converged` stays True alongside `first_party_mismatch=True`).
        if mismatched:
            self.converged = False
        detail = (
            f"rollout settled; peak concurrent Running={peak} <= replicas={replicas}"
            if ok
            else f"SURGE OBSERVED: peak Running={peak} EXCEEDED replicas={replicas} (WS1 violation)"
        )
        if mismatched:
            detail += f"; first-party image mismatch: {', '.join(mismatched)}"
        self._record(
            PHASES[4],
            "ok" if ok and not mismatched else "flagged",
            detail=detail,
            evidence={**self.surge, "first_party_images": rows},
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
            # BFF-BUMP-1.29.1 D3 — the shared first-party image rows read in
            # phase_settle (also mirrored under P4's own evidence); a
            # top-level flag so main() can decide the exit code without
            # re-deriving it from the phase list.
            "first_party_mismatch": self.first_party_mismatch,
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
            f"first-party mismatch : {report['first_party_mismatch']}",
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
    # Addendum A §3 S2 — a report whose first-party image rows are NOT all
    # equal (apply or noop path; never dry-run) gets its own exit so a
    # caller can tell "flagged, stop and report the rows" apart from both
    # "ok" (0) and an uncaught exception (1).
    if report["first_party_mismatch"]:
        return FIRST_PARTY_MISMATCH_EXIT
    return 0
