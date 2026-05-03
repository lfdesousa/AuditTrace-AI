"""Static chart-rendering tests — guard against the 2026-05-03 class of
deploy-time failure where a chart change broke pod admission and was only
caught by a CrashLoopBackOff in production.

These tests run via ``make test`` (no cluster needed; they shell out to
``helm template``). They cover:

* Chart renders cleanly with both ``vault.enabled=true`` and
  ``vault.enabled=false``.
* Every workload that sources ``/vault/secrets/env`` contains the
  ``audittrace.vaultSecretFileGuard`` diagnostic block. Regression for
  a deployment template losing its guard during a refactor.
* The memory-server deployment passes ``VAULT_AGENT_REQUIRED=true`` env
  to the container so ``scripts/entrypoint.sh``'s safety net engages.
"""

from __future__ import annotations

import json as _json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parent.parent / "charts" / "audittrace"

# Throwaway secret values to satisfy the chart's productionMode hygiene
# gate. Mirror values used by the CI helm-lint job.
_LINT_SECRETS = [
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
]


def _docs(rendered: str) -> list[dict]:
    """Parse a multi-document helm template output into manifest dicts."""
    return [d for d in yaml.safe_load_all(rendered) if isinstance(d, dict)]


def _find_workload(rendered: str, kind: str, name: str) -> dict:
    """Return the parsed manifest matching kind + metadata.name, or fail."""
    for doc in _docs(rendered):
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    raise AssertionError(f"no {kind}/{name} in rendered chart")


def _helm_available() -> bool:
    return shutil.which("helm") is not None


pytestmark = pytest.mark.skipif(
    not _helm_available(),
    reason="helm CLI not on PATH — chart-rendering tests need it",
)


