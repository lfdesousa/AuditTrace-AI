#!/usr/bin/env python3
"""Deterministic ClusterIP-path resilience check (SPEC #443 v2, the #307 family).

This is the **neuterable guard** for SPEC #443 v2 ("Mesh survives a
wifi<->wired network switch, the correct way"). It answers one falsifiable
question, exit-coded so it can be driven by an operator, the NetworkManager
dispatcher hook (``scripts/network/k3s_clusterip_dispatcher.py``), or CI:
**is the pod -> ClusterIP path that a wifi<->wired switch actually breaks
currently working, right now, regardless of which physical NIC owns the
default route?**

## Why v2 (the checks changed shape, 2026-08-09)

The v1 guard asserted an *implementation detail* — that the node's routing
table and ``kubectl``-reported ``InternalIP`` were pinned to a dummy ``k3s0``
interface. That assumption was **wrong**: the v1 fix (pin k3s ``node-ip`` /
``flannel-iface`` to a ``k3s0`` /32) was built, unit-tested, and independently
reviewed PASS, then **broke the mesh on live host-apply** — pinning node-ip
does not control kube-proxy/service-CIDR routing, and giving the node an
unreachable /32 as its primary IP broke istiod<->kube-API outright. See
SPEC #443 v2 RE-SPEC s1 and ``feedback_infra_fix_needs_live_dry_run``. The
v1 approach was reverted (2026-08-09) and MUST NOT be repeated: **no
node-ip/flannel-iface pin, no dummy interface, no steady-state host change.**

v2's guard instead tests the *real failing path directly*: a live in-cluster
TCP probe from the pod network to the kube-API ClusterIP
(``10.43.0.1:443``), the exact hop that goes stale when a wifi<->wired
switch changes the host's default route and kube-proxy/flannel (node IP
auto-detected at start) don't re-derive their programming. "Healthy" now
means "the path that breaks on a switch works" — independent of which NIC
is currently active, independent of any node-ip/interface assumption.

Three checks, each independently falsifiable:

    (a) **pod-clusterip-reachable** — an ephemeral in-cluster pod
        (``kubectl run --rm``) opens a real TCP connection to the kube-API
        ClusterIP (default ``10.43.0.1:443``). This is the literal failing
        hop from SPEC #443 v2 s2: pod network -> kube-proxy/flannel ->
        kube-API. A failed connect here IS the #443 failure signature,
        regardless of which interface currently holds the default route.
    (b) **istiod-reachable-no-route** — istiod is reachable AND its recent
        log window carries zero ``no route to host`` / ``Unauthenticated`` /
        ``tokenreviews`` lines (the #307 smoking-gun signature, see
        ``scripts/deploy/mesh.py``). Kept unchanged from v1 — this check was
        never approach-specific.
    (c) **front-door-health** — the front door's ``GET /health`` reports
        ``status=ok`` (external ingress plane, SPEC #443 s7: "must validate
        both planes"). Kept unchanged from v1.

Every external effect (subprocess, HTTP) funnels through a single seam
(``_run`` / ``_http_get``) so tests never touch a real cluster or socket,
mirroring the fail-closed style of ``scripts/deploy/mesh.py``: a probe that
cannot get a clean answer is a FAIL, never a false PASS.

Usage::

    python scripts/network/verify_clusterip_resilience.py
    python scripts/network/verify_clusterip_resilience.py --json
    python scripts/network/verify_clusterip_resilience.py \\
      --front-door https://audittrace.local --insecure

Exit code ``0`` when all three checks PASS (the pod->ClusterIP path is
genuinely working); ``1`` when any check FAILS. This asymmetry is what the
dispatcher (``k3s_clusterip_dispatcher.py``) relies on: it makes its
restart-or-noop decision from this exit code ALONE, never from parsing this
script's stdout, so a future change to this script's print format can never
silently break the dispatcher's safety decision. See
``tests/test_verify_clusterip_resilience.py`` for the non-vacuous proof (RED
on broken evidence, GREEN on healthy evidence, per check).

Front-door resolution is intentionally self-contained (no cross-package
import of ``scripts.deploy.frontdoor.resolve_front_door``): this script is
invoked directly by filename (``python scripts/network/verify_clusterip_
resilience.py``, and by the NetworkManager dispatcher hook), not via
``python -m`` from the repo root, so a package-qualified import would only
resolve when the caller's ``sys.path`` happens to include the repo root.
The precedence mirrors ``frontdoor.py`` exactly (explicit flag > env var >
hardcoded default); only the hardcoded default differs, because this is a
host/laptop-network tool whose reference environment IS the laptop (the
portability invariant's own "laptop is the reference profile" rule) rather
than the general deploy tooling's cloud-first default.

Filename note (deviation from SPEC #443 s5's literal
``verify-clusterip-resilience.py``): this module uses an underscore, not a
hyphen, so it (a) is a syntactically valid module name — ``ruff``'s N999
pep8-naming rule, enforced by the repo's pre-commit gate on every staged
``.py`` file including ``scripts/**``, rejects hyphenated module names
outright — and (b) matches every other coverage-gated ``scripts/*`` package
in this repo (``scripts/deploy``, ``scripts/hooks``, ``scripts/release``,
``scripts/curator``), all of which are plain Python identifiers so they can
be both ``import``ed normally in tests and run via ``python -m``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import ssl
import subprocess  # noqa: S404 - read-only ip/kubectl reads; fixed argv, no shell
import sys
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError

logger = logging.getLogger(__name__)

# ── fixed vocabulary (the gate's caller branches on these) ────────────────────
PASS = "pass"
FAIL = "fail"

# ── defaults (SPEC #443 v2 s4) ──────────────────────────────────────────────
DEFAULT_PROBE_IP = "10.43.0.1"  # kube-API ClusterIP (SPEC #443 s2)
DEFAULT_PROBE_PORT = 443
DEFAULT_PROBE_IMAGE = "busybox:1.36"
DEFAULT_PROBE_POD_TIMEOUT = "20s"
DEFAULT_ISTIOD_NAMESPACE = "istio-system"
DEFAULT_ISTIOD_LABEL = "app=istiod"
# SPEC #443 acceptance criterion 1: "0 no route to host in the 2 min after the
# switch" — narrower than scripts/deploy/mesh.py's 3m deploy-time-gate window
# (a different context: post-switch validation, not post-deploy cold start).
DEFAULT_LOG_WINDOW = "2m"
DEFAULT_PROBE_TIMEOUT = 30
DEFAULT_HTTP_TIMEOUT = 10

FRONT_DOOR_ENV_VAR = "AUDITTRACE_FRONT_DOOR"
# Laptop reference default (portability invariant) — this tool's own host IS
# the laptop being switched between wifi and wired; a cloud target sets
# AUDITTRACE_FRONT_DOOR explicitly (same precedence as scripts/deploy/frontdoor.py).
DEFAULT_FRONT_DOOR = "https://audittrace.local"

# The #307 smoking-gun signature (identical set to scripts/deploy/mesh.py's
# SMOKING_GUN_PATTERNS): istiod cannot reach the kube-API ClusterIP to
# authenticate/sign a CSR.
NO_ROUTE_PATTERNS: tuple[str, ...] = (
    "no route to host",
    "Unauthenticated",
    "tokenreviews",
)

# The pod->ClusterIP probe's two possible outcomes, printed by the ephemeral
# probe pod's shell one-liner (see gather_pod_clusterip_probe). Distinct,
# greppable sentinels — never inferred from returncode alone, because
# `kubectl run --rm` can exit non-zero for reasons unrelated to the probe
# itself (image pull, scheduling) and those must ALSO fail closed.
CLUSTERIP_PROBE_OK_MARKER = "AUDITTRACE_CLUSTERIP_TCP_OK"
CLUSTERIP_PROBE_FAIL_MARKER = "AUDITTRACE_CLUSTERIP_TCP_FAIL"


# ── external-effect seams (monkeypatched in tests; nothing else opens a
#    subprocess or a socket) ────────────────────────────────────────────────


def _run(
    cmd: list[str], *, timeout: int = DEFAULT_PROBE_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    """Run a read-only ``ip``/``kubectl`` command, bounded. Sole subprocess seam."""
    logger.info("exec: %s", " ".join(cmd))
    return subprocess.run(  # noqa: S603 - fixed argv lists, no shell
        cmd, capture_output=True, text=True, check=False, timeout=timeout
    )


def _http_get(
    url: str, *, insecure: bool, timeout: int = DEFAULT_HTTP_TIMEOUT
) -> tuple[int, bytes]:
    """Perform a GET; return ``(status, body)``. Sole network-egress seam.

    A transport failure (unreachable host, TLS error, timeout) returns status
    ``0`` so a probe reads it as a hard FAIL rather than an uncaught exception
    — the same fail-closed contract as ``scripts/deploy/verify.py``'s
    ``_http_request``.
    """
    context: ssl.SSLContext | None = None
    if insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(  # noqa: S310 - https front door
            request, timeout=timeout, context=context
        ) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()
    except OSError:
        # URLError (an OSError), a bare TimeoutError/socket.timeout, or any
        # ConnectionError — one sentinel for the whole transport-failure class.
        return 0, b""


def _now_iso() -> str:
    """UTC ISO-8601 timestamp — EVIDENCE ONLY, never read back into a decision."""
    return datetime.now(UTC).isoformat()


def _parse_json_bytes(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


# ── records ─────────────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    """One diagnosed check, with the evidence it stands on."""

    name: str
    status: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass
class ResilienceReport:
    """The three-check verdict, SPEC #443 v2 s4. ``healthy`` drives the exit code."""

    checks: list[CheckResult]
    generated_at: str

    @property
    def healthy(self) -> bool:
        return all(check.ok for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "healthy": self.healthy,
            "checks": [check.as_dict() for check in self.checks],
        }


