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
import os
import subprocess  # noqa: S404 - read-only kubectl reads; fixed argv, no shell
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
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


# ── WS5: the real privileged healer + its two-tier action map ─────────────────
#
# The heal model (spec §2): map each diagnosed ``Finding.signal`` to the action
# that resolves it, tiered by privilege:
#
#   * RBAC tier  — an in-cluster ``kubectl rollout restart deployment/istiod``.
#     No host privilege, no Q3 trigger; a stale/degraded control plane is re-
#     rolled. Idempotent: restarting an already-healthy istiod is a no-op roll.
#   * HOST-ROOT tier — the #307 class (a NotReady node, or istiod unable to
#     reach the kube-API ClusterIP: "no route to host"). The fix is a k3s /
#     ClusterIP-DNAT re-settle, which needs root the runner MUST NOT hold. This
#     is the ONLY tier that crosses the Q3 privilege boundary, and it does so
#     through exactly one narrow method, :meth:`PrivilegedHealer._trigger_host_resettle`.
#
# Escalation ORDER (fixed 2026-08-02, marker WS5-HEALER-ESC-20260802; see
# :meth:`PrivilegedHealer.heal`): host-root is checked BEFORE RBAC, not "least
# privilege first". A host-root signal is the DEEPER fault — RBAC's rollout
# restart cannot fix a broken node route — so when both tiers' signals are
# present simultaneously (the real #307 shape: ``istiod-api-unreachable`` +
# ``istiod-not-ready`` together), checking RBAC first stalls the bounded heal
# loop in a tier that can never converge and starves the tier that would.
#
# FALL-THROUGH on "not installed" (fixed 2026-08-02, same marker; reviewer
# REJECT item 4): host-root only OWNS the call when it can actually ACT. If
# Option-B is not installed here, the host-root tier self-gates to a no-op
# before ever touching the privilege boundary, and ``heal`` falls through to
# the RBAC tier as a best-effort fallback — preserving the class-level
# ``available`` guarantee ("RBAC-tier heal available on ANY reachable
# cluster, even before the units are installed") for the co-occurring-signal
# case too. A host-root attempt that genuinely FIRED (performed, timed out,
# or refused) does NOT fall through: RBAC cannot fix a route loss, so trying
# it would waste a bounded attempt; the gate's own retry/re-diagnose loop is
# the correct mechanism for that outcome.

# RBAC-tier signals — healed by an in-cluster istiod rollout restart.
RBAC_RESTART_SIGNALS = frozenset(
    {"istiod-not-ready", "istiod-degraded", "istiod-no-endpoints"}
)

# HOST-ROOT-tier signals — the ONLY signals the privileged root re-settle unit
# is allowed to act on. This set is the Python-side mirror of the hardcoded
# whitelist in ``scripts/audittrace-mesh-resettle.sh`` (the actual security
# boundary — the root executor re-validates independently and refuses anything
# outside its own copy). Keep the two in sync; both are unit-tested.
HOST_RESETTLE_WHITELIST = frozenset(
    {"node-not-ready", "nodes-unreadable", "istiod-api-unreachable"}
)

# Option B (systemd path-unit) wiring. Both this healer and the root executor
# agree on this directory (override via ``MESH_HEAL_DIR`` for tests / non-default
# installs). The runner writes request files under ``requests/`` (the only thing
# it is privileged to do — write a file); the root unit writes ``results/``.
DEFAULT_MESH_HEAL_DIR = "/var/lib/audittrace/mesh-heal"

# Bounded poll for the async root-unit result (Option B is fire-a-file + wait).
DEFAULT_RESETTLE_TIMEOUT = 120.0
DEFAULT_RESETTLE_POLL = 3.0

# WS5 escalation fix (2026-08-02, marker WS5-HEALER-ESC-20260802): the RBAC-tier
# rollout-status wait is a SEPARATE, SHORTER bound from the host-root poll above.
# Before this split both tiers shared ``DEFAULT_RESETTLE_TIMEOUT`` (120s), so two
# heal attempts stuck in the RBAC tier could burn 2*120s=240s and blow a case's
# 180s budget. RBAC rollout-status is a fast in-cluster wait (no root op, no
# systemd round-trip) so it does not need anywhere near the host-root budget;
# 40s is generous for an istiod re-roll while leaving headroom for a later
# host-root attempt within the same case budget.
DEFAULT_RBAC_ROLLOUT_TIMEOUT = 40.0


