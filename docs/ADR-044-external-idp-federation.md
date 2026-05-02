# ADR-044: External IdP federation via Keycloak brokering

**Status:** Accepted (2026-05-02 — live evidence captured)
**Date:** 2026-04-26 (proposed) · 2026-05-02 (accepted)
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-022 (Keycloak realm), ADR-023 (JWT validation + JWKS
caching), ADR-026 (multi-user identity), ADR-032 (OAuth2 Device Flow
for agents — established multi-issuer JWT validation), ADR-041
(product boundary — IdP is one of the eight named dependencies),
ADR-042 (BFF-first OIDC for UIs — depends on this ADR for the broker
configuration that lets UIs authenticate against an organisational
IdP), ADR-043 (Vault — IdP client secrets live there)

## Context

ADR-041 names "Identity Provider (OAuth2 / OIDC)" as one of the eight
dependencies the AuditTrace-AI product expects to integrate against,
not bundle. Today's chart bundles Keycloak as a hand-templated
deployment with its own realm — perfectly suitable for the
laptop-first dev profile and for self-hosted POCs, but **wrong** for
the customer-ready deployment story:

- A regulated-tier customer keeps their existing IdP (Microsoft Entra
  ID, Okta, Google Workspace, Ping Identity, etc.). Onboarding their
  users into a separate Keycloak directory creates a duplicate
  identity surface, an extra credential lifecycle to maintain, and a
  governance objection that lands the conversation back at "your
  software is not enterprise-ready."
- The pitch position is "we don't manage your employees; we
  authenticate against your IdP." The product boundary depends on
  this being true at deployment time, not aspirational.
- LibreChat (ADR-042 §2) ships in M3 as a `audittrace-webui`
  confidential client. When the user logs in via LibreChat, the
  user's identity must originate from their organisational IdP, not
  a Keycloak shadow user.

The deployable answer Keycloak already supports is **brokering**:
keep the AuditTrace Keycloak as the sole token issuer that the
memory-server validates against, and let Keycloak federate with the
customer's IdP via the `identityProviders` block. The customer's IdP
remains the identity source-of-truth; Keycloak mints its own
audience-mapped JWT downstream.

This ADR records the brokering posture, the supported IdP types, the
attribute-mapping contract, and the operator-side provisioning
workflow. **No code changes** to memory-server's auth path are
required: ADR-032 §2 already established multi-issuer JWT validation
via `keycloak_issuer_extras`, which covers brokered tokens out of
the box.

## Decision

### §1. Keycloak is the broker, not the source-of-truth

The realm `audittrace` continues to be the only OIDC issuer the
memory-server validates against. The memory-server JWKS cache, the
audience mapper (`aud=audittrace-server`), and every existing scope
remain unchanged.

What changes is **how users land in the realm**: the realm's
`identityProviders` array gains one entry per organisational IdP the
deployment brokers against. On the user's first login through that
IdP, Keycloak creates a shadow user in the audittrace realm bound
to the upstream identity (`sub` from the IdP), then issues its own
JWT signed with the realm key.

This is brokering, not federation in the SAML sense. The downstream
contract — what the memory-server sees on a request — is identical
whether the user authenticated locally, via Device Flow (ADR-032),
or via a brokered IdP.

### §2. Three IdP protocol types supported

Keycloak's broker engine handles three protocol families, each with
slightly different config:

| Protocol | Use case | Config key |
|---|---|---|
| **OIDC** | Microsoft Entra ID, Google Workspace, Okta-as-OIDC, Ping Identity, Auth0, any standards-compliant OIDC IdP | `oidc` |
| **SAML 2.0** | ADFS, legacy enterprise SAML, Okta-as-SAML | `saml` |
| **Social** (OAuth2 + provider-specific shims) | GitHub, Google personal accounts, LinkedIn — only for low-stakes deployments | per-provider type identifiers (`google`, `github`, etc.) |

This ADR defines the OIDC integration in detail. SAML is supported
by the same broker mechanism and the same attribute-mapping contract
(§4), but the realm-JSON shape differs; SAML configurations are
documented in the operator runbook rather than reproduced here.

