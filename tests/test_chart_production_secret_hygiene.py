"""Structural tests for the production-secret hygiene gate.

Guards against a trivially common failure mode: an operator copies the
dev Helm values into a production cluster with the test passwords still
in place. The chart's `audittrace.assertProductionSecrets` template
(see `charts/audittrace/templates/_helpers.tpl`) refuses to render in
production mode while known dev defaults are present.

These tests lock the gate so a future edit can't silently relax it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

CHART_DIR = Path(__file__).resolve().parent.parent / "charts" / "audittrace"


def _helm_template(extra_sets: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run `helm template` with the given --set overrides. Does NOT raise
    on non-zero exit (returncode inspected by the caller)."""
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm not installed")
    cmd = [
        helm,
        "template",
        "audittrace",
        str(CHART_DIR),
        "--namespace",
        "audittrace",
        "--set",
        "secrets.summariser.password=dummy-pw-for-render",
        # MinIO secret + KMS key are render-time `required` (see
        # templates/secrets/secret-minio.yaml + ADR-045 PM-1). Tests
        # that explicitly want to observe those flags override them
        # further down via `extra_sets`.
        "--set",
        "secrets.minio.secretKey=dummy-minio-secret-for-render",
        "--set",
        "secrets.minio.kmsKey=dummy-minio-kms-for-render",
    ]
    if extra_sets:
        for kv in extra_sets:
            cmd.extend(["--set", kv])
    return subprocess.run(cmd, capture_output=True, text=True)


def test_production_mode_off_with_dev_defaults_renders_cleanly() -> None:
    """Dev install (the common case) must continue to render OK."""
    # productionMode defaults to false; no overrides needed beyond the
    # summariser password the chart requires for other reasons.
    p = _helm_template()
    assert p.returncode == 0, f"dev-mode render must not fail:\nSTDERR: {p.stderr}"
    # Sanity: at least one resource was produced.
    assert "kind:" in p.stdout


def test_production_mode_on_with_dev_postgres_password_fails() -> None:
    """The gate fires when productionMode=true and postgres password
    still equals the test fixture value."""
    p = _helm_template(
        [
            "global.productionMode=true",
            "secrets.postgres.password=test-pg-pass",
            "secrets.postgres.appPassword=rotated-ok",
            "secrets.chromadb.token=rotated-ok",
            "secrets.redis.password=rotated-ok",
            "secrets.minio.secretKey=rotated-ok",
            "secrets.keycloak.adminPassword=rotated-ok",
            "secrets.summariser.password=rotated-ok",
        ]
    )
    assert p.returncode != 0, "production-mode + dev password must fail render"
    assert "productionMode=true" in p.stderr, p.stderr
    assert "secrets.postgres.password" in p.stderr, p.stderr
    assert "rotate them" in p.stderr.lower() or "rotate" in p.stderr.lower()


def test_production_mode_on_with_multiple_dev_passwords_reports_all() -> None:
    """When multiple creds are left at their dev defaults the gate
    reports the full list, not just the first one encountered. This is
    operator-useful — one fix-up pass rather than whack-a-mole."""
    p = _helm_template(
        [
            "global.productionMode=true",
            # Leaving two dev defaults deliberately.
            "secrets.postgres.password=test-pg-pass",
            "secrets.redis.password=test-redis-pass",
            # Rotate the rest so only the two above fire.
            "secrets.postgres.appPassword=rotated-ok",
            "secrets.chromadb.token=rotated-ok",
            "secrets.minio.secretKey=rotated-ok",
            "secrets.keycloak.adminPassword=rotated-ok",
            "secrets.summariser.password=rotated-ok",
        ]
    )
    assert p.returncode != 0
    assert "secrets.postgres.password" in p.stderr
    assert "secrets.redis.password" in p.stderr


def test_production_mode_on_with_all_rotated_renders_cleanly() -> None:
    """The gate lets a cleanly-configured production deploy through.
    Ensures the template does not over-reject."""
    p = _helm_template(
        [
            "global.productionMode=true",
            "secrets.postgres.password=real-strong-pw-1",
            "secrets.postgres.appPassword=real-strong-pw-2",
            "secrets.chromadb.token=real-strong-token",
            "secrets.redis.password=real-strong-pw-3",
            "secrets.minio.secretKey=real-strong-key",
            "secrets.keycloak.adminPassword=real-admin-pw",
            "secrets.summariser.password=real-strong-pw-4",
        ]
    )
    assert p.returncode == 0, (
        f"rotated-credentials production render must succeed; got stderr:\n{p.stderr}"
    )
