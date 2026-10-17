"""In-cluster exit-79 vault-injection auto-heal reaper (#384 WS4a).

The vault-agent-injector ``MutatingWebhookConfiguration`` runs with
``failurePolicy=Ignore`` (Vault's safe default — a down injector must never
wedge all admission). The cost of that safety is that when the injector is not
ready at a pod's admission moment, the pod is admitted WITHOUT the vault-agent
sidecars: ``/vault/secrets/env`` is never rendered, ``scripts/entrypoint.sh``
exits ``79``, and the pod enters ``CrashLoopBackOff`` *forever* until a human
runs ``kubectl delete pod``. Invariant I2 (startup degradations self-heal
zero-touch) fails for this case.

This controller closes that gap. It polls the ``memory-server`` pods, detects
the *exit-79 + missing-vault-sidecar* signature, and deletes matching pods so
the ReplicaSet recreates them through a now-healthy injector. It is deliberately
conservative:

* It only ever reaps pods carrying the memory-server component label (never
  anything else in the namespace).
* It only reaps the specific exit-79 / no-sidecar signature — a generic crash
  (exit != 79) or a pod that *did* get the sidecar is left alone (that is a
  different bug the reaper must not paper over).
* **Storm guard:** before reaping anything it checks the vault-agent-injector
  Deployment is Ready. Reaping while the injector is down would just spawn more
  exit-79 pods (a reap storm), so a not-ready injector skips the whole cycle.
* Per-pod cooldown guards against racing the API for a pod already deleted.

The reaper talks to the Kubernetes API directly over ``httpx`` using the
in-cluster ServiceAccount token + CA — no ``kubernetes`` client dependency. It
reads all configuration from the environment and needs no vault injection
itself (a least-privilege namespaced Role: pods get/list/watch/delete,
deployments get).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

# ── Signature constants ──────────────────────────────────────────────────────
# The container whose exit code carries the injection-skipped signal.
MEMORY_SERVER_CONTAINER = "memory-server"
# scripts/entrypoint.sh exits 79 when VAULT_AGENT_REQUIRED=true but
# /vault/secrets/env is missing (see charts/.../_helpers.tpl vaultSecretFileGuard).
VAULT_MISSING_EXIT_CODE = 79
# Sidecar/init containers the vault-agent-injector adds when it mutates a pod.
# Their ABSENCE is the definitive "injection was skipped" tell.
VAULT_SIDECAR_CONTAINERS = frozenset({"vault-agent", "vault-agent-init"})
# The label that marks a memory-server pod. Reaping is scoped to it.
COMPONENT_LABEL = "app.kubernetes.io/component"
MEMORY_SERVER_COMPONENT = "memory-server"

# In-cluster ServiceAccount projection paths.
_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_TOKEN_PATH = _SA_DIR / "token"
_CA_PATH = _SA_DIR / "ca.crt"


# ── Pure reap decision ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class ReapDecision:
    """Outcome of evaluating a single pod. ``reason`` is always populated so
    every skip and every reap is explainable in the logs."""

    reap: bool
    reason: str


def _memory_server_status(pod: dict[str, Any]) -> dict[str, Any] | None:
    """Return the memory-server container's status block, or None."""
    statuses = pod.get("status", {}).get("containerStatuses") or []
    for cs in statuses:
        if cs.get("name") == MEMORY_SERVER_CONTAINER:
            status: dict[str, Any] = cs
            return status
    return None


def _terminated_exit_code(state: dict[str, Any] | None) -> int | None:
    """Extract ``terminated.exitCode`` from a container state block if present."""
    if not state:
        return None
    terminated = state.get("terminated")
    if not terminated:
        return None
    exit_code: int | None = terminated.get("exitCode")
    return exit_code


def _has_exit79_signature(pod: dict[str, Any]) -> bool:
    """True iff the memory-server container shows the exit-79 signature.

    Two container-state shapes both carry the signature, mirroring how the
    kubelet reports a fast-failing container across the crash cycle:

    * ``state.terminated.exitCode == 79`` — the container has just exited
      before the backoff timer has re-parked it into ``waiting``; or
    * ``lastState.terminated.exitCode == 79`` — the ``CrashLoopBackOff``
      between-restarts view, where ``state`` is ``waiting`` and the exit-79
      terminated record has moved into ``lastState`` (history).

    Requiring exit code 79 specifically is what keeps the reaper off generic
    crashes: ``CrashLoopBackOff`` alone is never sufficient.
    """
    cs = _memory_server_status(pod)
    if cs is None:
        return False

    state_exit = _terminated_exit_code(cs.get("state"))
    last_exit = _terminated_exit_code(cs.get("lastState"))
    return state_exit == VAULT_MISSING_EXIT_CODE or last_exit == VAULT_MISSING_EXIT_CODE


def _container_names(pod: dict[str, Any]) -> set[str]:
    """All container + initContainer names declared on the pod spec."""
    spec = pod.get("spec", {})
    names: set[str] = set()
    for key in ("containers", "initContainers"):
        for c in spec.get(key) or []:
            name = c.get("name")
            if name:
                names.add(name)
    return names