Social providers are explicitly **not recommended** for production
deployments because the upstream identity is consumer-grade
(personal Google account, personal GitHub account) and lacks the
organisational governance — group memberships, deprovisioning on
employee exit, MFA enforcement — that regulated-tier customers
require. They are kept available for dev / sandbox testing only.

### §3. OIDC IdP configuration shape

The realm's `identityProviders` array carries one entry per brokered
IdP. The minimum-viable OIDC config, abbreviated:

```json
{
  "identityProviders": [
    {
      "alias": "<short-name>",
      "providerId": "oidc",
      "enabled": true,
      "trustEmail": false,
      "storeToken": false,
      "addReadTokenRoleOnCreate": false,
      "firstBrokerLoginFlowAlias": "first broker login",
      "config": {
        "issuer": "<discovery-issuer-URL>",
        "authorizationUrl": "...",
        "tokenUrl": "...",
        "userInfoUrl": "...",
        "jwksUrl": "...",
        "useJwksUrl": "true",
        "validateSignature": "true",
        "clientId": "<keycloak-as-IdP-client>",
        "clientAuthMethod": "client_secret_basic",
        "clientSecret": "${vault:idp/<alias>/client_secret}",
        "defaultScope": "openid email profile",
        "syncMode": "FORCE",
        "pkceEnabled": "true",
        "pkceMethod": "S256"
      }
    }
  ]
}
```

Three security-posture points:

- **`storeToken: false`** — Keycloak does NOT keep the upstream IdP's
  access token. We do not need to call upstream APIs on the user's
  behalf; Keycloak's own JWT is sufficient downstream.
- **`validateSignature: true`** — Every login validates the upstream
  IdP's signature against its JWKS. The `useJwksUrl: true` setting
  rotates with the upstream IdP automatically.
- **`syncMode: FORCE`** — On every login, the shadow user's
  attributes are re-synced from the upstream IdP. This means a
  group-membership change at the IdP (e.g. employee moved teams)
  reflects on the next login; we never have a stale Keycloak shadow.
- **`pkceEnabled: true`, `pkceMethod: S256`** — PKCE on the
  Keycloak-as-client side, even with a confidential client, per the
  same defence-in-depth posture as ADR-042 §2.

The **client secret per IdP** is read from Vault at
`kv/audittrace/idp/<alias>/client_secret` (per ADR-043 §5), never
from `values.yaml`, never from the realm JSON checked into the repo.
The realm JSON ships with a Vault-substitution placeholder; the
provisioner script (§7) materialises the secret at apply time.

### §4. Attribute-mapping contract

Every brokered IdP MUST map its claims into Keycloak's user record
in a consistent way so the downstream memory-server contract is
uniform. The mapping is:

| Keycloak user field | Source IdP claim | Required? | Notes |
|---|---|---|---|
| `username` | `preferred_username` (OIDC) or `nameid` (SAML) | required | Falls back to `email` if not provided |
| `email` | `email` | required | Used for `tokenCache` keying via `sub`; not displayed downstream |
| `firstName` | `given_name` | optional | UI-only |
| `lastName` | `family_name` | optional | UI-only |
| Keycloak `sub` | Generated locally from the upstream `sub` + IdP alias | derived | This is what the memory-server sees in the JWT's `sub` claim |
| Realm role | mapped from upstream `groups` claim (OIDC) | optional | One mapper per (upstream group → realm role) pair; configured per-deployment |

**Entra-specific gotcha (Microsoft):** Entra issues `oid` (Object
ID) as the durable user identifier and `sub` as a per-application
pseudonymous ID. The OIDC broker config MUST set
`config.userInfoUrl` correctly and add an attribute mapper that
collapses `oid` to a stable Keycloak federation key. Without this,
the same Entra user can land as two different Keycloak shadow users
across login sessions. This is the most common failure mode in
production Entra integrations and warrants a §Risks call-out.

### §5. JIT user provisioning

