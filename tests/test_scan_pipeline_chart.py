"""Drift-guard tests for ADR-048 PR-B8 chart-flip wiring.

Closes the three chart-side gaps the EOD audit on 2026-05-10 found:

1. ``charts/audittrace/values.yaml`` ``memoryServer.env`` carries the
   seven AUDITTRACE_SCAN_* settings the lifespan reads. Without them
   the publisher / janitor / verdict consumer / audit consumer tasks
   never schedule.
2. ``AUDITTRACE_SCAN_AMQP_URL`` is wired in the deployment template:
   - ``vault.enabled=false`` branch — built via ``$(VAR)`` expansion
     from ``AUDITTRACE_RABBITMQ_PASSWORD`` (which itself sources from
     the Bitnami subchart's ``<release>-rabbitmq`` Secret). Order
     matters: PASSWORD must precede URL.
   - ``vault.enabled=true`` branch — exported by Vault Agent via the
     ``vaultAnnotations.memoryServer`` template using
     ``kv/audittrace/rabbitmq/admin``.
3. Memory-server's MinIO ACCESS + SECRET keys come from the IAM-split
   ``audittrace_app`` user, not the root user. The ``audittrace_app``
   user has an explicit DENY on s3:GetObject quarantine/* — the
   parser-exploit close from ADR-048 Decision rule §1. Without
   switching memory-server's identity, that DENY is provisioned but
   never tested.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parent.parent / "charts" / "audittrace"

# The 7 SCAN_* env settings that must land in memoryServer.env. The
# 8th piece (AUDITTRACE_SCAN_AMQP_URL) is constructed in the
# deployment template / Vault Agent, not in values.yaml.
EXPECTED_SCAN_ENV = {
    "AUDITTRACE_SCAN_PIPELINE_ENABLED": "true",
    "AUDITTRACE_SCAN_REQUEST_EXCHANGE": "audittrace.scan",
    "AUDITTRACE_SCAN_REQUEST_ROUTING_KEY": "scan.requested",
    "AUDITTRACE_SCAN_QUARANTINE_PREFIX": "quarantine",
    "AUDITTRACE_SCAN_PUBLISHER_DRAIN_INTERVAL_MS": "100",
    "AUDITTRACE_SCAN_JANITOR_INTERVAL_SECONDS": "30",
    "AUDITTRACE_SCAN_JANITOR_GRACE_SECONDS": "60",
}


def _render(extra_args: list[str] | None = None) -> list[dict]:
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
        "secrets.summariser.password=dummy-pw",
        "--set",
        "secrets.minio.secretKey=dummy-mk",
        "--set",
        "secrets.minio.kmsKey=dummy-kms",
        "--set",
        "secrets.minio.audittraceAppPassword=dummy-app",
        "--set",
        "secrets.minio.contentControlPassword=dummy-cc",
        "--set",
        "secrets.rabbitmq.password=dummy-rmq",
        "--set",
        "secrets.rabbitmq.erlangCookie=dummy-cookie",
        "--set",
        "secrets.rabbitmq.contentControlPassword=dummy-rcc",
    ]
    if extra_args:
        args.extend(extra_args)
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _find(resources: list[dict], kind: str, name: str) -> dict:
    for r in resources:
        if r.get("kind") == kind and r.get("metadata", {}).get("name") == name:
            return r
    raise AssertionError(f"{kind}/{name} not found")


@pytest.fixture(scope="module")
def rendered_no_vault() -> list[dict]:
    return _render()


@pytest.fixture(scope="module")
def rendered_with_vault() -> list[dict]:
    return _render(["--set", "vault.enabled=true"])


def _memory_server_env(resources: list[dict]) -> list[dict]:
    dep = _find(resources, "Deployment", "audittrace-memory-server")
    return dep["spec"]["template"]["spec"]["containers"][0].get("env", [])


def _bucket_init_job(resources: list[dict]) -> dict:
    return _find(resources, "Job", "audittrace-minio-bucket-init")


class TestScanEnvInValuesYaml:
    """The 7 AUDITTRACE_SCAN_* settings must reach memory-server's pod
    via the env block — independent of vault.enabled. Without them the
    scan pipeline tasks never schedule."""

    def test_all_seven_scan_env_vars_present(
        self, rendered_no_vault: list[dict]
    ) -> None:
        env_by_name = {e["name"]: e for e in _memory_server_env(rendered_no_vault)}
        for name, expected in EXPECTED_SCAN_ENV.items():
            assert name in env_by_name, f"missing {name}"
            assert env_by_name[name].get("value") == expected, (
                f"{name}: expected '{expected}', got '{env_by_name[name].get('value')}'"
            )

    def test_scan_pipeline_enabled_in_vault_branch_too(
        self, rendered_with_vault: list[dict]
    ) -> None:
        env_by_name = {e["name"]: e for e in _memory_server_env(rendered_with_vault)}
        assert env_by_name["AUDITTRACE_SCAN_PIPELINE_ENABLED"]["value"] == "true"


class TestMemServerMinioIdentityIsAudittraceApp:
    """Memory-server's MinIO ACCESS + SECRET keys must source from the
    audittrace_app IAM user (not root). vault.enabled=false branch uses
    secretKeyRef into the audittrace-minio-iam Secret; vault.enabled=true
    branch comes from Vault Agent (asserted in TestVaultAgentSourcesNewPaths).
    """

    def test_access_key_from_iam_split_secret_no_vault(
        self, rendered_no_vault: list[dict]
    ) -> None:
        env_by_name = {e["name"]: e for e in _memory_server_env(rendered_no_vault)}
        access = env_by_name["AUDITTRACE_MINIO_ACCESS_KEY"]
        ref = access["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "audittrace-minio-iam"
        assert ref["key"] == "audittrace_app_user"

    def test_secret_key_from_iam_split_secret_no_vault(
        self, rendered_no_vault: list[dict]
    ) -> None:
        env_by_name = {e["name"]: e for e in _memory_server_env(rendered_no_vault)}
        secret = env_by_name["AUDITTRACE_MINIO_SECRET_KEY"]
        ref = secret["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "audittrace-minio-iam"
        assert ref["key"] == "audittrace_app_password"

    def test_no_hardcoded_minioadmin_anywhere_in_memory_server(
        self, rendered_no_vault: list[dict]
    ) -> None:
        # Regression guard: the previous chart wired
        # AUDITTRACE_MINIO_ACCESS_KEY: "minioadmin" unconditionally.
        # PR-B8 removes that — memory-server should never appear with
        # the literal string "minioadmin" in any env entry.
        env = _memory_server_env(rendered_no_vault)
        for entry in env:
            assert entry.get("value") != "minioadmin", (
                f"memory-server still hardcodes minioadmin at {entry['name']}"
            )

    def test_vault_branch_omits_explicit_minio_env(
        self, rendered_with_vault: list[dict]
    ) -> None:
        # When Vault Agent is the credential source, ACCESS/SECRET keys
        # MUST NOT appear in the k8s env block — otherwise the entrypoint's
        # `set -a; . /vault/secrets/env; set +a` ordering would allow the
        # k8s value to be overridden silently. Cleaner: omit and let
        # Vault Agent be the only source.
        env_names = {e["name"] for e in _memory_server_env(rendered_with_vault)}
        assert "AUDITTRACE_MINIO_ACCESS_KEY" not in env_names
        assert "AUDITTRACE_MINIO_SECRET_KEY" not in env_names


class TestRabbitmqAmqpUrlWiring:
    """The AMQP URL is the contract memory-server's scan-request publisher
    reads. Sourced unconditionally from the Bitnami subchart's
    `<release>-rabbitmq` Secret via $(VAR) expansion — Vault is
    intentionally NOT in this path (would force a policy upload on
    every chart upgrade for no real benefit; the Bitnami Secret is
    already the source of truth)."""

    def _assert_amqp_wiring(self, env: list[dict]) -> None:
        env_by_name = {e["name"]: e for e in env}
        names = [e["name"] for e in env]
        # k8s env $(VAR) expansion only resolves variables declared
        # earlier in the list; if the order is wrong the URL becomes
        # the literal string "$(AUDITTRACE_RABBITMQ_PASSWORD)".
        assert "AUDITTRACE_RABBITMQ_PASSWORD" in names
        assert "AUDITTRACE_SCAN_AMQP_URL" in names
        assert names.index("AUDITTRACE_RABBITMQ_PASSWORD") < names.index(
            "AUDITTRACE_SCAN_AMQP_URL"
        )
        # Bitnami subchart auto-creates this Secret with a
        # rabbitmq-password key; the chart MUST NOT introduce a
        # parallel secret that drifts.
        ref = env_by_name["AUDITTRACE_RABBITMQ_PASSWORD"]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "audittrace-rabbitmq"
        assert ref["key"] == "rabbitmq-password"
        url = env_by_name["AUDITTRACE_SCAN_AMQP_URL"]["value"]
        assert url.startswith("amqp://")
        assert "$(AUDITTRACE_RABBITMQ_PASSWORD)" in url, (
            "AMQP URL must reference the password via $(VAR) expansion "
            "(not bake it inline) so the rendered manifest stays clean"
        )
        assert "@audittrace-rabbitmq:5672" in url

    def test_amqp_wiring_no_vault(self, rendered_no_vault: list[dict]) -> None:
        self._assert_amqp_wiring(_memory_server_env(rendered_no_vault))

    def test_amqp_wiring_with_vault(self, rendered_with_vault: list[dict]) -> None:
        # Same wiring under both branches — the AMQP URL is intentionally
        # outside the vault.enabled gate.
        self._assert_amqp_wiring(_memory_server_env(rendered_with_vault))


class TestVaultAgentSourcesNewPaths:
    """vault.enabled=true: the memoryServer Vault Agent template MUST
    fetch from kv/audittrace/minio/audittrace_app and
    kv/audittrace/rabbitmq/admin. Otherwise the IAM split + AMQP URL
    are silently broken."""

    def test_template_includes_audittrace_app_path(
        self, rendered_with_vault: list[dict]
    ) -> None:
        dep = _find(rendered_with_vault, "Deployment", "audittrace-memory-server")
        anns = dep["spec"]["template"]["metadata"].get("annotations", {})
        template = anns.get("vault.hashicorp.com/agent-inject-template-env", "")
        assert "kv/data/audittrace/minio/audittrace_app" in template
        assert "AUDITTRACE_MINIO_ACCESS_KEY" in template
        assert "AUDITTRACE_MINIO_SECRET_KEY" in template

    def test_template_does_not_include_rabbitmq_secret_block(
        self, rendered_with_vault: list[dict]
    ) -> None:
        # AMQP URL is sourced via plain secretKeyRef + $(VAR) expansion
        # from the Bitnami `<release>-rabbitmq` Secret — Vault is NOT
        # in the path (would force a policy upload on every chart
        # upgrade). Guard against drift: ensure no Vault-Agent
        # `{{ with secret "kv/.../rabbitmq/..." }}` block exists, and
        # that the AMQP URL is not exported by Vault Agent. (Comments
        # mentioning rabbitmq are fine — only directives matter.)
        dep = _find(rendered_with_vault, "Deployment", "audittrace-memory-server")
        anns = dep["spec"]["template"]["metadata"].get("annotations", {})
        template = anns.get("vault.hashicorp.com/agent-inject-template-env", "")
        assert 'with secret "kv/data/audittrace/rabbitmq' not in template
        assert "export AUDITTRACE_SCAN_AMQP_URL" not in template
        assert "export AUDITTRACE_RABBITMQ_PASSWORD" not in template

    def test_template_no_longer_uses_minio_root(
        self, rendered_with_vault: list[dict]
    ) -> None:
        # PR-B8 swap: memory-server stops sourcing MinIO root creds
        # and uses the audittrace_app scoped user instead. If the
        # template regresses to minio/root, the IAM split is moot.
        dep = _find(rendered_with_vault, "Deployment", "audittrace-memory-server")
        anns = dep["spec"]["template"]["metadata"].get("annotations", {})
        template = anns.get("vault.hashicorp.com/agent-inject-template-env", "")
        assert "kv/data/audittrace/minio/root" not in template


class TestBucketInitJobIamSecretRefs:
    """PR-B8: bucket-init Job reads IAM creds via plain secretKeyRef
    (not Vault Agent). Earlier prototype used Vault Agent but the Job
    can't carry an Istio sidecar cleanly (no graceful exit), and
    going around Istio breaks Vault's mTLS posture. The k8s Secret
    is the simpler source of truth; memory-server still reads from
    Vault for its own credentials."""

    def test_no_vault_agent_annotations(self, rendered_with_vault: list[dict]) -> None:
        # Vault Agent injection MUST be absent from the bucket-init
        # Job — IAM creds come from k8s Secret, not Vault.
        job = _bucket_init_job(rendered_with_vault)
        anns = job["spec"]["template"]["metadata"].get("annotations", {})
        assert "vault.hashicorp.com/agent-inject" not in anns

    def test_istio_sidecar_enabled_when_istio_on(
        self, rendered_with_vault: list[dict]
    ) -> None:
        # PR-B8: Istio sidecar required because the cluster's
        # PeerAuthentication is STRICT mTLS and MinIO's
        # AuthorizationPolicy denies any non-mTLS caller. Without
        # this annotation the Job's mc commands silently fail (mTLS
        # handshake rejected → mc alias set fails → mc mb falls back
        # to the default `local` alias = localhost:9000 = connection
        # refused). 2026-05-10 incident anchor.
        job = _bucket_init_job(rendered_with_vault)
        anns = job["spec"]["template"]["metadata"].get("annotations", {})
        assert anns.get("sidecar.istio.io/inject") == "true"
        assert "holdApplicationUntilProxyStarts" in anns.get(
            "proxy.istio.io/config", ""
        ), (
            "must hold mc container until istio-proxy is up — otherwise "
            "the very first mc admin call races the sidecar and 503s"
        )

    def test_uses_dedicated_bucket_init_serviceaccount(
        self, rendered_with_vault: list[dict]
    ) -> None:
        # PR-B8: dedicated SA so the principal scope stays tight.
        job = _bucket_init_job(rendered_with_vault)
        assert (
            job["spec"]["template"]["spec"].get("serviceAccountName")
            == "audittrace-bucket-init"
        )

    def test_serviceaccount_resource_renders(
        self, rendered_with_vault: list[dict]
    ) -> None:
        sa = next(
            (
                r
                for r in rendered_with_vault
                if r.get("kind") == "ServiceAccount"
                and r.get("metadata", {}).get("name") == "audittrace-bucket-init"
            ),
            None,
        )
        assert sa is not None, "ServiceAccount audittrace-bucket-init must render"

    def test_authorizationpolicy_includes_bucket_init_principal(
        self, rendered_with_vault: list[dict]
    ) -> None:
        # The MinIO AuthorizationPolicy MUST allow the bucket-init SA;
        # otherwise STRICT mTLS rejects every mc call.
        ap = _find(rendered_with_vault, "AuthorizationPolicy", "audittrace-allow-minio")
        principals: list[str] = []
        for rule in ap["spec"]["rules"]:
            for source in rule.get("from", []):
                principals.extend(source.get("source", {}).get("principals", []))
        assert "cluster.local/ns/audittrace/sa/audittrace-bucket-init" in principals, (
            f"principals: {principals}"
        )

    def test_command_calls_quitquitquit(self, rendered_with_vault: list[dict]) -> None:
        # Standard Istio-on-Job pattern: tell the sidecar to exit so
        # the Job pod transitions Completed (without this it hangs
        # forever — istio-proxy never exits on its own).
        job = _bucket_init_job(rendered_with_vault)
        script = job["spec"]["template"]["spec"]["containers"][0]["command"][2]
        assert "/quitquitquit" in script
        # Endpoint is on the localhost pilot-agent port 15020.
        assert "localhost:15020" in script

    def test_iam_secret_refs_present_under_both_branches(
        self, rendered_no_vault: list[dict], rendered_with_vault: list[dict]
    ) -> None:
        # The audittrace-minio-iam Secret renders unconditionally now;
        # the Job must reference all 4 keys via optional secretKeyRef
        # in both branches.
        for resources in (rendered_no_vault, rendered_with_vault):
            job = _bucket_init_job(resources)
            env = job["spec"]["template"]["spec"]["containers"][0]["env"]
            iam_envs = {
                e["name"]: e
                for e in env
                if e["name"]
                in (
                    "AUDITTRACE_APP_USER",
                    "AUDITTRACE_APP_PASSWORD",
                    "CONTENT_CONTROL_USER",
                    "CONTENT_CONTROL_PASSWORD",
                )
            }
            assert len(iam_envs) == 4
            for entry in iam_envs.values():
                ref = entry["valueFrom"]["secretKeyRef"]
                assert ref["name"] == "audittrace-minio-iam"
                assert ref.get("optional") is True

    def test_minio_iam_secret_renders_under_both_branches(
        self, rendered_no_vault: list[dict], rendered_with_vault: list[dict]
    ) -> None:
        # PR-B8 — drop the (not vault.enabled) gate. Both branches
        # render the same Secret, populated from operator
        # --set-file values.
        for resources in (rendered_no_vault, rendered_with_vault):
            secret = next(
                (
                    r
                    for r in resources
                    if r.get("kind") == "Secret"
                    and r.get("metadata", {}).get("name") == "audittrace-minio-iam"
                ),
                None,
            )
            assert secret is not None, (
                "audittrace-minio-iam Secret must render under both vault.enabled values"
            )
            assert set(secret["stringData"].keys()) == {
                "audittrace_app_user",
                "audittrace_app_password",
                "content_control_user",
                "content_control_password",
            }
