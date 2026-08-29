"""M3-WU-3b — the LibreChat console on k3s (Helm) guards.

Rule 1 (spec Acceptance §1) of the ratified spec
(specs/2026-08-29-SPEC-m3-wu3b-librechat-k3s-oidc-forwarding.md, private
repo): a drift guard asserts the token-forwarding header +
``OPENID_REUSE_TOKENS`` + ``offline_access`` in scope + B4-off +
registration-disabled + librechat->BFF-only routing + the realm
``offline_access``/``profile``/``email`` on ``audittrace-librechat``; neuter
each -> RED.

Hermetic: shells out to ``helm template`` (no live cluster) and parses the
rendered manifests as YAML — same discipline as
``tests/test_chart_drift_guards.py``. The realm-scope half of this list
(``offline_access``/``profile``/``email`` on ``audittrace-librechat``) is
already covered, non-duplicated, by
``tests/test_chart_drift_guards.py::TestLibrechatConsoleClient`` (updated in
this same work unit) — not re-asserted here.

Drift classes covered:

* **D2 — the token-forwarding config (the paste-killer).** The librechat.yaml
  ConfigMap's custom endpoint carries the ``{{LIBRECHAT_OPENID_ACCESS_TOKEN}}``
  header placeholder, ``apiKey: 'dummy'`` (never ``user_provided`` — that was
  the WU-3 compose deviation this work unit supersedes), and LibreChat's own
  Deployment env carries ``OPENID_REUSE_TOKENS=true`` + ``offline_access`` in
  ``OPENID_SCOPE``.
* **B4 — cloud features off.** ``ENDPOINTS=custom``, no provider/search/
  speech API-key env vars anywhere in the LibreChat Deployment, no
  ``webSearch``/``speech`` block in librechat.yaml.
* **Registration disabled.** ``ALLOW_REGISTRATION=false`` +
  ``ALLOW_EMAIL_LOGIN=false``.
* **D4 — LibreChat reaches the BFF ONLY (structural, not just config).**
  The custom endpoint's ``baseURL`` targets the in-cluster BFF Service only;
  the ``allow-librechat-bff`` AuthorizationPolicy's only source principal is
  LibreChat's own ServiceAccount; the ``allow-memory``/``allow-keycloak``
  AuthorizationPolicies whitelist the BFF's ServiceAccount (never
  LibreChat's) for in-mesh /v1 and Keycloak access.
* **D1 — the chart is genuinely optional.** ``console.enabled=false``
  (the default) renders NO console resources and never breaks the core
  chart render; ``console.enabled=true`` renders cleanly under BOTH
  ``vault.enabled=true`` and ``vault.enabled=false``.
* **D4 — the BFF secret is Vault-sourced when vault.enabled=true**, never a
  plaintext env var alongside a live Vault Agent annotation.
* **D3 — MongoDB carries no external port.**

Anchors: ``feedback_no_more_drifts``, ``feedback_vacuous_neuter_test_antipattern``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "charts" / "audittrace"

RELEASE = "audittrace"
NAMESPACE = "audittrace"

# Satisfies every chart-side `required` so a bare console.enabled=true
# render doesn't fail on unrelated required fields (mirrors
# tests/test_chart_drift_guards.py's `_LINT_SECRETS`).
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

_PROVIDER_AND_FEATURE_ENV_VARS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_KEY",
    "GOOGLE_SEARCH_API_KEY",
    "AZURE_API_KEY",
    "BEDROCK_AWS_ACCESS_KEY_ID",
    "SERPER_API_KEY",
    "FIRECRAWL_API_KEY",
    "JINA_API_KEY",
    "STT_API_KEY",
    "TTS_API_KEY",
)

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm CLI not on PATH — chart-drift tests need it",
)


def _render(*, console_enabled: bool, vault_enabled: bool = True) -> list[dict]:
    """Render the chart, parsed into a list of manifest dicts."""
    cmd = [
        "helm",
        "template",
        RELEASE,
        str(CHART_DIR),
        "-n",
        NAMESPACE,
        "--set",
        "istio.enabled=true",
        "--set",
        f"vault.enabled={'true' if vault_enabled else 'false'}",
        "--set",
        f"console.enabled={'true' if console_enabled else 'false'}",
        *_LINT_SECRETS,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"helm template failed (rc={result.returncode}):\n"
            f"--- stderr ---\n{result.stderr}"
        )
    return [
        d
        for d in yaml.safe_load_all(result.stdout)
        if isinstance(d, dict) and d.get("kind")
    ]


def _find(docs: list[dict], kind: str, name_suffix: str) -> dict:
    for d in docs:
        if d.get("kind") == kind and d.get("metadata", {}).get("name", "").endswith(
            name_suffix
        ):
            return d
    raise AssertionError(f"no {kind} ending in {name_suffix!r} in rendered docs")


def _container_env(deployment: dict, container_name: str) -> dict[str, object]:
    """Flatten a container's `env:` list into {name: value-or-valueFrom}."""
    for c in deployment["spec"]["template"]["spec"]["containers"]:
        if c["name"] == container_name:
            out: dict[str, object] = {}
            for e in c.get("env", []) or []:
                out[e["name"]] = e.get("value", e.get("valueFrom"))
            return out
    raise AssertionError(f"no container named {container_name!r}")


