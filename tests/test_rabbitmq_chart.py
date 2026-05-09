"""Structural tests for the RabbitMQ broker subchart wiring (ADR-057, PR-B2.5).

The Bitnami RabbitMQ subchart provides the broker pod itself; this
chart adds:

- AuthorizationPolicy whitelisting memory-server-sa + content-control
  SA + the topology-bootstrap Job's SA on AMQP port 5672.
- A topology-bootstrap Job (post-install/post-upgrade Helm hook) that
  materialises the four exchanges + four quorum queues + bindings
  + content-control user permissions.
- A content-control user password Secret rendered from
  ``secrets.rabbitmq.contentControlPassword`` (vault.enabled=false
  path).

These tests verify the chart renders the right resources with the
right shape — the load-bearing parts of the AMQP topology contract.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parent.parent / "charts" / "audittrace"


def _render_chart(extra_args: list[str] | None = None) -> list[dict]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm not installed")
    args = [
        helm,
        "template",
        "audittrace",
        str(CHART_DIR),
        "--namespace",
        "audittrace",
        "--set",
        "secrets.summariser.password=dummy-pw-for-render",
        "--set",
        "secrets.minio.secretKey=dummy-minio-secret-for-render",
        "--set",
        "secrets.minio.kmsKey=dummy-minio-kms-for-render",
    ]
    if extra_args:
        args.extend(extra_args)
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _find(resources: list[dict], kind: str, name: str) -> dict:
    for r in resources:
        if r.get("kind") == kind and r.get("metadata", {}).get("name") == name:
            return r
    raise AssertionError(f"{kind}/{name} not found in rendered chart")


def _find_optional(resources: list[dict], kind: str, name: str) -> dict | None:
    for r in resources:
        if r.get("kind") == kind and r.get("metadata", {}).get("name") == name:
            return r
    return None


@pytest.fixture(scope="module")
def rendered() -> list[dict]:
    return _render_chart()


class TestRabbitMQAuthorizationPolicy:
    """Istio AuthorizationPolicy on the broker."""

    def test_authorization_policy_exists(self, rendered: list[dict]) -> None:
        ap = _find(rendered, "AuthorizationPolicy", "audittrace-allow-rabbitmq")
        assert ap["spec"]["selector"]["matchLabels"] == {
            "app.kubernetes.io/name": "rabbitmq"
        }
        assert ap["spec"]["action"] == "ALLOW"

    def test_memory_server_sa_whitelisted(self, rendered: list[dict]) -> None:
        ap = _find(rendered, "AuthorizationPolicy", "audittrace-allow-rabbitmq")
        principals: list[str] = []
        for rule in ap["spec"]["rules"]:
            for src in rule.get("from", []):
                principals.extend(src.get("source", {}).get("principals", []))
        assert any(
            "memory-server" in p or "audittrace-server" in p for p in principals
        ), f"memory-server SA not in principals: {principals}"

    def test_content_control_sa_whitelisted(self, rendered: list[dict]) -> None:
        ap = _find(rendered, "AuthorizationPolicy", "audittrace-allow-rabbitmq")
        principals: list[str] = []
        for rule in ap["spec"]["rules"]:
            for src in rule.get("from", []):
                principals.extend(src.get("source", {}).get("principals", []))
        assert any("audittrace-content-control" in p for p in principals), (
            f"content-control SA not in principals: {principals}"
        )

    def test_amqp_port_5672_allowed(self, rendered: list[dict]) -> None:
        ap = _find(rendered, "AuthorizationPolicy", "audittrace-allow-rabbitmq")
        ports: list[str] = []
        for rule in ap["spec"]["rules"]:
            for to in rule.get("to", []):
                ports.extend(to.get("operation", {}).get("ports", []))
        assert "5672" in ports, f"AMQP port 5672 missing from policy: {ports}"


class TestTopologyBootstrapJob:
    """The post-install Helm hook Job that materialises exchanges + queues."""

    def test_job_is_a_helm_hook(self, rendered: list[dict]) -> None:
        job = _find(rendered, "Job", "audittrace-rabbitmq-topology")
        anns = job["metadata"]["annotations"]
        assert "post-install" in anns["helm.sh/hook"]
        assert "post-upgrade" in anns["helm.sh/hook"]
        # Re-run discipline: previous Job is deleted before the new one
        # is created (so the Job name doesn't collide on upgrade).
        assert "before-hook-creation" in anns["helm.sh/hook-delete-policy"]

    def test_job_uses_dedicated_service_account(self, rendered: list[dict]) -> None:
        job = _find(rendered, "Job", "audittrace-rabbitmq-topology")
        sa = job["spec"]["template"]["spec"]["serviceAccountName"]
        assert sa == "audittrace-rabbitmq-topology"

    def test_job_serviceaccount_exists(self, rendered: list[dict]) -> None:
        sa = _find(rendered, "ServiceAccount", "audittrace-rabbitmq-topology")
        assert sa["metadata"]["labels"]["app.kubernetes.io/component"] == (
            "rabbitmq-topology"
        )

    def test_job_skips_istio_injection(self, rendered: list[dict]) -> None:
        # The bootstrap Job runs once per install; mTLS scoping doesn't
        # buy anything for a one-shot.
        job = _find(rendered, "Job", "audittrace-rabbitmq-topology")
        anns = job["spec"]["template"]["metadata"]["annotations"]
        assert anns.get("sidecar.istio.io/inject") == "false"

    def test_job_command_creates_required_topology(self, rendered: list[dict]) -> None:
        job = _find(rendered, "Job", "audittrace-rabbitmq-topology")
        container = job["spec"]["template"]["spec"]["containers"][0]
        # Command is shell-rendered; just verify all the load-bearing
        # exchange / queue / DLX names appear.
        cmd = " ".join(container["command"])
        for name in (
            "audittrace.scan",
            "audittrace.scan.verdicts",
            "audittrace.scan.audit",
            "audittrace.scan.dlx",
            "audittrace.scan.requests",
            "audittrace.scan.requests.dlq",
        ):
            assert name in cmd, (
                f"AMQP topology element {name!r} missing from Job command"
            )

    def test_job_quorum_queue_args(self, rendered: list[dict]) -> None:
        job = _find(rendered, "Job", "audittrace-rabbitmq-topology")
        container = job["spec"]["template"]["spec"]["containers"][0]
        cmd = " ".join(container["command"])
        assert "x-queue-type" in cmd, "queue type argument missing"
        assert "quorum" in cmd, "queues should be quorum-typed"
        assert "x-dead-letter-exchange" in cmd, "DLX argument missing"
        assert "x-delivery-limit" in cmd, "delivery-limit argument missing"

    def test_job_routing_keys(self, rendered: list[dict]) -> None:
        job = _find(rendered, "Job", "audittrace-rabbitmq-topology")
        container = job["spec"]["template"]["spec"]["containers"][0]
        cmd = " ".join(container["command"])
        for key in ("scan.request.*", "scan.verdict.*", "scan.audit.*"):
            assert key in cmd, f"routing key {key!r} missing from Job"


class TestContentControlUserSecret:
    """The scoped content-control user's password Secret (vault.enabled=false path)."""

    def test_secret_skipped_without_password(self, rendered: list[dict]) -> None:
        # Default values render with empty contentControlPassword; the
        # Secret template is gated on a non-empty value so existing
        # chart-rendering tests (which don't supply it) keep passing.
        secret = _find_optional(
            rendered, "Secret", "audittrace-rabbitmq-content-control"
        )
        assert secret is None

    def test_secret_renders_when_password_supplied(self) -> None:
        rendered_with = _render_chart(
            extra_args=[
                "--set",
                "secrets.rabbitmq.contentControlPassword=test-cc-password",
            ]
        )
        secret = _find(rendered_with, "Secret", "audittrace-rabbitmq-content-control")
        assert secret["type"] == "Opaque"
        assert secret["stringData"]["username"] == "content-control"
        assert secret["stringData"]["password"] == "test-cc-password"


class TestProductDependenciesDocCount:
    """The product-and-dependencies markdown reflects the 9 → 10 update.
    Sanity check at the doc level so a future PR that removes the
    RabbitMQ block surfaces here."""

    def test_doc_mentions_ten_dependencies(self) -> None:
        md = (
            Path(__file__).resolve().parent.parent
            / "docs"
            / "architecture"
            / "product-and-dependencies.md"
        ).read_text()
        assert "ten dependencies" in md.lower()
        assert "AMQP Broker" in md
        assert "ADR-057" in md
