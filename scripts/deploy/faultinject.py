"""Fault-injection certification runner — #384 WS6 (the self-heal Definition-of-Done).

WS1–WS5 built the machine: the deploy-time :class:`~scripts.deploy.mesh.MeshGate`
diagnoses a degraded mesh and the :class:`~scripts.deploy.mesh.PrivilegedHealer`
acts on it (an in-cluster istiod re-roll for the RBAC tier, a host-root k3s
re-settle behind the Q3 privilege boundary for the #307 class), then re-diagnoses
and returns ``HEALTHY`` / ``HEALED`` / ``UNSAFE`` fail-closed. On 2026-08-01 that
gate earned its keep live *once*. WS6 turns "healed once" into "heals every time,
on demand": it ACTIVELY INJECTS each substrate fault class the healer claims to
cover, arms the real gate + real healer, and asserts **zero-touch recovery within
a bound** — plus a **negative control** proving the gate still fails closed on an
unfixable fault (no false ``HEALED``).

This is a MAPE-K / chaos-engineering evaluation instrument (CHESS: perturbate →
observe → assert bounded recovery), with blast radius held to zero by a
single-node-laptop safety gate. It COMPOSES the frozen WS5 gate/healer unchanged
— it only OBSERVES and DRIVES them. If a heal semantic ever needs to change, that
is a WS5 bug to fix through the loop, never a WS6 edit.

**F2 is permanently reframed (2026-08-02, marker WS6-F2-TERMINATE-20260802).**
Live cert 2026-08-02 22:01 proved the healer worked PERFECTLY (journal: the
host re-settle unit fired, ``systemctl restart k3s`` succeeded twice) while F2
still FAILED — a test-harness design defect, not a healer defect. F2's fault
is a hand-inserted iptables ``INPUT`` REJECT rule, a SCAFFOLD standing in for
#307's real, kube-proxy-OWNED route loss; ``k3s restart`` genuinely rebuilds
kube-proxy's own ``KUBE-*`` chains (the real fix) but never touches a rule
this harness inserted OUTSIDE kube-proxy's ownership, so the old PASS
criterion ("the heal clears the scaffold, then the gate re-diagnoses
healthy") asserted a physically-impossible outcome. F2's PASS criterion is
now the journald-verified healer CHAIN itself (diagnosis
``istiod-api-unreachable`` → host-root tier selected → the root re-settle
unit fired → ``systemctl restart k3s`` reported success), and the HARNESS —
not the heal — removes its own scaffold once that chain is proven, then
confirms the mesh returns HEALTHY as a sanity check, not the proof. See
:func:`_verify_host_root_scaffold_recovery`. Per Luis's directive, this is
the terminal shape of F2 — no further scaffold-fidelity work.

Falsifiability is the whole point (the reviewer's mandate, §6 of the spec):

* **Non-vacuous injection** — after injecting, :meth:`certify_case` asserts the
  gate's own ``diagnose_mesh`` actually shows the expected signal, polling for
  up to a bounded settle window (:func:`_await_signal_landed`) because a fault
  is not instantaneous (WS6F2-LESSONS-20260802). If the fault never lands
  within that window (a "healthy after inject" reading), the case FAILS. An
  injection that changes nothing can never masquerade as a pass.
* **No false HEALED** — the negative control (N1) must resolve to ``UNSAFE``. A
  case whose observed outcome differs from its declared expectation FAILS, so a
  healer that reports success while the re-diagnose still finds the fault cannot
  produce a green certification.
* **Teardown-always** — restore is registered on an :class:`~contextlib.ExitStack`
  BEFORE the fault is injected and re-armed by a SIGINT/SIGTERM handler, so an
  exception mid-run, a budget timeout, or an operator abort all still restore the
  mesh. WS6 never leaves the laptop in a broken state.
* **Defence-in-depth safety gate** — the live (destructive) path refuses unless
  an explicit ``--i-understand-this-breaks-the-mesh`` flag AND ``WS6_FAULT_INJECT=1``
  AND a single-node-laptop guard (node count == 1 and the expected kube context)
  ALL hold. Absent any, the tool hard-refuses, exits non-zero, and injects nothing.

The default CLI invocation (no ``--live``) is a dry-run that lists the cases and
their expected outcomes and exits 0. CI runs ONLY the hermetic unit suite (every
inject / teardown / kubectl / iptables / journald / clock op is behind an injected
seam mocked to a fake); no destructive op ever runs in CI or unattended.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import signal
import subprocess  # noqa: S404 - fixed argv, no shell; the live path is operator-gated
import sys
import time
import types
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from scripts.deploy import mesh
from scripts.deploy.mesh import (
    HEALED,
    HEALTHY,
    ISTIO_NAMESPACE,
    ISTIOD_DEPLOYMENT,
    ISTIOD_LABEL,
    UNSAFE,
    Diagnosis,
    HealAttempt,
    MeshGateResult,
)

logger = logging.getLogger(__name__)

# ── vocabulary ────────────────────────────────────────────────────────────────

# Per-case verdicts. PASS/FAIL are the falsifiable outcomes; SKIP is a DECLARED,
# reasoned non-run (never a silent drop — the reason travels in the report).
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_SKIP = "skip"

# The heal tier a case exercises. Mirrors the two PrivilegedHealer tiers plus the
# deliberate negative control. The registry-integrity test couples each tier to
# the matching routing set in ``mesh.py`` (falsifiable — a mis-tiered case fails).
TIER_RBAC = "rbac"
TIER_HOST_ROOT = "host-root"
TIER_NEGATIVE = "negative-control"

# The root-owned systemd unit whose firing is the contemporaneous audit record for
# a host-root heal (``journalctl -u audittrace-mesh-healer.service``).
MESH_HEALER_UNIT = "audittrace-mesh-healer.service"

# Markers proving the root re-settle unit actually fired in the journal window.
# The unit's ExecStart is the resettle script, which logs the ``[mesh-resettle]``
# prefix; systemd also emits ``Started``/the unit name. Any one is sufficient
# proof the contemporaneous audit record exists.
JOURNAL_FIRED_MARKERS = ("mesh-resettle", MESH_HEALER_UNIT, "systemctl restart k3s")

# The kube-API ClusterIP whose host route #307 breaks (istiod loses "route to
# host" and can no longer sign CSRs). Single-node k3s default.
# TODO(#384 WS7 cloud-profile seam b): this 10.43.0.1 is k3s-specific — lift it
# (and the iptables-vs-EKS-security-group injection mechanism) into a cloud
# profile constant so a non-k3s target selects its own kube-API ClusterIP /
# route-break mechanism without editing F2's op.
KUBE_API_CLUSTERIP = "10.43.0.1"

# Default zero-touch recovery budget (seconds). A host-root k3s re-settle on a
# healthy laptop settles well within this; a degraded host is bounded by it.
DEFAULT_BUDGET_S = 180.0

# Bounded post-inject SETTLE window (seconds) — WS6F2-LESSONS-20260802 root
# cause #1: a fault is not instantaneous. istiod's ``istiod-api-unreachable``
# signal only appears once its own reconnect attempt fails and reaches its
# logs, which can take longer than the gap between "inject" and the very next
# diagnose call. This bounds how long :func:`_await_signal_landed` polls
# before ruling an injection vacuous — well under ``DEFAULT_BUDGET_S`` so a
# slow-to-manifest fault still leaves the heal itself most of the budget.
DEFAULT_MANIFEST_TIMEOUT_S = 60.0

# Poll cadence (seconds) while waiting for a fault to manifest.
DEFAULT_MANIFEST_POLL_INTERVAL_S = 2.0

# Safety-gate constants (defence in depth — the live path needs ALL of these).
LIVE_FLAG = "i_understand_this_breaks_the_mesh"
ENV_ARMED = "WS6_FAULT_INJECT"
ENV_EXPECTED_CONTEXT = "WS6_EXPECTED_CONTEXT"
# k3s writes ``default`` as the context name in /etc/rancher/k3s/k3s.yaml. The
# operator can override for a differently-named laptop context.
DEFAULT_EXPECTED_CONTEXT = "default"


# ── external-effect indirections (the injected seams; monkeypatched in tests) ──
#
# Every side-effecting operation funnels through one of these module-level seams so
# the whole orchestration is unit-testable with fakes and NO destructive op ever
# runs under test. This mirrors ``mesh.py`` / ``runner.py`` exactly.


def _run(cmd: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run a command, bounded and captured. Sole subprocess entry point.

    ``check=False`` — callers decide what a non-zero exit means. Fixed argv, no
    shell. The live path is operator-gated; tests monkeypatch this to a fake so
    the destructive argv is asserted but never executed.
    """
    logger.info("exec: %s", " ".join(cmd))
    return subprocess.run(  # noqa: S603 - fixed argv lists, no shell
        cmd, capture_output=True, text=True, check=False, timeout=timeout
    )


