"""Deterministic, INDEPENDENT deploy-verify runner — Component 3 of the CD program.

The instrument the **Deploy-Verify Agent** executes to certify a deploy *from the
outside*. It re-derives cluster health itself — read-only ``kubectl`` / ``helm``
plus the public front door — and emits a PASS/FAIL verdict.

**The independence contract is the whole point.** This runner NEVER reads the
deploy runner's report and NEVER trusts its claims. The ``reads_deploy_report``
field stamped into every report is DERIVED from :meth:`evidence_sources` (the
enumerated list of what the verdict actually stands on), not self-asserted — a
``deploy-report:`` source would flip it True. A green deploy report can never move
the verdict. The deployer decides "it deployed"; this
runner decides, separately and on evidence it gathered itself, "it works". FAIL
blocks "done": *up* means complete AND working, not "helm exited 0".

Nine deploy-health probes — five CORE + four MESH-HEALTH (#384 WS2) — each
PASS/FAIL with captured evidence (failures-are-findings, every check declares the
evidence it stands on):

* **pods-ready**             — every expected app component has ≥1 Ready pod.
* **digest-matches-published** — the LIVE memory-server pod digest == the intended
  published digest (reuses :func:`scripts.deploy.registry.resolve`). The
  tracks-published-digest invariant.
* **no-drift**               — helm release STATUS == ``deployed`` AND the live
  memory-server Deployment ``strategy`` matches the surge-safe chart intent
  (``maxSurge=0`` / ``maxUnavailable=1``). Catches a WS1 regression or a stray
  live ``kubectl patch``.
* **health-version**         — front-door ``GET /health`` reports ``status=ok`` and
  ``version`` == the intended target version.
* **recall-e2e**             — a REAL front-door recall through the audit plane:
  ``POST /v1/chat/completions`` (scoped JWT, real model) that triggers a
  ``recall_*``, then ``GET /interactions/{id}/tool-calls`` asserting a recall row
  was recorded. Memory + auth alone is not E2E.

The four MESH-HEALTH probes (#384 WS2, spec §4) certify that the deploy did not
leave the Istio mesh unable to cert, secret, or route — "up" means the mesh
survived, not "helm exited 0". Each RE-DERIVES its signal from an INDEPENDENT
read-only cluster read (never the deploy runner's report, never the WS1 mesh
gate's result — the same independence contract the core probes hold):

* **istiod-api-reachable**   — istiod Deployment fully Ready, Service has ≥1
  endpoint, and ZERO #307 smoking-gun lines (``no route to host`` /
  ``Unauthenticated`` / ``tokenreviews``) in the istiod log window — a new sidecar
  can get a cert. Reuses the *pure* diagnosers in :mod:`scripts.deploy.mesh`
  (:func:`mesh.evaluate_istiod_logs` / :func:`mesh.evaluate_istiod_readiness`) over
  reads THIS runner gathered itself — shared logic, independent evidence.
* **sidecars-have-certs**    — every istio-injected pod's ``istio-proxy`` container
  is Ready and the pod reached ``Running`` (no cert-starved data plane, no pod
  wedged in Init).
* **vault-secret-rendered**  — every vault-annotated pod has a Ready ``vault-agent``
  sidecar, no container exited ``79`` (``/vault/secrets/env`` absent), and no
  CrashLoop — captures container statuses, NEVER secret values.
* **no-unhealthy-upstream**  — front-door ``/health`` == 200 ``status=ok`` AND no
  "no healthy upstream" 503 signature in the memory-server log window.

**Method discipline:** front door + read-only ``kubectl``/``helm`` only. NO
``kubectl exec``, NO root creds, NO scope-skip. Deterministic: no wall-clock
branching in control flow; timestamps live in evidence only. All network egress
funnels through the single monkeypatchable :func:`_http_request`; all URL routing
decisions use :func:`urllib.parse.urlparse` exact-match, never substring checks.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import ssl
import subprocess  # noqa: S404 - read-only kubectl/helm; fixed argv, no shell
import sys
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse

from scripts.deploy import mesh, registry
from scripts.deploy.runner import extract_digest, normalize_version

logger = logging.getLogger("audittrace.deploy.verify")

# Independence is NOT a self-attested boolean. It is proven two ways: (1) the
# report exposes the FULL enumerated list of evidence sources this runner
# consulted (``evidence_sources``) and DERIVES ``reads_deploy_report`` from it —
# a deploy-report source would flip the flag and be visible; (2) the real
# guarantee is the behavioural property test
# (``test_hostile_deploy_report_cannot_move_the_verdict``): a hostile on-disk
# deploy report claiming PASS/certified cannot move a FAIL verdict, because the
# verdict is computed only from re-derived health. The tag prefix below is the
# marker an evidence source would carry IF it were a deploy report.
DEPLOY_REPORT_SOURCE_PREFIX = "deploy-report:"
INDEPENDENCE_NOTE = (
    "verdict re-derived from the cluster API + public front door; the deploy "
    "runner's report is never read and its success claims never move this verdict"
)

DEFAULT_FRONT_DOOR = "https://audittrace.allaboutdata.eu"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "audittrace" / "tokens.json"

# Every in-cluster app component that must have a Ready pod for the deploy to be
# "up", matched by EXACT LABEL (never a pod-NAME substring — a rogue/old pod
# whose name merely contains a token, e.g. ``redis-exporter`` or a stale
# ``memory-server-<oldhash>``, must not be able to MASK a down component and
# produce a false PASS in a certifier). App components carry
# ``app.kubernetes.io/component``; Bitnami subcharts (postgresql/redis/rabbitmq)
# carry ``app.kubernetes.io/name``.
#
# The LLM tier is deliberately NOT here: on the laptop target ``externalLLM`` is
# ExternalName Services with NO backing pods (``llmStub`` is kind-only, default
# off), so a pod-readiness check would false-FAIL. LLM health is instead
# certified END-TO-END by the recall-e2e probe (a real model call). Enumerating
# the in-cluster stub topology's three llm services is deferred (follow-up F3).
COMPONENT_SELECTORS: dict[str, tuple[str, str]] = {
    "memory-server": ("app.kubernetes.io/component", "memory-server"),
    "chromadb": ("app.kubernetes.io/component", "chromadb"),
    "keycloak": ("app.kubernetes.io/component", "keycloak"),
    "minio": ("app.kubernetes.io/component", "minio"),
    "otel-collector": ("app.kubernetes.io/component", "otel-collector"),
    "postgresql": ("app.kubernetes.io/name", "postgresql"),
    "redis": ("app.kubernetes.io/name", "redis"),
    "rabbitmq": ("app.kubernetes.io/name", "rabbitmq"),
}

MEMORY_SERVER_COMPONENT = "memory-server"
MEMORY_SERVER_CONTAINER = "memory-server"
_COMPONENT_SELECTOR = f"app.kubernetes.io/component={MEMORY_SERVER_COMPONENT}"

# The surge-safe chart intent (WS1). A live Deployment that drifts from this — a
# stray ``kubectl patch`` or a reverted chart change — reintroduces the cascade
# precondition and must FAIL the no-drift probe.
EXPECTED_STRATEGY_TYPE = "RollingUpdate"
EXPECTED_MAX_SURGE = "0"
EXPECTED_MAX_UNAVAILABLE = "1"

PASS = "PASS"
FAIL = "FAIL"

# ── #384 WS2 mesh-health probe constants ─────────────────────────────────────
# The mesh probes RE-DERIVE their signal from INDEPENDENT read-only cluster reads
# gathered by THIS runner. Where the fault SIGNATURE (the #307 log patterns,
# istiod readiness semantics, the log window) is already encoded as PURE, tested
# logic in ``scripts.deploy.mesh`` (WS1), we REUSE that logic rather than
# duplicate it — but never its deploy-side STATE / result. That keeps the
# #307 signature single-sourced while the evidence stays independent.
ISTIO_PROXY_CONTAINER = "istio-proxy"
VAULT_AGENT_CONTAINER = "vault-agent"
VAULT_INJECT_ANNOTATION = "vault.hashicorp.com/agent-inject"
# entrypoint.sh trips VAULT_AGENT_REQUIRED and exits 79 when /vault/secrets/env is
# absent (mode 4, fail-closed BY DESIGN). A 79 in lastState is the smoking gun.
VAULT_FAILCLOSED_EXIT_CODE = 79
# The cert-dead-mesh data-plane signature (mode 3): Envoy has no upstream cert/route.
UNHEALTHY_UPSTREAM_SIGNATURES = ("no healthy upstream",)
# Fail-closed ALLOWLIST of mesh-Finding evidence keys this runner may surface.
# A denylist would be fail-OPEN: mesh diagnosers already attach RAW command/log
# output under SEVERAL keys (``log_tail`` in evaluate_istiod_logs; ``error`` /
# ``deployment_error`` / ``endpoints_error`` in MeshGate.diagnose_mesh), and a
# future diagnoser could add another. So we keep ONLY these known-safe derived
# keys and DROP every other key — the raw ones today AND any unknown future key —
# by default, so a newly-added raw key can never leak
# (CodeQL py/clear-text-logging-of-sensitive-data). Each allowed key is a derived
# scalar or short status label, verified against every mesh diagnoser's evidence,
# never raw log/command output:
#   match_count       int   — count of smoking-gun log lines
#   window            str   — the log-window label (e.g. "3m")
#   ready_replicas    int   — istiod ready replica count
#   desired_replicas  int   — istiod desired replica count
#   endpoint_count    int   — istiod Service ready-endpoint count
#   spec_replicas     scalar— istiod spec.replicas value (or None)
#   ready_statuses    list  — node Ready booleans ("True"/"False"), no node names
#   deployment        None  — the null sentinel (only ever None in a finding)
ALLOWED_EVIDENCE_KEYS = frozenset(
    {
        "match_count",
        "window",
        "ready_replicas",
        "desired_replicas",
        "endpoint_count",
        "spec_replicas",
        "ready_statuses",
        "deployment",
    }
)
# Pods that have RUN TO COMPLETION — their istio-proxy sidecar terminates normally,
# so a not-Ready proxy on them is NOT a cert failure (e.g. a finished Helm-hook
# Job). They are exempt from the live-data-plane sidecar-readiness check.
COMPLETED_PHASES = ("Succeeded", "Completed")
# Single-source the istiod-log window from the WS1 gate (Q6: tied to the 150 s
# startupProbe budget so a normal cold start is never mis-read as the fault).
MESH_LOG_WINDOW = mesh.DEFAULT_LOG_WINDOW
# Fail-closed bound (seconds) on every read-only kubectl/helm read: a wedged
# ClusterIP (the #307 case) makes a read HANG, so it must time out into a
# non-zero-exit sentinel rather than block run(). Single-sourced from the WS1 gate.
PROBE_TIMEOUT = mesh.DEFAULT_PROBE_TIMEOUT

# The nine probe identifiers — fixed order (determinism contract). The four
# mesh-health probes are APPENDED so the core five keep their indices.
PROBES = (
    "pods-ready",
    "digest-matches-published",
    "no-drift",
    "health-version",
    "recall-e2e",
    "istiod-api-reachable",
    "sidecars-have-certs",
    "vault-secret-rendered",
    "no-unhealthy-upstream",
)


# ── external-effect indirections (monkeypatched in tests) ────────────────────


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a read-only kubectl/helm command. Sole subprocess entry point.

    Fail-closed like :meth:`scripts.deploy.mesh.MeshGate._probe`: a HUNG or MISSING
    ``kubectl`` must never hang or crash :meth:`VerifyRunner.run`. A wedged
    ClusterIP — the exact #307 case the mesh probes target — is precisely when a
    kube-API read HANGS, so every read is bounded by ``PROBE_TIMEOUT``; an
    ``OSError`` (binary not on PATH) or a ``subprocess.SubprocessError``
    (``TimeoutExpired``) is mapped to the SAME non-zero-exit ``CompletedProcess``
    sentinel a genuine non-zero exit already produces — so every probe that reads
    through this seam degrades to a clean FAIL, never a hang or an uncaught crash.
    """
    logger.info("exec: %s", " ".join(cmd))
    try:
        return subprocess.run(  # noqa: S603 - fixed argv lists, no shell
            cmd, capture_output=True, text=True, check=False, timeout=PROBE_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("read-only command failed (fail-closed): %s (%s)", cmd, exc)
        return subprocess.CompletedProcess(
            cmd, returncode=1, stdout="", stderr=str(exc)
        )


def ssl_context(insecure: bool) -> ssl.SSLContext | None:
    """Return an SSL context for front-door requests.

    Default (``insecure=False``) returns ``None`` → urllib uses the STRICT
    system trust store (certificate + hostname verified). ``insecure=True`` is an
    EXPLICIT dev/self-signed opt-in (``--insecure``) that disables verification —
    the laptop front door presents a self-signed cert, so live validation there
    needs this (the equivalent of ``curl -k``). Nothing is weakened by default.
    """
    if not insecure:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _http_request(  # pragma: no cover - thin urllib egress boundary; monkeypatched in tests
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 30,
    context: ssl.SSLContext | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Perform an HTTP request; return ``(status, response_headers, body)``.

    The single network-egress point (front door). Monkeypatched in tests so no
    real socket is opened. A transport failure returns status ``0`` so a probe
    reads it as a hard failure rather than an exception. ``context`` (when set)
    is the unverified SSL context selected by ``--insecure``.

    Real ``urlopen`` RAISES the WHOLE transport-error class here, not just
    ``URLError``: a READ timeout surfaces as a bare ``TimeoutError`` (==
    ``socket.timeout``, an ``OSError``) that is NOT a ``URLError``, so an
    ``except (HTTPError, URLError)`` would let it escape and crash the probe with
    no verdict (the WS5 fault-injection finding; same class as the WS2 CodeQL
    urllib lesson). Catching ``OSError`` after ``HTTPError`` maps the ENTIRE class
    — ``URLError``, ``TimeoutError``/``socket.timeout``, ``ConnectionError`` — to
    the same clean status-``0`` sentinel in ONE place, so no probe can ever see an
    uncaught transport exception.
    """
    request = urllib.request.Request(
        url, data=body, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - https front door
            request, timeout=timeout, context=context
        ) as response:
            status = response.status
            resp_headers = {k.lower(): v for k, v in response.headers.items()}
            payload = response.read()
        return status, resp_headers, payload
    except HTTPError as exc:
        return exc.code, {}, exc.read()
    except OSError:
        # URLError (an OSError), a bare TimeoutError/socket.timeout on a read
        # timeout, or any ConnectionError — one sentinel for the whole class.
        return 0, {}, b""


def _now_iso() -> str:
    """UTC ISO-8601 timestamp — EVIDENCE ONLY, never read back into logic."""
    return datetime.now(UTC).isoformat()


def load_token(token_file: Path) -> str | None:
    """Resolve a scoped JWT: ``AUDITTRACE_TOKEN`` env first, then the token file.

    The token is used only as a Bearer header and is NEVER logged or written to
    the report. Returns ``None`` when no token is available.
    """
    env_token = os.environ.get("AUDITTRACE_TOKEN")
    if env_token:
        return env_token
    try:
        data = json.loads(Path(token_file).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    token = data.get("access_token")
    return token if isinstance(token, str) and token else None


# ── config + records ─────────────────────────────────────────────────────────


def _normalize_front_door(url: str) -> str:
    """Validate + normalise the front-door base URL (exact scheme/host check).

    Uses :func:`urllib.parse.urlparse` — NOT substring matching — so a hostile
    URL like ``https://evil/?x=audittrace`` cannot slip through a naive
    ``"audittrace" in url`` test (the CodeQL ``py/incomplete-url-substring``
    class). Rejects anything that is not an http/https URL with a hostname.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"front-door must be an http(s) URL with a host: {url!r}")
    return url.rstrip("/")


@dataclass(frozen=True)
class VerifyConfig:
    target_version: str
    namespace: str = "audittrace"
    release: str = "audittrace"
    registry: str = "hub"
    front_door: str = DEFAULT_FRONT_DOOR
    token_file: Path = DEFAULT_TOKEN_FILE
    model: str = "audittrace-chat"
    interactions_limit: int = 5
    # STRICT TLS by default; --insecure is an explicit dev/self-signed opt-in.
    insecure: bool = False
    out_dir: Path = Path(__file__).resolve().parents[2] / "scripts" / "deploy" / "runs"

    @property
    def deployment(self) -> str:
        return f"{self.release}-memory-server"

    @property
    def image_tag(self) -> str:
        """The IMAGE tag / version to resolve + compare (git ``v`` prefix stripped)."""
        return normalize_version(self.target_version)


@dataclass
class ProbeResult:
    name: str
    status: str  # PASS | FAIL
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    checked_at: str = ""

    @property
    def passed(self) -> bool:
        return self.status == PASS


# ── pure helpers (unit-tested directly) ──────────────────────────────────────


def pod_labels(pod: dict[str, Any]) -> dict[str, str]:
    labels = pod.get("metadata", {}).get("labels", {})
    return labels if isinstance(labels, dict) else {}


def pod_ready(pod: dict[str, Any]) -> bool:
    """True iff the pod's ``Ready`` condition is ``True``."""
    for cond in pod.get("status", {}).get("conditions", []):
        if cond.get("type") == "Ready":
            return cond.get("status") == "True"
    return False


def component_readiness(
    pods: list[dict[str, Any]], selectors: dict[str, tuple[str, str]]
) -> dict[str, dict[str, int]]:
    """Map each expected component to ``{"pods": n, "ready": r}`` by EXACT LABEL.

    A pod matches a component only when it carries the component's label
    key=value EXACTLY. A pod whose NAME merely contains the component token but
    lacks the label (a metrics exporter, a stale ReplicaSet pod of a different
    component) does NOT match — so a rogue Ready pod cannot mask a down
    component. This is the certifier-correctness fix over name-substring
    matching.
    """
    result: dict[str, dict[str, int]] = {}
    for comp, (key, value) in selectors.items():
        matched = [p for p in pods if pod_labels(p).get(key) == value]
        ready = sum(1 for p in matched if pod_ready(p))
        result[comp] = {"pods": len(matched), "ready": ready}
    return result


def unready_components(readiness: dict[str, dict[str, int]]) -> list[str]:
    """Components with no Ready pod — missing OR present-but-not-Ready both fail."""
    return [comp for comp, st in readiness.items() if st["ready"] < 1]


def strategy_matches_intent(strategy: dict[str, Any]) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for the surge-safe chart intent.

    ``maxSurge`` / ``maxUnavailable`` may render as int or string ("0" vs 0, or
    "25%") — normalised to ``str`` for comparison so both forms agree.
    """
    stype = strategy.get("type")
    if stype != EXPECTED_STRATEGY_TYPE:
        return False, f"strategy.type={stype!r} (expected {EXPECTED_STRATEGY_TYPE!r})"
    rolling = strategy.get("rollingUpdate") or {}
    surge = str(rolling.get("maxSurge"))
    unavail = str(rolling.get("maxUnavailable"))
    if surge != EXPECTED_MAX_SURGE:
        return (
            False,
            f"maxSurge={surge} (expected {EXPECTED_MAX_SURGE}) — surge risk reintroduced",
        )
    if unavail != EXPECTED_MAX_UNAVAILABLE:
        return False, f"maxUnavailable={unavail} (expected {EXPECTED_MAX_UNAVAILABLE})"
    return True, f"surge-safe: maxSurge={surge}, maxUnavailable={unavail}"


def is_recall_tool(tool_name: str) -> bool:
    """A recorded tool call is a recall when its name starts with ``recall``."""
    return tool_name.startswith("recall")


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None


# ── #384 WS2 mesh-health pure helpers (unit-tested directly, no I/O) ──────────


def pod_name(pod: dict[str, Any]) -> str:
    name = pod.get("metadata", {}).get("name")
    return name if isinstance(name, str) and name else "<unknown>"


def pod_annotations(pod: dict[str, Any]) -> dict[str, str]:
    ann = pod.get("metadata", {}).get("annotations", {})
    return ann if isinstance(ann, dict) else {}


def _all_container_statuses(pod: dict[str, Any]) -> list[dict[str, Any]]:
    """Every reported container status — regular AND init/native-sidecar.

    Istio's ``istio-proxy`` may be a native sidecar (an initContainer with
    ``restartPolicy: Always``), so its readiness surfaces in
    ``initContainerStatuses``; the app's exit-79 surfaces in ``containerStatuses``.
    Scanning both makes the readiness / exit-code helpers robust to either shape.
    """
    status = pod.get("status", {})
    if not isinstance(status, dict):
        return []
    out: list[dict[str, Any]] = []
    for key in ("containerStatuses", "initContainerStatuses"):
        value = status.get(key)
        if isinstance(value, list):
            out.extend(c for c in value if isinstance(c, dict))
    return out


def pod_container_names(pod: dict[str, Any]) -> set[str]:
    """All container names known for the pod (spec + status, regular + init)."""
    names = {c.get("name") for c in _all_container_statuses(pod)}
    spec = pod.get("spec", {})
    if isinstance(spec, dict):
        for key in ("containers", "initContainers"):
            for container in spec.get(key, []) or []:
                if isinstance(container, dict):
                    names.add(container.get("name"))
    return {n for n in names if isinstance(n, str)}


def pod_has_container(pod: dict[str, Any], name: str) -> bool:
    return name in pod_container_names(pod)


def container_is_ready(pod: dict[str, Any], name: str) -> bool:
    """True iff a container status named ``name`` reports ``ready: true``.

    A container that is ABSENT (never injected) or present-but-not-ready both
    return False — the fail-closed reading a certifier needs.
    """
    for cs in _all_container_statuses(pod):
        if cs.get("name") == name:
            return cs.get("ready") is True
    return False


def containers_with_exit_code(pod: dict[str, Any], code: int) -> list[str]:
    """Names of containers whose ``lastState.terminated.exitCode`` == ``code``."""
    out: list[str] = []
    for cs in _all_container_statuses(pod):
        terminated = (cs.get("lastState", {}) or {}).get("terminated", {}) or {}
        if terminated.get("exitCode") == code:
            out.append(str(cs.get("name")))
    return out


def crashlooping_containers(pod: dict[str, Any]) -> list[str]:
    """Names of containers currently in ``CrashLoopBackOff``."""
    out: list[str] = []
    for cs in _all_container_statuses(pod):
        waiting = (cs.get("state", {}) or {}).get("waiting", {}) or {}
        if waiting.get("reason") == "CrashLoopBackOff":
            out.append(str(cs.get("name")))
    return out


def sidecar_cert_findings(pods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Istio-injected pods whose data plane is not certified.

    A pod is checked only if it carries an ``istio-proxy`` container. A pod that has
    RUN TO COMPLETION (``Succeeded``/``Completed`` — e.g. a finished Helm-hook Job)
    is exempt: its sidecar terminates normally, which is not a cert failure. Any
    remaining injected pod fails when its sidecar is not Ready (no workload cert) OR
    it has not reached ``Running`` (wedged in Init — the #307 cert-starved
    signature). Pods with no sidecar are not part of the data plane and are skipped.
    """
    findings: list[dict[str, Any]] = []
    for pod in pods:
        if not pod_has_container(pod, ISTIO_PROXY_CONTAINER):
            continue
        status = pod.get("status")
        status = status if isinstance(status, dict) else {}
        phase = status.get("phase")
        if phase in COMPLETED_PHASES:
            # A completed pod's terminated sidecar is not a cert failure.
            continue
        reasons: list[str] = []
        if not container_is_ready(pod, ISTIO_PROXY_CONTAINER):
            reasons.append("istio-proxy sidecar not Ready (no workload cert)")
        if phase != "Running":
            reasons.append(f"pod phase={phase!r} (wedged Init / not Running)")
        if reasons:
            findings.append({"pod": pod_name(pod), "phase": phase, "reasons": reasons})
    return findings


