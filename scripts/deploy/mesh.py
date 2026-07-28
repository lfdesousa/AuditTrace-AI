"""Deploy-path mesh-health gate + bounded auto-heal — #384 WS1 (public half).

This is the DEPLOY-TIME GATE that enforces invariant **I1** ("a deploy must
never break the mesh"): a fail-closed check the CD runner executes in P0, BEFORE
it mutates anything — in particular before P2 terminates the only memory-server
pod into a mesh that cannot re-issue its workload cert (the #307 fault path).

Architecture (RATIFIED 2026-07-28, spec §7): an **event-hook doctor + deploy-time
gate**. There is deliberately NO standalone always-on mesh controller (the
bootstrap-paradox was rejected). This module is the diagnosis + gate logic lifted
from the private ``k3s-doctor`` into a pure, unit-testable form.

What WS1 ships here:

* :func:`evaluate_nodes`, :func:`evaluate_istiod_logs`,
  :func:`evaluate_istiod_readiness` — **pure** diagnosers, each taking already
  gathered command output and returning structured :class:`Finding`s with the
  evidence they stand on. No I/O, trivially falsifiable.
* :class:`MeshGate` — gathers the WS1 signal set through a single monkeypatchable
  subprocess seam (:func:`_run`, same shape as ``runner.py`` / ``verify.py``),
  diagnoses, and on a degraded mesh runs a **bounded, idempotent** auto-heal via
  an INJECTED healer seam, re-diagnosing after each attempt. It returns a
  structured :class:`MeshGateResult` — ``HEALTHY`` / ``HEALED`` / ``UNSAFE``.
* The **privilege seam** (open question Q3, DEFERRED): the actual privileged
  recovery (restart k3s / reprogram the ClusterIP route) needs root, which the
  runner must never hold. WS1 ships only the seam (:class:`MeshHealer`) plus a
  safe default (:class:`UnconfiguredHealer`) that reports itself *unavailable* so
  a degraded mesh FAILS CLOSED. The real privileged healer is a later workstream
  (WS5) — this module never shells out to root.

Fail-closed discipline (the "real subprocess raises where a mock returns" lesson,
inherited from ``verify.py``/``registry.py``): every probe funnels through
:meth:`MeshGate._probe`, which maps a non-zero exit, an ``OSError`` (e.g.
``kubectl`` not on PATH), or a ``subprocess.SubprocessError`` (a probe timeout) to
a diagnosis *finding*, never to an uncaught exception and never to a false
"healthy". Any uncertainty is treated as UNSAFE.

The WS1 signal set is intentionally the minimum the *gate* must know before
mutating (can a new pod get a cert; is the mesh already cert-dead). The full
four-probe external verify set (``sidecars-have-certs``, ``vault-secret-rendered``,
``no-unhealthy-upstream`` …) belongs to the independent Verify runner and is
WS2's scope, not this module's.
"""

from __future__ import annotations

import json
import logging
import subprocess  # noqa: S404 - read-only kubectl reads; fixed argv, no shell
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# ── gate outcomes (fixed vocabulary; the runner branches on ``safe``) ─────────
HEALTHY = "healthy"
HEALED = "degraded-but-healed"
UNSAFE = "unsafe"

# The #307 smoking-gun signatures: istiod cannot reach the kube-API ClusterIP to
# authenticate a CSR (TokenReview) or sign it. Any of these in the istiod log
# window means a NEW sidecar cannot get a leaf cert — a deploy would strand the
# replacement pod. Identical set to the private k3s-doctor + preflight probes.
SMOKING_GUN_PATTERNS = ("no route to host", "Unauthenticated", "tokenreviews")

ISTIO_NAMESPACE = "istio-system"
ISTIOD_DEPLOYMENT = "istiod"
ISTIOD_LABEL = "app=istiod"