def _sleep(seconds: float) -> None:
    """Pause. Not a control-flow branch on wall-clock; mocked to 0 in tests."""
    time.sleep(seconds)


def _monotonic() -> float:
    """Monotonic clock for the recovery-budget measurement (seam for tests)."""
    return time.monotonic()


def _now_iso() -> str:
    """UTC ISO-8601 timestamp — EVIDENCE ONLY, never read back into logic."""
    return datetime.now(UTC).isoformat()


# The prefix journalctl prints for the opaque cursor token in ``--show-cursor``.
_CURSOR_PREFIX = "-- cursor: "


def _journal_cursor(unit: str) -> str:
    """Capture the journald cursor at the tail of ``unit`` BEFORE injection.

    Returns the opaque cursor token, or '' on any failure. A cursor is
    timezone-independent (unlike ``--since``, which journalctl parses in the
    host's LOCAL timezone — a real portability bug: west of UTC a UTC timestamp
    starts the window in the future and false-FAILs a genuinely-healed case).
    Scoping the step-6 read with ``--after-cursor`` guarantees we only ever see
    records the heal wrote AFTER this point, so a stale record from a previous
    run can never satisfy the proof.

    ``-n0 --show-cursor`` prints no entries, just the trailing ``-- cursor: …``
    line for the current tail. A blank/failed capture returns '' → the step-6
    proof falls back to a whole-unit read and stays fail-closed (see
    :func:`_read_journal`), never a false PASS.
    """
    try:
        proc = _run(
            ["journalctl", "-u", unit, "-n0", "--show-cursor", "--no-pager"],
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("journal cursor capture failed for %s: %s", unit, exc)
        return ""
    if proc.returncode != 0:
        return ""
    # ``--show-cursor`` emits the token on stderr as ``-- cursor: <token>``.
    for line in (proc.stderr + proc.stdout).splitlines():
        stripped = line.strip()
        if stripped.startswith(_CURSOR_PREFIX):
            return stripped[len(_CURSOR_PREFIX) :].strip()
    return ""


def _read_journal(unit: str, *, after_cursor: str) -> str:
    """Read ``unit``'s journal entries after ``after_cursor`` (fail-soft to '').

    Timezone-independent: ``--after-cursor`` bounds the window by the opaque
    cursor captured pre-injection, not a locale-parsed timestamp. A blank cursor
    (capture failed) degrades to a whole-unit read — still bounded by the caller's
    marker check, and any transport failure (journalctl missing / non-zero) yields
    '' so the host-root proof fails closed (no record → the case FAILS), never
    crashes and never a false PASS.
    """
    cmd = ["journalctl", "-u", unit, "--no-pager"]
    if after_cursor:
        cmd += ["--after-cursor", after_cursor]
    try:
        proc = _run(cmd, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("journal read failed for %s: %s", unit, exc)
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


class FaultInjectionError(RuntimeError):
    """Raised by an inject/teardown op when its kubectl/iptables command fails."""


def _exec_or_raise(cmd: list[str]) -> None:
    """Run a mutating op through :func:`_run`; raise on any failure.

    Used by the real (live) inject/teardown ops so a failed break surfaces as a
    case FAIL (via :meth:`certify_case`'s error handling) rather than a silent,
    non-vacuous "nothing happened". Restore ops that must be idempotent call
    :func:`_run` directly instead and tolerate a non-zero exit.
    """
    try:
        proc = _run(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FaultInjectionError(f"{' '.join(cmd)} failed to execute: {exc}") from exc
    if proc.returncode != 0:
        raise FaultInjectionError(
            f"{' '.join(cmd)} exited {proc.returncode}: {proc.stderr or proc.stdout}"
        )


# ── pure falsifiability helpers (no I/O; unit-tested directly) ─────────────────


def signal_landed(diagnosis: Diagnosis, expected: tuple[str, ...]) -> bool:
    """True iff the diagnosis shows at least one expected signal.

    The non-vacuous guard: an injection that produced none of its expected
    signals (e.g. a healthy diagnosis after "inject") returns False, so the case
    FAILS instead of vacuously passing. Falsifiable in both directions — an empty
    ``expected`` also returns False (a case must declare what landing looks like).
    """
    if not expected:
        return False
    present = set(diagnosis.signals)
    return any(sig in present for sig in expected)


def _await_signal_landed(
    gate: GateLike,
    expected_signals: tuple[str, ...],
    timeout_s: float,
    *,
    poll_interval_s: float = DEFAULT_MANIFEST_POLL_INTERVAL_S,
) -> Diagnosis:
    """Poll ``gate.diagnose_mesh()`` for up to ``timeout_s`` before giving up.

    WS6F2-LESSONS-20260802 root cause #1: a fault is not instantaneous. F2's
    ``istiod-api-unreachable`` signal only appears once istiod's own reconnect
    attempt fails and the failure reaches its logs — that can take longer than
    the gap between "inject" and the very next ``diagnose_mesh()`` call. A
    single post-inject snapshot can catch the fault mid-flight, read HEALTHY,
    and falsely rule a genuine injection "vacuous".

    Returns on the FIRST landed diagnosis — the common case (F1/N1, which
    manifest well inside one call) never touches the clock and behaves
    identically to a single-shot check, so this adds ~0 latency to the
    already-passing cases. Only a diagnosis that does NOT land on the first
    check starts the bounded poll loop; if the signal never lands within
    ``timeout_s`` the last (still-not-landed) diagnosis is returned and the
    caller's own :func:`signal_landed` check still fails the case — the
    guard's falsifiability is unchanged, only its patience is bounded.
    """
    diagnosis = gate.diagnose_mesh()
    if signal_landed(diagnosis, expected_signals):
        return diagnosis
    deadline = _monotonic() + timeout_s
    while not signal_landed(diagnosis, expected_signals):
        if _monotonic() >= deadline:
            break
        _sleep(poll_interval_s)
        diagnosis = gate.diagnose_mesh()
    return diagnosis


def _await_healthy(
    gate: GateLike,
    timeout_s: float,
    *,
    poll_interval_s: float = DEFAULT_MANIFEST_POLL_INTERVAL_S,
) -> Diagnosis:
    """Poll ``gate.diagnose_mesh()`` for up to ``timeout_s`` until HEALTHY.

    The mirror image of :func:`_await_signal_landed`: waiting for a signal to
    CLEAR rather than land. Used ONLY as F2's post-scaffold-clear SANITY check
    (WS6-F2-TERMINATE-20260802) — the journald chain, not this poll, is the
    load-bearing PASS criterion, so a diagnosis that stays degraded for the
    whole window is returned as-is and the caller's own ``.healthy`` check
    fails the case; this function itself never raises. Bounded to
    ``timeout_s`` (the caller passes whatever of the case ``budget_s``
    remains) instead of a fixed attempt cap, because a k3s restart + istiod
    reconnect can take materially longer than an RBAC rollout.
    """
    diagnosis = gate.diagnose_mesh()
    if diagnosis.healthy:
        return diagnosis
    deadline = _monotonic() + timeout_s
    while not diagnosis.healthy:
        if _monotonic() >= deadline:
            break
        _sleep(poll_interval_s)
        diagnosis = gate.diagnose_mesh()
    return diagnosis


def journal_shows_unit_fired(journal_text: str) -> bool:
    """True iff the journal window contains proof the root re-settle unit fired.

    Falsifiable: an empty window (no record) or unrelated log lines return False,
    so the host-root proof fails closed. Any of :data:`JOURNAL_FIRED_MARKERS`
    present is sufficient contemporaneous evidence.
    """
    if not journal_text:
        return False
    return any(marker in journal_text for marker in JOURNAL_FIRED_MARKERS)


# The healer's own action vocabulary (mesh.py's ``PrivilegedHealer.heal``, frozen)
# that means "the host-root tier OWNED this heal call" — as opposed to
# ``restart-istiod`` (the RBAC fallback) or ``none`` / ``host-resettle-refused``
# (self-gated: the tier never actually engaged the privilege boundary).
_HOST_ROOT_TIER_ACTIONS = ("host-resettle", "host-resettle-timeout")


def _host_root_tier_selected(heal_attempts: list[HealAttempt]) -> bool:
    """True iff at least one heal attempt shows the host-root tier fired.

    Part of F2's load-bearing chain (WS6-F2-TERMINATE-20260802): falsifiable in
    the direction that matters — a run where the healer fell through to RBAC
    (``restart-istiod``) or self-gated (``none`` / ``host-resettle-refused``)
    returns False, so a case that never actually engaged the host-root
    privilege boundary cannot pass on the strength of a stale, unrelated
    journald record.
    """
    return any(a.action in _HOST_ROOT_TIER_ACTIONS for a in heal_attempts)


# ── the fault-case model (data, not behaviour) ─────────────────────────────────


@dataclass(frozen=True)
class FaultCase:
    """One certifiable fault: how to break it, what breaking looks like, how it heals.

    Immutable data. The behaviour lives entirely in :func:`certify_case`, which
    reads these fields; the ``inject_fn`` / ``teardown_fn`` callables are the
    injected side-effect seams (real ops for the live path, fakes under test).
    """

    id: str
    label: str
    tier: str
    expected_signals: tuple[str, ...]
    expected_outcome: str
    inject_fn: Callable[[], None]
    teardown_fn: Callable[[], None]
    is_negative_control: bool = False
    skip_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "tier": self.tier,
            "expected_signals": list(self.expected_signals),
            "expected_outcome": self.expected_outcome,
            "is_negative_control": self.is_negative_control,
            "skip_reason": self.skip_reason,
        }


@dataclass
class CaseResult:
    """The falsifiable verdict for one certified case (mutable accumulator).

    Mutated in place through :func:`certify_case` so the restore step (run on the
    :class:`~contextlib.ExitStack` unwind) can stamp ``restored`` into the same
    object the caller receives.
    """

    case_id: str
    label: str
    tier: str
    expected_outcome: str
    verdict: str = VERDICT_FAIL
    reason: str = ""
    observed_outcome: str | None = None
    observed_signals: list[str] = field(default_factory=list)
    injected: bool = False
    restored: bool = False
    teardown_error: str | None = None
    elapsed_s: float | None = None
    pre_healthy: bool | None = None
    journal_confirmed: bool | None = None

    @property
    def passed(self) -> bool:
        return self.verdict == VERDICT_PASS

    @property
    def skipped(self) -> bool:
        return self.verdict == VERDICT_SKIP

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "label": self.label,
            "tier": self.tier,
            "expected_outcome": self.expected_outcome,
            "verdict": self.verdict,
            "reason": self.reason,
            "observed_outcome": self.observed_outcome,
            "observed_signals": self.observed_signals,
            "injected": self.injected,
            "restored": self.restored,
            "teardown_error": self.teardown_error,
            "elapsed_s": self.elapsed_s,
            "pre_healthy": self.pre_healthy,
            "journal_confirmed": self.journal_confirmed,
        }


@dataclass
class CertificationReport:
    """The WS6 evidence artefact: the falsifiable self-heal certification result."""

    results: list[CaseResult]
    budget_s: float
    generated_at: str = field(default_factory=_now_iso)

    @property
    def ran(self) -> list[CaseResult]:
        """Cases that actually ran (skips excluded)."""
        return [r for r in self.results if not r.skipped]

    @property
    def ok(self) -> bool:
        """True iff NO case FAILED and at least one case actually ran.

        Skips are allowed (a declared, reasoned non-run); a FAIL or a report where
        every case was skipped is not a certification.
        """
        if any(r.verdict == VERDICT_FAIL for r in self.results):
            return False
        return bool(self.ran)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "runner": "audittrace-ws6-faultinject",
            "ok": self.ok,
            "budget_s": self.budget_s,
            "counts": {
                "total": len(self.results),
                "passed": sum(1 for r in self.results if r.passed),
                "failed": sum(1 for r in self.results if r.verdict == VERDICT_FAIL),
                "skipped": sum(1 for r in self.results if r.skipped),
            },
            "results": [r.as_dict() for r in self.results],
            "generated_at": self.generated_at,
        }

    def human_summary(self) -> str:
        lines = [
            "AuditTrace WS6 fault-injection certification",
            "=" * 44,
            f"budget (s)   : {self.budget_s:.0f}",
            f"overall      : {'PASS' if self.ok else 'FAIL'}",
            "",
            "cases:",
        ]
        for r in self.results:
            lines.append(
                f"  [{r.case_id}] {r.verdict.upper():5} — {r.label}"
                f"  (expected {r.expected_outcome}, observed {r.observed_outcome})"
            )
            lines.append(f"        {r.reason}")
        return "\n".join(lines) + "\n"


