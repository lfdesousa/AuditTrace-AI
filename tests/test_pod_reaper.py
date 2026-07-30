"""Unit tests for the exit-79 vault-injection reaper (#384 WS4a).

Covers three surfaces:

* ``should_reap`` — the pure reap-decision truth table. This is the safety
  core: it must reap ONLY the exit-79 + missing-vault-sidecar + memory-server
  signature and nothing else.
* ``ReaperController.run_cycle`` — the storm guard (injector-not-ready → zero
  deletes) and the per-pod cooldown, both driven by an in-memory fake k8s API
  (no HTTP, no cluster).
* ``HttpxKubeApi`` — the thin k8s client, exercised against an ``httpx``
  MockTransport so its request shaping / 404 handling is measured.

Plus a falsifiability test: it neuters ``should_reap`` to always-True and
proves the healthy-pod guard test would then FAIL — so the guard tests are
non-vacuous.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from audittrace.ops import pod_reaper
from audittrace.ops.pod_reaper import (
    HttpxKubeApi,
    ReaperConfig,
    ReaperController,
    injector_ready,
    should_reap,
)

# ── Pod fixtures ─────────────────────────────────────────────────────────────
MEMORY_SERVER_LABELS = {"app.kubernetes.io/component": "memory-server"}


def _pod(
    *,
    name: str = "audittrace-memory-server-abc",
    uid: str = "uid-1",
    labels: dict | None = None,
    containers: list[str] | None = None,
    init_containers: list[str] | None = None,
    memory_server_state: dict | None = None,
    memory_server_last_state: dict | None = None,
    deletion_timestamp: str | None = None,
    include_memory_server_status: bool = True,
) -> dict:
    """Build a minimal pod dict shaped like the k8s API response.

    Defaults describe the *reap* case: memory-server label, no vault sidecar,
    the memory-server container terminated with exit 79, not Terminating.
    """
    if containers is None:
        containers = ["memory-server", "istio-proxy"]
    statuses = []
    if include_memory_server_status:
        statuses.append(
            {
                "name": "memory-server",
                "state": memory_server_state
                if memory_server_state is not None
                else {"terminated": {"exitCode": 79}},
                "lastState": memory_server_last_state or {},
            }
        )
    meta: dict = {
        "name": name,
        "uid": uid,
        "labels": MEMORY_SERVER_LABELS if labels is None else labels,
    }
    if deletion_timestamp is not None:
        meta["deletionTimestamp"] = deletion_timestamp
    spec: dict = {"containers": [{"name": c} for c in containers]}
    if init_containers:
        spec["initContainers"] = [{"name": c} for c in init_containers]
    return {"metadata": meta, "spec": spec, "status": {"containerStatuses": statuses}}


def _match(pod: dict) -> bool:
    return pod_reaper._matches_memory_server(pod)


# ── should_reap truth table ──────────────────────────────────────────────────
def test_reap_exit79_no_sidecar_memory_server() -> None:
    """The canonical case: exit-79 + no vault sidecar + memory-server → REAP."""
    pod = _pod()
    decision = should_reap(pod, memory_server_label_match=_match(pod))
    assert decision.reap is True
    assert "injection skipped" in decision.reason


def test_reap_exit79_in_crashloop_history() -> None:
    """CrashLoopBackOff with exit-79 in lastState (history) → REAP."""
    pod = _pod(
        memory_server_state={"waiting": {"reason": "CrashLoopBackOff"}},
        memory_server_last_state={"terminated": {"exitCode": 79}},
    )
    decision = should_reap(pod, memory_server_label_match=_match(pod))
    assert decision.reap is True


def test_skip_exit79_but_sidecar_present() -> None:
    """Exit-79 but the vault-agent sidecar IS present → SKIP (different bug)."""
    pod = _pod(containers=["memory-server", "istio-proxy", "vault-agent"])
    decision = should_reap(pod, memory_server_label_match=_match(pod))
    assert decision.reap is False
    assert "sidecar present" in decision.reason


def test_skip_exit79_but_vault_init_present() -> None:
    """The vault-agent-init container also counts as 'injected' → SKIP."""
    pod = _pod(init_containers=["vault-agent-init"])
    decision = should_reap(pod, memory_server_label_match=_match(pod))
    assert decision.reap is False
    assert "sidecar present" in decision.reason


def test_skip_generic_crash_non79() -> None:
    """A generic crash (exit 3, not 79) → SKIP — never our signature."""
    pod = _pod(memory_server_state={"terminated": {"exitCode": 3}})
    decision = should_reap(pod, memory_server_label_match=_match(pod))
    assert decision.reap is False
    assert "no exit-79 signature" in decision.reason


def test_skip_not_memory_server_label() -> None:
    """A pod without the memory-server component label → SKIP even with 79."""
    pod = _pod(labels={"app.kubernetes.io/component": "chromadb"})
    decision = should_reap(pod, memory_server_label_match=_match(pod))
    assert decision.reap is False
    assert "not a memory-server pod" in decision.reason


def test_skip_already_terminating() -> None:
    """A pod already Terminating (deletionTimestamp set) → SKIP (idempotency)."""
    pod = _pod(deletion_timestamp="2026-07-30T00:00:00Z")
    decision = should_reap(pod, memory_server_label_match=_match(pod))
    assert decision.reap is False
    assert "Terminating" in decision.reason


def test_skip_healthy_running() -> None:
    """A healthy Running memory-server pod → SKIP. THE guard that protects
    live pods from a reap storm."""
    pod = _pod(memory_server_state={"running": {"startedAt": "2026-07-30T00:00:00Z"}})
    decision = should_reap(pod, memory_server_label_match=_match(pod))
    assert decision.reap is False


def test_skip_no_memory_server_container_status() -> None:
    """A memory-server-labelled pod with no memory-server container status yet
    (e.g. still Pending) → SKIP; there is no exit code to key on."""
    pod = _pod(include_memory_server_status=False)
    decision = should_reap(pod, memory_server_label_match=_match(pod))
    assert decision.reap is False
    assert "no exit-79 signature" in decision.reason


# ── Fake k8s API for the controller loop ─────────────────────────────────────
class FakeKubeApi:
    """In-memory ``KubeApi`` — records deletes, returns scripted pods/deployment."""

    def __init__(
        self,
        pods: list[dict],
        *,
        injector_ready_replicas: int | None = 1,
        injector_exists: bool = True,
    ) -> None:
        self._pods = pods
        self._injector_ready_replicas = injector_ready_replicas
        self._injector_exists = injector_exists
        self.deleted: list[str] = []

    def list_pods(self, namespace: str, label_selector: str) -> list[dict]:
        return list(self._pods)

    def get_deployment(self, namespace: str, name: str) -> dict | None:
        if not self._injector_exists:
            return None
        return {"status": {"readyReplicas": self._injector_ready_replicas}}

    def delete_pod(self, namespace: str, name: str) -> None:
        self.deleted.append(name)


def _config(**kw) -> ReaperConfig:
    base = dict(namespace="audittrace", cooldown_seconds=120.0)
    base.update(kw)
    return ReaperConfig(**base)


def test_loop_reaps_matching_pod_when_injector_ready() -> None:
    api = FakeKubeApi([_pod(name="reap-me")])
    controller = ReaperController(_config(), clock=lambda: 100.0)
    deleted = controller.run_cycle(api)
    assert deleted == 1
    assert api.deleted == ["reap-me"]


def test_loop_storm_guard_injector_not_ready_zero_deletes() -> None:
    """Injector reports 0 ready replicas → NO deletes even though a pod matches
    the reap signature. This is the reap-storm safety."""
    api = FakeKubeApi([_pod(name="would-reap")], injector_ready_replicas=0)
    controller = ReaperController(_config(), clock=lambda: 100.0)
    deleted = controller.run_cycle(api)
    assert deleted == 0
    assert api.deleted == []


def test_loop_storm_guard_injector_missing_zero_deletes() -> None:
    """Injector Deployment absent (None) → treated as not ready → zero deletes."""
    api = FakeKubeApi([_pod(name="would-reap")], injector_exists=False)
    controller = ReaperController(_config(), clock=lambda: 100.0)
    assert controller.run_cycle(api) == 0
    assert api.deleted == []


def test_loop_ignores_healthy_pod() -> None:
    """A healthy memory-server pod in the list is never deleted."""
    healthy = _pod(
        name="healthy",
        memory_server_state={"running": {"startedAt": "2026-07-30T00:00:00Z"}},
    )
    api = FakeKubeApi([healthy])
    controller = ReaperController(_config(), clock=lambda: 100.0)
    assert controller.run_cycle(api) == 0
    assert api.deleted == []


def test_loop_cooldown_prevents_second_delete() -> None:
    """Same pod within the cooldown window is not deleted twice."""
    pod = _pod(name="reap-me", uid="uid-cooldown")
    now = {"t": 100.0}
    api = FakeKubeApi([pod])
    controller = ReaperController(
        _config(cooldown_seconds=120.0), clock=lambda: now["t"]
    )

    assert controller.run_cycle(api) == 1  # first: deleted
    now["t"] = 150.0  # 50s later, still inside the 120s cooldown
    assert controller.run_cycle(api) == 0  # second: suppressed
    assert api.deleted == ["reap-me"]

    now["t"] = 300.0  # past the cooldown
    assert controller.run_cycle(api) == 1  # eligible again
    assert api.deleted == ["reap-me", "reap-me"]


def test_loop_survives_api_error() -> None:
    """A cycle that raises inside the API must be swallowed (no crash-loop)."""

    class ExplodingApi(FakeKubeApi):
        def get_deployment(self, namespace: str, name: str) -> dict | None:
            raise httpx.ConnectError("boom")

    api = ExplodingApi([_pod()])
    controller = ReaperController(_config(), clock=lambda: 100.0)
    # No exception propagates; zero deletes.
    assert controller.run_cycle(api) == 0
    assert api.deleted == []


def test_injector_ready_helper_true_and_false() -> None:
    ready = FakeKubeApi([], injector_ready_replicas=2)
    not_ready = FakeKubeApi([], injector_ready_replicas=None)
    assert injector_ready(ready, _config()) is True
    assert injector_ready(not_ready, _config()) is False


# ── HttpxKubeApi over a MockTransport ────────────────────────────────────────
def _mock_client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://kube.test", transport=httpx.MockTransport(handler)
    )


def test_httpx_list_pods_shapes_request() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"items": [{"metadata": {"name": "p1"}}]})

    with _mock_client(handler) as client:
        api = HttpxKubeApi(client)
        pods = api.list_pods("audittrace", "app.kubernetes.io/component=memory-server")

    assert [p["metadata"]["name"] for p in pods] == ["p1"]
    assert "/api/v1/namespaces/audittrace/pods" in seen["url"]
    # The label selector is passed through as a query param (URL-encoded).
    assert "labelSelector=" in seen["url"]
    assert "memory-server" in seen["url"]


def test_httpx_get_deployment_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": {"readyReplicas": 1}})

    with _mock_client(handler) as client:
        dep = HttpxKubeApi(client).get_deployment("audittrace", "inj")
    assert dep is not None
    assert dep["status"]["readyReplicas"] == 1


def test_httpx_get_deployment_404_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    with _mock_client(handler) as client:
        assert HttpxKubeApi(client).get_deployment("audittrace", "missing") is None


def test_httpx_delete_pod_ok() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    with _mock_client(handler) as client:
        HttpxKubeApi(client).delete_pod("audittrace", "pod-x")
    assert seen["method"] == "DELETE"
    assert seen["url"].endswith("/api/v1/namespaces/audittrace/pods/pod-x")


def test_httpx_delete_pod_404_is_tolerated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    with _mock_client(handler) as client:
        # Already-gone pod must not raise.
        HttpxKubeApi(client).delete_pod("audittrace", "already-gone")


def test_httpx_delete_pod_raises_on_500() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    with _mock_client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            HttpxKubeApi(client).delete_pod("audittrace", "server-error")


# ── Config from env ──────────────────────────────────────────────────────────
def test_config_from_env_defaults() -> None:
    cfg = ReaperConfig.from_env({})
    assert cfg.namespace == "audittrace"
    assert cfg.poll_interval_seconds == 15.0
    assert cfg.cooldown_seconds == 120.0
    assert cfg.injector_deployment == "audittrace-vault-agent-injector"


def test_config_from_env_overrides() -> None:
    cfg = ReaperConfig.from_env(
        {
            "AUDITTRACE_REAPER_NAMESPACE": "other",
            "AUDITTRACE_REAPER_POLL_INTERVAL_SECONDS": "5",
            "AUDITTRACE_REAPER_COOLDOWN_SECONDS": "30",
            "AUDITTRACE_REAPER_LABEL_SELECTOR": "x=y",
            "AUDITTRACE_REAPER_INJECTOR_DEPLOYMENT": "custom-injector",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "KUBERNETES_SERVICE_PORT": "6443",
        }
    )
    assert cfg.namespace == "other"
    assert cfg.poll_interval_seconds == 5.0
    assert cfg.cooldown_seconds == 30.0
    assert cfg.label_selector == "x=y"
    assert cfg.injector_deployment == "custom-injector"
    assert cfg.kube_api_host == "10.0.0.1"
    assert cfg.kube_api_port == "6443"


def test_httpx_from_env_requires_in_cluster() -> None:
    """from_env must refuse to run outside a pod (no KUBERNETES_SERVICE_HOST)."""
    cfg = ReaperConfig.from_env({})  # empty → kube_api_host == ""
    with pytest.raises(RuntimeError, match="must run inside a pod"):
        HttpxKubeApi.from_env(cfg)


def test_httpx_from_env_builds_client_in_cluster(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With KUBERNETES_SERVICE_HOST set and the SA token/CA present, from_env
    constructs a working client wired to the Bearer token."""
    import certifi

    token_file = tmp_path / "token"
    token_file.write_text("sa-token-123")
    # A real CA bundle so httpx can build the SSL context; standing in for the
    # projected /var/run/secrets/kubernetes.io/serviceaccount/ca.crt.
    ca_file = Path(certifi.where())
    monkeypatch.setattr(pod_reaper, "_TOKEN_PATH", token_file)
    monkeypatch.setattr(pod_reaper, "_CA_PATH", ca_file)

    cfg = ReaperConfig.from_env(
        {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "KUBERNETES_SERVICE_PORT": "6443"}
    )
    api = HttpxKubeApi.from_env(cfg)
    try:
        assert api._client.headers["Authorization"] == "Bearer sa-token-123"
        assert str(api._client.base_url) == "https://10.0.0.1:6443"
    finally:
        api._client.close()