def _has_vault_sidecar(pod: dict[str, Any]) -> bool:
    """True iff the vault-agent sidecar/init container is present on the pod.

    Presence means the injector DID mutate the pod, so a crash here is a
    different bug — not ours to reap.
    """
    return bool(_container_names(pod) & VAULT_SIDECAR_CONTAINERS)


def _is_terminating(pod: dict[str, Any]) -> bool:
    """True iff the pod already carries a deletionTimestamp (Terminating)."""
    return pod.get("metadata", {}).get("deletionTimestamp") is not None


def _matches_memory_server(pod: dict[str, Any]) -> bool:
    """True iff the pod carries the memory-server component label."""
    labels = pod.get("metadata", {}).get("labels") or {}
    return labels.get(COMPONENT_LABEL) == MEMORY_SERVER_COMPONENT


def should_reap(
    pod: dict[str, Any], *, memory_server_label_match: bool
) -> ReapDecision:
    """Pure reap decision for one pod. Reap iff ALL hold:

    1. the pod matches the memory-server label selector, AND
    2. the memory-server container shows the exit-79 signature, AND
    3. the vault-agent sidecar is ABSENT (injection was skipped), AND
    4. the pod is not already Terminating.

    Any other state is a skip with a specific reason — never reap on a
    generic crash or on a pod that DID receive the sidecar.
    """
    if not memory_server_label_match:
        return ReapDecision(False, "not a memory-server pod")
    if _is_terminating(pod):
        return ReapDecision(False, "already Terminating")
    if not _has_exit79_signature(pod):
        return ReapDecision(False, "no exit-79 signature on memory-server container")
    if _has_vault_sidecar(pod):
        # Injection happened but the pod still crashed — a different bug.
        return ReapDecision(
            False, "vault-agent sidecar present (injection not skipped)"
        )
    return ReapDecision(
        True, "exit-79 with vault-agent sidecar absent (injection skipped)"
    )


# ── Configuration ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ReaperConfig:
    """All knobs, sourced from the environment."""

    namespace: str = "audittrace"
    poll_interval_seconds: float = 15.0
    cooldown_seconds: float = 120.0
    label_selector: str = f"{COMPONENT_LABEL}={MEMORY_SERVER_COMPONENT}"
    injector_deployment: str = "audittrace-vault-agent-injector"
    kube_api_host: str = ""
    kube_api_port: str = "443"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ReaperConfig:
        e = os.environ if env is None else env
        return cls(
            namespace=e.get("AUDITTRACE_REAPER_NAMESPACE", "audittrace"),
            poll_interval_seconds=float(
                e.get("AUDITTRACE_REAPER_POLL_INTERVAL_SECONDS", "15")
            ),
            cooldown_seconds=float(e.get("AUDITTRACE_REAPER_COOLDOWN_SECONDS", "120")),
            label_selector=e.get(
                "AUDITTRACE_REAPER_LABEL_SELECTOR",
                f"{COMPONENT_LABEL}={MEMORY_SERVER_COMPONENT}",
            ),
            injector_deployment=e.get(
                "AUDITTRACE_REAPER_INJECTOR_DEPLOYMENT",
                "audittrace-vault-agent-injector",
            ),
            kube_api_host=e.get("KUBERNETES_SERVICE_HOST", ""),
            kube_api_port=e.get("KUBERNETES_SERVICE_PORT", "443"),
        )


# ── Kubernetes API client (httpx over the in-cluster SA) ─────────────────────
class KubeApi(Protocol):
    """The narrow slice of the k8s API the controller needs. A Protocol so
    tests can substitute an in-memory fake with no HTTP at all."""

    def list_pods(  # pragma: no cover - interface stub
        self, namespace: str, label_selector: str
    ) -> list[dict[str, Any]]: ...

    def get_deployment(  # pragma: no cover - interface stub
        self, namespace: str, name: str
    ) -> dict[str, Any] | None: ...

    def delete_pod(  # pragma: no cover - interface stub
        self, namespace: str, name: str
    ) -> None: ...


class HttpxKubeApi:
    """``KubeApi`` backed by ``httpx`` and the in-cluster ServiceAccount.

    Auth: Bearer token from the projected SA token file; TLS verified against
    the projected cluster CA. The underlying ``httpx.Client`` is created by the
    caller as a context manager so the connection pool is always closed.
    """

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    @classmethod
    def from_env(cls, config: ReaperConfig) -> HttpxKubeApi:
        if not config.kube_api_host:
            raise RuntimeError(
                "KUBERNETES_SERVICE_HOST is unset — the reaper must run inside a pod"
            )
        token = _TOKEN_PATH.read_text().strip()
        base_url = f"https://{config.kube_api_host}:{config.kube_api_port}"
        client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            verify=str(_CA_PATH),
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        return cls(client)

    def list_pods(self, namespace: str, label_selector: str) -> list[dict[str, Any]]:
        resp = self._client.get(
            f"/api/v1/namespaces/{namespace}/pods",
            params={"labelSelector": label_selector},
        )
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        items: list[dict[str, Any]] = body.get("items", [])
        return items

    def get_deployment(self, namespace: str, name: str) -> dict[str, Any] | None:
        resp = self._client.get(
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}"
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        deployment: dict[str, Any] = resp.json()
        return deployment

    def delete_pod(self, namespace: str, name: str) -> None:
        resp = self._client.request(
            "DELETE", f"/api/v1/namespaces/{namespace}/pods/{name}"
        )
        # 404 = already gone (the ReplicaSet or a prior cycle beat us) — fine.
        if resp.status_code == 404:
            return
        resp.raise_for_status()