# ── the gate seam (duck-typed so a fake drives certify with no cluster) ────────


class GateLike(Protocol):
    """The two gate methods :func:`certify_case` drives — nothing more.

    Kept narrow (Interface Segregation) so a hermetic fake needs only these two;
    the real :class:`scripts.deploy.mesh.MeshGate` satisfies it structurally.
    """

    def diagnose_mesh(self) -> Diagnosis: ...  # pragma: no cover - Protocol stub

    def evaluate(self) -> MeshGateResult: ...  # pragma: no cover - Protocol stub


# ── the live (real) fault operations — exercised only in the operator run ──────
#
# Each is a THIN wrapper over the kubectl/iptables seam. Unit tests assert the
# exact argv with a fake ``_run`` (non-destructive); the real break happens only
# on the operator-gated live path. NB the *injection* is a deliberate operator
# break (it may need host privilege, e.g. iptables); the *heal* it certifies runs
# through the unprivileged runner -> request-file -> root-unit WS5 boundary, so the
# security property under test is preserved — the heal never borrows operator root.


def inject_istiod_degraded() -> None:
    """F1 inject (RBAC): delete the istiod pod so the control plane goes unready.

    Deleting the pod drops istiod's ready endpoints (``istiod-no-endpoints`` /
    ``istiod-not-ready``) — a fault a ``kubectl rollout restart`` (the healer's
    RBAC-tier action) genuinely clears by rolling a fresh, ready replica.
    """
    _exec_or_raise(
        ["kubectl", "delete", "pod", "-n", ISTIO_NAMESPACE, "-l", ISTIOD_LABEL]
    )