By default, Keycloak's broker creates a shadow user on first login
("Just-In-Time" provisioning). This is the right default for
laptop-first / POC deployments — no separate user-import workflow
needed. The realm config sets `firstBrokerLoginFlowAlias` to the
default flow, which auto-creates the user without operator
approval.

For deployments where pre-provisioning is required (e.g. group-based
licensing where membership in an upstream group is a precondition
for AuditTrace access), the realm flow can be switched to a
pre-provisioned mode. That's a per-deployment runbook concern, not
a chart-level decision.

### §6. Multi-issuer JWT validation — unchanged from ADR-032 §2

ADR-032 §2 already established the path: the memory-server's
`AUDITTRACE_KEYCLOAK_ISSUER_EXTRAS` env var carries a list of
acceptable `iss` values, and `_decode_jwt_with_allowed_issuers` in
`src/audittrace/auth.py` walks that list. Brokered tokens pass
through this path unchanged because:

- Keycloak signs the brokered JWT with its OWN realm key (not the
  upstream IdP's key).
- The brokered JWT's `iss` is the audittrace realm's issuer (not the
  upstream IdP's issuer).
- The audience mapper (`aud=audittrace-server`) fires identically to
  Device Flow tokens.

**No code change is required in M2.** The chart's
`AUDITTRACE_KEYCLOAK_ISSUER_EXTRAS` value already covers
Istio-Gateway-exposed and internal-DNS issuers (per ADR-032
§2). No new entries are required for brokered IdPs — the brokered
JWT's `iss` is unchanged from a non-brokered Keycloak login.

### §7. Provisioning workflow

A new operator script `scripts/setup-idp-federation.sh` adds a
single brokered IdP to the realm. Idempotent (creates or updates).
Reads the client secret from Vault if `vault.enabled=true`,
otherwise from `${IDP_CLIENT_SECRET}` env var.

Inputs:

```
IDP_TYPE=entra|google|okta|oidc-generic   (required)
IDP_ALIAS=<short-name>                     (required, e.g. "entra-acme")
IDP_DISCOVERY_URL=<...well-known URL>      (required)
IDP_CLIENT_ID=<keycloak-as-IdP-client>     (required)
IDP_CLIENT_SECRET=<...>                    (env or Vault)
IDP_GROUPS_TO_ROLES=<json mapping>         (optional)
KEYCLOAK_ADMIN_PASSWORD=<...>              (env or Vault)
```

Implementation pattern: kcadm.sh inside the audittrace-keycloak pod
(matches the Keycloak admin rotation pattern from
`feedback_keycloak_three_witnesses`). The script renders an OIDC
identity-provider JSON, posts it via `kcadm.sh create
identity-providers`, then adds the standard attribute mappers.

**Realm import remains idempotent.** A fresh `helm install`
deploying the realm JSON does NOT include any specific IdP — the
deployment-specific list of brokered IdPs is added post-install via
the provisioner script. The realm JSON in the repo is the
no-federation baseline; the provisioner is what materialises the
live deployment's IdP set.

### §8. Test matrix

Three IdPs are validated as part of M2:

| Scenario | Purpose |
|---|---|
| Keycloak realm with no brokered IdP (default) | Regression check that Device Flow + service-account flows are unaffected |
| Google Workspace as the brokered IdP | Third-party OIDC reference; most predictable test environment for OIDC mechanics |
| Microsoft Entra ID as the brokered IdP | Validation of the `oid`-vs-`sub` mapping (per §4) and the production-target shape |

Okta is **not** in the M2 validation matrix; its config shape is
covered by the `oidc-generic` template and tested when an Okta
deployment surfaces. The Okta-specific edge cases (tenant subdomains,
group-claim shape) are an operator-runbook concern, not a chart
decision.

For each scenario, the live evidence captured is:

1. A federated test user logs in end-to-end (browser flow against
   the upstream IdP, ending at a Keycloak-issued JWT).
2. The memory-server `/v1/chat/completions` accepts that JWT and
   produces a 200 with the user's federated `sub`.
3. The Postgres `interactions` row shows the Keycloak-mapped
   `user_id` (the federation key, not the upstream IdP's `sub`).
4. RLS isolation: a second federated user (from the same upstream
   IdP) cannot read user 1's interactions. Captured via a deliberate
   cross-user SELECT that fails.

## Consequences

### Positive

- **Customer's identity source-of-truth stays where it is.** The
  AuditTrace deployment never sees the customer's password, MFA
  factor, or directory; only audience-mapped JWTs derived from a
  signature-validated upstream session.
- **Deprovisioning works without our involvement.** When a customer
  removes an employee from their IdP, the next login attempt fails
  upstream and the Keycloak shadow simply stops being refreshed.
- **No memory-server code changes.** ADR-032 §2 already covers the
  multi-issuer path. M2 is realm-config + provisioner-script + arch
  diagrams.
- **Clean sequencing for M3.** LibreChat (ADR-042) speaks to
  Keycloak; Keycloak brokers to the customer's IdP; the user's
  experience is "log in with [organisational] account." No chain
  break.
- **Per-deployment control.** Each deployment's IdP set is
  configured by its operator via the provisioner, not by a chart
  default. The realm JSON in the repo is brokering-empty.
- **PKCE everywhere.** `pkceEnabled=true` on the Keycloak-as-client
  side closes a class of redirect-interception attacks even when
  the upstream IdP doesn't strictly enforce it.

### Negative / caveats

- **Per-IdP client secret in Vault.** Operationally another secret
  to rotate (per IdP per deployment). The `kv/audittrace/idp/<alias>/
  client_secret` path convention keeps it tidy; rotation is a
  per-IdP runbook step.
- **Entra `oid` vs `sub` is a footgun.** The realm config MUST
  collapse `oid` to a stable Keycloak federation key, or the same
  Entra user lands as two different shadows. Catch it early via the
  M2 §8 test matrix.
- **`syncMode: FORCE` adds a small latency to every login.** Each
  login re-fetches the upstream user's attributes. For interactive
  flows this is invisible (one extra HTTPS round-trip during login,
  not on every API call). For high-volume service-account flows
  (which use Device Flow / client_credentials, not brokering)
  there is no impact.
- **Group-claim mapping is per-deployment.** No two customers' group
  hierarchies are alike. The provisioner takes a JSON mapping
  argument; the operator owns it.
- **No SAML support in the M2 PR.** SAML deployments are still
  possible — Keycloak supports them — but the M2 chart code, the
  provisioner, and the test matrix are OIDC-focused. SAML lands in
  a follow-up if and when a deployment needs it.

### Risks

- **Entra federation drift.** As above, the `oid`-mapper requirement
  is non-obvious. Document it prominently in the operator runbook;
  add an integration test that exercises a fresh Entra user across
  two login sessions and asserts the same Keycloak federation key.
- **JIT race condition on first login.** If two browser sessions for
  the same upstream user hit Keycloak simultaneously, two shadow
  users can be created. Keycloak v24 mostly handles this with a DB
  lock, but the failure mode exists. Add a uniqueness constraint on
  the federation key and a one-line comment in the provisioner so
  the next operator knows.
- **Realm JSON reimport overwrites brokered IdPs.** A fresh
  `kc.sh start --import-realm` reads the realm JSON from the chart
  and overwrites the live realm. The current import is gated by
  "realm already exists, skip" (per the M1 live-install logs), but
  any future realm-replace operation would wipe the brokered IdPs.
  Document this in the provisioner script's header so operators
  know to re-run setup-idp-federation.sh after a realm wipe.

## Architecture documentation impact

Per the cross-cutting "architecture documentation in lock-step"
rule, this ADR's PR includes:

- **`docs/architecture/workspace.dsl`** — new external actor
  "Organisational IdP" (in the C4 Context view) with a brokerage
  arrow into Keycloak. Closes drift item D12 from
  `DRIFT-20260426.md`.
- **`docs/architecture/sequence-oauth2-flow.md`** (or new
  `sequence-idp-federation.md` — decision deferred to
  implementation; prefer the inline edit unless the diagram becomes
  unreadable) — broker hop showing
  user → external IdP → Keycloak → audience-mapped JWT → memory-
  server. Closes drift item D13.
- **`docs/architecture/product-and-dependencies.md`** — IdP
  dependency row updates: gap closed by this ADR, brokerage
  mechanism named, attribute-mapping contract pointer added.

## Follow-ups

- **SAML support** — separate ADR (or §-extension to this one) when
  a deployment requires it. Keycloak supports SAML brokering out of
  the box; the realm-JSON shape and the provisioner template
  differ.
- **Pre-provisioning flow** — the JIT default is right for most
  deployments. If a deployment requires pre-provisioning (group
  membership as licence gate), the realm's first-broker-login flow
  switches; runbook addition.
- **Group-based scope allocation** — today's scopes are per-client
  (`audittrace-dev`, `audittrace-opencode`, `audittrace-webui`).
  Future enhancement: map upstream groups → realm roles → per-user
  scope sets, so customers can grant `audittrace:audit` only to a
  designated audit-team group. Not blocking M2.
- **IdP-specific feedback memories** — when an Entra (or Okta, or
  Google) deployment hits a quirk that's customer-agnostic, the
  lesson lands as a `feedback_*.md` rule. For example: an
  Entra-specific JWKS rotation cadence quirk would be a
  `feedback_entra_jwks_rotation.md` durable rule. Captured here so
  the postmortem channel exists.

## Live evidence (2026-05-02)

End-to-end federation proven against a real Google Workspace tenant
(`@allaboutdata.eu`). Test artefacts captured in operator notes
(`audittrace-private/runbooks/10-m2-google-live-evidence.md` —
private, not committed).

Highlights:

- **Federated JWT issued by Keycloak** with
  `iss=https://audittrace.allaboutdata.eu:30952/realms/audittrace`,
  `aud=audittrace-server`,
  `sub=9e7a8d0f-3b5c-4a78-9833-4d241ebd6027` (the broker shadow
  user's stable Keycloak federation key, unrelated to Google's
  internal user ID — proves §4 attribute mapping works).
- **Multi-issuer validation accepted the brokered token** without
  any audittrace code change. ADR-032 §2's
  `_decode_jwt_with_allowed_issuers` path holds.
- **`/v1/chat/completions` HTTP 200** in 3.89s for the federated
  user — full chat path (request → JWT validate → memory tool loop
  → llama-server → response → audit row).
- **Postgres `interactions` rows** persisted with
  `user_id=9e7a8d0f-…` (multiple rows from the curl probe + browser
  follow-ups).
- **Per-user RLS** enforced — the Postgres `audittrace_app` role
  (NOSUPERUSER + NOBYPASSRLS) forces `app.current_user_id` from the
  JWT `sub` claim on every transaction.

Architecture (this PR):

- A new public PKCE client `audittrace-webui` was added to the realm
  (additive, no impact on existing clients) so a browser-side
  Authorization Code + PKCE flow can complete against the realm —
  the demonstrated flow path that customer browser UIs (LibreChat,
  M3 Day-1) will follow.
- A minimalist single-page OIDC harness landed at `webui/` so the
  evidence run could exercise the full browser flow without
  depending on LibreChat. See `webui/README.md`.

Test matrix coverage:

- ✅ Google Workspace OIDC broker (this evidence run).
- ⏳ Microsoft Entra ID (deferred to a follow-up backlog item — the
  `oid`-mapper test from §4 still warrants a dedicated run).
- ✅ Multi-issuer validation regression (existing `TestMultiIssuer`
  unit tests in `tests/test_auth.py` continue to pass).

Out of scope, filed as backlog:

- The Entra production-shape pass with `oid`-mapper.
- Audit-row status correctness for chat responses that contain an
  ADR-033 error envelope (observed: row marked `success` even when
  the response was a 500 envelope; should be `failed` with
  `failure_class` populated).
- A `trace_id` column on the `interactions` table to make the
  Postgres-to-Tempo correlation a one-query lookup (today requires
  joining on `(user_id, session_id, timestamp)`).