def test_memory_server_status_none_when_other_container_only() -> None:
    """A pod whose container statuses exist but include NO memory-server
    container yields no exit-79 signature (the loop finds no match)."""
    pod = {
        "metadata": {"name": "p", "uid": "u", "labels": MEMORY_SERVER_LABELS},
        "spec": {"containers": [{"name": "istio-proxy"}]},
        "status": {
            "containerStatuses": [
                {"name": "istio-proxy", "state": {"running": {}}, "lastState": {}}
            ]
        },
    }
    assert pod_reaper._memory_server_status(pod) is None
    decision = should_reap(pod, memory_server_label_match=_match(pod))
    assert decision.reap is False


def test_container_names_skips_entries_without_name() -> None:
    """A container/init entry lacking a name is ignored (no crash, no phantom
    name) — the vault sidecar check stays robust to malformed specs."""
    pod = {
        "spec": {
            "containers": [{}, {"name": "memory-server"}],
            "initContainers": [{"name": "vault-agent-init"}],
        }
    }
    names = pod_reaper._container_names(pod)
    assert names == {"memory-server", "vault-agent-init"}
    assert pod_reaper._has_vault_sidecar(pod) is True


# ── Falsifiability: prove the healthy-pod guard is non-vacuous ────────────────
def test_falsifiability_healthy_guard_bites(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neuter should_reap to always-True and prove the healthy-pod guard then
    FAILS — i.e. the reaper WOULD delete a healthy pod. This demonstrates the
    real guard tests above are load-bearing, not vacuously green."""
    monkeypatch.setattr(
        pod_reaper,
        "should_reap",
        lambda pod, *, memory_server_label_match: pod_reaper.ReapDecision(
            True, "NEUTERED-always-true"
        ),
    )
    healthy = _pod(
        name="healthy",
        memory_server_state={"running": {"startedAt": "2026-07-30T00:00:00Z"}},
    )
    api = FakeKubeApi([healthy])
    controller = ReaperController(_config(), clock=lambda: 100.0)
    deleted = controller.run_cycle(api)
    # With the guard neutered the healthy pod IS deleted — proving the healthy
    # guard test (test_loop_ignores_healthy_pod) would fail here.
    assert deleted == 1
    assert api.deleted == ["healthy"]


def test_falsifiability_storm_guard_bites(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neuter the storm guard (injector_ready → always True) and prove that a
    down-injector cycle then issues deletes — demonstrating the storm-guard
    test (test_loop_storm_guard_*) is non-vacuous."""
    monkeypatch.setattr(pod_reaper, "injector_ready", lambda api, config: True)
    api = FakeKubeApi([_pod(name="would-reap")], injector_ready_replicas=0)
    controller = ReaperController(_config(), clock=lambda: 100.0)
    deleted = controller.run_cycle(api)
    # Storm guard bypassed → the pod is reaped even with the injector down.
    assert deleted == 1
    assert api.deleted == ["would-reap"]