def restore_istiod_degraded() -> None:
    """F1 restore: re-roll istiod and wait for Ready (idempotent)."""
    _exec_or_raise(
        [
            "kubectl",
            "rollout",
            "restart",
            f"deployment/{ISTIOD_DEPLOYMENT}",
            "-n",
            ISTIO_NAMESPACE,
        ]
    )
    _exec_or_raise(
        [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{ISTIOD_DEPLOYMENT}",
            "-n",
            ISTIO_NAMESPACE,
            "--timeout=120s",
        ]
    )


def _clusterip_reject_rule() -> list[str]:
    """The match+target that severs the kube-API ClusterIP path for the F2 fault.

    Matches on the ClusterIP's ORIGINAL (pre-DNAT) destination via ``conntrack
    --ctorigdst``, not a plain ``-d``. On single-node k3s, kube-proxy's
    ``KUBE-SERVICES`` chain DNATs ``10.43.0.1:443`` to the node's own
    hostNetwork kube-apiserver (``<nodeIP>:6443``) in ``PREROUTING`` — BEFORE
    the routing decision runs. Because the new destination is local, the
    packet is delivered via ``INPUT``, not ``FORWARD``, and by the time it
    reaches ``INPUT`` its destination has already been rewritten away from
    the ClusterIP, so a plain ``-d 10.43.0.1`` match there would never fire.
    ``conntrack --ctorigdst`` reaches back into the connection-tracking entry
    recorded when kube-proxy's DNAT first ran, to the ORIGINAL destination —
    the ClusterIP — regardless of what the packet's current addressing looks
    like. That is what makes this rule catch a POD's egress to the ClusterIP
    (istiod is a pod), while a REJECT with ``icmp-host-unreachable`` still
    reproduces the exact #307 signature: istiod's Go client reports
    ``EHOSTUNREACH`` as "no route to host" and cannot sign CSRs, which the
    diagnoser maps to ``istiod-api-unreachable`` (the host-root class the
    root re-settle heals).
    """
    return [
        "-p",
        "tcp",
        "-m",
        "conntrack",
        "--ctorigdst",
        KUBE_API_CLUSTERIP,
        "--ctorigdstport",
        "443",
        "-j",
        "REJECT",
        "--reject-with",
        "icmp-host-unreachable",
    ]


def inject_clusterip_route_broken() -> None:
    """F2 inject (host-root, THE #307 incident): break the kube-API ClusterIP route
    AND force istiod onto the broken path.

    THIS RULE IS A SCAFFOLD, not the real fault (permanent, 2026-08-02, marker
    WS6-F2-TERMINATE-20260802). The real #307 fault is a kube-proxy-OWNED
    route loss; ``systemctl restart k3s`` genuinely rebuilds kube-proxy's OWN
    ``KUBE-*`` chains (the real fix, proven live twice the same night) but
    deliberately never touches a hand-inserted ``INPUT`` rule that was never
    kube-proxy's to own — nor should it: deleting arbitrary INPUT rules on
    every restart would be a far broader, un-auditable privilege than the
    real fix needs. This scaffold's only job is to reproduce the
    ``istiod-api-unreachable`` SIGNAL so the real host-root re-settle chain
    gets to run and prove itself over journald (see
    :func:`_verify_host_root_scaffold_recovery`); the HARNESS removes this
    scaffold once that chain is proven — never the heal, and never by
    pretending ``k3s restart`` cleared it.

    FIXED 2026-08-02, attempt 2 (WS6 F2 review, live cert 2026-08-02 F2 FAIL):
    the prior version inserted the REJECT into the host's ``OUTPUT`` chain,
    which only intercepts packets a process ON THE HOST ITSELF originates.
    istiod is a POD — its egress to the ClusterIP is DNAT'd and forwarded, so
    it never traverses ``OUTPUT``. Inserting into ``INPUT`` with the
    ``conntrack --ctorigdst`` match (see :func:`_clusterip_reject_rule`)
    catches the packet AFTER kube-proxy's DNAT, keyed on its pre-NAT
    destination, so it severs istiod's (and any pod's) path to the ClusterIP
    while leaving ``kubectl``'s own ``127.0.0.1:6443`` kubeconfig path
    untouched — the gate's own probes and the healer's own ``kubectl`` calls
    keep working, only the kube-API route istiod needs is broken.

    FIXED 2026-08-02, attempt 3 (WS6F2-LESSONS-20260802 root cause #2): the
    correct chain/match ALONE was still not enough — a second live run still
    failed "vacuous". A REJECT only intercepts NEW connection attempts;
    istiod's EXISTING long-lived kube-API watch connections stay ESTABLISHED
    and keep working through them, so ``diagnose_mesh`` never sees a failure.
    The real #307 is a node-level route loss that kills live connections too,
    not just new ones. To reproduce that, insert the rule FIRST, then bounce
    istiod (delete its pod) so the FRESH replica must establish its kube-API
    connection THROUGH the now-broken route — producing a genuine, immediate
    ``istiod-api-unreachable`` ("no route to host" / cannot get certs, the
    real #307 signature) instead of a rule that only guards against traffic
    that never needs to be sent again.
    """
    _exec_or_raise(["iptables", "-I", "INPUT", *_clusterip_reject_rule()])
    _exec_or_raise(
        ["kubectl", "delete", "pod", "-n", ISTIO_NAMESPACE, "-l", ISTIOD_LABEL]
    )


