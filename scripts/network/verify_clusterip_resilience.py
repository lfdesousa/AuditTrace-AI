#!/usr/bin/env python3
"""Deterministic ClusterIP-path resilience check (SPEC #443, the #307 family).

This is the **neuterable guard** for SPEC #443 ("Mesh survives a wifi<->wired
network switch"). It answers one falsifiable question, exit-coded so it can be
driven by an operator, a NetworkManager dispatcher hook
(``NN-k3s-clusterip-guard``), or CI: **is the pod -> ClusterIP path currently
bound to the stable ``k3s0`` dummy interface, or has it drifted back onto
whichever physical NIC owns the default route (the pre-fix, #307-class
failure mode)?**

Filename note (deviation from SPEC #443 s5's literal
``verify-clusterip-resilience.py``): this module uses an underscore, not a
hyphen, so it (a) is a syntactically valid module name — ``ruff``'s N999
pep8-naming rule, enforced by the repo's pre-commit gate on every staged
``.py`` file including ``scripts/**``, rejects hyphenated module names outright
— and (b) matches every other coverage-gated ``scripts/*`` package in this
repo (``scripts/deploy``, ``scripts/hooks``, ``scripts/release``,
``scripts/curator``), all of which are plain Python identifiers so they can be
both ``import``ed normally in tests and run via ``python -m``. The deliverable
is otherwise unchanged: same path prefix (``scripts/network/``), same CLI
contract, same neuterable-guard role.

Root cause this guards against (SPEC #443 s2, evidenced 2026-08-09): with no
``/etc/rancher/k3s/config.yaml``, k3s auto-detects its node IP from the
default-route interface. ``ip route get 10.43.0.1`` (the kube-API ClusterIP)
then egresses whichever NIC currently holds the default route. A wifi<->wired
switch changes the default route -> the ClusterIP path breaks -> istiod loses
its watch on the kube-API -> every sidecar goes ``Unauthenticated`` mesh-wide.
The fix (SPEC #443 s3) pins the node IP + flannel interface to a persistent
``k3s0`` dummy interface with a fixed ``/32`` outside the pod (10.42.0.0/16),
service (10.43.0.0/16), and LAN (192.168.1.0/24) ranges, so the ClusterIP path
no longer depends on which physical NIC is active.

Four checks (SPEC #443 s5), each independently falsifiable:

    (a) ``ip route get <service-cidr-probe>`` egresses the pinned dummy
        interface (default: ``k3s0``), not the physical NIC/default gateway.
    (b) the node's ``InternalIP`` (via kubectl) equals the pinned IP
        (default: ``10.10.10.1``).
    (c) istiod is reachable AND its recent log window carries zero
        ``no route to host`` / ``Unauthenticated`` / ``tokenreviews`` lines
        (the #307 smoking-gun signature, see ``scripts/deploy/mesh.py``).
    (d) the front door's ``GET /health`` reports ``status=ok`` (external
        ingress plane, SPEC #443 s7: "must validate both planes").

Every external effect (subprocess, HTTP) funnels through a single seam
(``_run`` / ``_http_get``) so tests never touch a real cluster or socket,
mirroring the fail-closed style of ``scripts/deploy/mesh.py``: a probe that
cannot get a clean answer is a FAIL, never a false PASS.

Usage::

    python scripts/network/verify_clusterip_resilience.py
    python scripts/network/verify_clusterip_resilience.py --json
    python scripts/network/verify_clusterip_resilience.py \\
      --front-door https://audittrace.local --insecure

Exit code ``0`` when all four checks PASS (the fixed, k3s0-bound state);
``1`` when any check FAILS (including the pre-fix, default-route-bound
state) — this asymmetry is what SPEC #443 acceptance criterion 4 requires
and what ``tests/test_verify_clusterip_resilience.py`` proves non-vacuous.

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
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import ssl
import subprocess  # noqa: S404 - read-only ip/kubectl reads; fixed argv, no shell
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError

logger = logging.getLogger(__name__)

# ── fixed vocabulary (the gate's caller branches on these) ────────────────────
PASS = "pass"
FAIL = "fail"

# ── defaults (SPEC #443 s3 / s5) ───────────────────────────────────────────────
DEFAULT_SERVICE_CIDR_PROBE = "10.43.0.1"  # kube-API ClusterIP (SPEC #443 s2)
DEFAULT_DUMMY_IFACE = "k3s0"
DEFAULT_PINNED_NODE_IP = "10.10.10.1"
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
    """The four-check verdict, SPEC #443 s5. ``healthy`` is the exit-code driver."""

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


