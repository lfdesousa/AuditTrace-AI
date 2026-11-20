"""What does Keycloak's token endpoint answer when it refuses a token exchange?

Records, per cause, the HTTP status and the RFC 6749 section 5.2 fields `error` and
`error_description`. Never prints or stores a token, a secret or a password.
Usage: kc-measure.py <base_url> <export_dir> <out.json>   (admin password on env KC_ADMIN_PW)
"""

from __future__ import annotations

import base64
import hashlib
import html
import http.cookiejar
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

BASE, EXPORT, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
REALM = "audittrace"
ADMIN_PW = os.environ["KC_ADMIN_PW"]
TOKEN_URL = f"{BASE}/realms/{REALM}/protocol/openid-connect/token"
LOGIN_CLIENT, BFF, CHAT_AUD = (
    "audittrace-librechat",
    "audittrace-librechat-bff",
    "audittrace-librechat-chat",
)
REDIRECT = "http://localhost:3080/oauth/openid/callback"
POLICY = "allow-audittrace-librechat-bff-token-exchange"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:
        return None


def call(
    method: str,
    url: str,
    *,
    form: dict[str, str] | None = None,
    body: Any = None,
    bearer: str | None = None,
    opener: Any = None,
) -> tuple[int, dict[str, str], bytes]:
    data, headers = None, {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with (opener or urllib.request.build_opener()).open(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, dict(exc.headers), exc.read()


def admin_token() -> str:
    s, _, b = call(
        "POST",
        f"{BASE}/realms/master/protocol/openid-connect/token",
        form={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "admin",
            "password": ADMIN_PW,
        },
    )
    assert s == 200, s
    return str(json.loads(b)["access_token"])


def adm(method: str, path: str, body: Any = None) -> tuple[Any, dict[str, str]]:
    s, h, b = call(
        method,
        f"{BASE}/admin/realms/{REALM}{'/' + path if path else ''}",
        body=body,
        bearer=admin_token(),
    )
    assert s < 300, (method, path, s, b[:300])
    return (json.loads(b) if b else None), h


def client_uuid(client_id: str) -> str:
    rows, _ = adm("GET", f"clients?clientId={urllib.parse.quote(client_id)}")
    return str(rows[0]["id"])


def allow_exchange(target_uuid: str, source_uuid: str) -> None:
    """The chart hook's permission step, by the same admin endpoints."""
    perms, _ = adm(
        "PUT", f"clients/{target_uuid}/management/permissions", {"enabled": True}
    )
    perm_id = perms["scopePermissions"]["token-exchange"]
    rm = client_uuid("realm-management")
    pols, _ = adm("GET", f"clients/{rm}/authz/resource-server/policy?name={POLICY}")
    pol = next((p for p in pols if p["name"] == POLICY), None)
    if pol is None:
        pol, _ = adm(
            "POST",
            f"clients/{rm}/authz/resource-server/policy/client",
            {"name": POLICY, "clients": [source_uuid], "logic": "POSITIVE"},
        )
    perm, _ = adm(
        "GET", f"clients/{rm}/authz/resource-server/permission/scope/{perm_id}"
    )
    perm["policies"] = [pol["id"]]
    adm("PUT", f"clients/{rm}/authz/resource-server/permission/scope/{perm_id}", perm)


def login(scope: str, username: str, password: str) -> str:
    """Authorization Code + PKCE on the public login client."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar), NoRedirect
    )
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    q = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": LOGIN_CLIENT,
            "redirect_uri": REDIRECT,
            "scope": scope,
            "state": secrets.token_urlsafe(8),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    s, _, b = call(
        "GET", f"{BASE}/realms/{REALM}/protocol/openid-connect/auth?{q}", opener=opener
    )
    assert s == 200, ("auth page", s)
    action = html.unescape(
        re.search(r'<form[^>]+action="([^"]+)"', b.decode()).group(1)
    )  # type: ignore[union-attr]
    s, h, b = call(
        "POST", action, form={"username": username, "password": password}, opener=opener
    )
    assert s == 302, ("login post", s)
    code = urllib.parse.parse_qs(urllib.parse.urlparse(h["Location"]).query)["code"][0]
    s, _, b = call(
        "POST",
        TOKEN_URL,
        form={
            "grant_type": "authorization_code",
            "client_id": LOGIN_CLIENT,
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert s == 200, ("code exchange", s)
    return str(json.loads(b)["access_token"])


def exchange(
    subject: str | None, secret: str, audience: str, scope: str
) -> dict[str, Any]:
    form = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": BFF,
        "client_secret": secret,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": audience,
        "scope": scope,
    }
    if subject is not None:
        form["subject_token"] = subject
    status, headers, raw = call("POST", TOKEN_URL, form=form)
    row: dict[str, Any] = {
        "status": status,
        "content_type": headers.get("Content-Type", "").split(";")[0],
    }
    try:
        parsed = json.loads(raw)
    except ValueError:
        row["body_shape"] = "not-json"
        return row
    row["body_shape"] = type(parsed).__name__
    if status == 200:
        row["keys"] = sorted(parsed)  # names only; the token itself is never stored
    elif isinstance(parsed, dict):
        row["error"], row["error_description"] = (
            parsed.get("error"),
            parsed.get("error_description"),
        )
        row["other_keys"] = sorted(set(parsed) - {"error", "error_description"})
    return row


def main() -> None:
    sys.path.insert(0, EXPORT)
    import yaml

    from bff.chat_scopes import CHAT_SCOPE_STRING  # the BFF's own request

    with open(f"{EXPORT}/charts/audittrace/values.yaml", encoding="utf-8") as handle:
        login_scope = " ".join(
            yaml.safe_load(handle)["console"]["librechat"]["openidScope"].split()
        )
    bff, chat = client_uuid(BFF), client_uuid(CHAT_AUD)
    secret = secrets.token_urlsafe(24)
    rep, _ = adm("GET", f"clients/{bff}")
    rep["secret"] = secret
    adm("PUT", f"clients/{bff}", rep)
    user, pw = "bffcre-a1-user", secrets.token_urlsafe(18)
    _, h = adm(
        "POST",
        "users",
        {
            "username": user,
            "enabled": True,
            "emailVerified": True,
            "email": "a1@example.invalid",
            "firstName": "A",
            "lastName": "One",
            "credentials": [{"type": "password", "value": pw, "temporary": False}],
        },
    )
    user_id = h["Location"].rsplit("/", 1)[1]
    version = json.loads(
        call("GET", f"{BASE}/admin/serverinfo", bearer=admin_token())[2]
    )["systemInfo"]["version"]
    cases: dict[str, dict[str, Any]] = {}

    def run(
        name: str,
        cause: str,
        subject: str | None,
        *,
        sec: str = secret,
        aud: str = CHAT_AUD,
    ) -> None:
        cases[name] = {
            "cause_class": cause,
            **exchange(subject, sec, aud, CHAT_SCOPE_STRING),
        }

    subject = login(login_scope, user, pw)
    run(
        "C1 audience client present, exchange permission not yet granted (the rc1 state)",
        "bff-configuration",
        subject,
    )
    allow_exchange(chat, bff)
    run("C0 control: valid session, permission granted", "none", subject)
    run("C2 wrong client secret", "bff-configuration", subject, sec="not-the-secret")
    run(
        "C3 audience that does not exist",
        "bff-configuration",
        subject,
        aud="no-such-client",
    )
    run(
        "C4 audience client without an exchange permission",
        "bff-configuration",
        subject,
        aud="audittrace-restricted",
    )
    run("S1 subject token is not a JWT", "subject-token", "not-a-jwt")
    foreign = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )
    now = int(time.time())
    forged = jwt.encode(
        {
            "iss": f"{BASE}/realms/{REALM}",
            "sub": user_id,
            "iat": now,
            "exp": now + 300,
            "azp": LOGIN_CLIENT,
            "typ": "Bearer",
        },
        foreign,
        algorithm="RS256",
    )
    run("S2 subject token signed by a foreign key", "subject-token", forged)
    run("S3 subject token absent", "bff-configuration", None)
    # The token must be OLDER than the logout (Keycloak's not-before has one-second resolution).
    time.sleep(2)
    adm("POST", f"users/{user_id}/logout")
    run(
        "S4a session ended by an administrator; login scope as deployed (with offline_access); same unexpired token",
        "subject-token",
        subject,
    )
    time.sleep(2)
    run("S4b the same token again, 2 s later", "subject-token", subject)
    plain = login("openid profile email", user, pw)
    run("C0c control: login without offline_access, valid session", "none", plain)
    time.sleep(2)
    adm("POST", f"users/{user_id}/logout")
    run(
        "S4c session ended by an administrator; login WITHOUT offline_access; same unexpired token",
        "subject-token",
        plain,
    )
    subject = login(login_scope, user, pw)
    sessions, _ = adm("GET", f"users/{user_id}/sessions")
    for session in sessions:
        adm("DELETE", f"sessions/{session['id']}")
    run(
        "S4d the user's online session deleted by id (admin console 'Sign out' on one session); login scope as deployed",
        "subject-token",
        subject,
    )
    subject = login(login_scope, user, pw)
    rep, _ = adm("GET", f"users/{user_id}")
    rep["enabled"] = False
    adm("PUT", f"users/{user_id}", rep)
    run("S5 the user was disabled", "subject-token", subject)
    rep["enabled"] = True
    adm("PUT", f"users/{user_id}", rep)
    realm, _ = adm("GET", "")
    lifespan = realm["accessTokenLifespan"]
    adm("PUT", "", {"accessTokenLifespan": 2})
    short = login(login_scope, user, pw)
    time.sleep(4)
    run("S6 subject token expired", "subject-token", short)
    adm("PUT", "", {"accessTokenLifespan": lifespan})
    subject = login(login_scope, user, pw)
    rep, _ = adm("GET", f"clients/{bff}")
    rep["enabled"] = False
    adm("PUT", f"clients/{bff}", rep)
    run("C5 the BFF client is disabled", "bff-configuration", subject)
    rep["enabled"] = True
    adm("PUT", f"clients/{bff}", rep)
    run("C0b control after the sequence: valid session", "none", subject)

    _, _, raw = call("GET", f"{BASE}/realms/{REALM}/protocol/openid-connect/certs")
    keys = json.loads(raw)["keys"]
    out = {
        "keycloak_version": version,
        "access_token_lifespan_s": lifespan,
        "cases": cases,
        "jwks_keys_in_order": [
            {"kty": k.get("kty"), "alg": k.get("alg"), "use": k.get("use")}
            for k in keys
        ],
    }
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=1, sort_keys=True)
        handle.write("\n")
    for name, row in cases.items():
        print(
            f"{row['status']} error={row.get('error')!r} description={row.get('error_description')!r} | {name}"
        )
    print("jwks:", out["jwks_keys_in_order"])


if __name__ == "__main__":
    main()