def restore_clusterip_route() -> None:
    """F2 restore: remove the REJECT rule (idempotent).

    CORRECTED 2026-08-02 (marker WS6-F2-TERMINATE-20260802): earlier drafts of
    this docstring claimed "the heal's ``systemctl restart k3s`` resyncs
    kube-proxy's iptables rules and may already have removed this rule" —
    that claim was FALSE. This rule is a SCAFFOLD that lives OUTSIDE
    kube-proxy's ownership (see :func:`inject_clusterip_route_broken`); a
    real ``k3s restart`` only ever rebuilds kube-proxy's OWN ``KUBE-*``
    chains and never touches it. Clearing this rule is deliberately the
    HARNESS's job, never the heal's:
    :func:`_verify_host_root_scaffold_recovery` calls this function (via its
    ``do_restore`` seam) only AFTER the real host-root re-settle chain is
    journald-proven, and :func:`certify_case`'s ``ExitStack`` calls it again
    unconditionally on every exit path — idempotent either way: a non-zero
    delete (rule already gone) is tolerated, this is the fail-safe restore,
    not a mutation whose success we assert.

    Only the injected artefact (the iptables rule) is explicitly restored
    here. The istiod pod bounced by :func:`inject_clusterip_route_broken` is
    a normal Deployment-managed replica; whether the case heals (a fresh
    Ready istiod already exists by the time this runs) or aborts early (the
    Deployment controller replaces the deleted pod on its own regardless of
    this function), no separate pod-level restore step is needed.
    """
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _run(["iptables", "-D", "INPUT", *_clusterip_reject_rule()])


def inject_unfixable_istiod_scaled_zero() -> None:
    """N1 inject (negative control): scale istiod to 0 replicas.

    Signal ``istiod-not-ready`` IS RBAC-mapped, so the healer DOES attempt its
    least-privileged action (a rollout restart) — but a restart does not change
    the replica count, so istiod stays at 0, the re-diagnose still finds the
    fault, and the gate fails closed to ``UNSAFE``. That is the strongest form of
    negative control: it proves the gate trusts its own RE-DIAGNOSE, not the
    healer's ``performed`` flag, so a healer that "succeeds" without fixing
    anything can never yield a false ``HEALED``.
    """
    _exec_or_raise(
        [
            "kubectl",
            "scale",
            f"deployment/{ISTIOD_DEPLOYMENT}",
            "-n",
            ISTIO_NAMESPACE,
            "--replicas=0",
        ]
    )


def restore_istiod_scaled_zero() -> None:
    """N1 restore: scale istiod back to 1 and wait for Ready (idempotent)."""
    _exec_or_raise(
        [
            "kubectl",
            "scale",
            f"deployment/{ISTIOD_DEPLOYMENT}",
            "-n",
            ISTIO_NAMESPACE,
            "--replicas=1",
        ]
    )
    _exec_or_raise(
        [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{ISTIOD_DEPLOYMENT}",
            "-n",
            ISTIO_NAMESPACE,
            "--timeout=120s",
        ]
    )


def _f3_not_injectable() -> None:
    """F3 placeholder: node NotReady is not independently injectable here.

    On single-node k3s, stopping the kubelet == stopping k3s, so a node-NotReady
    fault folds into F2's k3s re-settle and cannot be injected as a distinct,
    cleanly-reversible fault. Registered as a DECLARED skip (never silently
    dropped); this body is never reached because the case carries a skip_reason.
    """
    raise NotImplementedError("F3 folds into F2 on single-node k3s (see skip_reason)")


# ── the frozen case registry (F1 + F2 + N1 mandatory core; F3 declared-skip) ───

REGISTRY: dict[str, FaultCase] = {
    "F1": FaultCase(
        id="F1",
        label="istiod degraded (RBAC re-roll) -> HEALED",
        tier=TIER_RBAC,
        expected_signals=("istiod-not-ready", "istiod-no-endpoints"),
        expected_outcome=HEALED,
        inject_fn=inject_istiod_degraded,
        teardown_fn=restore_istiod_degraded,
    ),
    "F2": FaultCase(
        id="F2",
        label="#307 kube-API ClusterIP route broken (host-root re-settle) -> HEALED",
        tier=TIER_HOST_ROOT,
        expected_signals=("istiod-api-unreachable",),
        expected_outcome=HEALED,
        inject_fn=inject_clusterip_route_broken,
        teardown_fn=restore_clusterip_route,
    ),
    "F3": FaultCase(
        id="F3",
        label="node NotReady (host-root re-settle) -> HEALED",
        tier=TIER_HOST_ROOT,
        expected_signals=("node-not-ready", "nodes-unreadable"),
        expected_outcome=HEALED,
        inject_fn=_f3_not_injectable,
        teardown_fn=_f3_not_injectable,
        skip_reason=(
            "node NotReady is not independently, reversibly injectable on "
            "single-node k3s (stopping kubelet == stopping k3s); it folds into "
            "F2's k3s re-settle"
        ),
    ),
    "N1": FaultCase(
        id="N1",
        label="negative control: unfixable istiod scaled-to-0 -> UNSAFE (no false HEALED)",
        tier=TIER_NEGATIVE,
        expected_signals=("istiod-not-ready", "istiod-no-endpoints"),
        expected_outcome=UNSAFE,
        inject_fn=inject_unfixable_istiod_scaled_zero,
        teardown_fn=restore_istiod_scaled_zero,
        is_negative_control=True,
    ),
}

# The mandatory core (spec §3). ``all`` runs these in order (F3 is a declared skip
# surfaced in the report, not part of the auto-run core).
CORE_CASE_IDS = ("F1", "F2", "N1")