# ── pure parsers (no I/O — trivially unit-testable) ────────────────────────────


def parse_clusterip_probe_result(output: str) -> bool | None:
    """Extract the probe verdict from the ephemeral pod's captured stdout.

    Returns ``True`` (reached), ``False`` (explicitly could not reach), or
    ``None`` if neither sentinel is present (the pod never ran its command —
    e.g. ``ImagePullBackOff``, scheduling timeout — an inconclusive result
    that the caller MUST treat as a FAIL, never a silent PASS).
    """
    if CLUSTERIP_PROBE_OK_MARKER in output:
        return True
    if CLUSTERIP_PROBE_FAIL_MARKER in output:
        return False
    return None


def count_no_route_lines(
    logs: str, *, patterns: tuple[str, ...] = NO_ROUTE_PATTERNS
) -> int:
    """Count LOG LINES (not raw substring hits) carrying a #307 smoking-gun pattern.

    Counting by line (not by pattern occurrence) avoids double-counting a
    single log line that happens to carry two patterns (e.g. both
    "no route to host" and "Unauthenticated" on one line).
    """
    lowered_patterns = tuple(p.lower() for p in patterns)
    hits = 0
    for line in logs.splitlines():
        lowered_line = line.lower()
        if any(pattern in lowered_line for pattern in lowered_patterns):
            hits += 1
    return hits


