"""Drift-guards for the host-Prometheus bridge NodePort Services.

The host-side Prometheus (in the sibling ``observability-stack``
Docker stack) scrapes Kubernetes metrics endpoints via pinned
NodePort numbers — the chart-side templates here MUST emit those
exact ports so the scrape jobs in
``observability-stack/prometheus/prometheus.yml`` stay aligned.

If anyone renames or re-pins these NodePorts, the Prometheus side
silently scrapes the wrong/missing target and dashboards quietly
go blank. These tests catch that drift at chart-build time.

Reserved bridge band: 30900-30999 (OTel uses 30888/30889).

| Service                                       | NodePort | Target |
|-----------------------------------------------|----------|--------|
| audittrace-rabbitmq-metrics-nodeport          | 30919    | rabbitmq pods :9419 |
| audittrace-redis-metrics-nodeport             | 30921    | redis-master pods :9121 |
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "charts" / "audittrace"

_HELM_SET_ARGS = (
    "--set",
    "vault.enabled=false",
    "--set",
    "secrets.minio.secretKey=ci-test",
    "--set",
    "secrets.minio.kmsKey=ci-test",
    "--set",
    "secrets.chromadb.token=ci-test",
    "--set",
    "secrets.keycloak.adminPassword=ci-test",
    "--set",
    "secrets.postgres.appPassword=ci-test",
    "--set",
    "secrets.postgres.password=ci-test",
    "--set",
    "secrets.redis.password=ci-test",
    "--set",
    "secrets.summariser.password=ci-test",
)

EXPECTED_BRIDGES: dict[str, dict] = {
    "audittrace-rabbitmq-metrics-nodeport": {
        "node_port": 30919,
        "service_port": 9419,
        "selector": {
            "app.kubernetes.io/instance": "audittrace",
            "app.kubernetes.io/name": "rabbitmq",
        },
    },
    "audittrace-redis-metrics-nodeport": {
        "node_port": 30921,
        "service_port": 9121,
        "selector": {
            "app.kubernetes.io/instance": "audittrace",
            "app.kubernetes.io/name": "redis",
            "app.kubernetes.io/component": "master",
        },
    },
}


def _helm_template() -> list[dict]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm not installed")
    # S603: helm path is from shutil.which (PATH-resolved, trusted);
    # all other args are static literals. Safe by construction.
    result = subprocess.run(  # noqa: S603
        [
            helm,
            "template",
            "audittrace",
            str(CHART_DIR),
            "--namespace",
            "audittrace",
            *_HELM_SET_ARGS,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _find_service(resources: list[dict], name: str) -> dict:
    for r in resources:
        if r.get("kind") == "Service" and r.get("metadata", {}).get("name") == name:
            return r
    raise AssertionError(f"Service/{name} not rendered by chart")


@pytest.fixture(scope="module")
def rendered() -> list[dict]:
    return _helm_template()


@pytest.mark.parametrize("name", sorted(EXPECTED_BRIDGES.keys()))
class TestNodePortBridge:
    """Each bridge Service renders with the pinned NodePort + selector."""

    def test_service_is_rendered(self, rendered: list[dict], name: str) -> None:
        _find_service(rendered, name)

    def test_service_type_is_nodeport(self, rendered: list[dict], name: str) -> None:
        svc = _find_service(rendered, name)
        assert svc["spec"]["type"] == "NodePort", (
            f"{name} type={svc['spec'].get('type')!r} — must be NodePort "
            "(host-side Prometheus scrapes via host.docker.internal:<NodePort>)"
        )

    def test_node_port_is_pinned(self, rendered: list[dict], name: str) -> None:
        svc = _find_service(rendered, name)
        ports = svc["spec"]["ports"]
        assert len(ports) == 1, f"{name} should expose exactly one port (metrics)"
        expected = EXPECTED_BRIDGES[name]["node_port"]
        actual = ports[0].get("nodePort")
        assert actual == expected, (
            f"{name} nodePort={actual!r} — must be {expected} "
            "(pinned so observability-stack/prometheus.yml stays aligned). "
            "If renumbering is intentional, update both this test AND "
            "observability-stack's scrape config in lockstep."
        )

    def test_service_port_matches_exporter(
        self, rendered: list[dict], name: str
    ) -> None:
        svc = _find_service(rendered, name)
        expected = EXPECTED_BRIDGES[name]["service_port"]
        actual = svc["spec"]["ports"][0].get("port")
        assert actual == expected, (
            f"{name} port={actual!r} — must be {expected} (the exporter's "
            "container port on the selected pod)"
        )

    def test_selector_matches_bitnami_pods(
        self, rendered: list[dict], name: str
    ) -> None:
        svc = _find_service(rendered, name)
        actual = svc["spec"]["selector"]
        expected = EXPECTED_BRIDGES[name]["selector"]
        for k, v in expected.items():
            assert actual.get(k) == v, (
                f"{name} selector[{k}]={actual.get(k)!r} — must be {v!r} "
                "to match the Bitnami sub-chart's pod labels"
            )


class TestRedisMetricsEnabled:
    """The Bitnami redis-exporter sidecar must be enabled, otherwise
    the redis NodePort Service has nothing to forward to."""

    def test_redis_metrics_enabled_in_values(self) -> None:
        values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
        assert values["redis"]["metrics"]["enabled"] is True, (
            "redis.metrics.enabled must be true so the Bitnami "
            "redis-exporter sidecar runs on container port 9121. "
            "Without it, the redis-metrics-nodeport Service has no "
            "backend and Prometheus scrape will return empty."
        )


class TestIstioMetricsPortExclude:
    """The Bitnami pods are Istio-injected (init-container iptables
    redirection). Host-side NodePort scrapes are out-of-mesh, so the
    metrics port MUST be excluded from Istio iptables via the
    ``traffic.sidecar.istio.io/excludeInboundPorts`` annotation —
    otherwise inbound traffic gets redirected to a non-existent /
    unconfigured istio-proxy and the kernel sends a TCP RST after the
    handshake (signature: connection succeeds, then ``recv failure:
    connection reset by peer`` on first HTTP byte).

    Pin EXACTLY the metrics port for each pod — never the whole pod —
    so data/protocol ports (AMQP, Redis protocol, management API)
    keep their mesh protection (mTLS + AuthorizationPolicy).

    Source-of-truth memory: `feedback_istio_metrics_port_exclude`.
    """

    ANNOTATION = "traffic.sidecar.istio.io/excludeInboundPorts"

    def test_rabbitmq_pod_excludes_only_metrics_port(
        self, rendered: list[dict]
    ) -> None:
        sts = next(
            r
            for r in rendered
            if r.get("kind") == "StatefulSet"
            and r.get("metadata", {}).get("name") == "audittrace-rabbitmq"
        )
        annotations = sts["spec"]["template"]["metadata"].get("annotations", {})
        value = annotations.get(self.ANNOTATION)
        assert value == "9419", (
            f"rabbitmq pod annotation {self.ANNOTATION}={value!r} — "
            "must be exactly '9419' (the rabbitmq_prometheus plugin port). "
            "Widening to other ports or removing this annotation would "
            "either break host-side Prometheus scrape (no exclude) or "
            "weaken mTLS on data ports (over-exclude)."
        )

    def test_redis_master_pod_excludes_only_metrics_port(
        self, rendered: list[dict]
    ) -> None:
        sts = next(
            r
            for r in rendered
            if r.get("kind") == "StatefulSet"
            and r.get("metadata", {}).get("name") == "audittrace-redis-master"
        )
        annotations = sts["spec"]["template"]["metadata"].get("annotations", {})
        value = annotations.get(self.ANNOTATION)
        assert value == "9121", (
            f"redis-master pod annotation {self.ANNOTATION}={value!r} — "
            "must be exactly '9121' (the redis-exporter sidecar port). "
            "Widening would weaken mTLS on the Redis protocol port (6379)."
        )


class TestNoUnexpectedBridgeServices:
    """Catch accidental addition of new bridges without test coverage."""

    def test_bridge_service_set_matches_expectations(
        self, rendered: list[dict]
    ) -> None:
        bridge_names = sorted(
            r["metadata"]["name"]
            for r in rendered
            if r.get("kind") == "Service"
            and r.get("metadata", {}).get("name", "").endswith("-metrics-nodeport")
        )
        assert bridge_names == sorted(EXPECTED_BRIDGES.keys()), (
            f"bridge Service set drift — rendered={bridge_names}, "
            f"expected={sorted(EXPECTED_BRIDGES.keys())}. Add the new "
            "Service to EXPECTED_BRIDGES (with its pinned NodePort + "
            "selector) AND to observability-stack/prometheus.yml in "
            "the same PR."
        )