def cases_for(selector: str) -> list[FaultCase]:
    """Resolve a CLI selector (``F1``/``F2``/``N1``/``F3``/``all``) to cases.

    ``all`` returns the mandatory core in a fixed order. An unknown selector
    raises :class:`KeyError` so the CLI fails loudly rather than running nothing.
    """
    if selector == "all":
        return [REGISTRY[cid] for cid in CORE_CASE_IDS]
    return [REGISTRY[selector]]


# ── fail-safe teardown: an abort must still restore ────────────────────────────


@contextlib.contextmanager
def _restore_on_signal(restore: Callable[[], None]) -> Iterator[None]:
    """Re-arm restore against SIGINT/SIGTERM for the duration of a case.

    Defence in depth over the :class:`~contextlib.ExitStack`: if the operator
    Ctrl-C's or the host sends SIGTERM mid-injection, the handler runs the restore
    and re-raises ``KeyboardInterrupt`` so the ExitStack unwinds too (restore is
    idempotent-guarded by the caller). Previous handlers are always reinstated on
    exit. Installs only when running on the main thread (``signal`` requires it);
    a non-main-thread caller relies on the ExitStack alone.
    """

    def _handler(signum: int, frame: types.FrameType | None) -> None:
        logger.error("WS6 received signal %s mid-run — restoring the mesh", signum)
        restore()
        raise KeyboardInterrupt(f"aborted by signal {signum}")

    try:
        previous = {
            signal.SIGINT: signal.signal(signal.SIGINT, _handler),
            signal.SIGTERM: signal.signal(signal.SIGTERM, _handler),
        }
    except ValueError:
        # Not the main thread → cannot install signal handlers. The ExitStack
        # still guarantees restore; yield without the extra arming.
        logger.warning("WS6 signal handlers unavailable (not main thread)")
        yield
        return
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


class _CaseAbortedError(RuntimeError):
    """Internal: a controlled per-case failure carrying its reason (never leaks)."""


# ── the certification flow (the 7-step per-case DoD) ───────────────────────────


def certify_case(
    case: FaultCase,
    gate: GateLike,
    *,
    budget_s: float = DEFAULT_BUDGET_S,
    manifest_timeout_s: float = DEFAULT_MANIFEST_TIMEOUT_S,
) -> CaseResult:
    """Certify one fault case end to end, ALWAYS restoring the mesh afterwards.

    The seven steps (spec §4), each a falsifiable assertion:

    0. assert pre-state HEALTHY (refuse to inject onto an already-broken mesh);
    1. register the restore on an ExitStack FIRST (before any break);
    2. inject the fault;
    3. assert the gate's ``diagnose_mesh`` shows an expected signal within a
       bounded post-inject SETTLE window (non-vacuous — the fault actually
       landed; see :func:`_await_signal_landed`, WS6F2-LESSONS-20260802);
    4. run ``gate.evaluate()`` (the real WS5 diagnose -> bounded heal -> re-diagnose);
    5. assert the outcome equals the declared expectation within ``budget_s``,
       with zero manual intervention — EXCEPT for a HOST-ROOT SCAFFOLD case
       (F2's shape, WS6-F2-TERMINATE-20260802), whose steps 5-6 are replaced
       wholesale by :func:`_verify_host_root_scaffold_recovery`: its scaffold
       fault lives outside the heal's own reach by design, so ``gate.evaluate``'s
       raw outcome is never the PASS criterion there — the journald-verified
       chain is;
    6. for a host-root heal, assert a journald record of the root unit firing
       (folded into step 5 above for the scaffold case, since there the chain
       IS the proof, not a follow-on check);
    7. restore in every exit path (ExitStack + signal handler).

    Returns a :class:`CaseResult`; never raises for a fault-under-test failure
    (those become ``verdict=FAIL``). A declared-skip case returns immediately
    without touching the mesh.
    """
    res = CaseResult(
        case_id=case.id,
        label=case.label,
        tier=case.tier,
        expected_outcome=case.expected_outcome,
    )

    if case.skip_reason is not None:
        res.verdict = VERDICT_SKIP
        res.reason = f"skipped: {case.skip_reason}"
        logger.warning("[%s] SKIP — %s", case.id, case.skip_reason)
        return res

    # Step 0 — refuse to inject onto an already-broken mesh (no compounding faults).
    pre = gate.diagnose_mesh()
    res.pre_healthy = pre.healthy
    if not pre.healthy:
        res.verdict = VERDICT_FAIL
        res.reason = (
            "refused: pre-state not HEALTHY "
            f"(signals: {', '.join(pre.signals) or 'unknown'}) — will not inject "
            "onto a broken mesh"
        )
        logger.error("[%s] %s", case.id, res.reason)
        return res

    # Capture the journald cursor BEFORE injecting so the step-6 host-root proof
    # reads only records the heal writes after this point (timezone-independent,
    # and stale-record-proof). Empty for non-host-root cases and harmless there.
    journal_cursor = _journal_cursor(MESH_HEALER_UNIT)

    def _do_restore() -> None:
        if res.restored:
            return
        try:
            case.teardown_fn()
            res.restored = True
            logger.info("[%s] mesh restored", case.id)
        except Exception as exc:  # noqa: BLE001 - restore is best-effort, never fatal
            res.teardown_error = str(exc)
            logger.error("[%s] restore FAILED: %s", case.id, exc)

    # Step 1 — register restore FIRST, so any later exit path still restores.
    with contextlib.ExitStack() as stack:
        stack.callback(_do_restore)
        with _restore_on_signal(_do_restore):
            try:
                _run_case_steps(
                    case,
                    gate,
                    res,
                    budget_s,
                    journal_cursor,
                    manifest_timeout_s,
                    _do_restore,
                )
                res.verdict = VERDICT_PASS
                res.reason = (
                    f"injected -> observed {res.observed_outcome} within "
                    f"{res.elapsed_s:.1f}s, zero-touch; mesh restored"
                )
                logger.info("[%s] PASS — %s", case.id, res.reason)
            except _CaseAbortedError as fail:
                res.verdict = VERDICT_FAIL
                res.reason = str(fail)
                logger.error("[%s] FAIL — %s", case.id, res.reason)
            except Exception as exc:  # noqa: BLE001 - any seam error fails the case, still restores
                res.verdict = VERDICT_FAIL
                res.reason = f"unexpected error during certification: {exc}"
                logger.error("[%s] FAIL — %s", case.id, res.reason)
    return res


