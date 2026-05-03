# IdP federation setup (operator runbook)

> Per-deployment procedure for adding an upstream OIDC identity provider
> (Google, Microsoft Entra, Okta, generic OIDC) brokered through the
> audittrace Keycloak realm. ADR-044 §7 places this firmly on the
> operator side, NOT in the chart — every deployment registers a
> different IdP with different client credentials.

## When to follow this guide

You're standing up a fresh AuditTrace-AI cluster and want users to
sign in with an enterprise identity (Google Workspace, Microsoft 365,
Okta) instead of (or alongside) Keycloak-local accounts.

If you only need Keycloak-local accounts, skip this guide — the chart
ships a working realm out of the box.

## Prerequisites

- AuditTrace-AI cluster up and Keycloak reachable on its external
  hostname (`KC_HOSTNAME_URL`). Confirm with:
  `curl https://<your-host>:30952/realms/audittrace/.well-known/openid-configuration`
- HashiCorp Vault unsealed (production) or `secrets.*` overrides set
  in your `values-local.yaml` (dev fallback).
- `kubectl`, `jq`, `curl` on your PATH (the provisioner script
  validates these on entry; see Phase B.3 in the 2026-05-03 sweep
  for context).
- An OAuth 2.0 client registered with your upstream IdP, with:
  - **Redirect URI**: the broker callback for your Keycloak hostname.
    For example, with Keycloak at `https://keycloak.example.com:30952`
    and an IdP alias `corp-idp`, register
    `https://keycloak.example.com:30952/realms/audittrace/broker/corp-idp/endpoint`.
  - **Scopes**: `openid email profile` at minimum.
- The IdP's discovery URL — the well-known endpoint that exposes the
  IdP's authorization / token / userinfo / jwks endpoints. Examples:
  - Google: `https://accounts.google.com/.well-known/openid-configuration`
  - Microsoft Entra (tenant-specific):
    `https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration`
  - Okta: `https://<okta-domain>/.well-known/openid-configuration`

## 1. Seed the IdP client secret in Vault (production)

```sh
vault kv put kv/audittrace/idp/<alias>/client_secret value=<the-secret-from-the-IdP>
```

For dev (vault.enabled=false) the secret is passed inline via the
`IDP_CLIENT_SECRET` env var on the provisioner; skip this step.

## 2. Run the provisioner

```sh
KEYCLOAK_ADMIN_PASSWORD=<keycloak-admin-pw> \
IDP_TYPE=google \
IDP_ALIAS=<short-name>          \
IDP_DISCOVERY_URL=<.well-known/openid-configuration URL> \
IDP_CLIENT_ID=<oauth-client-id> \
IDP_CLIENT_SECRET=<oauth-client-secret> \
./scripts/setup-idp-federation.sh
```

`IDP_TYPE` controls the attribute mappers added to the broker:
- `google`, `oidc-generic`, `okta` → standard mappers
  (`username`, `email`, `firstName`, `lastName`)
- `entra` → standard mappers + an extra `oid → federation-key`
  mapper to avoid the duplicate-shadow-user footgun (ADR-044 §Risks)

The script:
1. Authenticates `kcadm.sh` against the local Keycloak as admin.
2. Fetches the discovery document and extracts every required endpoint
   (`authorization_endpoint`, `token_endpoint`, `userinfo_endpoint`,
   `jwks_uri`, `end_session_endpoint`). Hard-fails if any required
   endpoint is missing — better to surface the bad IdP config now
   than to install a half-working broker that 500s at first login.
3. Creates (or updates — script is idempotent) the identity provider
   with `pkceEnabled=true`, `validateSignature=true`, `syncMode=FORCE`.
4. Adds the standard attribute mappers, plus the Entra-specific one
   when `IDP_TYPE=entra`.

## 3. Register the redirect URI with the upstream IdP

Each IdP rejects a redirect that wasn't pre-registered. Once the
broker is created, register **the broker callback URL** with the IdP:

```
https://<your-keycloak>:30952/realms/audittrace/broker/<IDP_ALIAS>/endpoint
```

For Google specifically: this URL **cannot use a `.local` TLD** —
Google only accepts publicly-resolvable domains as redirect URIs.
Use a subdomain of an owned domain (a `/etc/hosts` entry on the
operator's machine is sufficient for local-only browsers). See
`feedback_pitch_public_vs_private` and the M2 evidence for context.

## 4. Verify the broker is wired correctly

```sh
kubectl -n audittrace exec deploy/audittrace-keycloak -c keycloak -- \
  /opt/keycloak/bin/kcadm.sh get \
  identity-provider/instances/<IDP_ALIAS> -r audittrace
```

Confirm the response shows all five endpoint URLs populated, not just
`issuer`. Pre-Phase-B.3 the script left these fields empty and required
a manual `kcadm update` post-run; the auto-populate change now does this
in one shot.

## 5. Trigger a real broker login

The simplest harness is the bundled webui at `webui/`:

```sh
python3 -m http.server -d webui 8765
```

Open `http://localhost:8765/`, set:
- **Issuer** = `https://<your-keycloak>:30952/realms/audittrace`
- **Identity provider hint** = `<IDP_ALIAS>` (so Keycloak skips its
  IdP chooser and routes straight to your IdP)

Click **Sign in**. Expect:
- Redirect to your IdP's login page
- Successful authentication
- Redirect back to `localhost:8765/?code=…`
- The webui exchanges the code, displays the access token

## 6. Confirm the identity threaded into AuditTrace's audit trail

```sh
kubectl -n audittrace exec audittrace-postgresql-0 -c postgresql -- \
  psql -U audittrace -d audittrace -c \
  "SELECT user_id, source, timestamp FROM interactions ORDER BY id DESC LIMIT 1;"
```

The `user_id` should be the federated subject (Keycloak-shadow UUID,
not your raw Google `sub`). That's the per-user RLS key — every
downstream policy uses it.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "Cookie not found" after IdP redirect | `KC_HOSTNAME_URL` doesn't match the redirect URI registered with the IdP | Set `keycloak.hostnameUrl` in your `values-local.yaml` to match exactly, then `helm upgrade` |
| "redirect_uri_mismatch" from upstream IdP | Registered redirect URI doesn't match the broker callback URL Keycloak generates | Re-register; the URL must include `/realms/audittrace/broker/<alias>/endpoint` literally |
| `state mismatch — possible CSRF` in webui log | Multiple sign-in clicks in the same window overwrote each other's state | Open a fresh incognito window, click Sign in once, wait for the full flow |
| TLS handshake errors during admission of new pods | Vault Agent injector CA bundle drift (ADR observed 2026-05-03) | `kubectl rollout restart deploy/audittrace-vault-agent-injector -n audittrace`; then re-run the deploy |
| Tokens validated against the in-cluster issuer but rejected when minted via the gateway | `keycloak.externalIssuers` in chart values doesn't include the gateway hostname | Add an entry like `https://<your-host>:30952/realms/audittrace` to `keycloak.externalIssuers` and helm upgrade |

## References

- ADR-044 — External IdP federation via Keycloak brokering (decision)
- `scripts/setup-idp-federation.sh` — the provisioner this guide uses
- `charts/audittrace/values.yaml` — `keycloak.externalIssuers` and
  `keycloak.hostnameUrl` documentation blocks for the override pattern
- `webui/README.md` — minimalist OIDC harness for verification