# Q6: the log window must be long enough to catch the fault yet short enough that
# a normal cold start (150 s startupProbe budget) is never mis-read. 3m matches
# the documented startup profile and the k3s-doctor default; boundary unit-tested.
DEFAULT_LOG_WINDOW = "3m"

# Bounded auto-heal: never loop unbounded, never bounce a healthy cluster.
DEFAULT_MAX_HEAL_ATTEMPTS = 2
DEFAULT_HEAL_BACKOFF = 5.0

# Every kubectl read is bounded: a hanging call against a wedged ClusterIP IS the
# fault, so it must time out into a fail-closed finding, not block the deploy.
DEFAULT_PROBE_TIMEOUT = 30


# ── external-effect indirections (monkeypatched in tests) ─────────────────────


def _run(
    cmd: list[str], *, timeout: int = DEFAULT_PROBE_TIMEOUT
) -> subprocess.CompletedProcess:
    """Run a read-only kubectl command, bounded. Sole subprocess entry point."""
    logger.info("exec: %s", " ".join(cmd))
    return subprocess.run(  # noqa: S603 - fixed argv lists, no shell
        cmd, capture_output=True, text=True, check=False, timeout=timeout
    )


def _sleep(seconds: float) -> None:
    """Backoff between heal attempts. Not a control-flow branch; mocked to 0."""
    time.sleep(seconds)


def _now_iso() -> str:
    """UTC ISO-8601 timestamp — EVIDENCE ONLY, never read back into logic."""
    return datetime.now(UTC).isoformat()


def _parse_json(payload: str) -> Any:
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None


# ── records ───────────────────────────────────────────────────────────────────


@dataclass
class Finding:
    """One diagnosed mesh problem, with the evidence it stands on."""

    signal: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"signal": self.signal, "detail": self.detail, "evidence": self.evidence}


@dataclass
class Diagnosis:
    """The full set of findings from one diagnose pass. Healthy == no findings."""

    findings: list[Finding] = field(default_factory=list)
    checked_at: str = field(default_factory=_now_iso)

    @property
    def healthy(self) -> bool:
        return not self.findings

    @property
    def signals(self) -> list[str]:
        return [f.signal for f in self.findings]

    def as_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "findings": [f.as_dict() for f in self.findings],
            "checked_at": self.checked_at,
        }


@dataclass
class HealAttempt:
    """The record of one auto-heal attempt (what the healer seam did)."""

    performed: bool
    action: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "performed": self.performed,
            "action": self.action,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass
class MeshGateResult:
    """The gate's structured verdict. ``safe`` is what the runner branches on."""

    outcome: str
    initial_diagnosis: Diagnosis
    heal_attempts: list[HealAttempt] = field(default_factory=list)
    final_diagnosis: Diagnosis | None = None
    reason: str = ""
    checked_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        if not self.reason:
            self.reason = self._default_reason()

    def _default_reason(self) -> str:
        if self.outcome == HEALTHY:
            return "mesh healthy; safe to proceed"
        signals = ", ".join(self.initial_diagnosis.signals) or "unknown"
        if self.outcome == HEALED:
            return f"mesh was degraded ({signals}) and auto-healed; safe to proceed"
        return f"mesh degraded ({signals}) and could not be auto-healed; failing closed"

    @property
    def safe(self) -> bool:
        """True iff the deploy may proceed — HEALTHY or auto-HEALED, never UNSAFE."""
        return self.outcome in (HEALTHY, HEALED)

    @property
    def healthy(self) -> bool:
        return self.outcome == HEALTHY

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "safe": self.safe,
            "reason": self.reason,
            "initial_diagnosis": self.initial_diagnosis.as_dict(),
            "heal_attempts": [h.as_dict() for h in self.heal_attempts],
            "final_diagnosis": (
                self.final_diagnosis.as_dict() if self.final_diagnosis else None
            ),
            "checked_at": self.checked_at,
        }


class MeshUnsafeError(RuntimeError):
    """Raised by :func:`assert_safe_to_deploy` when the mesh is UNSAFE."""

    def __init__(self, result: MeshGateResult) -> None:
        super().__init__(result.reason)
        self.result = result