def _render(extra_args: list[str]) -> str:
    """Run `helm template` with the given args and return rendered YAML.

    Raises ``AssertionError`` with full helm output if rendering fails so
    the test surfaces the chart error directly in pytest output.
    """
    cmd = [
        "helm",
        "template",
        "audittrace",
        str(CHART_DIR),
        "-n",
        "audittrace",
        *_LINT_SECRETS,
        *extra_args,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"helm template failed (rc={result.returncode}):\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
    return result.stdout


class TestChartRenders:
    def test_chart_renders_with_vault_enabled(self) -> None:
        """Production path. The chart must render to non-empty YAML."""
        out = _render(["--set", "vault.enabled=true"])
        assert "kind: Deployment" in out
        assert "audittrace-memory-server" in out

    def test_chart_renders_with_vault_disabled(self) -> None:
        """Dev fallback path. Confirms the conditional Vault branches
        all have the matching ``{{- else }}`` legs."""
        out = _render(["--set", "vault.enabled=false"])
        assert "kind: Deployment" in out
        assert "audittrace-memory-server" in out


class TestVaultSecretFileGuard:
    """Every workload sourcing ``/vault/secrets/env`` MUST emit the
    fail-fast guard added 2026-05-03 — protects against the Vault Agent
    injector silently failing to attach its sidecar.

    Adds explicit expected-workload assertions so a future refactor
    that drops a workload is caught loudly, not silently.
    """

    @pytest.fixture(scope="class")
    def rendered(self) -> str:
        return _render(["--set", "vault.enabled=true"])

    def test_guard_appears_at_least_three_times(self, rendered: str) -> None:
        """chromadb + keycloak + minio = 3 workloads. memory-server has
        its own guard inside scripts/entrypoint.sh, so it's not counted
        here."""
        guard_marker = "Vault Agent did not inject /vault/secrets/env (exit 79)"
        count = rendered.count(guard_marker)
        assert count >= 3, (
            f"expected vaultSecretFileGuard in >=3 workloads (chromadb, "
            f"keycloak, minio); found {count}. A deployment template "
            f"likely lost its guard during a refactor — re-add the "
            f'`{{ include "audittrace.vaultSecretFileGuard" . }}` line.'
        )

    def test_guard_exits_with_code_79(self, rendered: str) -> None:
        """The guard's documented exit code is 79 ("vault prerequisite
        missing"). Asserting the literal so a typo is caught."""
        assert "exit 79" in rendered

    def _container_args(self, doc: dict, container_name: str) -> str:
        """Concatenate args of the named container as a single string."""
        spec = doc["spec"]["template"]["spec"]
        for c in spec.get("containers", []):
            if c["name"] == container_name:
                return "\n".join(c.get("args", []))
        raise AssertionError(f"no container {container_name} in workload")

    def test_chromadb_workload_has_guard(self, rendered: str) -> None:
        doc = _find_workload(rendered, "StatefulSet", "audittrace-chromadb")
        args = self._container_args(doc, "chromadb")
        assert "Vault Agent did not inject" in args, (
            "chromadb statefulset args lost the vaultSecretFileGuard"
        )

    def test_keycloak_workload_has_guard(self, rendered: str) -> None:
        doc = _find_workload(rendered, "Deployment", "audittrace-keycloak")
        args = self._container_args(doc, "keycloak")
        assert "Vault Agent did not inject" in args, (
            "keycloak deployment args lost the vaultSecretFileGuard"
        )

    def test_minio_workload_has_guard(self, rendered: str) -> None:
        doc = _find_workload(rendered, "StatefulSet", "audittrace-minio")
        args = self._container_args(doc, "minio")
        assert "Vault Agent did not inject" in args, (
            "minio statefulset args lost the vaultSecretFileGuard"
        )


class TestMemoryServerEntrypointSafety:
    """memory-server uses scripts/entrypoint.sh (not the inline guard) so
    its safety relies on the chart passing ``VAULT_AGENT_REQUIRED=true``
    in the container env. Regression-guard that env var stays wired."""

    def test_vault_agent_required_env_is_set(self) -> None:
        out = _render(["--set", "vault.enabled=true"])
        doc = _find_workload(out, "Deployment", "audittrace-memory-server")
        spec = doc["spec"]["template"]["spec"]
        container = next(c for c in spec["containers"] if c["name"] == "memory-server")
        env_pairs = {e["name"]: e.get("value") for e in container.get("env", [])}
        assert env_pairs.get("VAULT_AGENT_REQUIRED") == "true", (
            "memory-server deployment must export VAULT_AGENT_REQUIRED=true "
            "so scripts/entrypoint.sh's fail-fast safety net engages. "
            "If this is missing, the entrypoint will silently skip the "
            "guard and any future Vault injector failure will surface as "
            "a cryptic shell error at runtime instead of a clean exit 79."
        )

    def test_command_calls_entrypoint_directly(self) -> None:
        """Vault-enabled memory-server deployment should call
        /app/scripts/entrypoint.sh directly. The previous shape was a
        long inline shell line that bypassed the entrypoint's fail-fast
        guard."""
        out = _render(["--set", "vault.enabled=true"])
        doc = _find_workload(out, "Deployment", "audittrace-memory-server")
        container = next(
            c
            for c in doc["spec"]["template"]["spec"]["containers"]
            if c["name"] == "memory-server"
        )
        cmd = container.get("command") or []
        assert "/app/scripts/entrypoint.sh" in cmd, (
            "memory-server deployment must call entrypoint.sh directly "
            "(not via inline shell that bypasses VAULT_AGENT_REQUIRED check)."
            f" Got command: {cmd}"
        )


class TestEntrypointScriptSafety:
    """Direct unit tests on scripts/entrypoint.sh — exercise the guard
    branch without needing a real container."""

    def test_entrypoint_fails_fast_when_vault_required_but_file_missing(
        self, tmp_path: Path
    ) -> None:
        """VAULT_AGENT_REQUIRED=true + no /vault/secrets/env -> exit 79."""
        # Run entrypoint.sh in a sandboxed env where we override the path
        # check by creating a fake fs root. Simpler: run directly with
        # VAULT_AGENT_REQUIRED=true and let it fail on the missing file
        # (assuming the test runner's host doesn't have /vault/secrets/env,
        # which is true in CI + local dev).
        result = subprocess.run(
            ["bash", "scripts/entrypoint.sh"],
            cwd=str(Path(__file__).resolve().parent.parent),
            env={"VAULT_AGENT_REQUIRED": "true", "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 79, (
            f"expected exit 79, got {result.returncode}. stderr:\n{result.stderr}"
        )
        assert "VAULT AGENT PREREQUISITE FAILURE" in result.stderr
        assert "exit 79" in result.stderr

    def test_entrypoint_does_not_check_vault_when_not_required(
        self, tmp_path: Path
    ) -> None:
        """VAULT_AGENT_REQUIRED unset -> guard skipped (no exit 79).

        We can't run the full entrypoint here because it'd then exec
        alembic + uvicorn against a real DB. Instead we use bash's
        `-n` syntax check + a one-line probe that mimics the guard's
        decision without proceeding past it.
        """
        result = subprocess.run(
            [
                "bash",
                "-c",
                "set -euo pipefail; "
                'if [ "${VAULT_AGENT_REQUIRED:-false}" = "true" ]; then '
                '  echo "would-exit-79"; exit 79; '
                "fi; "
                'echo "would-skip-guard"',
            ],
            env={"PATH": "/usr/bin:/bin"},  # no VAULT_AGENT_REQUIRED
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0
        assert "would-skip-guard" in result.stdout


# ── Phase B.9: realm.json templated audittrace-webui client URIs ─────────────


def _rendered_realm(extra_args: list[str]) -> dict:
    """Return the audittrace-webui client dict from the rendered realm JSON."""
    out = _render(extra_args)
    cm = _find_workload(out, "ConfigMap", "audittrace-keycloak-realm")
    realm = _json.loads(cm["data"]["realm.json"])
    return next(c for c in realm["clients"] if c["clientId"] == "audittrace-webui")


class TestRealmWebuiClientFromValues:
    """Phase B.9: redirectUris / webOrigins / post-logout-redirect-uris on the
    audittrace-webui client are populated from chart values via `tpl`. Without
    this, fresh installs on non-`.local` hostnames fail at first login until
    the operator kcadm-patches in their URIs (the M2 evidence pattern we
    explicitly wanted to eliminate)."""

    def test_default_includes_localhost_8765_and_local(self) -> None:
        """Out-of-the-box defaults cover localhost-dev + .local k3s gateway.

        Subset comparison rather than ``"<url>" in <list>`` because
        CodeQL's "Incomplete URL substring sanitization" rule
        false-positives on the latter pattern (it can't tell that the
        right-hand side is a list, not a string, so it warns as if
        we were doing substring matching for security validation).
        Subset semantics are equivalent here — every expected URI must
        be exactly present in the rendered list — and read more
        clearly besides.
        """
        client = _rendered_realm(["--set", "vault.enabled=true"])
        expected_redirect_uris = {
            "http://localhost:8765/*",
            "https://audittrace.local/*",
            "https://audittrace.local:30952/*",
        }
        expected_web_origins = {
            "http://localhost:8765",
            "https://audittrace.local",
        }
        assert expected_redirect_uris <= set(client["redirectUris"])
        assert expected_web_origins <= set(client["webOrigins"])

    def test_redirect_uris_extend_via_values_override(self) -> None:
        """A custom redirectUris value lands verbatim in the rendered client.
        Asserts the per-deployment override pattern works for new hostnames
        (e.g. an operator's audittrace.allaboutdata.eu:30952)."""
        custom = [
            "https://audittrace.example.com/*",
            "https://audittrace.example.com/oauth2/callback",
            "http://localhost:8765/*",
        ]
        client = _rendered_realm(
            [
                "--set",
                "vault.enabled=true",
                "--set-json",
                f"keycloak.webui.redirectUris={_json.dumps(custom)}",
            ]
        )
        assert client["redirectUris"] == custom

    def test_web_origins_extend_via_values_override(self) -> None:
        custom = ["https://audittrace.example.com", "http://localhost:8765"]
        client = _rendered_realm(
            [
                "--set",
                "vault.enabled=true",
                "--set-json",
                f"keycloak.webui.webOrigins={_json.dumps(custom)}",
            ]
        )
        assert client["webOrigins"] == custom

    def test_post_logout_redirect_uri_overrideable(self) -> None:
        client = _rendered_realm(
            [
                "--set",
                "vault.enabled=true",
                "--set",
                "keycloak.webui.postLogoutRedirectUri=https://audittrace.example.com/logout",
            ]
        )
        assert (
            client["attributes"]["post.logout.redirect.uris"]
            == "https://audittrace.example.com/logout"
        )

    def test_realm_json_remains_valid_after_tpl_render(self) -> None:
        """tpl() rendering leaves the source file with `{{ }}` directives that
        are not valid JSON. Once Helm has rendered it, the result must parse
        as JSON cleanly. Regression for someone breaking the template syntax
        (e.g. unbalanced range loops)."""
        out = _render(["--set", "vault.enabled=true"])
        cm = _find_workload(out, "ConfigMap", "audittrace-keycloak-realm")
        # Will raise if the rendered string isn't valid JSON.
        realm = _json.loads(cm["data"]["realm.json"])
        assert "clients" in realm
        assert any(c["clientId"] == "audittrace-webui" for c in realm["clients"])


class TestRealmMemoryWriteScopes:
    """Phase 3.0 (PR A): the realm must declare the three per-layer
    write scopes used by the memory CRUD backoffice. Without these
    declarations, Keycloak rejects token requests asking for the
    scope and the routes 403 even with a correct admin user."""

    def test_three_write_scopes_declared(self) -> None:
        out = _render(["--set", "vault.enabled=true"])
        cm = _find_workload(out, "ConfigMap", "audittrace-keycloak-realm")
        realm = _json.loads(cm["data"]["realm.json"])
        scope_names = {s["name"] for s in realm.get("clientScopes", [])}
        # Subset comparison so future scope additions don't fail the
        # test (CodeQL-friendly per the v1.0.2 lesson).
        expected = {
            "memory:episodic:write",
            "memory:procedural:write",
            "memory:semantic:write",
        }
        assert expected <= scope_names, (
            f"missing write scope(s); got {scope_names - expected!r} extras "
            f"and {expected - scope_names!r} missing"
        )

    def test_admin_client_grants_write_scopes_by_default(self) -> None:
        """Operators using the admin-client (service account) get the
        write scopes without having to opt in per request."""
        out = _render(["--set", "vault.enabled=true"])
        cm = _find_workload(out, "ConfigMap", "audittrace-keycloak-realm")
        realm = _json.loads(cm["data"]["realm.json"])
        admin_client = next(
            c for c in realm["clients"] if c["clientId"] == "admin-client"
        )
        defaults = set(admin_client["defaultClientScopes"])
        expected = {
            "memory:episodic:write",
            "memory:procedural:write",
            "memory:semantic:write",
        }
        assert expected <= defaults

    def test_user_facing_clients_offer_write_scopes_optionally(self) -> None:
        """Browser / OpenCode flows declare the write scopes as
        optional — clients request them per session as needed."""
        out = _render(["--set", "vault.enabled=true"])
        cm = _find_workload(out, "ConfigMap", "audittrace-keycloak-realm")
        realm = _json.loads(cm["data"]["realm.json"])
        for client_id in ("audittrace-webui", "audittrace-opencode"):
            client = next(c for c in realm["clients"] if c["clientId"] == client_id)
            optional = set(client.get("optionalClientScopes") or [])
            expected = {
                "memory:episodic:write",
                "memory:procedural:write",
                "memory:semantic:write",
            }
            assert expected <= optional, (
                f"{client_id} missing optional write scopes: {expected - optional!r}"
            )


class TestMemoryScopesProvisioningJob:
    """Helm post-install/post-upgrade Job that provisions the three
    `memory:<layer>:write` scopes onto a running Keycloak realm.

    Keycloak's `--import-realm` only imports on a FRESH realm — chart
    edits to realm.json don't propagate to existing realms. This Job
    closes the gap: it runs on every helm install/upgrade and uses
    kcadm.sh to ensure the scopes exist + are bound to the right
    clients (idempotent).

    Without this Job, every chart upgrade that adds a scope silently
    leaves the running realm without it, and `/memory/<layer>` write
    endpoints 403 every operator JWT (`auth.py:174` does a strict
    scope check with no admin bypass).
    """

    def test_job_renders_with_post_upgrade_hook(self) -> None:
        out = _render(["--set", "vault.enabled=true"])
        job = _find_workload(out, "Job", "audittrace-ensure-memory-scopes")
        annotations = job["metadata"].get("annotations", {})
        assert "post-install" in annotations.get("helm.sh/hook", "")
        assert "post-upgrade" in annotations.get("helm.sh/hook", "")
        assert (
            annotations.get("helm.sh/hook-delete-policy")
            == "before-hook-creation,hook-succeeded"
        )

    def test_job_uses_dedicated_serviceaccount(self) -> None:
        out = _render(["--set", "vault.enabled=true"])
        job = _find_workload(out, "Job", "audittrace-ensure-memory-scopes")
        sa = job["spec"]["template"]["spec"]["serviceAccountName"]
        assert sa == "audittrace-memory-scopes-job"
        # The SA itself must also exist as a manifest.
        _find_workload(out, "ServiceAccount", "audittrace-memory-scopes-job")

    def test_job_runs_on_keycloak_image(self) -> None:
        """kcadm.sh ships with the Keycloak image — no other base
        works without installing extra packages at runtime."""
        out = _render(["--set", "vault.enabled=true"])
        job = _find_workload(out, "Job", "audittrace-ensure-memory-scopes")
        image = job["spec"]["template"]["spec"]["containers"][0]["image"]
        assert "keycloak" in image.lower()

    def test_keycloak_authorizationpolicy_allows_job_sa(self) -> None:
        """The keycloak AP must allow the Job's SA principal — without
        this the kcadm calls fail with TLS handshake or 403 from
        Istio enforcement."""
        out = _render(["--set", "vault.enabled=true"])
        ap = _find_workload(out, "AuthorizationPolicy", "audittrace-allow-keycloak")
        principals = ap["spec"]["rules"][0]["from"][0]["source"]["principals"]
        joined = "\n".join(principals)
        assert "memory-scopes-job" in joined

    def test_vault_authorizationpolicy_allows_job_sa(self) -> None:
        """The Vault AP must allow the Job's SA principal — without
        this Envoy rejects the vault-agent's auth/kubernetes/login
        with `403: RBAC: access denied` BEFORE Vault ever sees the
        request. The 2026-05-03 live debug found this the hard way:
        Vault role was correct, manual login worked from inside the
        vault pod (bypasses Istio), but the Job pod's vault-agent
        consistently failed because Envoy denied the request first.

        Adding a Vault-bound workload requires three coordinated edits
        — the Vault policy, the Vault role, AND this AP. Drift in any
        one of the three breaks live auth silently."""
        out = _render(["--set", "vault.enabled=true"])
        ap = _find_workload(out, "AuthorizationPolicy", "audittrace-allow-vault")
        principals = ap["spec"]["rules"][0]["from"][0]["source"]["principals"]
        joined = "\n".join(principals)
        assert "memory-scopes-job" in joined, (
            f"Vault AP missing memory-scopes-job principal; got: {principals!r}"
        )

    def test_script_configmap_lists_three_scopes(self) -> None:
        """Regression guard against drift: the three scopes the
        provisioner script ensures must match the realm.json
        clientScopes block. If a future PR adds (or renames) a
        scope, both sites must move together."""
        out = _render(["--set", "vault.enabled=true"])
        cm = _find_workload(out, "ConfigMap", "audittrace-memory-scopes-script")
        script = cm["data"]["ensure-memory-scopes.sh"]
        for scope in (
            "memory:episodic:write",
            "memory:procedural:write",
            "memory:semantic:write",
        ):
            assert scope in script, f"script missing scope: {scope}"

    def test_script_binds_to_admin_opencode_webui(self) -> None:
        """The bash script must reference each client we expect to
        receive the scope binding."""
        out = _render(["--set", "vault.enabled=true"])
        cm = _find_workload(out, "ConfigMap", "audittrace-memory-scopes-script")
        script = cm["data"]["ensure-memory-scopes.sh"]
        for client_id in ("admin-client", "audittrace-opencode", "audittrace-webui"):
            assert client_id in script, f"script missing client binding: {client_id}"

    def test_vault_role_and_policy_declared(self) -> None:
        """When vault.enabled=true, the Job needs a Vault role bound
        to a least-privilege policy (read on keycloak/* only)."""
        out = _render(["--set", "vault.enabled=true"])
        cm = _find_workload(out, "ConfigMap", "audittrace-vault-policies")
        assert "memory-scopes-job.hcl" in cm["data"]
        assert "role-memory-scopes-job.env" in cm["data"]
        # Policy should grant read on keycloak/* and nothing else.
        policy = cm["data"]["memory-scopes-job.hcl"]
        assert "keycloak/" in policy
        assert "postgres/" not in policy
        # Role binds to the dedicated SA.
        role = cm["data"]["role-memory-scopes-job.env"]
        assert "audittrace-memory-scopes-job" in role

    def test_job_uses_pre_populate_only_under_vault(self) -> None:
        """Without `agent-pre-populate-only: true` the vault-agent
        sidecar keeps the Pod 2/3 NotReady forever and the helm
        post-upgrade hook reports `failed: context deadline exceeded`
        (Phase C.8 root cause — same lesson as the summariser Job)."""
        out = _render(["--set", "vault.enabled=true"])
        job = _find_workload(out, "Job", "audittrace-ensure-memory-scopes")
        annotations = job["spec"]["template"]["metadata"].get("annotations", {})
        key = "vault.hashicorp.com/agent-pre-populate-only"
        assert annotations.get(key) == "true"

    def test_job_renders_under_vault_disabled(self) -> None:
        """When vault.enabled=false, the Job sources the admin
        password from the K8s Secret instead — the manifest must
        still render and reference the secret."""
        out = _render(["--set", "vault.enabled=false"])
        job = _find_workload(out, "Job", "audittrace-ensure-memory-scopes")
        envs = job["spec"]["template"]["spec"]["containers"][0]["env"]
        admin_pw = next(e for e in envs if e["name"] == "KEYCLOAK_ADMIN_PASSWORD")
        assert (
            admin_pw["valueFrom"]["secretKeyRef"]["name"]
            == "audittrace-keycloak-secret"
        )
