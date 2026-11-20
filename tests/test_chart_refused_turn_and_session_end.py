"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-2, items 2 and 3 (deployment side).

Item 2: the refused-turn record has no JWT of its writer, so WHO MAY WRITE is
the mesh's decision. The rendered policy must admit the BFF's service account
ALONE to ``POST /interactions/refusals`` and keep the gateway off that path;
the two switches are on only together with that rule.

Item 3: measured on Keycloak 24.0.5, a token of a signed-out user keeps
exchanging while the login scope carries ``offline_access``, for as long as
the token lives. So the login scope must not carry it, and the login client's
access token lives five minutes (in both realm files, and reconciled on an
existing realm by the hook).

Neuters (each alone): C1 let the gateway rule cover every path -> ``test_the
_gateway_is_kept_off_the_refusal_path``; C2 admit the gateway to the refusal
rule -> ``test_only_the_bff_may_write_a_refusal``; C3 name the writer
without the console -> ``test_the_switches_follow_the_mesh_rule``; C7
(ADDENDUM-3) name another identity than the mesh rule's -> ``test_the_route_
names_its_one_writer``; C8 name the writer under PERMISSIVE mutual TLS ->
``test_no_writer_is_named_without_strict_mutual_tls``; C9 drop the realm's SSO
idle timeout from one source -> ``test_the_realm_sets_its_sso_idle_timeout``;
C10 drop Step 8 -> ``test_the_hook_reconciles_the_idle_timeout_on_an_existing_
realm``; C4 put
``offline_access`` back -> ``test_the_login_scope_has_no_offline_access``; C5
drop the lifetime from one realm file -> ``test_the_login_client_token_lives
_five_minutes``; C6 drop Step 7 from the hook -> ``test_the_hook_reconciles_the
_lifetime_on_an_existing_realm``.

LibreChat logout ends the Keycloak session (ADDENDUM-2 finding 1, Q15; measured
E13 on v1.33.2-rc1: a LibreChat logout left the SSO session alive and Login
signed the user straight back in). The fork redirects to the realm's
``end_session_endpoint`` only when ``OPENID_USE_END_SESSION_ENDPOINT`` is
enabled (api/server/controllers/auth/LogoutController.js:54, ``isEnabled``),
and returns to ``OPENID_POST_LOGOUT_REDIRECT_URI`` or ``DOMAIN_CLIENT`` +
``/login`` (same file, 67-68), which the realm client must allow. Neuters
(each alone): C11 drop the env var -> ``test_librechat_logout_ends_the_sso_
session``; C12 drop the client's post-logout URI from one realm file -> ``test_
the_logout_return_page_is_allowed_by_the_realm``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_console_k3s_guards import _LINT_SECRETS, CHART_DIR, NAMESPACE, RELEASE

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY = "cluster.local/ns/istio-system/sa/istio-ingressgateway-service-account"
REFUSAL_PATH = "/interactions/refusals"
# The operator's number (Q13); 28800 s = 8 h is the recommendation.
SSO_IDLE_SECONDS = 28800


