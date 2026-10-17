"""ADR-062 Phase B — MinIO bucket ↔ IAM policy drift guard.

v1.19.0 shipped WU-B1 (config + storage wiring for the per-user private
tier, ``objectStorage.minio.privateBucket`` = ``memory-private``) without
updating ``templates/minio/job-bucket-init.yaml``'s ``audittrace_app``
policy statement to grant that bucket. The bucket existed; the policy
statement did not. Every per-user private-tier write returned 502
AccessDenied on the live cluster.

This module is a sibling of ``test_chart_drift_guards.py`` (kept separate
per PYTHON-ENGINEERING §11 — that module is already 1500+ LOC) enforcing
exactly one invariant going forward: **every bucket named under**
``values.objectStorage.minio`` **must be referenced by the rendered**
``audittrace_app`` **MinIO IAM policy**. A new bucket added to
``objectStorage.minio.*`` without a matching policy statement fails this
guard immediately, instead of surfacing as a live 502 weeks later.

Falsifiability: ``TestMinioBucketPolicyCheckerFalsifiable`` proves the
comparison logic itself goes RED on a synthetic policy shaped exactly like
the pre-fix bug (every ``memory-shared`` statement present, the
``memory-private`` statement absent) — the checker is not vacuously green.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "charts" / "audittrace"

RELEASE = "audittrace"
NAMESPACE = "audittrace"

# Mirrors tests/test_chart_drift_guards.py's _LINT_SECRETS — throwaway
# values that satisfy every chart-side `required` so the render doesn't
# fail on an unrelated missing secret.
_LINT_SECRETS: list[str] = []
for kv in (
    "secrets.minio.secretKey=preflight",
    "secrets.minio.kmsKey=preflight",
    "secrets.chromadb.token=preflight",
    "secrets.keycloak.adminPassword=preflight",
    "secrets.postgres.appPassword=preflight",
    "secrets.postgres.password=preflight",
    "secrets.redis.password=preflight",
    "secrets.summariser.password=preflight",
):
    _LINT_SECRETS.extend(["--set", kv])


pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm CLI not on PATH — chart-drift tests need it",
)


# ─────────────────────────────────────────────────────────────────────
# Rendering + extraction helpers
# ─────────────────────────────────────────────────────────────────────

# tests/conftest.py transparently injects the four FQDN --set flags
# (externalLLM.host, observability.external.{langfuse,tempo,loki}Host)
# into any `helm template ...` subprocess.run call — this render relies
# on that monkeypatch, same as every helm-shelling test in this suite.


def _render_bucket_init_command(extra_sets: list[str] | None = None) -> str:
    """Render the chart and return the ``mc`` container's shell command
    string from the bucket-init Job (the literal ``|`` block scalar, i.e.
    the actual script MinIO's IAM policy gets materialised from)."""
    cmd = [
        "helm",
        "template",
        RELEASE,
        str(CHART_DIR),
        "-n",
        NAMESPACE,
        "--set",
        "vault.enabled=true",
        "--set",
        "istio.enabled=true",
        "--show-only",
        "templates/minio/job-bucket-init.yaml",
        *_LINT_SECRETS,
        *(extra_sets or []),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"helm template failed (rc={result.returncode}):\n"
            f"--- stderr ---\n{result.stderr}"
        )
    # The command is YAML-block-scalar text, not a separate field we can
    # pluck via yaml.safe_load without losing exact whitespace, so we
    # regex it out of the raw rendered text directly.
    match = re.search(
        r"command:\n\s*- /bin/sh\n\s*- -c\n\s*- \|\n(?P<body>(?:.*\n)+?)(?=^\S|\Z)",
        result.stdout,
        re.MULTILINE,
    )
    if match is None:
        raise AssertionError(
            "Could not locate the mc container's `command: [/bin/sh, -c, |...]` "
            "block in the rendered bucket-init Job — render regression."
        )
    return textwrap.dedent(match.group("body"))


def _extract_audittrace_app_policy(
    command_text: str, *, shared_bucket: str, private_bucket: str
) -> dict:
    """Pull the ``/tmp/audittrace_app.json`` heredoc out of the rendered
    shell script, substitute ``${SHARED_BUCKET}``/``${PRIVATE_BUCKET}``
    with the values actually rendered into the Job's env, and parse it as
    JSON — i.e. reconstruct exactly what ``mc admin policy create`` would
    receive at runtime."""
    match = re.search(
        r"cat >/tmp/audittrace_app\.json <<JSON\n(?P<body>.*?)\n[ \t]*JSON\n",
        command_text,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(
            "Could not locate the `cat >/tmp/audittrace_app.json <<JSON ... JSON` "
            "heredoc in the rendered bucket-init command — render regression "
            "(or the policy materialisation was refactored; update this regex)."
        )
    raw = textwrap.dedent(match.group("body"))
    raw = raw.replace("${SHARED_BUCKET}", shared_bucket).replace(
        "${PRIVATE_BUCKET}", private_bucket
    )
    return json.loads(raw)


def _referenced_bucket_names(policy: dict) -> set[str]:
    """Every distinct bucket name referenced by any ``Resource`` ARN
    across every statement in an S3 IAM policy document (bucket-level
    ARNs like ``arn:aws:s3:::foo`` and object-level ARNs like
    ``arn:aws:s3:::foo/bar/*`` both count — either form proves the
    bucket is in scope of the policy)."""
    names: set[str] = set()
    for stmt in policy.get("Statement", []) or []:
        for arn in stmt.get("Resource", []) or []:
            after_prefix = arn.split(":::", 1)[-1]
            bucket = after_prefix.split("/", 1)[0]
            if bucket:
                names.add(bucket)
    return names


# ─────────────────────────────────────────────────────────────────────
# 1. Live-render integration guard — the real drift guard
# ─────────────────────────────────────────────────────────────────────


class TestMinioBucketPolicyDriftGuard:
    """Renders the ACTUAL chart and asserts every configured
    ``objectStorage.minio.*`` bucket is referenced by the rendered
    ``audittrace_app`` policy. This is the guard that fires the next
    time a bucket is added to values.yaml without a matching policy
    statement — the exact shape of the v1.19.0 memory-private gap.
    """

    def test_default_bucket_names_are_both_granted(self) -> None:
        command = _render_bucket_init_command()
        policy = _extract_audittrace_app_policy(
            command, shared_bucket="memory-shared", private_bucket="memory-private"
        )
        granted = _referenced_bucket_names(policy)
        missing = {"memory-shared", "memory-private"} - granted
        assert not missing, (
            "Drift: the rendered audittrace_app MinIO IAM policy does not "
            f"reference bucket(s) {sorted(missing)} configured under "
            "objectStorage.minio.{sharedBucket,privateBucket} in values.yaml. "
            "This is the exact v1.19.0 gap where memory-private writes "
            "returned 502 AccessDenied — every configured bucket needs a "
            "matching Allow statement in templates/minio/job-bucket-init.yaml. "
            f"Granted: {sorted(granted)}"
        )

    def test_custom_bucket_names_flow_through_to_the_policy(self) -> None:
        """Non-default bucket names, proving the check ties to the
        rendered ``${SHARED_BUCKET}``/``${PRIVATE_BUCKET}`` env
        substitution — not a hardcoded ``memory-shared``/``memory-private``
        string match that would stay green even if the template stopped
        parameterising on Values."""
        shared = "custom-shared-bucket-drift-guard"
        private = "custom-private-bucket-drift-guard"
        command = _render_bucket_init_command(
            extra_sets=[
                "--set",
                f"objectStorage.minio.sharedBucket={shared}",
                "--set",
                f"objectStorage.minio.privateBucket={private}",
            ]
        )
        policy = _extract_audittrace_app_policy(
            command, shared_bucket=shared, private_bucket=private
        )
        granted = _referenced_bucket_names(policy)
        missing = {shared, private} - granted
        assert not missing, (
            f"Custom bucket names {sorted(missing)} did not propagate into "
            "the rendered audittrace_app policy — the policy statement is "
            "not correctly parameterised on objectStorage.minio.* Values."
        )

    def test_content_control_never_granted_memory_private(self) -> None:
        """IAM-split isolation invariant (ADR-048 Decision rule §1,
        reinforced by ADR-062 Phase B): content_control must NEVER gain
        any memory-private access — that would defeat the entire purpose
        of the split (content-control never touches user memory data)."""
        command = _render_bucket_init_command()
        match = re.search(
            r"cat >/tmp/content_control\.json <<JSON\n(?P<body>.*?)\n[ \t]*JSON\n",
            command,
            re.DOTALL,
        )
        assert match is not None, (
            "Could not locate the content_control.json heredoc — render regression."
        )
        raw = textwrap.dedent(match.group("body")).replace(
            "${SHARED_BUCKET}", "memory-shared"
        )
        policy = json.loads(raw)
        granted = _referenced_bucket_names(policy)
        assert "memory-private" not in granted, (
            "Drift: content_control's MinIO policy references memory-private — "
            "this VOIDS the IAM split's isolation guarantee. content_control "
            "must have zero memory-private access."
        )

    def test_private_bucket_mb_block_is_idempotent(self) -> None:
        """The private bucket must be created via the same
        ls-then-mb-if-absent idempotent shape already used for
        SHARED_BUCKET, so repeated helm upgrades stay a no-op."""
        command = _render_bucket_init_command()
        assert 'mc ls "local/${PRIVATE_BUCKET}"' in command, (
            "Drift: no idempotent existence check for PRIVATE_BUCKET before "
            "`mc mb` — re-running the Job against an already-provisioned "
            "MinIO would either error or silently fail to be idempotent."
        )
        assert 'mc mb "local/${PRIVATE_BUCKET}"' in command, (
            "Drift: PRIVATE_BUCKET is never created via `mc mb` — the bucket "
            "would not exist on a fresh install."
        )


# ─────────────────────────────────────────────────────────────────────
# 2. Falsifiability proof — the checker itself must go RED on the
#    pre-fix shape (every memory-shared statement present, no
#    memory-private statement at all).
# ─────────────────────────────────────────────────────────────────────


class TestMinioBucketPolicyCheckerFalsifiable:
    """Proves ``_referenced_bucket_names`` + the missing-set comparison
    used by ``TestMinioBucketPolicyDriftGuard`` is not vacuously green:
    fed a policy document shaped exactly like the pre-fix
    ``audittrace_app.json`` (memory-shared statements only, no
    memory-private statement), it correctly reports memory-private as
    missing."""

    _PRE_FIX_POLICY: dict = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject"],
                "Resource": ["arn:aws:s3:::memory-shared/quarantine/*"],
            },
            {
                "Effect": "Deny",
                "Action": ["s3:GetObject", "s3:DeleteObject"],
                "Resource": ["arn:aws:s3:::memory-shared/quarantine/*"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:ListBucket",
                    "s3:GetBucketLocation",
                ],
                "Resource": [
                    "arn:aws:s3:::memory-shared",
                    "arn:aws:s3:::memory-shared/episodic/*",
                    "arn:aws:s3:::memory-shared/procedural/*",
                    "arn:aws:s3:::memory-shared/trust-store/*",
                    "arn:aws:s3:::memory-shared/assessments/*",
                ],
            },
        ],
    }

    def test_pre_fix_shaped_policy_is_flagged_missing_private_bucket(self) -> None:
        granted = _referenced_bucket_names(self._PRE_FIX_POLICY)
        missing = {"memory-shared", "memory-private"} - granted
        assert missing == {"memory-private"}, (
            "The checker did not detect the missing memory-private grant on "
            "a policy document shaped exactly like the pre-fix bug — the "
            f"drift guard is vacuous. granted={sorted(granted)!r}"
        )

    def test_post_fix_shaped_policy_is_not_flagged(self) -> None:
        """Sanity: appending the memory-private statement (the actual fix
        shape) clears the missing set."""
        fixed = json.loads(json.dumps(self._PRE_FIX_POLICY))  # deep copy
        fixed["Statement"].append(
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:ListBucket",
                    "s3:GetBucketLocation",
                ],
                "Resource": [
                    "arn:aws:s3:::memory-private",
                    "arn:aws:s3:::memory-private/*",
                ],
            }
        )
        granted = _referenced_bucket_names(fixed)
        missing = {"memory-shared", "memory-private"} - granted
        assert not missing
