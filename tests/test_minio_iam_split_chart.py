"""Structural tests for the MinIO IAM split (ADR-048 PR-B7).

The bucket-init Job — already shipped in PR-B2 — is extended to:

1. Provision two MinIO IAM policies: ``audittrace_app`` (memory-server)
   and ``content_control`` (the sibling pod). The ``audittrace_app``
   policy carries an explicit ``Deny`` on ``s3:GetObject`` against
   ``quarantine/*`` so the Decision rule §1 invariant is enforced
   at the bucket-policy layer (not just by the Python adapter).
2. Provision two scoped users with the matching policies attached.
3. Read the user passwords from a new ``<release>-minio-iam`` Secret
   rendered by ``templates/secrets/secret-minio-iam.yaml`` (or by
   Vault Agent when ``vault.enabled=true``).

These tests verify the rendered chart actually contains those
pieces — the load-bearing surface of the PR-B7 invariant. Regression
on any of them should fail in CI before a deploy.
"""

from __future__ import annotations

import json
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
        "--set",
        "secrets.minio.audittraceAppPassword=dummy-app-pw",
        "--set",
        "secrets.minio.contentControlPassword=dummy-cc-pw",
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


@pytest.fixture(scope="module")
def bucket_init_script(rendered: list[dict]) -> str:
    """The inline shell script inside the bucket-init Job. The IAM
    split lives entirely in this script (mc admin policy create +
    user add + policy attach), so all PR-B7 assertions run against
    its text."""
    job = _find(rendered, "Job", "audittrace-minio-bucket-init")
    containers = job["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    cmd = containers[0]["command"]
    # command is ["/bin/sh", "-c", "<script>"]
    assert cmd[0] == "/bin/sh"
    assert cmd[1] == "-c"
    return cmd[2]


class TestMinioIamSecret:
    """The k8s Secret carrying the IAM split user credentials."""

    def test_secret_renders_when_passwords_supplied(self, rendered: list[dict]) -> None:
        secret = _find_optional(rendered, "Secret", "audittrace-minio-iam")
        assert secret is not None, (
            "minio-iam Secret should render when audittraceAppPassword "
            "+ contentControlPassword are supplied"
        )

    def test_secret_carries_four_keys(self, rendered: list[dict]) -> None:
        secret = _find(rendered, "Secret", "audittrace-minio-iam")
        keys = set(secret["stringData"].keys())
        assert keys == {
            "audittrace_app_user",
            "audittrace_app_password",
            "content_control_user",
            "content_control_password",
        }

    def test_default_usernames_match_iam_policy_names(
        self, rendered: list[dict]
    ) -> None:
        # The bucket-init Job's `mc admin policy create` calls use
        # the literal policy names "audittrace_app" / "content_control";
        # the Secret's default usernames must match so `mc admin
        # policy attach --user $USER` succeeds.
        secret = _find(rendered, "Secret", "audittrace-minio-iam")
        assert secret["stringData"]["audittrace_app_user"] == "audittrace_app"
        assert secret["stringData"]["content_control_user"] == "content_control"


class TestBucketInitJobConsumesIamSecret:
    """The Job must source the four credential env vars from the
    minio-iam Secret so the inline script has them at run time."""

    def test_job_references_minio_iam_secret(self, rendered: list[dict]) -> None:
        job = _find(rendered, "Job", "audittrace-minio-bucket-init")
        env_list = job["spec"]["template"]["spec"]["containers"][0]["env"]
        names = {e["name"] for e in env_list}
        assert {
            "AUDITTRACE_APP_USER",
            "AUDITTRACE_APP_PASSWORD",
            "CONTENT_CONTROL_USER",
            "CONTENT_CONTROL_PASSWORD",
        }.issubset(names), f"env vars missing: {names}"
        # All four point to the same Secret.
        for env in env_list:
            if env["name"].startswith(("AUDITTRACE_APP_", "CONTENT_CONTROL_")):
                ref = env["valueFrom"]["secretKeyRef"]
                assert ref["name"] == "audittrace-minio-iam"
                assert ref.get("optional") is True


class TestAudittraceAppPolicy:
    """The ``audittrace_app`` policy is the parser-exploit close.
    These tests pin the load-bearing JSON body."""

    def test_policy_create_command_emitted(self, bucket_init_script: str) -> None:
        assert "mc admin policy create local audittrace_app" in bucket_init_script

    def test_explicit_deny_on_get_quarantine(self, bucket_init_script: str) -> None:
        # The DENY statement is the IAM-level enforcement of
        # ADR-048 Decision rule §1. If it disappears from the
        # rendered chart, a future memory-server bug or compromised
        # adapter could read pre-scanned bytes — exactly the gap
        # ADR-048 closes.
        assert '"Effect": "Deny"' in bucket_init_script
        assert "quarantine/*" in bucket_init_script
        # Cross-check structurally: parse the JSON document the
        # script writes to /tmp/audittrace_app.json.
        block = _extract_heredoc(bucket_init_script, "/tmp/audittrace_app.json")
        assert block is not None
        policy = json.loads(block)
        deny_statements = [s for s in policy["Statement"] if s["Effect"] == "Deny"]
        assert len(deny_statements) >= 1
        deny = deny_statements[0]
        assert "s3:GetObject" in deny["Action"]
        assert any("quarantine/*" in r for r in deny["Resource"])

    def test_allows_put_to_quarantine(self, bucket_init_script: str) -> None:
        block = _extract_heredoc(bucket_init_script, "/tmp/audittrace_app.json")
        policy = json.loads(block)
        allow_put = [
            s
            for s in policy["Statement"]
            if s["Effect"] == "Allow" and "s3:PutObject" in s["Action"]
        ]
        assert any("quarantine/*" in r for s in allow_put for r in s["Resource"])


class TestContentControlPolicy:
    """The sibling pod's policy: read+delete quarantine, write
    episodic/papers; explicit deny on procedural / trust-store."""

    def test_policy_create_command_emitted(self, bucket_init_script: str) -> None:
        assert "mc admin policy create local content_control" in bucket_init_script

    def test_allows_get_delete_quarantine_and_put_episodic_papers(
        self, bucket_init_script: str
    ) -> None:
        block = _extract_heredoc(bucket_init_script, "/tmp/content_control.json")
        policy = json.loads(block)
        # Allow GET + DELETE on quarantine.
        allow_quarantine = next(
            s
            for s in policy["Statement"]
            if s["Effect"] == "Allow"
            and "s3:GetObject" in s["Action"]
            and any("quarantine/*" in r for r in s["Resource"])
        )
        assert "s3:DeleteObject" in allow_quarantine["Action"]
        # Allow PUT on episodic/papers/.
        assert any(
            s["Effect"] == "Allow"
            and "s3:PutObject" in s["Action"]
            and any("episodic/papers/*" in r for r in s["Resource"])
            for s in policy["Statement"]
        )

    def test_denies_get_on_procedural_and_trust_store(
        self, bucket_init_script: str
    ) -> None:
        block = _extract_heredoc(bucket_init_script, "/tmp/content_control.json")
        policy = json.loads(block)
        deny = next(s for s in policy["Statement"] if s["Effect"] == "Deny")
        assert "s3:GetObject" in deny["Action"]
        assert any("procedural/*" in r for r in deny["Resource"])
        assert any("trust-store/*" in r for r in deny["Resource"])


class TestUserProvisioning:
    def test_both_users_added(self, bucket_init_script: str) -> None:
        assert 'mc admin user add local "${AUDITTRACE_APP_USER}"' in bucket_init_script
        assert 'mc admin user add local "${CONTENT_CONTROL_USER}"' in bucket_init_script

    def test_both_policies_attached(self, bucket_init_script: str) -> None:
        assert (
            "mc admin policy attach local audittrace_app --user " in bucket_init_script
        )
        assert (
            "mc admin policy attach local content_control --user " in bucket_init_script
        )


def _extract_heredoc(script: str, target_path: str) -> str | None:
    """Pull the JSON body out of ``cat >${target} <<JSON ... JSON``."""
    marker = f"cat >{target_path} <<JSON"
    start = script.find(marker)
    if start == -1:
        return None
    after_marker = script.index("\n", start) + 1
    end = script.index("JSON", after_marker)
    body = script[after_marker:end]
    # The chart indents heredoc content with leading whitespace; strip
    # the common prefix so json.loads sees a flat document.
    lines = body.splitlines()
    stripped = [ln.lstrip() for ln in lines if ln.strip()]
    return "\n".join(stripped)