def parse_route_get_iface(route_output: str) -> str | None:
    """Extract the egress interface from ``ip route get <ip>`` stdout.

    Pre-fix / RED (default-route-bound)::

        10.43.0.1 via 192.168.1.1 dev enxa0cec8afb44d src 192.168.1.231 uid 1000

    Fixed / GREEN (k3s0-bound)::

        10.43.0.1 dev k3s0 src 10.10.10.1 uid 1000

    Returns ``None`` if no ``dev <iface>`` token is present (unparseable ->
    the caller treats this as a FAIL, never a silent PASS).
    """
    match = re.search(r"\bdev\s+(\S+)", route_output)
    return match.group(1) if match else None


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


def check_route_via_iface(
    route_output: str, expected_iface: str = DEFAULT_DUMMY_IFACE
) -> CheckResult:
    """Acceptance criterion 1 / 4: the ClusterIP path must egress ``expected_iface``."""
    iface = parse_route_get_iface(route_output)
    if iface is None:
        return CheckResult(
            "route-via-dummy-iface",
            FAIL,
            "could not parse an egress interface from `ip route get` output "
            "(fail-closed)",
            {"raw": route_output},
        )
    if iface != expected_iface:
        return CheckResult(
            "route-via-dummy-iface",
            FAIL,
            f"ClusterIP path egresses {iface!r}, not {expected_iface!r} "
            "(default-route-bound — the pre-fix #307-class state)",
            {"iface": iface, "expected": expected_iface, "raw": route_output},
        )
    return CheckResult(
        "route-via-dummy-iface",
        PASS,
        f"ClusterIP path egresses {expected_iface!r}",
        {"iface": iface},
    )


def check_node_internal_ip(
    node_ip: str | None, expected_ip: str = DEFAULT_PINNED_NODE_IP
) -> CheckResult:
    """Acceptance criterion 2: node InternalIP must equal the pinned dummy IP."""
    if not node_ip:
        return CheckResult(
            "node-internal-ip-pinned",
            FAIL,
            "could not read the node's InternalIP via kubectl (fail-closed)",
            {},
        )
    if node_ip != expected_ip:
        return CheckResult(
            "node-internal-ip-pinned",
            FAIL,
            f"node InternalIP={node_ip!r}, expected {expected_ip!r} "
            "(was the physical NIC's IP, which changes on a wifi<->wired switch)",
            {"internal_ip": node_ip, "expected": expected_ip},
        )
    return CheckResult(
        "node-internal-ip-pinned",
        PASS,
        f"node InternalIP == {expected_ip!r}",
        {"internal_ip": node_ip},
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
    cluster-internal node IP moves to k3s0. Must validate both planes." This
    check is the ingress-plane half; the other three cover the internal plane.
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


def gather_route_output(service_cidr_probe: str = DEFAULT_SERVICE_CIDR_PROBE) -> str:
    proc = _run(["ip", "route", "get", service_cidr_probe])
    return proc.stdout if proc.returncode == 0 else ""


def gather_node_internal_ip() -> str | None:
    proc = _run(
        [
            "kubectl",
            "get",
            "node",
            "-o",
            "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}",
        ]
    )
    ip = proc.stdout.strip()
    return ip if proc.returncode == 0 and ip else None


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
    expected_iface: str = DEFAULT_DUMMY_IFACE,
    expected_node_ip: str = DEFAULT_PINNED_NODE_IP,
    service_cidr_probe: str = DEFAULT_SERVICE_CIDR_PROBE,
    log_window: str = DEFAULT_LOG_WINDOW,
) -> ResilienceReport:
    """Gather all evidence, run the four checks, return the structured report."""
    route_output = gather_route_output(service_cidr_probe)
    node_ip = gather_node_internal_ip()
    istiod_reachable, istiod_logs = gather_istiod_probe(window=log_window)
    fd_status, fd_body = gather_front_door(front_door, insecure=insecure)

    checks = [
        check_route_via_iface(route_output, expected_iface),
        check_node_internal_ip(node_ip, expected_node_ip),
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
            "SPEC #443 — verify the pod->ClusterIP path is bound to the "
            "pinned k3s0 dummy interface, not the physical NIC/default route."
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
    parser.add_argument("--expected-iface", default=DEFAULT_DUMMY_IFACE)
    parser.add_argument("--expected-node-ip", default=DEFAULT_PINNED_NODE_IP)
    parser.add_argument("--service-cidr-probe", default=DEFAULT_SERVICE_CIDR_PROBE)
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
        expected_iface=args.expected_iface,
        expected_node_ip=args.expected_node_ip,
        service_cidr_probe=args.service_cidr_probe,
        log_window=args.log_window,
    )

    for check in report.checks:
        marker = "PASS" if check.ok else "FAIL"
        print(f"[{marker}] {check.name}: {check.detail}")

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))

    if report.healthy:
        print("RESULT: healthy (ClusterIP path is k3s0-bound)")
    else:
        print("RESULT: UNHEALTHY (ClusterIP path is NOT reliably k3s0-bound)")

    return 0 if report.healthy else 1


if __name__ == "__main__":
    sys.exit(main())