# ── the privilege seam (Q3 — DEFERRED to WS5) ─────────────────────────────────


class MeshHealer(Protocol):
    """The injected privileged-recovery seam.

    WS1 defines the CONTRACT only. The real implementation (restart k3s /
    reprogram the ClusterIP DNAT — a root operation the runner must never hold)
    is WS5, behind whichever least-privilege trigger Q3 settles on (scoped
    sudoers / systemd trigger / root helper). A test injects a fake healer.
    """

    @property
    def available(self) -> bool:
        """False → the gate skips the heal loop and fails closed."""
        ...  # pragma: no cover - Protocol stub

    def heal(self, diagnosis: Diagnosis) -> HealAttempt:
        """Attempt a bounded, idempotent recovery for the given findings."""
        ...  # pragma: no cover - Protocol stub


class UnconfiguredHealer:
    """The safe WS1 default: no privileged healer wired, so heal is UNAVAILABLE.

    A degraded mesh therefore fails CLOSED (never a silent proceed). The real
    privileged healer replaces this in WS5.
    """

    available = False

    def heal(self, diagnosis: Diagnosis) -> HealAttempt:
        return HealAttempt(
            performed=False,
            action="none",
            detail=(
                "auto-heal not configured — the privileged recovery seam is "
                "deferred to WS5 (Q3); failing closed"
            ),
        )


# ── pure diagnosers (unit-tested directly, no I/O) ────────────────────────────


def count_smoking_gun(logs: str) -> int:
    """Count istiod log LINES matching any #307 smoking-gun pattern (grep -c)."""
    return sum(
        1
        for line in logs.splitlines()
        if any(pattern in line for pattern in SMOKING_GUN_PATTERNS)
    )


def evaluate_nodes(ready_statuses: str) -> list[Finding]:
    """Diagnose node readiness from a ``Ready`` condition jsonpath dump.

    ``ready_statuses`` is the space-joined ``status`` of every node's ``Ready``
    condition (e.g. ``"True True"``). An empty string means the cluster returned
    no node — fail-closed uncertainty, not health.
    """
    tokens = ready_statuses.split()
    if not tokens:
        return [
            Finding(
                "nodes-unreadable",
                "no node Ready status returned (cluster empty or unreachable)",
                {"ready_statuses": ready_statuses},
            )
        ]
    not_ready = [t for t in tokens if t != "True"]
    if not_ready:
        return [
            Finding(
                "node-not-ready",
                f"{len(not_ready)} of {len(tokens)} node(s) NotReady",
                {"ready_statuses": tokens},
            )
        ]
    return []


def evaluate_istiod_logs(logs: str, window: str) -> list[Finding]:
    """Diagnose the #307 istiod↔kube-API fault from the istiod log window."""
    count = count_smoking_gun(logs)
    if count > 0:
        return [
            Finding(
                "istiod-api-unreachable",
                (
                    f"{count} istiod<->kube-API smoking-gun line(s) in the last "
                    f"{window} (#307: new sidecars cannot get a cert)"
                ),
                {"match_count": count, "window": window, "log_tail": logs[-2000:]},
            )
        ]
    return []