def _run_case_steps(
    case: FaultCase,
    gate: GateLike,
    res: CaseResult,
    budget_s: float,
    journal_cursor: str,
    manifest_timeout_s: float,
    do_restore: Callable[[], None],
) -> None:
    """Steps 2-6 of :func:`certify_case`. Raises :class:`_CaseAbortedError` on any miss.

    Split out so :func:`certify_case` keeps a single, legible try/except around the
    whole break-observe-heal sequence (the ExitStack owns restore either way).
    """
    # Step 2 — inject the fault.
    case.inject_fn()
    res.injected = True

    # Step 3 — non-vacuous, with a bounded post-inject SETTLE window
    # (WS6F2-LESSONS-20260802 root cause #1): the fault must actually land,
    # but landing is not instantaneous, so poll for up to manifest_timeout_s
    # before ruling it vacuous. Returns on first success — a fast fault
    # (F1/N1) adds ~0 latency over the old single-shot check.
    post = _await_signal_landed(gate, case.expected_signals, manifest_timeout_s)
    res.observed_signals = post.signals
    if not signal_landed(post, case.expected_signals):
        raise _CaseAbortedError(
            "fault did not land within "
            f"{manifest_timeout_s:.0f}s settle window: expected one of "
            f"{list(case.expected_signals)} but diagnose shows "
            f"{post.signals or 'HEALTHY'} — injection was vacuous"
        )

    # Step 4 — arm the real gate + healer; measure the recovery wall-time.
    start = _monotonic()
    result = gate.evaluate()
    res.observed_outcome = result.outcome

    if case.tier == TIER_HOST_ROOT and case.expected_outcome == HEALED:
        # WS6-F2-TERMINATE-20260802: a HOST-ROOT SCAFFOLD case (F2's shape) is
        # verified by a wholly different steps 5-6 — see the function docstring
        # for why ``result.outcome`` is deliberately not read here.
        _verify_host_root_scaffold_recovery(
            gate, res, result, budget_s, start, journal_cursor, do_restore
        )
        return

    # Step 5 — bounded, zero-touch: within budget AND the declared outcome.
    res.elapsed_s = _monotonic() - start
    if res.elapsed_s > budget_s:
        raise _CaseAbortedError(
            f"recovery exceeded budget: {res.elapsed_s:.1f}s > {budget_s:.0f}s "
            f"(outcome {result.outcome})"
        )
    if result.outcome != case.expected_outcome:
        raise _CaseAbortedError(
            f"outcome {result.outcome!r} != expected {case.expected_outcome!r} "
            f"(reason: {result.reason})"
        )


def _verify_host_root_scaffold_recovery(
    gate: GateLike,
    res: CaseResult,
    result: MeshGateResult,
    budget_s: float,
    start: float,
    journal_cursor: str,
    do_restore: Callable[[], None],
) -> None:
    """F2's PASS criterion (WS6-F2-TERMINATE-20260802): the journald-verified
    host-root re-settle CHAIN, not "the scaffold cleared itself".

    Live cert 2026-08-02 proved F2's injected fault — a hand-inserted iptables
    ``INPUT`` REJECT rule — is a SCAFFOLD standing in for #307's real,
    kube-proxy-OWNED route fault (see :func:`inject_clusterip_route_broken`).
    ``systemctl restart k3s`` genuinely rebuilds kube-proxy's own ``KUBE-*``
    chains (the real #307 fix, proven live twice the same night) but NEVER
    touches a rule this harness inserted outside kube-proxy's ownership — so
    ``gate.evaluate()``'s own re-diagnose can never observe the mesh healthy
    while the scaffold still stands, and ``result.outcome`` (typically
    ``unsafe`` here) is deliberately NOT the PASS criterion for this case,
    unlike the RBAC / negative-control cases in :func:`_run_case_steps`.

    The load-bearing, falsifiable proof is instead the CHAIN itself:

    1. the fault landed as the case's expected host-root signal (already
       proven by step 3 before this function is ever called);
    2. the healer's OWN tier-routing selected host-root, not RBAC
       (:func:`_host_root_tier_selected` — reads the frozen
       ``PrivilegedHealer.heal``'s action vocabulary, never re-implements it);
    3. the root re-settle unit fired and reported ``systemctl restart k3s``
       success in its OWN journal (the pre-existing, unchanged
       :func:`journal_shows_unit_fired` check, scoped by the pre-inject
       cursor so a stale record can never satisfy the proof).

    Only once ALL THREE hold does the HARNESS remove its OWN scaffold via
    ``do_restore`` — explicitly NOT something the heal could ever do, because
    the rule was never kube-proxy's to own — and poll for HEALTHY as a SANITY
    check, not the proof. The poll runs to whatever of ``budget_s`` remains
    (not a fixed attempt cap): a k3s restart + istiod reconnect can take
    materially longer than an RBAC rollout, and a fixed-cap single-shot check
    is exactly what declared a live cert FAIL ~11s after k3s had already come
    back successfully.
    """
    if not _host_root_tier_selected(result.heal_attempts):
        raise _CaseAbortedError(
            "host-root tier was not selected for the heal attempt(s): actions="
            f"{[a.action for a in result.heal_attempts] or ['none']} — expected "
            f"one of {list(_HOST_ROOT_TIER_ACTIONS)}"
        )

    # The contemporaneous journald audit record must exist. Read only entries
    # AFTER the pre-injection cursor (timezone-independent, stale-record-proof).
    # TODO(#384 WS7 cloud-profile seam a): make this a per-case pluggable
    # ``audit_proof_fn`` on FaultCase so a cloud profile can prove the host-root
    # heal from a different contemporaneous source (e.g. CloudWatch Logs / an
    # SSM run-command record) instead of laptop journald — plug-in, not a rewrite.
    journal = _read_journal(MESH_HEALER_UNIT, after_cursor=journal_cursor)
    res.journal_confirmed = journal_shows_unit_fired(journal)
    if not res.journal_confirmed:
        raise _CaseAbortedError(
            f"no journald record of {MESH_HEALER_UNIT} firing (re-settle + "
            f"systemctl restart k3s) after cursor {journal_cursor or '(none captured)'}"
            " — host-root heal is not auditable"
        )

    # The chain is proven. The harness now clears its OWN scaffold — the heal
    # never could, by design (see the docstring above). Idempotent: safe even
    # if the ExitStack unwind also calls it again later.
    do_restore()
    if res.teardown_error:
        raise _CaseAbortedError(f"scaffold teardown failed: {res.teardown_error}")

    # Sanity check, not the proof: the mesh must actually return HEALTHY once
    # the scaffold is gone. Poll to whatever of the case budget remains (not a
    # fixed attempt cap — see the docstring above).
    remaining = max(0.0, budget_s - (_monotonic() - start))
    final = _await_healthy(gate, remaining)
    res.elapsed_s = _monotonic() - start
    if not final.healthy:
        raise _CaseAbortedError(
            f"mesh did not return HEALTHY within the {budget_s:.0f}s case budget "
            "after the harness cleared the scaffold (signals: "
            f"{', '.join(final.signals) or 'unknown'})"
        )
    res.observed_outcome = HEALTHY


