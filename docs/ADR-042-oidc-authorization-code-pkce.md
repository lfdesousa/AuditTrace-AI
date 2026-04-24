# ADR-042: OIDC Authorization Code flow for user-facing UIs (BFF-first, confidential clients)

**Status:** Proposed
**Date:** 2026-04-23
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-022 (Keycloak realm), ADR-023 (JWT validation + JWKS caching),
ADR-026 (multi-user identity), ADR-032 (OAuth2 Device Flow for agents),
ADR-041 (product boundary — UIs are BYO)
**External normative reference:** `draft-ietf-oauth-browser-based-apps-26`
(December 2025) — "OAuth 2.0 for Browser-Based Applications."

## Context

Authentication to AuditTrace-AI today supports two grants:

1. **`client_credentials`** (ADR-022) — service accounts.
2. **OAuth2 Device Authorization Grant** (ADR-032, RFC 8628) — browser-less
   agents (OpenCode, Continue, Roo Code).

Neither fits a **user-facing web UI** where the user sits in front of a
browser and expects a standard "log in with your org account" experience.
The first concrete consumer is **LibreChat** — a FOSS, OpenAI-compatible,
self-hostable chat UI (MIT-licensed, Node.js server + React front-end,
OIDC-native). Consistent with ADR-041, UIs are BYO; we integrate against
LibreChat rather than bundle it.

### IETF draft-26 framing — why this matters

The authoritative current guidance is
`draft-ietf-oauth-browser-based-apps-26` (December 2025). It names **three
architectural patterns, in decreasing order of security**:

1. **Backend-For-Frontend (BFF).** Tokens never reach the browser. A
   server-side component performs the OAuth dance and stores tokens
   server-side; the browser carries only a session cookie to the BFF.
   The draft: *"This architecture is strongly recommended for business
   applications, sensitive applications, and applications that handle
   personal data."*
2. **Token-Mediating Backend.** Access tokens exposed to the browser;
   refresh tokens stay server-side. Moderate security.
3. **Browser-based OAuth 2.0 Client** (pure SPA with PKCE). All tokens
   accessible to any malicious code executing in the browser. Least
   secure.

Traditional **server-side web applications with their own backend** are
explicitly *out of scope* of the draft: *"Many web applications consist
of a frontend and API running on a common domain, allowing for an
architecture that does not rely on OAuth 2.0... Such scenarios... are
not within scope of this specification."* They should use **confidential
client credentials** per standard OAuth practices.

AuditTrace-AI processes privileged legal and regulated-financial content.
The "sensitive applications / personal data" threshold in the draft is
met. Whatever architecture we pick must be consistent with the BFF-first
guidance, not the pure-SPA pattern.

## Decision

### §1. UI integrations classified by architecture; auth mechanism follows

Two UI categories, and only two, are permitted:

| UI category | Examples | Auth mechanism | Tokens visible to browser? |
|---|---|---|---|
| **Server-side web app with its own backend + browser frontend** | LibreChat (Node.js), Open WebUI, Langfuse UI, internal dashboards | OIDC Authorization Code, **confidential client**, client secret in Vault, PKCE S256 as defence-in-depth | No — the app's server stores them |
| **Pure browser SPA with no backend** | Hypothetical future custom admin UI | Must introduce a **dedicated BFF** (see §5). Session cookie to BFF; BFF holds tokens; browser never sees tokens | No — the BFF stores them |

The **Token-Mediating Backend** pattern (middle-security of the draft's
three) is **not adopted**. Binary decision only: either the UI has a
real backend and uses a confidential client, or we introduce a BFF.
Halfway-mediated arrangements expose access tokens to the browser for no
clear reason in our topology.

The **Browser-based OAuth 2.0 Client** pattern (pure SPA holding tokens)
is **forbidden for AuditTrace-AI UIs**. Any proposal for a pure-SPA UI
triggers the §5 BFF requirement; it cannot ship with PKCE-in-browser as
its only protection.

### §2. LibreChat is a server-side web app — confidential client, not public

LibreChat is a Node.js server with its own session store. The browser
never speaks OIDC directly — LibreChat's server-side handles the
Authorization Code exchange, stores the access + refresh tokens in its
session store, and forwards Bearer tokens to AuditTrace-AI per-request.
This is effectively a BFF arrangement by construction; LibreChat's own
OIDC plugin acts as the backend-for-frontend.