def evaluate_istiod_readiness(
    deployment: dict[str, Any] | None, endpoint_ips: str
) -> list[Finding]:
    """Diagnose istiod control-plane readiness.

    ``deployment`` is the parsed ``istiod`` Deployment JSON (``None`` when it
    could not be read → fail closed). ``endpoint_ips`` is the space-joined ready
    endpoint IP list; zero endpoints means no reachable istiod even if the
    Deployment claims readiness.
    """
    if not isinstance(deployment, dict):
        return [
            Finding(
                "istiod-unreadable",
                "istiod Deployment could not be read (fail-closed)",
                {"deployment": None},
            )
        ]
    # ``... or {}`` (not ``.get(k, {})``) so a present-but-NULL ``spec``/``status``
    # — valid JSON the kube-API can emit for a half-created object — degrades to a
    # fail-closed finding instead of ``None.get(...)`` raising AttributeError. A
    # crash here would escape run()'s abort handler and emit NO report.
    desired = (deployment.get("spec") or {}).get("replicas")
    if not isinstance(desired, int):
        return [
            Finding(
                "istiod-unreadable",
                "istiod desired replica count is unreadable (fail-closed)",
                {"spec_replicas": desired},
            )
        ]
    ready = (deployment.get("status") or {}).get("readyReplicas", 0)
    if not isinstance(ready, int):
        ready = 0
    endpoints = len(endpoint_ips.split())
    findings: list[Finding] = []
    if desired < 1 or ready < desired:
        findings.append(
            Finding(
                "istiod-not-ready",
                f"istiod readyReplicas={ready} of desired={desired}",
                {"ready_replicas": ready, "desired_replicas": desired},
            )
        )
    if endpoints < 1:
        findings.append(
            Finding(
                "istiod-no-endpoints",
                "istiod Service has no Ready endpoints (control plane unreachable)",
                {"endpoint_count": endpoints},
            )
        )
    return findings


# ── the gate ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MeshGateConfig:
    namespace: str = "audittrace"
    istio_namespace: str = ISTIO_NAMESPACE
    log_window: str = DEFAULT_LOG_WINDOW
    max_heal_attempts: int = DEFAULT_MAX_HEAL_ATTEMPTS
    heal_backoff: float = DEFAULT_HEAL_BACKOFF
    probe_timeout: int = DEFAULT_PROBE_TIMEOUT