def certify(
    cases: list[FaultCase],
    gate: GateLike,
    *,
    budget_s: float = DEFAULT_BUDGET_S,
    manifest_timeout_s: float = DEFAULT_MANIFEST_TIMEOUT_S,
) -> CertificationReport:
    """Certify each case in turn and collect the falsifiable report.

    Cases run sequentially — each fully restores before the next injects — so the
    blast radius is one fault at a time (CHESS: bounded chaos, restored between
    perturbations).
    """
    results = [
        certify_case(
            case, gate, budget_s=budget_s, manifest_timeout_s=manifest_timeout_s
        )
        for case in cases
    ]
    return CertificationReport(results=results, budget_s=budget_s)


# ── the safety gate (defence in depth — ALL conditions must hold to go live) ───


class LiveGuardError(RuntimeError):
    """Raised when the live path is refused; nothing is injected."""


def _node_count() -> int:
    """Number of cluster nodes (via the kubectl seam). -1 on any read failure.

    -1 (not 0/1) so an unreadable cluster can never accidentally satisfy the
    single-node guard — it fails closed.
    """
    try:
        proc = _run(
            ["kubectl", "get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"],
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return -1
    if proc.returncode != 0:
        return -1
    return len(proc.stdout.split())


def _current_context() -> str:
    """The active kube context name (via the kubectl seam). '' on failure."""
    try:
        proc = _run(["kubectl", "config", "current-context"], timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def guard_live_or_refuse(
    args: argparse.Namespace, *, environ: Mapping[str, str] | None = None
) -> None:
    """Refuse the destructive live path unless EVERY safety condition holds.

    The conjunction (all four, defence in depth):

    * the explicit ``--i-understand-this-breaks-the-mesh`` flag;
    * ``WS6_FAULT_INJECT=1`` in the environment;
    * exactly one cluster node (blast-radius = the single laptop);
    * the active kube context equals the expected laptop context.

    Any failure raises :class:`LiveGuardError` BEFORE a single ``inject_fn`` is
    called. Falsifiable: each condition is checked independently, so a test can
    remove exactly one and prove the whole path is blocked.
    """
    env = os.environ if environ is None else environ

    if not getattr(args, LIVE_FLAG, False):
        raise LiveGuardError(
            "refused: the live path requires the explicit "
            "--i-understand-this-breaks-the-mesh flag"
        )
    if env.get(ENV_ARMED) != "1":
        raise LiveGuardError(
            f"refused: the live path requires {ENV_ARMED}=1 in the environment"
        )

    # TODO(#384 WS7 cloud-profile seam c): the ``node count == 1`` blast-radius
    # guard is laptop-specific. Lift it into a pluggable profile predicate (e.g.
    # "is this the disposable ephemeral cloud-rig, not prod?") so a multi-node
    # cloud rig can arm WS6 against its OWN safety invariant — plug-in, not a
    # weakening of this control.
    nodes = _node_count()
    if nodes != 1:
        raise LiveGuardError(
            f"refused: single-node-laptop guard — node count is {nodes}, not 1 "
            "(WS6 destructive injection is laptop-only)"
        )

    expected = env.get(ENV_EXPECTED_CONTEXT, DEFAULT_EXPECTED_CONTEXT)
    context = _current_context()
    if context != expected:
        raise LiveGuardError(
            f"refused: kube context is {context!r}, expected {expected!r} "
            f"(override via {ENV_EXPECTED_CONTEXT})"
        )
    logger.warning(
        "WS6 safety gate PASSED — live fault injection ARMED on context %r "
        "(single node)",
        context,
    )


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.deploy.faultinject",
        description=(
            "WS6 fault-injection certification runner (#384). Default is a "
            "non-destructive dry-run; --live actually breaks + heals the mesh."
        ),
    )
    parser.add_argument(
        "--case",
        choices=("F1", "F2", "F3", "N1", "all"),
        default="all",
        help="which fault case(s) to certify (default: all)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="actually inject faults (destructive; requires the safety gate)",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=DEFAULT_BUDGET_S,
        help=f"zero-touch recovery budget in seconds (default: {DEFAULT_BUDGET_S:.0f})",
    )
    parser.add_argument(
        "--manifest-timeout",
        type=float,
        default=DEFAULT_MANIFEST_TIMEOUT_S,
        help=(
            "bounded post-inject settle window in seconds — how long to poll "
            "diagnose for the expected signal before ruling an injection "
            f"vacuous (default: {DEFAULT_MANIFEST_TIMEOUT_S:.0f})"
        ),
    )
    parser.add_argument(
        f"--{LIVE_FLAG.replace('_', '-')}",
        dest=LIVE_FLAG,
        action="store_true",
        help="acknowledge the live path breaks the mesh (required with --live)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=mesh_out_dir_default(),
        help="where to write the certification report artefact",
    )
    return parser


def mesh_out_dir_default() -> Path:
    """Default report directory (kept beside the deploy runner's run artefacts)."""
    return Path(__file__).resolve().parent / "faultinject-runs"


def dry_run_lines(
    cases: list[FaultCase],
    budget_s: float,
    manifest_timeout_s: float = DEFAULT_MANIFEST_TIMEOUT_S,
) -> list[str]:
    """The dry-run plan: cases + expected outcomes, mutating nothing."""
    lines = [
        "WS6 fault-injection — DRY RUN (no --live; nothing will be injected)",
        f"recovery budget: {budget_s:.0f}s",
        f"manifest settle window: {manifest_timeout_s:.0f}s",
        "",
    ]
    for case in cases:
        marker = " [SKIP]" if case.skip_reason else ""
        lines.append(f"  [{case.id}] {case.label}{marker}")
        lines.append(
            f"        tier={case.tier}  expect signal(s)="
            f"{list(case.expected_signals)}  expect outcome={case.expected_outcome}"
        )
        if case.skip_reason:
            lines.append(f"        skip: {case.skip_reason}")
    lines.append("")
    lines.append(
        "re-run with --live --i-understand-this-breaks-the-mesh (and "
        f"{ENV_ARMED}=1) to certify."
    )
    return lines


def _write_report(report: CertificationReport, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now_iso().replace(":", "").replace("-", "")
    base = out_dir / f"ws6-certification-{stamp}"
    base.with_suffix(".json").write_text(json.dumps(report.as_dict(), indent=2))
    base.with_suffix(".txt").write_text(report.human_summary())
    return base.with_suffix(".json")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = build_parser().parse_args(argv)
    cases = cases_for(args.case)

    if not args.live:
        for line in dry_run_lines(cases, args.budget, args.manifest_timeout):
            print(line)
        return 0

    try:
        guard_live_or_refuse(args)
    except LiveGuardError as exc:
        print(f"WS6 LIVE REFUSED — {exc}", file=sys.stderr)
        return 2

    gate = mesh.MeshGate(healer=mesh.PrivilegedHealer())
    report = certify(
        cases, gate, budget_s=args.budget, manifest_timeout_s=args.manifest_timeout
    )
    report_path = _write_report(report, args.out_dir)
    print(report.human_summary())
    print(f"certification report: {report_path}")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