# ── decision functions (pure — evidence in, CheckResult out) ──────────────────


def check_pod_clusterip_reachable(
    returncode: int,
    output: str,
    probe_ip: str = DEFAULT_PROBE_IP,
    probe_port: int = DEFAULT_PROBE_PORT,
) -> CheckResult:
    """SPEC #443 v2 s4(a): the literal failing hop, tested for real.

    ``returncode`` is ``kubectl run``'s own exit code (nonzero can mean the
    probe pod never got to run its command at all — scheduling/image-pull
    failure — a DIFFERENT failure mode from a clean TCP-connect-refused, but
    one that must ALSO fail this check, per the fail-closed contract).
    """
    result = parse_clusterip_probe_result(output)
    if result is True and returncode == 0:
        return CheckResult(
            "pod-clusterip-reachable",
            PASS,
            f"in-cluster TCP probe reached {probe_ip}:{probe_port} "
            "(the path a wifi<->wired switch breaks)",
            {"returncode": returncode},
        )
    if result is False:
        return CheckResult(
            "pod-clusterip-reachable",
            FAIL,
            f"in-cluster TCP probe could NOT reach {probe_ip}:{probe_port} "
            "(the #443/#307 failure signature — pod network -> ClusterIP path "
            "is broken, independent of which NIC currently owns the default "
            "route)",
            {"returncode": returncode, "raw": output},
        )
    return CheckResult(
        "pod-clusterip-reachable",
        FAIL,
        "in-cluster TCP probe produced no parseable result "
        f"(`kubectl run` exited {returncode}; fail-closed — never assume "
        "reachability on missing evidence)",
        {"returncode": returncode, "raw": output},
    )