class MeshGate:
    """Diagnoses the WS1 mesh signal set and, if degraded, bounded-auto-heals.

    Fail-closed: any probe that cannot be read (non-zero exit, ``kubectl`` not on
    PATH, or a probe timeout) becomes a *finding*, so an unreadable mesh is
    treated as UNSAFE, never silently healthy.
    """

    def __init__(
        self,
        cfg: MeshGateConfig | None = None,
        healer: MeshHealer | None = None,
    ) -> None:
        self.cfg = cfg or MeshGateConfig()
        self.healer: MeshHealer = healer or UnconfiguredHealer()

    # -- the single fail-closed probe boundary --

    def _probe(self, cmd: list[str]) -> tuple[bool, str]:
        """Run a read-only probe. Return ``(ok, output)``.

        ``ok`` is False on a non-zero exit OR any transport failure — a missing
        binary (``OSError``) or a timeout (``subprocess.SubprocessError``, which
        ``TimeoutExpired`` subclasses). This is the ONE place a real subprocess
        error is mapped to a fail-closed signal instead of crashing the gate.
        """
        try:
            proc = _run(cmd, timeout=self.cfg.probe_timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("mesh probe failed: %s (%s)", " ".join(cmd), exc)
            return False, str(exc)
        if proc.returncode != 0:
            return False, proc.stderr or proc.stdout
        return True, proc.stdout

    # -- gathering commands --

    def _nodes_cmd(self) -> list[str]:
        return [
            "kubectl",
            "get",
            "nodes",
            "-o",
            'jsonpath={.items[*].status.conditions[?(@.type=="Ready")].status}',
        ]

    def _istiod_logs_cmd(self) -> list[str]:
        return [
            "kubectl",
            "logs",
            "-n",
            self.cfg.istio_namespace,
            "-l",
            ISTIOD_LABEL,
            f"--since={self.cfg.log_window}",
        ]

    def _istiod_deployment_cmd(self) -> list[str]:
        return [
            "kubectl",
            "get",
            "deployment",
            ISTIOD_DEPLOYMENT,
            "-n",
            self.cfg.istio_namespace,
            "-o",
            "json",
        ]

    def _istiod_endpoints_cmd(self) -> list[str]:
        return [
            "kubectl",
            "get",
            "endpoints",
            ISTIOD_DEPLOYMENT,
            "-n",
            self.cfg.istio_namespace,
            "-o",
            "jsonpath={.subsets[*].addresses[*].ip}",
        ]

    # -- diagnose --

    def diagnose_mesh(self) -> Diagnosis:
        """Gather the WS1 signal set and return a structured :class:`Diagnosis`."""
        findings: list[Finding] = []

        ok, out = self._probe(self._nodes_cmd())
        if not ok:
            findings.append(
                Finding(
                    "nodes-unreadable",
                    "could not read node status (fail-closed)",
                    {"error": out[-2000:]},
                )
            )
        else:
            findings.extend(evaluate_nodes(out))

        ok, out = self._probe(self._istiod_logs_cmd())
        if not ok:
            findings.append(
                Finding(
                    "istiod-logs-unreadable",
                    "could not read istiod logs (fail-closed)",
                    {"error": out[-2000:]},
                )
            )
        else:
            findings.extend(evaluate_istiod_logs(out, self.cfg.log_window))

        dep_ok, dep_out = self._probe(self._istiod_deployment_cmd())
        ep_ok, ep_out = self._probe(self._istiod_endpoints_cmd())
        if not dep_ok or not ep_ok:
            findings.append(
                Finding(
                    "istiod-unreadable",
                    "could not read istiod readiness (fail-closed)",
                    {
                        "deployment_error": None if dep_ok else dep_out[-2000:],
                        "endpoints_error": None if ep_ok else ep_out[-2000:],
                    },
                )
            )
        else:
            findings.extend(evaluate_istiod_readiness(_parse_json(dep_out), ep_out))

        return Diagnosis(findings=findings)

    # -- evaluate (diagnose → bounded heal → re-diagnose) --

    def evaluate(self) -> MeshGateResult:
        """The fail-closed gate: HEALTHY, HEALED, or UNSAFE (bounded heal)."""
        initial = self.diagnose_mesh()
        if initial.healthy:
            logger.info("mesh healthy — safe to proceed")
            return MeshGateResult(HEALTHY, initial, [], initial)

        logger.error("mesh degraded: %s", ", ".join(initial.signals))
        if not self.healer.available:
            return MeshGateResult(
                UNSAFE,
                initial,
                [],
                initial,
                reason=(
                    f"mesh degraded ({', '.join(initial.signals)}) and auto-heal is "
                    "not configured (privilege seam deferred to WS5); failing closed"
                ),
            )

        attempts: list[HealAttempt] = []
        latest = initial
        for i in range(self.cfg.max_heal_attempts):
            try:
                attempt = self.healer.heal(latest)
            except Exception as exc:  # noqa: BLE001 - fail closed on ANY healer error
                logger.error("healer raised on attempt %d: %s", i + 1, exc)
                attempts.append(HealAttempt(False, "error", f"healer raised: {exc}"))
                return MeshGateResult(UNSAFE, initial, attempts, latest)
            attempts.append(attempt)
            _sleep(self.cfg.heal_backoff * (i + 1))
            latest = self.diagnose_mesh()
            if latest.healthy:
                logger.info("mesh auto-healed after %d attempt(s)", i + 1)
                return MeshGateResult(HEALED, initial, attempts, latest)

        logger.error(
            "mesh still degraded after %d heal attempt(s): %s",
            self.cfg.max_heal_attempts,
            ", ".join(latest.signals),
        )
        return MeshGateResult(UNSAFE, initial, attempts, latest)


def assert_safe_to_deploy(gate: MeshGate) -> MeshGateResult:
    """Evaluate the gate; raise :class:`MeshUnsafeError` if the mesh is UNSAFE.

    A convenience for callers that prefer an exception to a status check. The CD
    runner uses :meth:`MeshGate.evaluate` directly so it can record the structured
    result into its report before aborting.
    """
    result = gate.evaluate()
    if not result.safe:
        raise MeshUnsafeError(result)
    return result
