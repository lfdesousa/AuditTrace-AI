"""M3-WU-3d — fold the 5 k3s console deploy-fixes into the chart.

Rule 1 (spec Acceptance §1) of the ratified spec
(specs/2026-08-29-SPEC-m3-wu3d-console-deploy-fixes.md, private repo): the
console worked live on k3s ONLY via 5 live `kubectl patch` calls that a
fresh `helm upgrade` reverts. This module asserts each of the 5 fixes is
committed to the chart (not just applied by hand), with one falsifiable
guard per fix — neuter any of them and the corresponding test goes RED.

Hermetic: shells out to `helm template` (no live cluster) and parses the
rendered manifests as YAML/JSON — same discipline as
`tests/test_chart_drift_guards.py` and `tests/test_console_wu3c_testable_
deploy.py`.

Drift classes covered:

* **D1 — Mongo binds all interfaces.** The rendered `librechat-mongodb`
  StatefulSet's container `command` is exactly
  `["mongod", "--noauth", "--bind_ip_all"]` — dropping `--bind_ip_all`
  reproduces the live ECONNRESET CrashLoop (Istio forwards inbound
  traffic to the pod IP, not loopback).
* **D2 — Mongo Service/container port named for plain TCP.** Both the
  StatefulSet's container port and the Service's port/targetPort are
  named `tcp-mongodb`, never `mongo` (a port literally named `mongo`
  makes Istio apply its own flaky MongoDB protocol filter).
* **D3 — OIDC issuer over HTTPS + local CA trust (or the insecureTLS
  escape hatch).** `OPENID_ISSUER` is the HTTPS `.well-known` form via
  `console.librechat.frontDoorHost`; the default (`insecureTLS=false`)
  path mounts the cluster CA and sets `NODE_EXTRA_CA_CERTS`;
  `insecureTLS=true` swaps to `NODE_TLS_REJECT_UNAUTHORIZED=0` and drops
  the CA mount entirely.
* **D4 — LibreChat served at ROOT on a dedicated host.**
  `DOMAIN_CLIENT`/`DOMAIN_SERVER` point at `console.librechat.host` with
  no subpath; a VirtualService routes that host at `/` with NO rewrite;
  the retired `/librechat`-prefix route is gone; the self-signed gateway
  cert's SAN includes the dedicated host when `console.enabled=true` (and
  omits it when `console.enabled=false`).
* **D5 — realm redirect + scopes.** The dedicated-host OIDC callback URI
  is registered in BOTH realm files (top-level dev-import copy and the
  chart's rendered copy), the retired `/librechat` callback URI is gone,
  the dev `localhost:3080` URI is preserved, and the `ensure-memory-
  scopes` Job's Step 5 still idempotently binds `profile`/`email`/
  `offline_access` onto `audittrace-librechat` (the scope-declaration
  half of D5 is already covered, non-duplicated, by
  `tests/test_chart_drift_guards.py::TestLibrechatConsoleClient` and
  `::TestConsoleRealmScopeReconcile`).

Anchors: `feedback_no_more_drifts`, `feedback_vacuous_neuter_test_antipattern`,
`feedback_ratified_spec_immutable`.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from cryptography import x509

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "charts" / "audittrace"
VALUES_FILE = CHART_DIR / "values.yaml"
VALUES_LAPTOP_FILE = CHART_DIR / "values-laptop.yaml"
TOP_LEVEL_REALM_FILE = REPO_ROOT / "keycloak" / "realm-audittrace.json"
MEMORY_SCOPES_SCRIPT = (
    CHART_DIR / "templates" / "keycloak" / "configmap-memory-scopes-script.yaml"
)

RELEASE = "audittrace"
NAMESPACE = "audittrace"

EXPECTED_HOST = "librechat.audittrace.local"
EXPECTED_CALLBACK = f"https://{EXPECTED_HOST}/oauth/openid/callback"

# Satisfies every chart-side `required` so a bare render doesn't fail on
# unrelated required fields (mirrors the sibling console test files).
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
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _render(
    *,
    console_enabled: bool = True,
    vault_enabled: bool = True,
    extra_set: list[str] | None = None,
) -> list[dict]:
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
        *(extra_set or []),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"helm template failed (rc={result.returncode}):\n"
            f"--- args ---\n{cmd}\n"
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


def _find_exact(docs: list[dict], kind: str, name: str) -> dict:
    for d in docs:
        if d.get("kind") == kind and d.get("metadata", {}).get("name") == name:
            return d
    raise AssertionError(f"no {kind} named {name!r} in rendered docs")


def _container(deployment_or_sts: dict, container_name: str) -> dict:
    for c in deployment_or_sts["spec"]["template"]["spec"]["containers"]:
        if c["name"] == container_name:
            return c
    raise AssertionError(f"no container named {container_name!r}")


def _container_env(deployment_or_sts: dict, container_name: str) -> dict[str, object]:
    c = _container(deployment_or_sts, container_name)
    out: dict[str, object] = {}
    for e in c.get("env", []) or []:
        out[e["name"]] = e.get("value", e.get("valueFrom"))
    return out


def _rendered_realm_json(docs: list[dict]) -> dict:
    cm = _find(docs, "ConfigMap", "-keycloak-realm")
    raw = (cm.get("data") or {}).get("realm.json", "")
    if not raw:
        raise AssertionError("keycloak-realm ConfigMap has no data.realm.json")
    return json.loads(raw)


def _librechat_client(realm: dict) -> dict:
    for c in realm.get("clients", []) or []:
        if c.get("clientId") == "audittrace-librechat":
            return c
    raise AssertionError("audittrace-librechat missing from realm")


def _both_realms() -> list[tuple[str, dict]]:
    top_level = json.loads(TOP_LEVEL_REALM_FILE.read_text(encoding="utf-8"))
    chart_rendered = _rendered_realm_json(_render())
    return [
        ("keycloak/realm-audittrace.json", top_level),
        ("charts/audittrace/files/realm-audittrace.json (rendered)", chart_rendered),
    ]


def _tls_secret_san(docs: list[dict]) -> list[str]:
    secret = _find_exact(docs, "Secret", "audittrace-tls")
    cert_pem = base64.b64decode(secret["data"]["tls.crt"])
    cert = x509.load_pem_x509_certificate(cert_pem)
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    return ext.value.get_values_for_type(x509.DNSName)


# ---------------------------------------------------------------------------
# D1 — Mongo binds all interfaces
# ---------------------------------------------------------------------------


class TestD1MongoBindsAllInterfaces:
    def test_statefulset_command_binds_all_interfaces(self) -> None:
        docs = _render()
        sts = _find(docs, "StatefulSet", "-librechat-mongodb")
        cmd = _container(sts, "mongodb")["command"]
        assert cmd == ["mongod", "--noauth", "--bind_ip_all"], (
            f"librechat-mongodb command={cmd!r} — dropping --bind_ip_all "
            "reproduces the live ECONNRESET CrashLoop (Istio forwards "
            "inbound traffic to the pod IP, not loopback)."
        )


# ---------------------------------------------------------------------------
# D2 — Mongo Service/container port named for plain TCP
# ---------------------------------------------------------------------------


class TestD2MongoPlainTcpPortName:
    def test_statefulset_container_port_named_tcp_mongodb(self) -> None:
        docs = _render()
        sts = _find(docs, "StatefulSet", "-librechat-mongodb")
        ports = _container(sts, "mongodb")["ports"]
        names = [p["name"] for p in ports]
        assert names == ["tcp-mongodb"], (
            f"librechat-mongodb container port name(s)={names!r} — a port "
            "literally named 'mongo' makes Istio apply its flaky MongoDB "
            "protocol filter instead of treating this as plain TCP."
        )

    def test_service_port_name_and_target_are_tcp_mongodb(self) -> None:
        docs = _render()
        svc = _find(docs, "Service", "-librechat-mongodb")
        port = svc["spec"]["ports"][0]
        assert port["name"] == "tcp-mongodb"
        assert port["targetPort"] == "tcp-mongodb"


# ---------------------------------------------------------------------------
# D3 — OIDC issuer over HTTPS + local CA trust (or insecureTLS escape hatch)
# ---------------------------------------------------------------------------


class TestD3OidcHttpsIssuerAndCaTrust:
    def test_issuer_is_https_wellknown_via_frontdoorhost(self) -> None:
        docs = _render()
        dep = _find(docs, "Deployment", "-librechat")
        env = _container_env(dep, "librechat")
        values = yaml.safe_load(VALUES_FILE.read_text())
        front_door_host = values["console"]["librechat"]["frontDoorHost"]
        expected = (
            f"https://{front_door_host}/realms/audittrace/"
            ".well-known/openid-configuration"
        )
        assert env["OPENID_ISSUER"] == expected, (
            f"OPENID_ISSUER={env.get('OPENID_ISSUER')!r} — LibreChat "
            "refuses to complete OIDC discovery over plain HTTP; this "
            f"must be the HTTPS .well-known form, expected {expected!r}."
        )

    def test_insecure_tls_default_is_false(self) -> None:
        values = yaml.safe_load(VALUES_FILE.read_text())
        assert values["console"]["librechat"]["insecureTLS"] is False, (
            "console.librechat.insecureTLS must default to false — the CA "
            "mount is the preferred/default path, not disabling TLS "
            "validation."
        )

    def test_default_path_mounts_ca_and_sets_node_extra_ca_certs(self) -> None:
        docs = _render()  # insecureTLS defaults false
        dep = _find(docs, "Deployment", "-librechat")
        env = _container_env(dep, "librechat")
        assert env.get("NODE_EXTRA_CA_CERTS") == "/etc/audittrace/ca.crt"
        assert "NODE_TLS_REJECT_UNAUTHORIZED" not in env
        c = _container(dep, "librechat")
        mount_names = {m["name"] for m in c.get("volumeMounts", []) or []}
        assert "ca-trust" in mount_names
        vol_names = {
            v["name"] for v in dep["spec"]["template"]["spec"].get("volumes", []) or []
        }
        assert "ca-trust" in vol_names

    def test_insecure_tls_true_swaps_env_and_drops_ca_mount(self) -> None:
        docs = _render(extra_set=["--set", "console.librechat.insecureTLS=true"])
        dep = _find(docs, "Deployment", "-librechat")
        env = _container_env(dep, "librechat")
        assert env.get("NODE_TLS_REJECT_UNAUTHORIZED") == "0", (
            "console.librechat.insecureTLS=true must set "
            "NODE_TLS_REJECT_UNAUTHORIZED=0."
        )
        assert "NODE_EXTRA_CA_CERTS" not in env, (
            "insecureTLS=true still rendered NODE_EXTRA_CA_CERTS — the two "
            "paths are meant to be mutually exclusive."
        )
        c = _container(dep, "librechat")
        mount_names = {m["name"] for m in c.get("volumeMounts", []) or []}
        assert "ca-trust" not in mount_names, (
            "insecureTLS=true still mounted the CA-trust ConfigMap — dead "
            "weight for a path that disables cert validation entirely."
        )
        vol_names = {
            v["name"] for v in dep["spec"]["template"]["spec"].get("volumes", []) or []
        }
        assert "ca-trust" not in vol_names


# ---------------------------------------------------------------------------
# D4 — served at ROOT on a dedicated host
# ---------------------------------------------------------------------------


class TestD4DedicatedHostRootNoRewrite:
    def test_console_librechat_host_default(self) -> None:
        values = yaml.safe_load(VALUES_FILE.read_text())
        assert values["console"]["librechat"]["host"] == EXPECTED_HOST

    def test_values_laptop_commits_the_host_too(self) -> None:
        laptop_values = yaml.safe_load(VALUES_LAPTOP_FILE.read_text())
        assert laptop_values["console"]["librechat"]["host"] == EXPECTED_HOST, (
            "values-laptop.yaml must commit console.librechat.host so "
            "`-f values-laptop.yaml` is the whole reproducible story."
        )

    def test_domain_client_and_server_point_at_host_root(self) -> None:
        docs = _render()
        dep = _find(docs, "Deployment", "-librechat")
        env = _container_env(dep, "librechat")
        expected = f"https://{EXPECTED_HOST}"
        assert env["DOMAIN_CLIENT"] == expected
        assert env["DOMAIN_SERVER"] == expected
        assert not str(env["DOMAIN_CLIENT"]).endswith("/librechat")
        assert not str(env["DOMAIN_SERVER"]).endswith("/librechat")

    def test_virtualservice_serves_dedicated_host_at_root_no_rewrite(self) -> None:
        docs = _render()
        vs = _find(docs, "VirtualService", "-librechat-console")
        assert vs["spec"]["hosts"] == [EXPECTED_HOST]
        route = vs["spec"]["http"][0]
        assert route["match"][0]["uri"]["prefix"] == "/"
        assert "rewrite" not in route, (
            "the dedicated-host route must carry NO uri rewrite — "
            "LibreChat expects to be served exactly as-is at root."
        )
        dest = route["route"][0]["destination"]
        assert dest["host"] == f"{RELEASE}-librechat.{NAMESPACE}.svc.cluster.local"

    def test_retired_subpath_route_is_gone(self) -> None:
        docs = _render()
        offenders = []
        for d in docs:
            if d.get("kind") != "VirtualService":
                continue
            for route in d.get("spec", {}).get("http", []) or []:
                for match in route.get("match", []) or []:
                    if match.get("uri", {}).get("prefix") == "/librechat":
                        offenders.append(d["metadata"]["name"])
        assert not offenders, (
            f"VirtualService(s) {offenders!r} still route the retired "
            "/librechat prefix — D4 requires it gone."
        )

    def test_tls_cert_san_includes_console_host_when_enabled(self) -> None:
        docs = _render(console_enabled=True)
        dns_names = _tls_secret_san(docs)
        assert EXPECTED_HOST in dns_names, (
            f"gateway cert SAN={dns_names!r} — must include the dedicated "
            "console host or the browser's TLS handshake fails."
        )
        assert "audittrace.local" in dns_names
        assert "localhost" in dns_names

    def test_tls_cert_san_omits_console_host_when_disabled(self) -> None:
        docs = _render(console_enabled=False)
        dns_names = _tls_secret_san(docs)
        assert EXPECTED_HOST not in dns_names, (
            "console.enabled=false must not add the console host to the "
            "gateway cert SAN."
        )


# ---------------------------------------------------------------------------
# D5 — realm redirect + scopes
# ---------------------------------------------------------------------------


class TestD5RealmRedirectAndScopeReconcile:
    def test_dedicated_host_redirect_present_in_both_realm_files(self) -> None:
        for label, realm in _both_realms():
            client = _librechat_client(realm)
            assert EXPECTED_CALLBACK in (client.get("redirectUris") or []), (
                f"{label}: audittrace-librechat.redirectUris is missing "
                f"{EXPECTED_CALLBACK!r} — the D4 dedicated-host callback."
            )
            assert f"https://{EXPECTED_HOST}" in (client.get("webOrigins") or []), (
                f"{label}: audittrace-librechat.webOrigins is missing the "
                "dedicated console host."
            )

    def test_dev_localhost_redirect_preserved(self) -> None:
        dev_uri = "http://localhost:3080/oauth/openid/callback"
        for label, realm in _both_realms():
            client = _librechat_client(realm)
            assert dev_uri in (client.get("redirectUris") or []), (
                f"{label}: the localhost:3080 dev redirect URI must be kept."
            )

    def test_retired_subpath_redirect_is_gone(self) -> None:
        for label, realm in _both_realms():
            client = _librechat_client(realm)
            offenders = [
                u
                for u in (client.get("redirectUris") or [])
                + (client.get("webOrigins") or [])
                if "/librechat/oauth" in u
            ]
            assert not offenders, (
                f"{label}: audittrace-librechat still carries retired "
                f"/librechat-subpath URI(s) {offenders!r}."
            )

    def test_ensure_memory_scopes_job_still_binds_all_three_scopes(self) -> None:
        """The Job's Step 5 reconcile (`bind_scope "audittrace-librechat"
        "<scope>" "..."`) is what actually self-heals a PRE-EXISTING realm
        that only imported the realm.json once — dropping any of the
        three bind_scope calls reproduces the earlier deploy's gap (the
        Job's post-hook timed out before it finished, so the scopes had to
        be applied live)."""
        script = MEMORY_SCOPES_SCRIPT.read_text(encoding="utf-8")
        for scope in ("profile", "email", "offline_access"):
            assert f'bind_scope "audittrace-librechat" "{scope}"' in script, (
                f"ensure-memory-scopes Job no longer reconciles {scope!r} "
                "onto audittrace-librechat."
            )