def check_istiod_no_route(
    istiod_reachable: bool, logs: str | None, window: str = DEFAULT_LOG_WINDOW
) -> CheckResult:
    """Acceptance criterion 1: istiod reachable AND 0 #307 smoking-gun lines."""
    if not istiod_reachable:
        return CheckResult(
            "istiod-reachable-no-route",
            FAIL,
            "istiod is not reachable (kubectl probe failed or no Running pod)",
            {"reachable": False},
        )
    if logs is None:
        return CheckResult(
            "istiod-reachable-no-route",
            FAIL,
            "istiod is reachable but its log window could not be read "
            "(fail-closed — never assume 0 hits on missing evidence)",
            {"reachable": True, "logs": None},
        )
    hits = count_no_route_lines(logs)
    if hits > 0:
        return CheckResult(
            "istiod-reachable-no-route",
            FAIL,
            f"{hits} no-route-to-host/Unauthenticated/tokenreviews line(s) "
            f"in the last {window} (the #307 smoking-gun signature)",
            {"hits": hits, "window": window},
        )
    return CheckResult(
        "istiod-reachable-no-route",
        PASS,
        f"istiod reachable, 0 smoking-gun lines in the last {window}",
        {"hits": 0, "window": window},
    )


def check_front_door(status_code: int, body: bytes) -> CheckResult:
    """Acceptance criterion 3: the front door must still serve `/health` ok.

    SPEC #443 s7: "front-door ingress is on the physical IP ... only the
    cluster-internal path is at risk. Must validate both planes." This check
    is the ingress-plane half; the other two cover the internal plane.
    """
    if status_code != 200:
        return CheckResult(
            "front-door-health",
            FAIL,
            f"GET /health returned HTTP {status_code} (expected 200)",
            {"status_code": status_code},
        )
    parsed = _parse_json_bytes(body)
    if not isinstance(parsed, dict) or parsed.get("status") != "ok":
        return CheckResult(
            "front-door-health",
            FAIL,
            f"/health body did not report status=ok: {parsed!r}",
            {"body": parsed},
        )
    return CheckResult(
        "front-door-health",
        PASS,
        "front-door /health reports status=ok",
        {"body": parsed},
    )


# ── evidence gathering (the only I/O in this module) ───────────────────────────


def gather_pod_clusterip_probe(
    probe_ip: str = DEFAULT_PROBE_IP,
    probe_port: int = DEFAULT_PROBE_PORT,
    image: str = DEFAULT_PROBE_IMAGE,
    pod_timeout: str = DEFAULT_PROBE_POD_TIMEOUT,
) -> tuple[int, str]:
    """Spin an ephemeral in-cluster pod that TCP-connects to the ClusterIP.

    ``kubectl run --rm -i --restart=Never`` schedules a throwaway pod, runs
    one shell one-liner (``nc -z`` — a plain TCP connect, no TLS/HTTP needed
    to prove the hop is open), prints one of the two sentinel markers, and is
    deleted on exit (``--rm``). This is a REAL round trip through the same
    pod-network -> kube-proxy/flannel -> kube-API path that goes stale on a
    wifi<->wired switch (SPEC #443 v2 s2/s4) — not an assertion about routing
    table contents, so it cannot be fooled by an implementation that "looks"
    fixed but isn't (the v1 lesson).
    """
    pod_name = f"audittrace-clusterip-probe-{uuid.uuid4().hex[:8]}"
    shell_probe = (
        f"nc -z -w 5 {probe_ip} {probe_port} "
        f"&& echo {CLUSTERIP_PROBE_OK_MARKER} "
        f"|| echo {CLUSTERIP_PROBE_FAIL_MARKER}"
    )
    proc = _run(
        [
            "kubectl",
            "run",
            pod_name,
            "--rm",
            "-i",
            "--restart=Never",
            "--image",
            image,
            f"--pod-running-timeout={pod_timeout}",
            "--command",
            "--",
            "sh",
            "-c",
            shell_probe,
        ]
    )
    return proc.returncode, proc.stdout