def _container(deployment: dict, container_name: str) -> dict:
    for c in deployment["spec"]["template"]["spec"]["containers"]:
        if c["name"] == container_name:
            return c
    raise AssertionError(f"no container named {container_name!r}")


def _librechat_yaml_configmap_doc(docs: list[dict]) -> dict:
    cm = _find(docs, "ConfigMap", "-librechat-yaml")
    raw = cm["data"]["librechat.yaml"]
    return yaml.safe_load(raw)


class TestConsoleEnabledDefaultOff:
    """D1 — a plain `helm template` (console.enabled defaults false) must
    render NONE of the console resources, and the core chart must not
    break from their mere presence in the templates directory."""

    def test_no_console_resources_when_disabled(self) -> None:
        docs = _render(console_enabled=False)
        offenders = [
            d
            for d in docs
            if d.get("metadata", {})
            .get("labels", {})
            .get("app.kubernetes.io/component")
            in (
                "librechat-bff",
                "librechat",
                "librechat-mongodb",
            )
        ]
        assert not offenders, (
            f"console.enabled=false rendered console resources: "
            f"{[(d['kind'], d['metadata']['name']) for d in offenders]}"
        )

    def test_renders_cleanly_when_enabled_vault_true(self) -> None:
        docs = _render(console_enabled=True, vault_enabled=True)
        assert _find(docs, "Deployment", "-librechat-bff")
        assert _find(docs, "Deployment", "-librechat")
        assert _find(docs, "StatefulSet", "-librechat-mongodb")

    def test_renders_cleanly_when_enabled_vault_false(self) -> None:
        docs = _render(console_enabled=True, vault_enabled=False)
        assert _find(docs, "Deployment", "-librechat-bff")
        assert _find(docs, "Deployment", "-librechat")
        assert _find(docs, "StatefulSet", "-librechat-mongodb")


class TestD2TokenForwarding:
    """The paste-killer: neuter any one of these and a real user would be
    back to pasting their own access token (or worse, silently broken)."""

    def test_librechat_yaml_header_placeholder_present(self) -> None:
        cfg = _librechat_yaml_configmap_doc(_render(console_enabled=True))
        custom = cfg["endpoints"]["custom"]
        assert len(custom) == 1
        headers = custom[0].get("headers") or {}
        assert (
            headers.get("Authorization") == "Bearer {{LIBRECHAT_OPENID_ACCESS_TOKEN}}"
        ), (
            f"custom endpoint headers={headers!r} — the "
            "{{LIBRECHAT_OPENID_ACCESS_TOKEN}} placeholder must be wired "
            "into the Authorization header, or the token never forwards."
        )

    def test_librechat_yaml_apikey_is_not_user_provided(self) -> None:
        """This work unit supersedes the WU-3 compose deviation
        (`apiKey: user_provided` — the paste-per-user workaround). Its
        reappearance here would mean the paste crutch silently came back."""
        cfg = _librechat_yaml_configmap_doc(_render(console_enabled=True))
        api_key = cfg["endpoints"]["custom"][0]["apiKey"]
        assert api_key != "user_provided", (
            "custom endpoint apiKey='user_provided' — this is the WU-3 "
            "paste-per-user crutch this work unit exists to eliminate."
        )
        assert api_key == "dummy", (
            f"custom endpoint apiKey={api_key!r} — expected 'dummy'."
        )

    def test_openid_reuse_tokens_true(self) -> None:
        docs = _render(console_enabled=True)
        env = _container_env(_find(docs, "Deployment", "-librechat"), "librechat")
        assert env.get("OPENID_REUSE_TOKENS") == "true", (
            f"OPENID_REUSE_TOKENS={env.get('OPENID_REUSE_TOKENS')!r} — without "
            "it LibreChat mints a separate session JWT instead of reusing the "
            "server-side OIDC access token, and the header placeholder above "
            "has nothing to resolve from."
        )

    def test_offline_access_in_openid_scope(self) -> None:
        docs = _render(console_enabled=True)
        env = _container_env(_find(docs, "Deployment", "-librechat"), "librechat")
        scope = env.get("OPENID_SCOPE") or ""
        assert "offline_access" in scope.split(), (
            f"OPENID_SCOPE={scope!r} is missing offline_access — the #1 "
            "stale-token-window mitigation (refresh-token grant) would be "
            "absent."
        )