def _render(*extra: str) -> list[dict[str, Any]]:
    helm = shutil.which("helm")
    assert helm is not None, "helm CLI not on PATH: the chart render tests need it"
    result = subprocess.run(
        [
            helm,
            "template",
            RELEASE,
            str(CHART_DIR),
            "-n",
            NAMESPACE,
            *_LINT_SECRETS,
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _one(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, (kind, name, len(found))
    return found[0]


def _env(docs: list[dict[str, Any]], deployment: str) -> dict[str, Any]:
    containers = _one(docs, "Deployment", deployment)["spec"]["template"]["spec"][
        "containers"
    ]
    return {e["name"]: e.get("value") for c in containers for e in c.get("env", [])}


@pytest.fixture(scope="module")
def console() -> list[dict[str, Any]]:
    return _render("--set", "console.enabled=true")


@pytest.fixture(scope="module")
def plain() -> list[dict[str, Any]]:
    return _render()


def _rules(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = _one(
        docs, "AuthorizationPolicy", f"{RELEASE}-allow-memory"
    )["spec"]["rules"]
    return rules


def _bff_principal(docs: list[dict[str, Any]]) -> str:
    account = _one(docs, "Deployment", f"{RELEASE}-librechat-bff")["spec"]["template"][
        "spec"
    ]["serviceAccountName"]
    return f"cluster.local/ns/{NAMESPACE}/sa/{account}"


# ── item 2 ──────────────────────────────────────────────────────────────────


def test_only_the_bff_may_write_a_refusal(console: list[dict[str, Any]]) -> None:
    rules = [
        rule
        for rule in _rules(console)
        if any(REFUSAL_PATH in op["operation"].get("paths", []) for op in rule["to"])
    ]
    assert len(rules) == 1
    assert [s["source"]["principals"] for s in rules[0]["from"]] == [
        [_bff_principal(console)]
    ]
    assert [op["operation"] for op in rules[0]["to"]] == [
        {
            "ports": [str(_port(console))],
            "methods": ["POST"],
            "paths": [REFUSAL_PATH],
        }
    ]


def _port(docs: list[dict[str, Any]]) -> int:
    ports = _one(docs, "Service", f"{RELEASE}-memory-server")["spec"]["ports"]
    return int(ports[0]["port"])


def test_the_gateway_is_kept_off_the_refusal_path(
    console: list[dict[str, Any]],
) -> None:
    """Every rule that admits the gateway excludes the path, so no outside
    caller can reach a route that carries no JWT of its writer."""
    with_gateway = [
        rule
        for rule in _rules(console)
        if any(GATEWAY in s["source"]["principals"] for s in rule["from"])
    ]
    assert with_gateway
    for rule in with_gateway:
        for op in rule["to"]:
            assert op["operation"].get("notPaths") == [REFUSAL_PATH]


def _peer_authentication_modes(docs: list[dict[str, Any]]) -> list[str]:
    return [
        d["spec"]["mtls"]["mode"] for d in docs if d["kind"] == "PeerAuthentication"
    ]


def test_the_route_names_its_one_writer(console: list[dict[str, Any]]) -> None:
    """ADDENDUM-3: the memory server is told the BFF's SPIFFE identity, the
    SAME identity the mesh rule admits, and only under STRICT mutual TLS (over
    plaintext the sidecar's statement of the peer would be the caller's own)."""
    writer = _env(console, f"{RELEASE}-memory-server").get(
        "AUDITTRACE_REFUSAL_INGEST_WRITER"
    )
    assert writer == "spiffe://" + _bff_principal(console)
    assert _peer_authentication_modes(console) == ["STRICT"]
    assert "AUDITTRACE_REFUSAL_INGEST_ENABLED" not in _env(
        console, f"{RELEASE}-memory-server"
    )


def test_no_writer_is_named_without_strict_mutual_tls() -> None:
    permissive = _render(
        "--set", "console.enabled=true", "--set", "istio.mtls.mode=PERMISSIVE"
    )
    assert "AUDITTRACE_REFUSAL_INGEST_WRITER" not in _env(
        permissive, f"{RELEASE}-memory-server"
    )


def test_the_switches_follow_the_mesh_rule(
    console: list[dict[str, Any]], plain: list[dict[str, Any]]
) -> None:
    """The BFF sender is on with the console (whose render carries the rule);
    without the console no writer is named and the rule is the old one."""
    assert (
        _env(console, f"{RELEASE}-librechat-bff").get(
            "AUDITTRACE_BFF_REFUSAL_RECORD_ENABLED"
        )
        == "true"
    )
    assert "AUDITTRACE_REFUSAL_INGEST_WRITER" not in _env(
        plain, f"{RELEASE}-memory-server"
    )
    assert len(_rules(plain)) == 1
    assert all("notPaths" not in op["operation"] for op in _rules(plain)[0]["to"])


# ── item 3 ──────────────────────────────────────────────────────────────────


def test_the_login_scope_has_no_offline_access(console: list[dict[str, Any]]) -> None:
    scope = _env(console, f"{RELEASE}-librechat")["OPENID_SCOPE"]
    assert "offline_access" not in scope.split()
    assert {"openid", "profile", "email"} <= set(scope.split())


def _realms(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The realm a fresh install imports: the repository file, and the one the
    chart renders into its ConfigMap."""
    rendered = [
        text
        for doc in docs
        if doc["kind"] == "ConfigMap"
        for text in (doc.get("data") or {}).values()
        if text.lstrip().startswith("{") and '"realm"' in text
    ]
    assert len(rendered) == 1
    file_text = (REPO_ROOT / "keycloak" / "realm-audittrace.json").read_text("utf-8")
    return {"repository file": json.loads(file_text), "chart": json.loads(rendered[0])}


@pytest.mark.parametrize("which", ["repository file", "chart"])
def test_the_login_client_token_lives_five_minutes(
    console: list[dict[str, Any]], which: str
) -> None:
    realm = _realms(console)[which]
    client = next(
        c for c in realm["clients"] if c["clientId"] == "audittrace-librechat"
    )
    assert client["attributes"].get("access.token.lifespan") == "300"
    others = {
        c["clientId"]: (c.get("attributes") or {}).get("access.token.lifespan")
        for c in realm["clients"]
        if c["clientId"] != "audittrace-librechat"
    }
    assert set(others.values()) <= {None, "900"}  # nobody else was changed


def test_the_hook_reconciles_the_lifetime_on_an_existing_realm(
    console: list[dict[str, Any]],
) -> None:
    """``--import-realm`` never updates a realm that exists; the hook must."""
    scripts = [
        text
        for doc in console
        if doc["kind"] == "ConfigMap"
        for text in (doc.get("data") or {}).values()
        if "reconciling audittrace-librechat redirectUris" in text
    ]
    assert len(scripts) == 1
    step = scripts[0].split("── Step 7:", 1)
    assert len(step) == 2, "the hook has no Step 7"
    body = step[1].split("── Istio sidecar shutdown", 1)[0]
    assert (
        'acl_e2e_kc update "clients/${LIBRECHAT_CLIENT_UUID}" -r "${REALM}" \\ '
        "-s 'attributes.\"access.token.lifespan\"=300' >/dev/null"
    ) in " ".join(body.split())


@pytest.mark.parametrize("which", ["repository file", "chart"])
def test_the_realm_sets_its_sso_idle_timeout(
    console: list[dict[str, Any]], which: str
) -> None:
    """ADDENDUM-3 (Q13): without ``offline_access`` the LibreChat refresh token
    lives and dies with the SSO session, and Keycloak's default idle timeout is
    1800 s (measured). The realm sets it."""
    realm = _realms(console)[which]
    assert realm["ssoSessionIdleTimeout"] == SSO_IDLE_SECONDS
    assert realm["ssoSessionMaxLifespan"] >= SSO_IDLE_SECONDS


def test_the_hook_reconciles_the_idle_timeout_on_an_existing_realm(
    console: list[dict[str, Any]],
) -> None:
    scripts = [
        text
        for doc in console
        if doc["kind"] == "ConfigMap"
        for text in (doc.get("data") or {}).values()
        if "reconciling audittrace-librechat redirectUris" in text
    ]
    assert len(scripts) == 1
    step = scripts[0].split("── Step 8:", 1)
    assert len(step) == 2, "the hook has no Step 8"
    body = step[1].split("── Istio sidecar shutdown", 1)[0]
    assert (
        'acl_e2e_kc update "realms/${REALM}" '
        f"-s 'ssoSessionIdleTimeout={SSO_IDLE_SECONDS}' >/dev/null"
    ) in " ".join(body.split())


# ── logout ends the SSO session (ADDENDUM-2 finding 1, Q15) ─────────────────


def test_librechat_logout_ends_the_sso_session(console: list[dict[str, Any]]) -> None:
    """The fork's ``isEnabled`` reads ``true``; any other value leaves the
    Keycloak session alive after a LibreChat logout (measured, E13)."""
    env = _env(console, f"{RELEASE}-librechat")
    assert env.get("OPENID_USE_END_SESSION_ENDPOINT") == "true"
    # The id_token_hint the end-session request carries comes from the session
    # tokens that OPENID_REUSE_TOKENS keeps.
    assert env.get("OPENID_REUSE_TOKENS") == "true"


def _logout_return_page(console: list[dict[str, Any]]) -> str:
    """Where the fork sends the browser after the end-session call."""
    env = _env(console, f"{RELEASE}-librechat")
    return str(
        env.get("OPENID_POST_LOGOUT_REDIRECT_URI") or f"{env['DOMAIN_CLIENT']}/login"
    )


@pytest.mark.parametrize("which", ["repository file", "chart"])
def test_the_logout_return_page_is_allowed_by_the_realm(
    console: list[dict[str, Any]], which: str
) -> None:
    realm = _realms(console)[which]
    client = next(
        c for c in realm["clients"] if c["clientId"] == "audittrace-librechat"
    )
    allowed = client["attributes"].get("post.logout.redirect.uris")
    assert allowed == _logout_return_page(console)
    assert allowed.startswith("https://")
    assert "*" not in allowed  # exact match only (RFC 9700)


def test_the_hook_reconciles_the_post_logout_uri_on_an_existing_realm(
    console: list[dict[str, Any]],
) -> None:
    """``--import-realm`` never updates a realm that exists; without this step
    the running realm refuses the end-session redirect."""
    scripts = [
        text
        for doc in console
        if doc["kind"] == "ConfigMap"
        for text in (doc.get("data") or {}).values()
        if "reconciling audittrace-librechat redirectUris" in text
    ]
    assert len(scripts) == 1
    step = scripts[0].split("── Step 9:", 1)
    assert len(step) == 2, "the hook has no Step 9"
    body = " ".join(step[1].split("── Istio sidecar shutdown", 1)[0].split())
    uri = _logout_return_page(console)
    write = (
        'acl_e2e_kc update "clients/${LIBRECHAT_CLIENT_UUID}" -r "${REALM}" \\ '
        f"-s 'attributes.\"post.logout.redirect.uris\"={uri}' >/dev/null"
    )
    assert write in body
    # the confirmation line comes only after the write
    assert body.index(write) < body.index(
        f"✓ audittrace-librechat post.logout.redirect.uris={uri}"
    )