Keycloak client config for `audittrace-webui`:

- `publicClient=false` — confidential.
- `clientAuthenticatorType=client-secret` — the client secret is the
  server-side client-authentication mechanism.
- `secret` → **stored in Vault** (M1 roadmap dependency). LibreChat
  reads it via Vault Agent Injector or equivalent at container start.
  Never committed to Git. Never in Helm `values.yaml`.
- `standardFlowEnabled=true` — Authorization Code endpoint.
- `directAccessGrantsEnabled=false` — Resource Owner Password grant
  forbidden; credentials never transit the UI layer.
- `oauth2.device.authorization.grant.enabled=false` — Device Flow has
  its own client (`audittrace-opencode`, ADR-032).
- `serviceAccountsEnabled=false` — user-facing only.
- Attribute `pkce.code.challenge.method=S256` — PKCE S256 enabled as
  **defence-in-depth** even for a confidential client, per OAuth 2.1
  guidance. Hardens against authorization-code interception.
- Attribute `require.pushed.authorization.requests=true` where the
  operator's Keycloak version supports it (PAR, RFC 9126) — further
  hardens by moving authorization-request parameters off the user-
  agent redirect path.
- Audience mapper `aud=audittrace-server` — identical to the other
  clients; JWT validation path requires zero changes.

### §3. Redirect URI discipline

Exact-match only, HTTPS-only, no wildcards. The realm JSON ships a
placeholder; the provisioner substitutes the deployment's actual UI
hostname from an environment variable sourced from Vault or the
Helm values file. Post-logout URI follows the same rule.

Wildcards in redirect URIs have been the root cause of multiple
public-client breaches in the wild; they remain forbidden here.

### §4. Cookie and session hygiene at the BFF/UI layer

The IETF draft's cookie rules apply by analogy wherever a server-side
component holds tokens on behalf of a browser (i.e. LibreChat here, or
a dedicated BFF in §5):

- `Secure` **MUST** be set — HTTPS-only.
- `HttpOnly` **MUST** be set — no JavaScript access.
- `SameSite=Strict` **SHOULD** be set — CSRF mitigation; production
  config enforces.

These rules land in the **LibreChat deployment manifest** (and any
future BFF), not in the Keycloak realm. Operator discipline: the
deployment values file must not override any of these to weaker
settings.

### §5. Pure-SPA UIs (future) — dedicated BFF, never browser-based client

If a pure-SPA UI is ever proposed (custom admin dashboard, anything
else with no server-side component), two implementation options; no
third:

- **Option A — Dedicated BFF sidecar.** A lightweight proxy
  (`oauth2-proxy`, Pomerium, or a small FastAPI service) stands in
  front of the SPA, handles OIDC server-side, issues session cookies
  to the browser, and forwards backend calls with the access token
  injected server-side. Concerns cleanly separated. **Default
  recommendation.**
- **Option B — memory-server hosts BFF endpoints.** The memory-server
  exposes `/auth/login`, `/auth/callback`, `/auth/logout` and honours
  session cookies on `/v1/*`. Tighter integration; expands the
  memory-server's surface beyond its current resource-server role.
  Use only if Option A's operational overhead is judged too high.

Both options keep tokens server-side. Both are acceptable. Neither
exposes access tokens to the browser.

### §6. Multi-issuer acceptance — unchanged

ADR-032 §2's `keycloak_issuer_extras: list[str]` covers UI tokens
without modification. Browser-originated flows eventually produce
tokens with whichever `iss` Keycloak observed the authorization
request on; deployments add the public UI hostname to the extras
list when provisioning.

### §7. CORS

The memory-server currently has no CORS middleware (agents
authenticate same-origin or over TLS without a browser). Adding
LibreChat or any other browser UI on a different origin requires
an **exact-match origin allow-list**, narrow, no wildcards,
populated per-deployment from the operator-configured UI hostname(s).

### §8. Provisioning

**Fresh deploys** — the updated `keycloak/realm-audittrace.json` ships
the `audittrace-webui` client definition (without the secret; the
secret is injected from Vault at deploy-time via Helm secret templating
or a post-boot admin API call).

**Existing deploys** — `scripts/setup-webui-client.sh` is idempotent,
creates/updates the client via the master-realm admin API, reads the
secret from Vault, and sets it on the client.

**Redirect URI configured at provisioning time**, never defaulted in
production.