def gather_istiod_probe(
    namespace: str = DEFAULT_ISTIOD_NAMESPACE,
    label: str = DEFAULT_ISTIOD_LABEL,
    window: str = DEFAULT_LOG_WINDOW,
) -> tuple[bool, str | None]:
    """Returns ``(reachable, logs)``. ``logs`` is ``None`` only if unreachable-adjacent."""
    proc = _run(
        [
            "kubectl",
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            label,
            "-o",
            "jsonpath={.items[0].status.phase}",
        ]
    )
    reachable = proc.returncode == 0 and proc.stdout.strip() == "Running"
    if not reachable:
        return False, None
    logs_proc = _run(
        ["kubectl", "logs", "-n", namespace, "-l", label, f"--since={window}"]
    )
    logs = logs_proc.stdout if logs_proc.returncode == 0 else None
    return True, logs


def gather_front_door(front_door: str, *, insecure: bool) -> tuple[int, bytes]:
    return _http_get(f"{front_door}/health", insecure=insecure)


def run_all(
    *,
    front_door: str,
    insecure: bool,
    probe_ip: str = DEFAULT_PROBE_IP,
    probe_port: int = DEFAULT_PROBE_PORT,
    probe_image: str = DEFAULT_PROBE_IMAGE,
    log_window: str = DEFAULT_LOG_WINDOW,
) -> ResilienceReport:
    """Gather all evidence, run the three checks, return the structured report."""
    probe_rc, probe_output = gather_pod_clusterip_probe(
        probe_ip, probe_port, probe_image
    )
    istiod_reachable, istiod_logs = gather_istiod_probe(window=log_window)
    fd_status, fd_body = gather_front_door(front_door, insecure=insecure)

    checks = [
        check_pod_clusterip_reachable(probe_rc, probe_output, probe_ip, probe_port),
        check_istiod_no_route(istiod_reachable, istiod_logs, log_window),
        check_front_door(fd_status, fd_body),
    ]
    return ResilienceReport(checks=checks, generated_at=_now_iso())


# ── CLI ─────────────────────────────────────────────────────────────────────


def _resolve_front_door(explicit: str | None) -> str:
    """Explicit flag > ``AUDITTRACE_FRONT_DOOR`` env (read fresh) > laptop default.

    Same precedence chain as ``scripts/deploy/frontdoor.resolve_front_door``;
    see the module docstring for why this script keeps its own copy instead of
    importing it.
    """
    if explicit:
        return explicit
    raw = os.environ.get(FRONT_DOOR_ENV_VAR, "").strip()
    return raw or DEFAULT_FRONT_DOOR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "SPEC #443 v2 — verify the pod->ClusterIP path that a wifi<->wired "
            "switch breaks is genuinely reachable, right now."
        )
    )
    parser.add_argument(
        "--front-door",
        default=None,
        help=f"Front-door base URL (default: ${FRONT_DOOR_ENV_VAR} or {DEFAULT_FRONT_DOOR})",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS verification (the laptop front door's self-signed cert)",
    )
    parser.add_argument("--probe-ip", default=DEFAULT_PROBE_IP)
    parser.add_argument("--probe-port", type=int, default=DEFAULT_PROBE_PORT)
    parser.add_argument("--probe-image", default=DEFAULT_PROBE_IMAGE)
    parser.add_argument("--log-window", default=DEFAULT_LOG_WINDOW)
    parser.add_argument(
        "--json", action="store_true", help="Also print the full report as JSON"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    front_door = _resolve_front_door(args.front_door)

    report = run_all(
        front_door=front_door,
        insecure=args.insecure,
        probe_ip=args.probe_ip,
        probe_port=args.probe_port,
        probe_image=args.probe_image,
        log_window=args.log_window,
    )

    for check in report.checks:
        marker = "PASS" if check.ok else "FAIL"
        print(f"[{marker}] {check.name}: {check.detail}")

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))

    if report.healthy:
        print("RESULT: healthy (pod->ClusterIP path is genuinely reachable)")
    else:
        print("RESULT: UNHEALTHY (pod->ClusterIP path is NOT reliably reachable)")

    return 0 if report.healthy else 1


if __name__ == "__main__":
    sys.exit(main())
