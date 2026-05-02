# AuditTrace WebUI — minimalist OIDC + chat probe

A single-page browser harness that exercises the full
**OIDC Authorization Code + PKCE** flow against the `audittrace-webui`
Keycloak client, then uses the resulting JWT to hit
`/v1/chat/completions`.

This is a **harness**, not a product. It exists to:

1. Provide a real browser-side flow (rather than a CLI helper) for
   live-evidence screenshots that demonstrate ADR-042 (BFF-first OIDC)
   and ADR-044 (External IdP federation via Keycloak brokering).
2. Give a concrete example of the protocol any future first-party UI
   (LibreChat, audit dashboards) can lift verbatim. Vanilla JS, no
   build step.
3. Let an operator validate that the realm wiring is correct without
   spinning up LibreChat.

The full first-party UI work lives separately under M3 (LibreChat
Day-1, see `project_m3_librechat_split` memory). This webui is
deliberately *minimal* — it is not LibreChat in disguise.

## Quick start

```bash
# 1. Make sure k3s is up and Keycloak's `audittrace-webui` client exists
#    (see Phase 1 of the M2 runbook).

# 2. Serve the webui at http://localhost:8765/
./webui/serve.py
#    or, equivalently:  python3 -m http.server -d webui --bind 127.0.0.1 8765

# 3. Open http://localhost:8765/ in a browser.
# 4. Click "Sign in" → Keycloak → (optional) federated IdP → JWT.
# 5. Click "Send /v1/chat/completions".
```

## What the page does

- **PKCE generation** in `crypto.subtle` — random verifier, SHA-256
  challenge, stored in `sessionStorage` across the redirect.
- **Authorization Code flow** with optional `kc_idp_hint` parameter
  to pre-select a brokered IdP (e.g. `google-test`).
- **Token exchange** against `/realms/audittrace/protocol/openid-connect/token`.
- **JWT decoding** — header.payload split + base64url decode of the
  payload, displayed inline.
- **Chat probe** — `POST /v1/chat/completions` with the JWT as
  bearer; response shown inline.

## Trust model + scope

- **Localhost-only.** `serve.py` binds 127.0.0.1, never `0.0.0.0`.
  The page is for the operator running the live-evidence test, not
  for LAN users.
- **No service worker. No persistence beyond sessionStorage.** Closing
  the tab clears the token.
- **No build step. No external CDN.** Everything inline. Audit-friendly.
- **Default `state` + PKCE.** The page rejects code returns whose
  `state` doesn't match what was put in `sessionStorage`.

The redirect URI `http://localhost:8765/*` MUST be registered on the
`audittrace-webui` client. The Keycloak realm JSON
(`keycloak/realm-audittrace.json`) ships with this URI in the client's
`redirectUris` list.

## Configuration

All four fields are editable in the page itself (top panel) so you can
re-point the harness at a different realm or memory-server without
editing code:

| Field | Default | Purpose |
|---|---|---|
| Issuer | `https://audittrace.local:30952/realms/audittrace` | Keycloak realm base URL |
| Client ID | `audittrace-webui` | Public PKCE client |
| Redirect URI | `http://localhost:8765/` | Must match a registered `redirectUris` entry |
| Scopes | `openid audittrace:query audittrace:audit memory:*` | Drives `defaultClientScopes` exposure |
| IdP hint | `google-test` (M2) | `kc_idp_hint` query param — leave blank to show the chooser |
| API base | `https://audittrace.local:30952` | `POST {API}/v1/chat/completions` |

If your hosts file doesn't map `audittrace.local`, edit it to point at
the k3s node, or change the issuer URL to whatever your gateway
exposes.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Redirect URI mismatch error from Keycloak | `http://localhost:8765/*` missing from the live `audittrace-webui` client's `redirectUris`. Re-run the kcadm provisioner step from the M2 runbook. |
| `ERR_CERT_AUTHORITY_INVALID` on the issuer URL | Browser doesn't trust the local `audittrace.local` cert. Either install the CA (`~/.config/audittrace/ca.crt`) into the browser's trust store, or accept the warning once. |
| `403 Forbidden` from `/v1/chat/completions` | Token missing the `audittrace:query` scope. Check that `defaultClientScopes` on the `audittrace-webui` client includes it (set on first creation; verify via kcadm). |
| `401 Unauthorized — Invalid issuer` | The token's `iss` claim isn't in `AUDITTRACE_KEYCLOAK_ISSUER_EXTRAS`. Double-check the chart's value matches the public Keycloak URL. |
| Chat probe shows CORS error | `webOrigins` on the client doesn't include `http://localhost:8765`. The realm ships with this in webOrigins; if missing, re-apply via kcadm. |

## Cross-references

- ADR-042 — OIDC Authorization Code + PKCE (the design pattern)
- ADR-044 — External IdP federation via Keycloak brokering (M2)
- `keycloak/realm-audittrace.json` — declares the `audittrace-webui` client
- `scripts/setup-idp-federation.sh` — provisions the brokered IdP