### §9. LibreChat-side configuration (first consumer, reference)

- `OPENID_ISSUER_URL` → Keycloak realm's `.well-known/openid-configuration`
- `OPENID_CLIENT_ID` → `audittrace-webui`
- `OPENID_CLIENT_SECRET` → injected from Vault at container start
- `OPENID_CALLBACK_URL` → LibreChat `/oauth/openid/callback`
- `OPENID_REQUIRED_ROLE` / `OPENID_BUTTON_LABEL` → deployment choice
- `OPENAI_API_BASE` → memory-server `/v1` endpoint
- Browser ↔ LibreChat: HTTPS + `HttpOnly` + `Secure` + `SameSite=Strict`
  session cookie. **No access token ever materialises in the browser.**

## Consequences

### Positive

- **Access tokens stay server-side.** LibreChat's session store (or a
  future dedicated BFF) holds them. Browser carries only a session
  cookie. Compliant with the draft-26 strong-recommendation for
  sensitive applications.
- **Zero changes to the memory-server JWT validation code path.** Same
  JWKS cache, same audience mapper, same scope enforcement, same RLS
  binding.
- **Per-user identity through the UI path.** RLS bites at user
  granularity on every chat request; the UI does not collapse users
  into a service-account identity.
- **Client-secret hardening + PKCE S256 + optional PAR → OAuth 2.1
  aligned.** Defence-in-depth beyond what a confidential client
  alone would provide.
- **Vault dependency is correctly sequenced.** M1 ships before M2/M3;
  the client secret lands in Vault before LibreChat is stood up.
- **External IdP federation is transparent to the UI.** When Keycloak
  is later configured to broker to an organisational IdP (Entra,
  Okta, Google Workspace), the UI path inherits it without any
  change to this ADR — the OIDC dance looks identical to LibreChat
  regardless of where the user's identity ultimately resolves.
- **Explicit policy against pure-SPA OAuth clients** closes off a
  class of vulnerabilities (token exfiltration via XSS) before any
  UI work begins.

### Negative / caveats

- **Confidential-client secret rotation is an operational concern.**
  Solved by Vault's dynamic-secrets or periodic-rotation pattern;
  operationally ADR-043-ish follow-up.
- **LibreChat's session-store implementation is outside our custody.**
  We rely on LibreChat's OIDC + cookie hygiene. Vetting the LibreChat
  release version and pinning it in the Helm chart is part of
  provisioning. Consider tracking upstream CVEs.
- **PKCE-on-confidential-client adds a small config complexity** for a
  real-but-minor hardening win. Worth it; no negotiation.
- **CORS middleware is a new surface** on the memory-server. Narrow
  allow-list, exact-match origin, covered by tests.
- **Multiple clients in the realm** (`audittrace-dev` + `audittrace-opencode`
  + `audittrace-webui`). Each with its own scope allocation, redirect
  URI discipline, and secret (where applicable). Documented here and
  in the realm README.

### Follow-ups

- **Implementation** — `keycloak/realm-audittrace.json` update (client
  definition minus secret) + `scripts/setup-webui-client.sh` provisioner
  + LibreChat Helm sub-chart or standalone manifest + CORS middleware
  in the memory-server. Target window: post-Vault (M1), post-IdP-
  federation (M2).
- **External IdP federation** — separate ADR (proposed **ADR-043**).
  Keycloak brokers to organisational IdPs; this ADR's UI path inherits
  the brokered flow without modification. ADR-043 lands before any
  production UI deploy where users are federated.
- **Backchannel logout.** Keycloak pushes logout events; LibreChat
  honours `backchannel_logout_uri`. Closes the "logged out of Keycloak
  but LibreChat session lingered" gap. Non-blocking for a first cut.
- **PAR (RFC 9126) rollout.** Enable where the deployed Keycloak
  supports it; confirm LibreChat's OIDC client advertises support.
  Not a blocker.
- **Second UI consumer pattern.** Open WebUI, a future custom admin
  surface, or internal Langfuse SSO each land as their own confidential
  client (if server-side) or their own BFF (if pure-SPA). This ADR
  sets the template; per-consumer follow-ups only if the pattern
  diverges.
- **Deferred decision** — Option A (dedicated BFF sidecar) vs Option B
  (memory-server-hosted BFF) for any future pure-SPA UI. Revisit when
  a concrete SPA proposal arrives.