def vault_secret_findings(pods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Vault-annotated pods whose secret was not rendered fail-closed.

    For each pod carrying the vault inject annotation: the ``vault-agent`` sidecar
    must be Ready (an annotated pod with NO ready vault-agent is the mode-4a
    silent-non-injection signature), no container may have exited 79
    (``/vault/secrets/env`` absent), and no container may be in CrashLoopBackOff.
    Evidence is container status only — secret VALUES are never read or emitted.
    """
    findings: list[dict[str, Any]] = []
    for pod in pods:
        if pod_annotations(pod).get(VAULT_INJECT_ANNOTATION) != "true":
            continue
        reasons: list[str] = []
        if not container_is_ready(pod, VAULT_AGENT_CONTAINER):
            reasons.append(
                "vault-agent sidecar missing or not Ready "
                "(secret not rendered / silent non-injection)"
            )
        exited = containers_with_exit_code(pod, VAULT_FAILCLOSED_EXIT_CODE)
        if exited:
            reasons.append(
                f"container(s) {exited} exited {VAULT_FAILCLOSED_EXIT_CODE} "
                "(/vault/secrets/env absent)"
            )
        crashing = crashlooping_containers(pod)
        if crashing:
            reasons.append(f"container(s) {crashing} in CrashLoopBackOff")
        if reasons:
            findings.append({"pod": pod_name(pod), "reasons": reasons})
    return findings


def count_unhealthy_upstream(logs: str) -> int:
    """Count log LINES carrying a cert-dead-mesh "no healthy upstream" signature."""
    return sum(
        1
        for line in logs.splitlines()
        if any(sig in line for sig in UNHEALTHY_UPSTREAM_SIGNATURES)
    )


def sanitized_finding(finding: mesh.Finding) -> dict[str, Any]:
    """A mesh :class:`~scripts.deploy.mesh.Finding` as a dict, safe to put in the report.

    FAIL-CLOSED: keeps ``signal`` + ``detail`` (the diagnoser's own derived status
    strings) and, for evidence, ONLY keys in :data:`ALLOWED_EVIDENCE_KEYS` — every
    other key is DROPPED. mesh diagnosers attach RAW command/log output under keys
    like ``log_tail`` / ``error`` / ``deployment_error`` / ``endpoints_error``;
    allowlisting (rather than denylisting those) means a NEW raw key added to any
    diagnoser is dropped by default, never leaked by default
    (CodeQL py/clear-text-logging-of-sensitive-data).
    """
    raw = finding.as_dict()
    evidence = {
        key: value
        for key, value in (raw.get("evidence") or {}).items()
        if key in ALLOWED_EVIDENCE_KEYS
    }
    return {"signal": raw["signal"], "detail": raw["detail"], "evidence": evidence}


# ── the runner ────────────────────────────────────────────────────────────────


class VerifyRunner:
    """Runs the five deploy-health probes and emits an independent PASS/FAIL verdict."""

    def __init__(self, cfg: VerifyConfig) -> None:
        self.cfg = cfg
        self.results: list[ProbeResult] = []

    # -- independence, DERIVED (not self-attested) --

    def evidence_sources(self) -> list[str]:
        """The full, enumerated list of evidence this runner consults.

        Exposed in the report so a reader sees EXACTLY what the verdict stands on.
        None of these is a deploy-runner report — the deployer's report is not an
        input. If a deploy-report source were ever added it would appear here and
        flip :attr:`reads_deploy_report`. This transparency is secondary; the real
        guarantee is behavioural (a hostile on-disk deploy report cannot move the
        verdict — see the property test).
        """
        return [
            "cluster-api:kubectl:get-pods",
            "cluster-api:kubectl:get-deployment",
            "cluster-api:kubectl:memory-server-pod-imageID",
            "cluster-api:helm:status",
            f"registry:{self.cfg.registry}:published-digest",
            f"front-door:{self.cfg.front_door}",
            # ── #384 WS2 mesh-health evidence (independent cluster reads) ──
            "cluster-api:kubectl:istiod-logs",
            "cluster-api:kubectl:istiod-deployment",
            "cluster-api:kubectl:istiod-endpoints",
            "cluster-api:kubectl:memory-server-logs",
        ]

    @property
    def reads_deploy_report(self) -> bool:
        """DERIVED from :meth:`evidence_sources`: True iff any source is a deploy
        report. Always False here because no source is tagged as one — but it is
        computed, not asserted, so it cannot silently lie about a source that was
        added."""
        return any(
            s.startswith(DEPLOY_REPORT_SOURCE_PREFIX) for s in self.evidence_sources()
        )

    def _record(
        self, name: str, status: str, detail: str, evidence: dict[str, Any]
    ) -> ProbeResult:
        rec = ProbeResult(
            name=name,
            status=status,
            detail=detail,
            evidence=evidence,
            checked_at=_now_iso(),
        )
        self.results.append(rec)
        level = logging.INFO if status == PASS else logging.ERROR
        logger.log(level, "[%s] %s — %s", name, status, detail)
        return rec

    # -- read-only cluster reads --

    def _pods_json(self) -> list[dict[str, Any]]:
        proc = _run(["kubectl", "get", "pods", "-n", self.cfg.namespace, "-o", "json"])
        if proc.returncode != 0:
            return []
        parsed = _parse_json(proc.stdout.encode())
        if not isinstance(parsed, dict):
            return []
        items = parsed.get("items")
        return items if isinstance(items, list) else []

    def _live_memory_server_digest(self) -> str | None:
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
        for token in proc.stdout.split():
            digest = extract_digest(token)
            if digest:
                return digest
        return None

    def _helm_status(self) -> str | None:
        proc = _run(
            ["helm", "status", self.cfg.release, "-n", self.cfg.namespace, "-o", "json"]
        )
        if proc.returncode != 0:
            return None
        parsed = _parse_json(proc.stdout.encode())
        if not isinstance(parsed, dict):
            return None
        return parsed.get("info", {}).get("status")

    def _deployment_strategy(self) -> dict[str, Any] | None:
        proc = _run(
            [
                "kubectl",
                "get",
                "deployment",
                self.cfg.deployment,
                "-n",
                self.cfg.namespace,
                "-o",
                "json",
            ]
        )
        if proc.returncode != 0:
            return None
        parsed = _parse_json(proc.stdout.encode())
        if not isinstance(parsed, dict):
            return None
        strategy = parsed.get("spec", {}).get("strategy")
        return strategy if isinstance(strategy, dict) else None

    # -- #384 WS2 mesh-health cluster reads (independent, read-only) --

    def _istiod_logs(self) -> str | None:
        """istiod log window. ``None`` on a non-zero exit → fail-closed FAIL."""
        proc = _run(
            [
                "kubectl",
                "logs",
                "-n",
                mesh.ISTIO_NAMESPACE,
                "-l",
                mesh.ISTIOD_LABEL,
                f"--since={MESH_LOG_WINDOW}",
            ]
        )
        return proc.stdout if proc.returncode == 0 else None

    def _istiod_deployment(self) -> dict[str, Any] | None:
        """Parsed istiod Deployment JSON. ``None`` when unreadable (fail-closed)."""
        proc = _run(
            [
                "kubectl",
                "get",
                "deployment",
                mesh.ISTIOD_DEPLOYMENT,
                "-n",
                mesh.ISTIO_NAMESPACE,
                "-o",
                "json",
            ]
        )
        if proc.returncode != 0:
            return None
        parsed = _parse_json(proc.stdout.encode())
        return parsed if isinstance(parsed, dict) else None

    def _istiod_endpoint_ips(self) -> str | None:
        """Space-joined istiod Service endpoint IPs. ``None`` on error (fail-closed)."""
        proc = _run(
            [
                "kubectl",
                "get",
                "endpoints",
                mesh.ISTIOD_DEPLOYMENT,
                "-n",
                mesh.ISTIO_NAMESPACE,
                "-o",
                "jsonpath={.subsets[*].addresses[*].ip}",
            ]
        )
        return proc.stdout if proc.returncode == 0 else None

    def _memory_server_logs(self) -> str | None:
        """memory-server log window. ``None`` on a non-zero exit → fail-closed FAIL."""
        proc = _run(
            [
                "kubectl",
                "logs",
                "-n",
                self.cfg.namespace,
                "-l",
                _COMPONENT_SELECTOR,
                f"--since={MESH_LOG_WINDOW}",
            ]
        )
        return proc.stdout if proc.returncode == 0 else None

    # -- front-door reads (single egress seam: _http_request) --

    def _front_get(self, path: str, token: str | None) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        status, _h, body = _http_request(
            "GET",
            f"{self.cfg.front_door}{path}",
            headers,
            context=ssl_context(self.cfg.insecure),
        )
        return status, _parse_json(body)

    def _front_post(
        self, path: str, token: str | None, payload: dict[str, Any]
    ) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = json.dumps(payload).encode()
        status, _h, resp = _http_request(
            "POST",
            f"{self.cfg.front_door}{path}",
            headers,
            body,
            context=ssl_context(self.cfg.insecure),
        )
        return status, _parse_json(resp)

    # -- probe 1: pods-ready --

    def probe_pods_ready(self) -> ProbeResult:
        pods = self._pods_json()
        if not pods:
            return self._record(
                PROBES[0],
                FAIL,
                "no pods returned from the cluster API (unreachable or namespace empty)",
                {"namespace": self.cfg.namespace, "pod_count": 0},
            )
        readiness = component_readiness(pods, COMPONENT_SELECTORS)
        failing = unready_components(readiness)
        if failing:
            return self._record(
                PROBES[0],
                FAIL,
                f"components with no Ready pod (matched by exact label): {', '.join(failing)}",
                {"readiness": readiness, "unready": failing},
            )
        return self._record(
            PROBES[0],
            PASS,
            f"all {len(COMPONENT_SELECTORS)} expected components have a Ready pod (exact-label match)",
            {"readiness": readiness},
        )

    # -- probe 2: digest-matches-published --

    def probe_digest_matches_published(self) -> ProbeResult:
        try:
            ref = registry.resolve(self.cfg.image_tag, self.cfg.registry)
        except registry.DigestResolutionError as exc:
            return self._record(
                PROBES[1],
                FAIL,
                f"could not resolve the published digest for {self.cfg.image_tag}: {exc}",
                {"error": str(exc), "registry": self.cfg.registry},
            )
        if not ref.digest:
            return self._record(
                PROBES[1],
                FAIL,
                f"registry {self.cfg.registry} returned no digest — cannot certify the "
                "tracks-published-digest invariant",
                {"published": ref.as_dict()},
            )
        live = self._live_memory_server_digest()
        if live is None:
            return self._record(
                PROBES[1],
                FAIL,
                "no live memory-server pod digest readable from the cluster",
                {"published_digest": ref.digest, "live_digest": None},
            )
        if live != ref.digest:
            return self._record(
                PROBES[1],
                FAIL,
                "live digest does NOT match the published digest (running an unpublished image)",
                {"published_digest": ref.digest, "live_digest": live},
            )
        return self._record(
            PROBES[1],
            PASS,
            "live memory-server digest matches the published digest",
            {"published_digest": ref.digest, "live_digest": live},
        )

    # -- probe 3: no-drift --

    def probe_no_drift(self) -> ProbeResult:
        status = self._helm_status()
        strategy = self._deployment_strategy()
        if status != "deployed":
            return self._record(
                PROBES[2],
                FAIL,
                f"helm release status={status!r} (expected 'deployed')",
                {"helm_status": status, "strategy": strategy},
            )
        if strategy is None:
            return self._record(
                PROBES[2],
                FAIL,
                "could not read the live Deployment strategy",
                {"helm_status": status, "strategy": None},
            )
        ok, reason = strategy_matches_intent(strategy)
        if not ok:
            return self._record(
                PROBES[2],
                FAIL,
                f"chart↔cluster drift — {reason}",
                {"helm_status": status, "strategy": strategy},
            )
        return self._record(
            PROBES[2],
            PASS,
            f"no drift: release deployed and {reason}",
            {"helm_status": status, "strategy": strategy},
        )

    # -- probe 4: health-version --

    def probe_health_version(self) -> ProbeResult:
        status_code, body = self._front_get("/health", token=None)
        if status_code != 200 or not isinstance(body, dict):
            return self._record(
                PROBES[3],
                FAIL,
                f"front-door /health returned HTTP {status_code} (expected 200 + JSON)",
                {"http_status": status_code},
            )
        health_status = body.get("status")
        version = body.get("version")
        want = self.cfg.image_tag
        if health_status != "ok":
            return self._record(
                PROBES[3],
                FAIL,
                f"/health status={health_status!r} (expected 'ok')",
                {"health": body},
            )
        if normalize_version(str(version)) != want:
            return self._record(
                PROBES[3],
                FAIL,
                f"/health version={version!r} does NOT match target {want!r}",
                {"reported_version": version, "target_version": want},
            )
        return self._record(
            PROBES[3],
            PASS,
            f"/health ok and reports the target version {want}",
            {"reported_version": version, "target_version": want},
        )

    # -- probe 5: recall-e2e (front door, scoped JWT, real model) --

    def probe_recall_e2e(self) -> ProbeResult:
        token = load_token(self.cfg.token_file)
        if not token:
            return self._record(
                PROBES[4],
                FAIL,
                "no scoped JWT available — the mandatory recall E2E cannot run "
                "(a deploy that cannot be exercised through the audit plane is not certified)",
                {"token_source": None},
            )
        chat_payload = {
            "model": self.cfg.model,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Use your memory tools to recall what governs memory_mode in "
                        "this project. Call recall_semantic before answering."
                    ),
                }
            ],
        }
        chat_status, chat_body = self._front_post(
            "/v1/chat/completions", token, chat_payload
        )
        if (
            chat_status != 200
            or not isinstance(chat_body, dict)
            or not chat_body.get("choices")
        ):
            return self._record(
                PROBES[4],
                FAIL,
                f"front-door /v1/chat/completions returned HTTP {chat_status} without a completion",
                {"http_status": chat_status},
            )
        list_status, list_body = self._front_get(
            f"/interactions?limit={self.cfg.interactions_limit}", token
        )
        interactions = (
            list_body.get("interactions") if isinstance(list_body, dict) else None
        )
        if list_status != 200 or not interactions:
            return self._record(
                PROBES[4],
                FAIL,
                f"audit plane returned no interaction after the chat call (HTTP {list_status})",
                {"http_status": list_status},
            )
        interaction_id = interactions[0].get("id")
        tc_status, tc_body = self._front_get(
            f"/interactions/{interaction_id}/tool-calls", token
        )
        tool_calls = tc_body.get("tool_calls") if isinstance(tc_body, dict) else None
        if tc_status != 200 or tool_calls is None:
            return self._record(
                PROBES[4],
                FAIL,
                f"tool-calls endpoint returned HTTP {tc_status} for interaction {interaction_id}",
                {"http_status": tc_status, "interaction_id": interaction_id},
            )
        recalls = [
            tc for tc in tool_calls if is_recall_tool(str(tc.get("tool_name", "")))
        ]
        if not recalls:
            return self._record(
                PROBES[4],
                FAIL,
                "the audit plane recorded the interaction but NO recall_* tool call — "
                "recall did not reach the audit trail",
                {
                    "interaction_id": interaction_id,
                    "tool_names": [tc.get("tool_name") for tc in tool_calls],
                },
            )
        return self._record(
            PROBES[4],
            PASS,
            f"recall recorded on the audit plane for interaction {interaction_id} "
            f"({len(recalls)} recall row(s))",
            {
                "interaction_id": interaction_id,
                "recall_tool_names": [tc.get("tool_name") for tc in recalls],
            },
        )

    # -- probe 6: istiod-api-reachable (#384 WS2, the #307 signal) --

    def probe_istiod_api_reachable(self) -> ProbeResult:
        """istiod can reach the kube-API to cert a NEW sidecar (#307).

        Independent evidence: this runner reads the istiod logs, Deployment and
        endpoints ITSELF, then feeds them to the PURE ``mesh`` diagnosers. It never
        reads the WS1 gate's result. Any unreadable transport read → fail-closed.
        """
        logs = self._istiod_logs()
        deployment = self._istiod_deployment()
        endpoint_ips = self._istiod_endpoint_ips()
        unreadable = [
            label
            for label, value in (
                ("istiod-logs", logs),
                ("istiod-endpoints", endpoint_ips),
            )
            if value is None
        ]
        if unreadable:
            return self._record(
                PROBES[5],
                FAIL,
                f"could not read {', '.join(unreadable)} (fail-closed)",
                {"unreadable": unreadable},
            )
        findings = mesh.evaluate_istiod_logs(
            logs, MESH_LOG_WINDOW
        ) + mesh.evaluate_istiod_readiness(deployment, endpoint_ips)
        if findings:
            return self._record(
                PROBES[5],
                FAIL,
                "istiod<->kube-API path degraded: "
                + ", ".join(f.signal for f in findings),
                # sanitized: raw istiod log_tail stripped (see sanitized_finding).
                {"findings": [sanitized_finding(f) for f in findings]},
            )
        return self._record(
            PROBES[5],
            PASS,
            "istiod fully Ready with endpoints and no #307 smoking-gun in the log window",
            {"log_window": MESH_LOG_WINDOW},
        )

    # -- probe 7: sidecars-have-certs (#384 WS2) --

    def probe_sidecars_have_certs(self) -> ProbeResult:
        """Every istio-injected pod has a Ready istio-proxy and is Running."""
        pods = self._pods_json()
        if not pods:
            return self._record(
                PROBES[6],
                FAIL,
                "no pods returned from the cluster API (unreachable or namespace empty)",
                {"namespace": self.cfg.namespace, "pod_count": 0},
            )
        injected = [p for p in pods if pod_has_container(p, ISTIO_PROXY_CONTAINER)]
        if not injected:
            return self._record(
                PROBES[6],
                FAIL,
                "no istio-injected pods found — the data plane cannot be certified",
                {"pod_count": len(pods)},
            )
        findings = sidecar_cert_findings(pods)
        if findings:
            return self._record(
                PROBES[6],
                FAIL,
                "cert-starved data plane: " + ", ".join(f["pod"] for f in findings),
                {"findings": findings},
            )
        return self._record(
            PROBES[6],
            PASS,
            f"all {len(injected)} istio-injected pod(s) have a Ready istio-proxy sidecar",
            {"injected_pods": [pod_name(p) for p in injected]},
        )

    # -- probe 8: vault-secret-rendered (#384 WS2) --

    def probe_vault_secret_rendered(self) -> ProbeResult:
        """Every vault-annotated pod rendered its secret (no exit-79 / non-injection)."""
        pods = self._pods_json()
        if not pods:
            return self._record(
                PROBES[7],
                FAIL,
                "no pods returned from the cluster API (unreachable or namespace empty)",
                {"namespace": self.cfg.namespace, "pod_count": 0},
            )
        annotated = [
            p for p in pods if pod_annotations(p).get(VAULT_INJECT_ANNOTATION) == "true"
        ]
        if not annotated:
            return self._record(
                PROBES[7],
                FAIL,
                "no vault-annotated pods found — cannot certify secret render (fail-closed)",
                {"pod_count": len(pods)},
            )
        findings = vault_secret_findings(pods)
        if findings:
            return self._record(
                PROBES[7],
                FAIL,
                "vault secret not rendered: " + ", ".join(f["pod"] for f in findings),
                {"findings": findings},
            )
        return self._record(
            PROBES[7],
            PASS,
            f"all {len(annotated)} vault-annotated pod(s) rendered their secret "
            "(Ready vault-agent, no exit-79)",
            {"vault_pods": [pod_name(p) for p in annotated]},
        )

    # -- probe 9: no-unhealthy-upstream (#384 WS2) --

    def probe_no_unhealthy_upstream(self) -> ProbeResult:
        """Front door serves AND no cert-dead-mesh 503 in the memory-server logs."""
        status_code, body = self._front_get("/health", token=None)
        if (
            status_code != 200
            or not isinstance(body, dict)
            or body.get("status") != "ok"
        ):
            reported = body.get("status") if isinstance(body, dict) else None
            return self._record(
                PROBES[8],
                FAIL,
                f"front-door /health HTTP {status_code} status={reported!r} "
                "(expected 200 + status=ok)",
                {"http_status": status_code, "health_status": reported},
            )
        logs = self._memory_server_logs()
        if logs is None:
            return self._record(
                PROBES[8],
                FAIL,
                "could not read memory-server logs (fail-closed)",
                {"http_status": status_code},
            )
        hits = count_unhealthy_upstream(logs)
        if hits:
            return self._record(
                PROBES[8],
                FAIL,
                f"{hits} 'no healthy upstream' 503 signature(s) in the last {MESH_LOG_WINDOW} "
                "(cert-dead mesh data plane)",
                # SECURITY: emit only the DERIVED count + window + the named signature,
                # never raw log content — memory-server logs can carry tokens / request
                # content / PII (CodeQL py/clear-text-logging-of-sensitive-data). The
                # count + window + signature name is sufficient reconstruction evidence.
                {"match_count": hits, "log_window": MESH_LOG_WINDOW},
            )
        return self._record(
            PROBES[8],
            PASS,
            "front door serving and no 'no healthy upstream' 503 in the log window",
            {"log_window": MESH_LOG_WINDOW},
        )

    # -- orchestration + verdict --

    @property
    def verdict(self) -> str:
        """PASS iff every probe passed; ANY FAIL → FAIL (the mandate-to-fail)."""
        if not self.results:
            return FAIL
        return PASS if all(r.passed for r in self.results) else FAIL

    def run(self) -> dict[str, Any]:
        self.probe_pods_ready()
        self.probe_digest_matches_published()
        self.probe_no_drift()
        self.probe_health_version()
        self.probe_recall_e2e()
        # ── #384 WS2 mesh-health probes (independent, fail-closed) ──
        self.probe_istiod_api_reachable()
        self.probe_sidecars_have_certs()
        self.probe_vault_secret_rendered()
        self.probe_no_unhealthy_upstream()
        return self.write_report()

    def build_report(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "runner": "audittrace-verify-runner",
            "target_version": self.cfg.target_version,
            "namespace": self.cfg.namespace,
            "release": self.cfg.release,
            "registry": self.cfg.registry,
            "front_door": self.cfg.front_door,
            "insecure_tls": self.cfg.insecure,
            # ── the independence contract: DERIVED + transparent ──
            "evidence_sources": self.evidence_sources(),
            "reads_deploy_report": self.reads_deploy_report,
            "independence": INDEPENDENCE_NOTE,
            "verdict": self.verdict,
            "probes": [asdict(r) for r in self.results],
            "generated_at": _now_iso(),
        }

    def human_summary(self, report: dict[str, Any]) -> str:
        lines = [
            "AuditTrace verify runner — INDEPENDENT deploy certification",
            "=" * 56,
            f"target version : {report['target_version']}",
            f"front door     : {report['front_door']}",
            f"namespace      : {report['namespace']}",
            f"reads deploy report : {report['reads_deploy_report']}  (must be False)",
            "",
            "probes:",
        ]
        for rec in report["probes"]:
            lines.append(f"  [{rec['name']}] {rec['status']} — {rec['detail']}")
        lines += ["", f"VERDICT: {report['verdict']}  (any FAIL blocks 'done')"]
        return "\n".join(lines) + "\n"

    def write_report(self) -> dict[str, Any]:
        out_dir = Path(self.cfg.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        report = self.build_report()
        stamp = _now_iso().replace(":", "").replace("-", "")
        base = f"verify-{self.cfg.target_version}-{stamp}"
        (out_dir / f"{base}.json").write_text(json.dumps(report, indent=2, default=str))
        (out_dir / f"{base}.txt").write_text(self.human_summary(report))
        return report


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.deploy.verify",
        description="Deterministic INDEPENDENT deploy-verify runner (front door + read-only kubectl).",
    )
    parser.add_argument(
        "--target-version", required=True, help="intended deployed version"
    )
    parser.add_argument("--namespace", default="audittrace")
    parser.add_argument("--release", default="audittrace")
    parser.add_argument("--registry", choices=("hub", "local"), default="hub")
    parser.add_argument("--front-door", default=DEFAULT_FRONT_DOOR)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument(
        "--model", default="audittrace-chat", help="chat model for the recall E2E"
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help=(
            "disable front-door TLS verification (dev/self-signed opt-in, the "
            "equivalent of curl -k). Default is STRICT verification."
        ),
    )
    parser.add_argument("--out-dir", type=Path, default=VerifyConfig.out_dir)
    return parser


def config_from_args(args: argparse.Namespace) -> VerifyConfig:
    return VerifyConfig(
        target_version=args.target_version,
        namespace=args.namespace,
        release=args.release,
        registry=args.registry,
        front_door=_normalize_front_door(args.front_door),
        token_file=args.token_file,
        model=args.model,
        insecure=args.insecure,
        out_dir=args.out_dir,
    )


def print_verdict(report: dict[str, Any]) -> None:
    print(
        f"verify — target {report['target_version']} via {report['front_door']} "
        f"(reads_deploy_report={report['reads_deploy_report']})\n"
    )
    for rec in report["probes"]:
        print(f"  [{rec['name']}] {rec['status']} — {rec['detail']}")
    print(f"\n  VERDICT: {report['verdict']}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = build_parser().parse_args(argv)
    cfg = config_from_args(args)
    report = VerifyRunner(cfg).run()
    print_verdict(report)
    return 0 if report["verdict"] == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