def mesh_heal_dir() -> Path:
    """The Option-B heal directory (env-overridable; read at call time)."""
    return Path(os.environ.get("MESH_HEAL_DIR", DEFAULT_MESH_HEAL_DIR))


def is_host_resettle_allowed(signal: str) -> bool:
    """True iff ``signal`` is in the host-root re-settle whitelist (pure).

    The single Python-side source of truth for host-root routing. Falsifiable:
    a whitelisted signal returns True, anything else (including an empty string
    or a near-miss) returns False so the healer never smuggles an un-vetted op
    across the privilege boundary.
    """
    return signal in HOST_RESETTLE_WHITELIST


class PrivilegedHealer:
    """The real WS5 healer: bounded, idempotent, two-tier, fail-closed.

    ``available`` reflects KUBECTL REACHABILITY (decoupled from the Option-B
    install, ratified by Luis 2026-08-01), so the safe RBAC-tier istiod restart
    is available on ANY reachable cluster — even before the systemd units are
    installed — and an unreachable cluster keeps the gate failing closed. The
    host-root re-settle tier SELF-GATES on the install via
    :meth:`_host_root_installed` inside :meth:`_trigger_host_resettle`, so an
    uninstalled host still fails closed for a HOST-ROOT-ONLY fault class (no
    request written, no raise → the gate re-diagnoses → UNSAFE). When a
    host-root signal co-occurs with an RBAC-fixable one on an uninstalled host
    (fix 2026-08-02, marker WS5-HEALER-ESC-20260802, reviewer REJECT item 4),
    :meth:`heal` falls through to the RBAC tier instead of returning that
    no-op directly — so the ``available`` guarantee above holds for that
    co-occurring cell too, not only the host-root-only case.

    ``heal`` performs at most ONE bounded action per call (the gate owns the
    retry/backoff loop and re-diagnoses after each). It NEVER raises to mean
    "couldn't fix" — it returns ``HealAttempt(performed=False, ...)`` and lets
    the gate's re-diagnose decide; a raise is reserved for a true internal error
    (the gate catches it → UNSAFE, ``mesh.py`` evaluate()).

    **#411 v2 (write ONLY to the watched dir, loud-fail, no fallback).** The
    request is written to ``heal_dir/requests`` — the SAME dir
    ``audittrace-mesh-healer.path``'s ``DirectoryNotEmpty=`` watches, sourced
    from the SAME ``MESH_HEAL_DIR`` the ``.service`` sets (single source of
    truth; see :func:`mesh_heal_dir`). There is deliberately no fallback dir:
    the rejected v1 fix wrote to an unwatched location on
    ``PermissionError`` and called that "healed" — nothing was ever watching
    the fallback, so the root ``.path`` unit never fired and the deploy still
    ended UNSAFE, just slower and silently. The durable fix is
    ``scripts/audittrace-mesh-heal.tmpfiles.conf`` (recreates the watched dir
    runner-owned on every boot), not a fallback in this class. If the write
    still raises ``PermissionError``/``OSError`` — a genuine, un-recovered
    misconfiguration — :meth:`_trigger_host_resettle` logs an ERROR naming the
    dir and the tmpfiles fix, then RE-RAISES: this is exactly the "true
    internal error" case the class docstring above carves out, so the gate's
    ``evaluate()`` resolves UNSAFE immediately and loudly rather than either
    fabricating a false "healed" or burning the bounded retry budget against a
    static permission problem retries cannot fix.
    """

    def __init__(
        self,
        heal_dir: Path | None = None,
        *,
        resettle_timeout: float = DEFAULT_RESETTLE_TIMEOUT,
        resettle_poll: float = DEFAULT_RESETTLE_POLL,
        rbac_rollout_timeout: float = DEFAULT_RBAC_ROLLOUT_TIMEOUT,
    ) -> None:
        self._heal_dir = heal_dir or mesh_heal_dir()
        self._resettle_timeout = resettle_timeout
        self._resettle_poll = resettle_poll
        self._rbac_rollout_timeout = rbac_rollout_timeout

    @property
    def _requests_dir(self) -> Path:
        return self._heal_dir / "requests"

    @property
    def _results_dir(self) -> Path:
        return self._heal_dir / "results"

    def _host_root_installed(self) -> bool:
        """True iff the Option-B host-root trigger is wired + writable here."""
        reqs = self._requests_dir
        return reqs.is_dir() and os.access(reqs, os.W_OK)

    @property
    def available(self) -> bool:
        """True iff kubectl can reach the cluster (RBAC-tier heals are possible).

        DECOUPLED from the Option-B host-root install (Luis 2026-08-01): the safe
        RBAC-tier istiod restart works on ANY reachable cluster, even before the
        systemd units are installed, so the gate can recover the common degraded-
        istiod case everywhere. The host-root tier self-gates on the request dir
        inside :meth:`_trigger_host_resettle`, so an UNINSTALLED host still fails
        closed for the host-root fault class (the gate re-diagnoses → UNSAFE).
        Kubectl unreachable → unavailable → the gate fails closed with no heal.
        """
        ok, _ = self._kubectl(["version", "--request-timeout=5s", "-o", "json"])
        return ok

    # -- the least-privileged probe boundary for RBAC-tier heals --

    def _kubectl(self, args: list[str]) -> tuple[bool, str]:
        """Run a kubectl action through the module ``_run`` seam, fail-soft.

        A transport failure (missing binary / timeout) or a non-zero exit is a
        "couldn't fix" — returned as ``(False, msg)``, NEVER raised — so the gate
        re-diagnoses and fails closed instead of falsely reporting HEALED.
        """
        try:
            proc = _run(["kubectl", *args])
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("healer kubectl failed: %s (%s)", " ".join(args), exc)
            return False, str(exc)
        if proc.returncode != 0:
            return False, proc.stderr or proc.stdout
        return True, proc.stdout

    def _restart_istiod(self, finding: Finding) -> HealAttempt:
        """RBAC tier: re-roll istiod (idempotent) + a bounded readiness wait.

        The readiness wait uses ``_rbac_rollout_timeout`` — a SHORTER, separate
        bound from the host-root poll's ``_resettle_timeout`` (WS5 escalation
        fix, marker WS5-HEALER-ESC-20260802). Sharing one timeout let an RBAC
        wait alone consume the whole heal-attempt budget; splitting it leaves
        headroom for a later host-root attempt within the same case budget.
        """
        ok, out = self._kubectl(
            [
                "rollout",
                "restart",
                f"deployment/{ISTIOD_DEPLOYMENT}",
                "-n",
                ISTIO_NAMESPACE,
            ]
        )
        if not ok:
            return HealAttempt(
                performed=False,
                action="restart-istiod",
                detail=f"kubectl rollout restart failed for {finding.signal}",
                evidence={"signal": finding.signal, "error": out[-2000:]},
            )
        # Best-effort bounded wait; its outcome is EVIDENCE only — the gate's
        # re-diagnose is the authority on whether the mesh actually recovered.
        status_ok, status_out = self._kubectl(
            [
                "rollout",
                "status",
                f"deployment/{ISTIOD_DEPLOYMENT}",
                "-n",
                ISTIO_NAMESPACE,
                f"--timeout={int(self._rbac_rollout_timeout)}s",
            ]
        )
        return HealAttempt(
            performed=True,
            action="restart-istiod",
            detail=f"rolled istiod for {finding.signal} (RBAC tier, no privilege seam)",
            evidence={
                "signal": finding.signal,
                "rollout_status_ok": status_ok,
                "rollout_status": status_out[-2000:],
            },
        )

    def _publish_request(self, request_id: str, payload: dict[str, Any]) -> Path:
        """Atomically publish the request JSON into the watched ``requests/`` dir.

        Write a tmp file OUTSIDE ``requests/`` then ``os.replace`` it in, so the
        ``.path`` unit never fires on a half-written request. Context-managed
        write (``feedback_use_context_managers`` — the handle is always closed,
        on every exit path, before the rename). Writes ONLY to the watched dir
        (``self._heal_dir`` / ``self._requests_dir`` — #411 v2, no unwatched
        fallback); propagates ``PermissionError``/``OSError`` unchanged so the
        caller can log the specific failure before it turns UNSAFE.
        """
        tmp_path = self._heal_dir / f".{request_id}.json.tmp"
        req_path = self._requests_dir / f"{request_id}.json"
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload))
        os.replace(tmp_path, req_path)
        return req_path

    def _trigger_host_resettle(self, finding: Finding) -> HealAttempt:
        """HOST-ROOT tier (Q3 boundary): the ONLY method that touches privilege.

        Writes a VALIDATED request file (signal + evidence, JSON) atomically into
        the watched ``requests/`` dir, then polls ``results/`` for the root unit's
        verdict within a bounded timeout. If Q3 is ever revisited, only this
        method changes. Fails closed (``performed=False``) on refusal or timeout;
        never raises for "couldn't fix" — EXCEPT the write itself (see #411 v2
        note below, and the class docstring).
        """
        if not is_host_resettle_allowed(finding.signal):
            # Defence in depth: the caller already routed by tier, but never let
            # an un-whitelisted signal reach the privilege boundary.
            return HealAttempt(
                performed=False,
                action="host-resettle-refused",
                detail=f"signal {finding.signal!r} is not in the host-resettle whitelist",
                evidence={"signal": finding.signal},
            )
        # Self-gate on the Option-B install (decoupled from ``available``, which
        # now only reflects kubectl reachability). If the host-root trigger is
        # not wired on this host, do NOT write a request and do NOT raise — return
        # a no-op so the gate re-diagnoses and, if the host-root fault persists,
        # resolves to UNSAFE (still fail-closed for the unfixable class).
        if not self._host_root_installed():
            return HealAttempt(
                performed=False,
                action="none",
                detail="host-root re-settle not installed on this host",
                evidence={"signal": finding.signal},
            )
        request_id = f"{finding.signal}-{uuid.uuid4().hex[:12]}"
        payload = {
            "request_id": request_id,
            "signal": finding.signal,
            "detail": finding.detail,
            "evidence": finding.evidence,
            "requested_at": _now_iso(),
        }
        result_path = self._results_dir / f"{request_id}.result.json"
        try:
            self._results_dir.mkdir(parents=True, exist_ok=True)
            req_path = self._publish_request(request_id, payload)
        except (PermissionError, OSError) as exc:
            # #411 v2 — the anti-vacuity fix: NEVER write to an unwatched
            # fallback and call it "healed" (the exact bug the reviewer proved
            # live in v1). ``_host_root_installed`` passing is only a snapshot
            # (os.access at call time), not a guarantee the write itself will
            # succeed — root re-recreated the dir, a read-only remount, disk
            # pressure, .... Log the specific dir + the durable fix, then
            # RE-RAISE: a true internal error (class docstring), never a
            # "couldn't fix" HealAttempt, so evaluate() resolves UNSAFE loudly
            # and immediately instead of burning the bounded retry budget on a
            # static permission problem retries cannot resolve.
            logger.error(
                "mesh-heal watched dir %s is not writable (%s: %s) — install/"
                "verify scripts/audittrace-mesh-heal.tmpfiles.conf (systemd-"
                "tmpfiles --create) so the runner owns it durably across "
                "reboots; refusing to fabricate a false heal via an unwatched "
                "fallback (#411 v1 was rejected for exactly that)",
                self._heal_dir,
                type(exc).__name__,
                exc,
            )
            raise
        logger.info("host-resettle requested: %s -> %s", finding.signal, req_path)

        waited = 0.0
        while waited < self._resettle_timeout:
            if result_path.exists():
                result = _parse_json(result_path.read_text())
                performed = isinstance(result, dict) and bool(result.get("performed"))
                return HealAttempt(
                    performed=performed,
                    action="host-resettle",
                    detail=(
                        f"root unit re-settled for {finding.signal}"
                        if performed
                        else f"root unit did not perform for {finding.signal}"
                    ),
                    evidence={"signal": finding.signal, "result": result},
                )
            _sleep(self._resettle_poll)
            waited += self._resettle_poll

        return HealAttempt(
            performed=False,
            action="host-resettle-timeout",
            detail=(
                f"no result from the root re-settle unit within "
                f"{self._resettle_timeout:.0f}s for {finding.signal}"
            ),
            evidence={"signal": finding.signal, "request_id": request_id},
        )

    def heal(self, diagnosis: Diagnosis) -> HealAttempt:
        """One bounded action for the given diagnosis, root-cause-first.

        Escalation ordering (WS5 fix, marker WS5-HEALER-ESC-20260802): a
        HOST-ROOT signal is checked BEFORE an RBAC signal, because host-root is
        the DEEPER fault when both are present — a broken node route (the real
        #307 shape) always manifests as ``istiod-api-unreachable`` (host-root,
        root cause) together with ``istiod-not-ready`` (RBAC, symptom of the
        same break). RBAC's ``kubectl rollout restart`` cannot fix a route loss,
        so checking RBAC first would loop the bounded heal budget in a tier that
        never converges and never reach the tier that does. Only when NO
        host-root signal is present does an RBAC-tier finding get actioned
        (e.g. an istiod-only fault with no node/route breakage — still the
        least-privileged fix for that class).

        Fall-through on "not installed" (fix 2026-08-02, same marker; reviewer
        REJECT item 4): a host-root signal only gets to OWN the call when the
        host-root tier can actually act on it. If Option-B is not installed on
        this host, :meth:`_trigger_host_resettle` self-gates to a no-op
        (``action="none"``) *before* touching the privilege boundary — and in
        that specific case ``heal`` falls through to the RBAC tier as a
        best-effort fallback, so the class-level ``available`` guarantee
        ("RBAC-tier heal available on ANY reachable cluster, even before the
        units are installed") holds even when a host-root-whitelisted signal
        happens to co-occur. A host-root attempt that genuinely FIRED
        (performed, timed out, or refused as un-whitelisted) does NOT fall
        through: RBAC cannot fix a route loss, so trying it would only waste a
        bounded attempt — the gate's own retry/re-diagnose loop is the correct
        mechanism for that outcome, not an in-call fallback.

        With the gate's bounded retry loop this converges: co-present signals
        on an INSTALLED host re-settle on attempt 1; an RBAC-only fault
        re-rolls istiod on attempt 1; co-present signals on an UNINSTALLED
        host fall through to that same RBAC re-roll on attempt 1 instead of a
        silent no-op.
        """
        host_root_finding = next(
            (f for f in diagnosis.findings if f.signal in HOST_RESETTLE_WHITELIST),
            None,
        )
        if host_root_finding is not None:
            attempt = self._trigger_host_resettle(host_root_finding)
            if attempt.action != "none":
                # A genuine host-root attempt fired (performed, timed out, or
                # refused) — host-root owns this call. RBAC cannot fix a route
                # loss, so no in-call fallback; the gate's bounded retry loop
                # re-diagnoses and decides.
                return attempt
            # action == "none": Option-B isn't installed here. Fall through to
            # the RBAC tier so a co-occurring RBAC-fixable signal still gets a
            # best-effort remediation attempt instead of a silent no-op.
            for finding in diagnosis.findings:
                if finding.signal in RBAC_RESTART_SIGNALS:
                    return self._restart_istiod(finding)
            # No RBAC-tier signal to fall back to either: this is the
            # original host-root-only, uninstalled no-op — still fails closed.
            return attempt
        for finding in diagnosis.findings:
            if finding.signal in RBAC_RESTART_SIGNALS:
                return self._restart_istiod(finding)
        return HealAttempt(
            performed=False,
            action="none",
            detail=(
                "no privileged-healer action maps to signals: "
                f"{', '.join(diagnosis.signals) or 'none'}"
            ),
            evidence={"signals": diagnosis.signals},
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