def injector_ready(api: KubeApi, config: ReaperConfig) -> bool:
    """True iff the vault-agent-injector Deployment reports >= 1 ready replica.

    This is the storm guard: if the injector is not Ready, recreating an
    exit-79 pod would just admit another sidecar-less pod. A missing Deployment
    (None) is treated as NOT ready — fail safe, do not reap.
    """
    dep = api.get_deployment(config.namespace, config.injector_deployment)
    if dep is None:
        logger.warning(
            "injector deployment %s not found in namespace %s — treating as NOT ready",
            config.injector_deployment,
            config.namespace,
        )
        return False
    ready = (dep.get("status") or {}).get("readyReplicas") or 0
    return ready >= 1


# ── Controller ───────────────────────────────────────────────────────────────
@dataclass
class ReaperController:
    """Polls memory-server pods and reaps the exit-79 / no-sidecar signature.

    Time and (optionally) sleeping are injected so the loop is fully testable
    without wall-clock waits.
    """

    config: ReaperConfig
    clock: Callable[[], float] = time.monotonic
    _last_reap: dict[str, float] = field(default_factory=dict)

    def _in_cooldown(self, uid: str, now: float) -> bool:
        last = self._last_reap.get(uid)
        if last is None:
            return False
        return (now - last) < self.config.cooldown_seconds

    def run_cycle(self, api: KubeApi) -> int:
        """Run one poll/evaluate/reap cycle. Returns the number of pods deleted.

        Never raises on API errors — a cycle failure is logged and swallowed so
        the reaper itself never crash-loops.
        """
        try:
            # Storm guard FIRST: never reap while the injector is down.
            if not injector_ready(api, self.config):
                logger.info(
                    "vault-agent-injector not ready — skipping reap cycle "
                    "(reaping now would just spawn more exit-79 pods)"
                )
                return 0

            pods = api.list_pods(self.config.namespace, self.config.label_selector)
            return self._reap_pods(api, pods)
        except Exception:  # noqa: BLE001 — controller must survive any cycle error
            logger.exception("reap cycle failed; backing off until next poll")
            return 0

    def _reap_pods(self, api: KubeApi, pods: Iterable[dict[str, Any]]) -> int:
        now = self.clock()
        deleted = 0
        for pod in pods:
            meta = pod.get("metadata", {})
            name = meta.get("name", "<unknown>")
            uid = meta.get("uid", name)

            decision = should_reap(
                pod, memory_server_label_match=_matches_memory_server(pod)
            )
            if not decision.reap:
                logger.debug("skip pod %s: %s", name, decision.reason)
                continue

            if self._in_cooldown(uid, now):
                logger.info(
                    "pod %s matches reap signature but is within cooldown "
                    "(%.0fs) — skipping",
                    name,
                    self.config.cooldown_seconds,
                )
                continue

            logger.warning(
                "reaping pod %s (uid=%s, namespace=%s): %s",
                name,
                uid,
                self.config.namespace,
                decision.reason,
            )
            api.delete_pod(self.config.namespace, name)
            self._last_reap[uid] = now
            deleted += 1
        return deleted

    def run_forever(
        self, api: KubeApi, *, sleep: Callable[[float], None] = time.sleep
    ) -> None:  # pragma: no cover
        """Poll indefinitely. Only reached by the real entrypoint; the
        unit tests drive ``run_cycle`` directly."""
        logger.info(
            "pod-reaper starting: namespace=%s poll=%.0fs cooldown=%.0fs "
            "selector=%r injector=%s",
            self.config.namespace,
            self.config.poll_interval_seconds,
            self.config.cooldown_seconds,
            self.config.label_selector,
            self.config.injector_deployment,
        )
        while True:
            self.run_cycle(api)
            sleep(self.config.poll_interval_seconds)


def main() -> None:  # pragma: no cover — exercised live, not in unit tests
    logging.basicConfig(
        level=os.environ.get("AUDITTRACE_REAPER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = ReaperConfig.from_env()
    controller = ReaperController(config)
    # Context-managed client so the connection pool is always closed on exit.
    with httpx.Client(
        base_url=f"https://{config.kube_api_host}:{config.kube_api_port}",
        headers={"Authorization": f"Bearer {_TOKEN_PATH.read_text().strip()}"},
        verify=str(_CA_PATH),
        timeout=httpx.Timeout(10.0, connect=5.0),
    ) as client:
        api = HttpxKubeApi(client)
        controller.run_forever(api)


if __name__ == "__main__":  # pragma: no cover
    main()