class TestB4CloudFeaturesOff:
    def test_endpoints_restricted_to_custom(self) -> None:
        docs = _render(console_enabled=True)
        env = _container_env(_find(docs, "Deployment", "-librechat"), "librechat")
        assert env.get("ENDPOINTS") == "custom", (
            "librechat Deployment is missing ENDPOINTS=custom — without it "
            "the built-in openAI/google/anthropic/agents/assistants/bedrock "
            "endpoints become selectable if any provider env var is ever set."
        )

    def test_no_provider_or_feature_api_keys_configured(self) -> None:
        docs = _render(console_enabled=True)
        env = _container_env(_find(docs, "Deployment", "-librechat"), "librechat")
        for var in _PROVIDER_AND_FEATURE_ENV_VARS:
            assert var not in env, (
                f"librechat Deployment sets {var} — B4 requires no cloud "
                "provider / web-search / speech API key anywhere in this "
                "deployment."
            )

    def test_librechat_yaml_has_no_websearch_or_speech_config(self) -> None:
        cfg = _librechat_yaml_configmap_doc(_render(console_enabled=True))
        assert "webSearch" not in cfg
        assert "speech" not in cfg


class TestRegistrationDisabled:
    def test_allow_registration_false(self) -> None:
        docs = _render(console_enabled=True)
        env = _container_env(_find(docs, "Deployment", "-librechat"), "librechat")
        assert env.get("ALLOW_REGISTRATION") == "false"

    def test_allow_email_login_false(self) -> None:
        docs = _render(console_enabled=True)
        env = _container_env(_find(docs, "Deployment", "-librechat"), "librechat")
        assert env.get("ALLOW_EMAIL_LOGIN") == "false"


class TestD4LibrechatReachesBffOnly:
    """The structural half — mesh AuthorizationPolicies, not just config
    files a future edit could silently widen."""

    def test_custom_endpoint_baseurl_targets_bff_only(self) -> None:
        cfg = _librechat_yaml_configmap_doc(_render(console_enabled=True))
        base_url = cfg["endpoints"]["custom"][0]["baseURL"]
        assert base_url == f"http://{RELEASE}-librechat-bff:8766/v1", (
            f"custom endpoint baseURL={base_url!r} — must target the "
            "in-cluster BFF Service only."
        )

    def test_allow_librechat_bff_ap_only_source_is_librechat_sa(self) -> None:
        docs = _render(console_enabled=True)
        ap = _find(docs, "AuthorizationPolicy", "-allow-librechat-bff")
        principals = ap["spec"]["rules"][0]["from"][0]["source"]["principals"]
        assert principals == [f"cluster.local/ns/{NAMESPACE}/sa/{RELEASE}-librechat"], (
            f"allow-librechat-bff principals={principals!r} — the BFF must "
            "be reachable from LibreChat's own ServiceAccount ONLY."
        )

    def test_allow_memory_ap_whitelists_bff_sa_not_librechat_sa(self) -> None:
        docs = _render(console_enabled=True)
        ap = _find(docs, "AuthorizationPolicy", "-allow-memory")
        principals = ap["spec"]["rules"][0]["from"][0]["source"]["principals"]
        assert (
            f"cluster.local/ns/{NAMESPACE}/sa/{RELEASE}-librechat-bff" in principals
        ), (
            "allow-memory AuthorizationPolicy is missing the BFF's own SA — "
            "the BFF could not reach memory-server /v1 in-mesh."
        )
        assert (
            f"cluster.local/ns/{NAMESPACE}/sa/{RELEASE}-librechat" not in principals
        ), (
            "allow-memory AuthorizationPolicy whitelists LibreChat's own SA "
            "directly — D4 requires LibreChat reach /v1 via the BFF only."
        )

    def test_allow_keycloak_ap_whitelists_bff_sa_not_librechat_sa(self) -> None:
        docs = _render(console_enabled=True)
        ap = _find(docs, "AuthorizationPolicy", "-allow-keycloak")
        principals = ap["spec"]["rules"][0]["from"][0]["source"]["principals"]
        assert (
            f"cluster.local/ns/{NAMESPACE}/sa/{RELEASE}-librechat-bff" in principals
        ), "allow-keycloak AuthorizationPolicy is missing the BFF's own SA."
        assert (
            f"cluster.local/ns/{NAMESPACE}/sa/{RELEASE}-librechat" not in principals
        ), (
            "allow-keycloak AuthorizationPolicy whitelists LibreChat's own "
            "SA directly — D1 requires Keycloak in-mesh access via the BFF "
            "only (LibreChat's OWN OIDC discovery/token calls go via the "
            "front door, not the in-mesh Keycloak Service)."
        )


