"""Structural tests for postgres `audittrace_app` password wiring (Chart-B).

Closes Finding #3 of the PR-B10 chart-hardening plan. Pins the
end-to-end wiring so the bug class can't silently regress:

    1. Render-time Secret  `<release>-postgres-app` carries the
       `secrets.postgres.appPassword` value.
    2. The Bitnami postgresql primary container declares an
       `AUDITTRACE_APP_PASSWORD` env var that pulls from that Secret.
    3. The Secret is gated on `vault.enabled=false` — operators
       using Vault Agent injection get a different path.
    4. The Secret is skipped when `secrets.postgres.appPassword`
       is empty (chart can still render for `helm lint` without
       leaking dev-defaults).

Anchor: project_postgres_app_password_wiring.
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
        "--set",
        "secrets.minio.audittraceAppPassword=dummy-minio-app-pw",
        "--set",
        "secrets.minio.contentControlPassword=dummy-cc-pw",
        "--set",
        "secrets.chromadb.token=dummy-chroma-token",
        "--set",
        "secrets.keycloak.adminPassword=dummy-kc-pw",
        "--set",
        "secrets.postgres.password=dummy-pg-pw",
        "--set",
        "secrets.redis.password=dummy-redis-pw",
    ]
    if extra_args:
        args.extend(extra_args)
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _find_optional(resources: list[dict], kind: str, name: str) -> dict | None:
    for r in resources:
        if r.get("kind") == kind and r.get("metadata", {}).get("name") == name:
            return r
    return None


def _find(resources: list[dict], kind: str, name: str) -> dict:
    r = _find_optional(resources, kind, name)
    if r is None:
        raise AssertionError(f"{kind}/{name} not found in rendered chart")
    return r


class TestSecretRendering:
    """secret-postgres-app.yaml — render-time gating."""

    def test_renders_when_vault_disabled_and_password_set(self) -> None:
        rendered = _render_chart(
            ["--set", "secrets.postgres.appPassword=app-pw-explicit-different"]
        )
        secret = _find_optional(rendered, "Secret", "audittrace-postgres-app")
        assert secret is not None, (
            "secret-postgres-app should render when vault.enabled=false "
            "AND secrets.postgres.appPassword is non-empty"
        )
        assert secret["stringData"]["password"] == "app-pw-explicit-different"

    def test_skipped_when_vault_enabled(self) -> None:
        rendered = _render_chart(
            [
                "--set",
                "vault.enabled=true",
                "--set",
                "secrets.postgres.appPassword=app-pw-explicit-different",
            ]
        )
        secret = _find_optional(rendered, "Secret", "audittrace-postgres-app")
        assert secret is None, (
            "secret-postgres-app must NOT render under vault.enabled=true — "
            "the Vault Agent path supplies AUDITTRACE_APP_PASSWORD elsewhere"
        )

    def test_skipped_when_app_password_empty(self) -> None:
        # secrets.postgres.appPassword unset (only --set'd values from
        # _render_chart() are present; appPassword defaults to "").
        rendered = _render_chart()
        secret = _find_optional(rendered, "Secret", "audittrace-postgres-app")
        assert secret is None, (
            "secret-postgres-app must NOT render when appPassword is empty "
            "— gating prevents leaking an empty-string credential"
        )


class TestStatefulSetInjection:
    """Bitnami postgresql primary container must consume the Secret."""

    def _postgres_env(self, rendered: list[dict]) -> list[dict]:
        sts = _find(rendered, "StatefulSet", "audittrace-postgresql")
        containers = sts["spec"]["template"]["spec"]["containers"]
        # The primary container is named "postgresql".
        primary = next(c for c in containers if c["name"] == "postgresql")
        return primary["env"]

    def test_env_var_injects_from_secret(self) -> None:
        rendered = _render_chart(
            ["--set", "secrets.postgres.appPassword=app-pw-explicit"]
        )
        env = self._postgres_env(rendered)
        names = {e["name"] for e in env}
        assert "AUDITTRACE_APP_PASSWORD" in names, (
            "postgresql primary must inject AUDITTRACE_APP_PASSWORD so the "
            "initdb script's init-audittrace-app-role.sh creates the "
            "audittrace_app role with the same password memory-server uses"
        )
        entry = next(e for e in env if e["name"] == "AUDITTRACE_APP_PASSWORD")
        ref = entry["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "audittrace-postgres-app"
        assert ref["key"] == "password"

    def test_secret_key_ref_is_optional(self) -> None:
        # `optional: true` is load-bearing for the vault.enabled=true
        # path: when the Secret is not rendered, kubelet must NOT
        # block pod startup. The init script's `${AUDITTRACE_APP_PASSWORD:-…}`
        # fallback takes over (operators wire Vault Agent separately).
        rendered = _render_chart(
            ["--set", "secrets.postgres.appPassword=app-pw-explicit"]
        )
        env = self._postgres_env(rendered)
        entry = next(e for e in env if e["name"] == "AUDITTRACE_APP_PASSWORD")
        assert entry["valueFrom"]["secretKeyRef"].get("optional") is True


class TestPasswordIndependenceFromPostgresPassword:
    """The whole point of Chart-B: secrets.postgres.appPassword and
    secrets.postgres.password are now genuinely independent values.
    Memory-server's connect URL (postgresAppUrl helper) uses appPassword;
    the postgres init script uses the same — so they MUST be wired to
    the same Secret value at render time."""

    def test_app_password_flows_to_postgres_secret(self) -> None:
        rendered = _render_chart(
            [
                "--set",
                "secrets.postgres.appPassword=app-pw-alpha",
                "--set",
                "secrets.postgres.password=super-pw-omega",
            ]
        )
        secret = _find(rendered, "Secret", "audittrace-postgres-app")
        # The Secret consumed by postgresql carries appPassword, NOT
        # the superuser password. Before Chart-B these two diverging
        # would have silently corrupted the audittrace_app role's
        # password (kept the postgres-side value, broke memory-server
        # which used the app-side value).
        assert secret["stringData"]["password"] == "app-pw-alpha"
        assert secret["stringData"]["password"] != "super-pw-omega"