class TestD4BffSecretVaultSourced:
    def test_vault_annotations_present_when_vault_enabled(self) -> None:
        docs = _render(console_enabled=True, vault_enabled=True)
        dep = _find(docs, "Deployment", "-librechat-bff")
        annotations = dep["spec"]["template"]["metadata"].get("annotations", {})
        assert annotations.get("vault.hashicorp.com/agent-inject") == "true"
        assert annotations.get("vault.hashicorp.com/role") == "librechat-bff"
        env = _container_env(dep, "bff")
        assert "AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET" not in env, (
            "vault.enabled=true but the BFF Deployment still carries a "
            "plaintext AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET env var — the "
            "secret must come from /vault/secrets/env only."
        )

    def test_vault_secret_file_actually_sourced_before_exec(self) -> None:
        """The annotation-presence guard above stays green even if the
        injected secret file is never READ by the process (the 2026-08-29
        independent-review defect: `command`/`args` rendered `None`, so
        AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET never reached bff/config.py's
        Settings and the pod CrashLooped at startup). This asserts the
        container's actual `command`/`args` source `/vault/secrets/env`
        before exec'ing the app — the thing that makes the secret reach the
        running process, not just the thing that makes Vault Agent inject
        it into the filesystem."""
        docs = _render(console_enabled=True, vault_enabled=True)
        dep = _find(docs, "Deployment", "-librechat-bff")
        c = _container(dep, "bff")
        command = c.get("command")
        args = c.get("args") or []
        assert command == ["/bin/sh", "-c"], (
            f"bff container command={command!r} — vault.enabled=true must "
            "override the container command to a shell that sources "
            "/vault/secrets/env before exec'ing the app; the image's "
            "default CMD never reads that file."
        )
        joined_args = "\n".join(str(a) for a in args)
        assert ". /vault/secrets/env" in joined_args, (
            f"bff container args do not source /vault/secrets/env "
            f"(args={args!r}) — the injected AUDITTRACE_BFF_EXCHANGE_"
            "CLIENT_SECRET would never reach the process."
        )
        assert "exec uvicorn" in joined_args, (
            "bff container args source the Vault secret file but never "
            f"exec the app (args={args!r})."
        )

    def test_secret_env_fallback_present_when_vault_disabled(self) -> None:
        docs = _render(console_enabled=True, vault_enabled=False)
        dep = _find(docs, "Deployment", "-librechat-bff")
        env = _container_env(dep, "bff")
        assert "AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET" in env, (
            "vault.enabled=false but the BFF Deployment has no "
            "AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET secretKeyRef fallback."
        )
        annotations = dep["spec"]["template"]["metadata"].get("annotations") or {}
        assert annotations.get("vault.hashicorp.com/agent-inject") != "true"


class TestD3MongodbNoExternalPort:
    def test_mongodb_service_has_no_nodeport_or_loadbalancer(self) -> None:
        docs = _render(console_enabled=True)
        svc = _find(docs, "Service", "-librechat-mongodb")
        assert svc["spec"].get("type") in (None, "ClusterIP"), (
            f"librechat-mongodb Service type={svc['spec'].get('type')!r} — "
            "D3 requires no external exposure."
        )

    def test_mongodb_authorizationpolicy_only_allows_librechat_sa(self) -> None:
        docs = _render(console_enabled=True)
        ap = _find(docs, "AuthorizationPolicy", "-allow-librechat-mongodb")
        principals = ap["spec"]["rules"][0]["from"][0]["source"]["principals"]
        assert principals == [f"cluster.local/ns/{NAMESPACE}/sa/{RELEASE}-librechat"]
