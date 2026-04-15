# ADR-032: OAuth2 Device Authorization Grant for Human Agents

**Status:** Accepted
**Date:** 2026-04-15
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-022 (OAuth2 scopes), ADR-023 (Keycloak realm), ADR-026 (multi-user identity), ADR-029 (project tagging)

## Context

Authenticating OpenCode (and Continue / Roo Code / any browser-less
coding agent) against AuditTrace-AI today is painful:

1. **Only `client_credentials` grant is wired.** Every agent session
   authenticates as the `sovereign-memory-dev` service account. Every
   interaction, tool_call, and session in Postgres lands under the
   same flat identity. The Phase 4 per-user RLS work (ADR-026 §16)
   does not meaningfully bite — there is only one user as far as the
   database can tell.
2. **Manual token minting is brittle.** `scripts/mint-dev-jwt.sh`
   requires the script to run *inside* the docker network (because
   Keycloak emits the `iss` claim matching the hostname of the
   incoming request, and only the internal `http://keycloak:8080`
   URL matches the memory-server's configured
   `SOVEREIGN_KEYCLOAK_ISSUER`). Running from the host fails with
   `Could not resolve host: keycloak` — confirmed in a live session
   on 2026-04-15.
3. **Access tokens are short-lived.** Every expired token means
   another manual mint. No refresh story, no silent re-auth.

The right pattern for agents like OpenCode is **OAuth2 Device
Authorization Grant** (RFC 8628). It is specifically designed for
clients that have no browser of their own: the agent asks for a
device code, shows the user a URL + short code, the user logs in
on any browser they have handy, and the agent gets a refreshable
access token bound to the real user's identity.

## Decision

Add a **parallel** authentication path alongside the existing
service-account path. The legacy flow stays intact (dev tooling,
automated tests continue to use `client_credentials`). The new
flow handles the human-user scenario.

### §1. Public Keycloak client `audittrace-opencode`

A new client in the `sovereign-ai` realm, configured as:

- `publicClient=true` — no client secret needed; Device Flow does
  not require one (the security comes from the user_code + browser
  login step, not from a client secret the agent would have to
  safeguard).
- `standardFlowEnabled=true` + `oauth2.device.authorization.grant.enabled=true`
  attribute — enables the Device Authorization endpoint.
- `serviceAccountsEnabled=false` — this client is for users, not
  service-to-service.
- `defaultClientScopes`: the full `memory:*` quadruple + `sovereign-ai:query`
  + `sovereign-ai:context` + `sovereign-ai:audit`. No admin scopes —
  admin operations keep using the separate admin-client.
- Audience mapper `aud=sovereign-memory-server` — identical to the
  dev client, so our existing JWT validation path accepts the token
  without modification.

### §2. Multi-issuer acceptance

The memory-server's JWT validation previously enforced a single
`iss` claim via python-jose's built-in `issuer` kwarg. This is the
root cause of the "tokens minted externally don't work" experience:
Keycloak emits `iss=http://keycloak:8080/realms/sovereign-ai` for
requests arriving via the docker-network internal URL (how the
dev client authenticates) and `iss=http://localhost/realms/sovereign-ai`
for requests arriving via Traefik (how browsers and external
Device-Flow clients authenticate). Both are signed by the same
Keycloak; they differ only in the `iss` string.

A new setting `keycloak_issuer_extras: list[str]` accepts additional
valid issuers. `_decode_jwt_with_allowed_issuers` validates the
token without a single-issuer enforcement and cross-checks `iss`
against the union of `keycloak_issuer` + `keycloak_issuer_extras`.
Production defaults (`docker-compose.yml`) set the extras to the
`http://localhost` + `https://localhost` Traefik variants so both
token families work out of the box.

This preserves backwards compatibility — deployments that do not
use Device Flow leave the list empty and the behaviour reverts to
exact single-issuer matching.

### §3. Token persistence

Tokens live in `~/.config/audittrace/tokens.json`, mode 0600, in a
directory created mode 0700. Shape:

```json
{
  "access_token": "eyJ…",
  "refresh_token": "eyJ…",
  "access_expires_at": 1776280000,
  "refresh_expires_at": 1778800000,
  "token_type": "Bearer",
  "realm_issuer": "http://localhost/realms/sovereign-ai",
  "client_id": "audittrace-opencode"
}
```

Absolute timestamps (epoch seconds) instead of `expires_in` so a
consumer never has to track "when did I fetch this". The refresh
threshold is a configurable window (`REFRESH_THRESHOLD_SECONDS`,
default 60) — tokens within that window of expiry refresh
transparently on `--show` and `--ensure`.

### §4. Tooling

Three scripts, each doing one thing:

- **`scripts/audittrace-login`** — user-facing CLI:
  - `audittrace-login` → initiate Device Flow, block on polling,
    persist tokens.
  - `audittrace-login --show` → print the current access_token to
    stdout; refresh silently if near expiry.
  - `audittrace-login --ensure` → refresh if needed, exit 0 when a
    valid token is available. For scripts that just need to know
    "am I authed?".
  - `audittrace-login --logout` → delete the saved tokens.
  - Stdlib-only (`curl` + `jq` only). ~250 LOC of bash.

- **`scripts/opencode-wrapper.sh`** — the launcher. Runs
  `--ensure`, falls back to `--show`, merges
  `Authorization: Bearer <token>` into every provider's
  `options.headers` in `~/.config/opencode/config.json` (backups
  alongside, atomic via mktemp+mv), then execs `opencode`. One
  command starts a properly-authed session.

- **`scripts/setup-human-user.sh`** — realm provisioner for
  *already-running* Keycloak instances. Uses the master-realm
  admin API to create the `audittrace-opencode` client + a
  `luis` realm user if they are not already present. Idempotent.
  Required because Keycloak's realm JSON import only runs on
  first-boot — existing deployments need a backfill path.

### §5. Provisioning: fresh vs existing deploys

Fresh deploys (`docker compose up` against an empty Postgres/Keycloak
state) pick up the new client + user via the updated
`keycloak/realm-sovereign-ai.json`. No extra steps required.

Existing deploys (Keycloak already has the sovereign-ai realm
from before this ADR) run `scripts/setup-human-user.sh` once to
add the new client + user via the admin API. The script is
idempotent — re-running against a realm that already has them
is a no-op.

The `luis` user ships with `temporary: true` on the password
(`change-me-on-first-login`). Keycloak's login page forces a
password-change on first login, so there is no persistent weak
password in the realm.

## Consequences

### Positive

- **Real per-user identity on every OpenCode request.** The
  `interactions.user_id` column now carries the Keycloak `sub`
  of the actual human, not the flat dev-client sub. RLS works as
  intended. Audit trail maps to real people, not to a service
  account.
- **One-command OpenCode launch** (`scripts/opencode-wrapper.sh`)
  replaces the multi-step manual token-minting dance.
- **Silent refresh within the SSO session lifetime** (30 days by
  default). A user logs in once and doesn't see Keycloak again
  until the refresh chain expires.
- **Drop-in for Continue + Roo Code** via the same header-merge
  pattern used by `configure-project.py` (ADR-029). Any
  OpenAI-compatible client with `options.headers` support works.
- **No client-secret management.** Public clients don't have one;
  the security comes from the user_code + browser login step.
- **Legacy flows unchanged.** Dev tooling using `mint-dev-jwt.sh`
  still works. CI integration tests using `client_credentials`
  against `sovereign-memory-dev` still work. This ADR is purely
  additive.

### Negative / caveats

- **Shipped temporary password in realm JSON.** The `luis` user
  arrives with `change-me-on-first-login` marked `temporary: true`;
  Keycloak forces a change on first login. That's standard, but
  it is still a default credential that a careful reader might
  flag. Acceptable for dev; production would provision users via
  an external IdP instead of shipping them in the realm JSON.
- **Multi-issuer acceptance broadens the trust surface.** Any
  token with `iss` in the allow-list + valid signature from our
  JWKS passes. The extras list must be kept tight in production —
  it is NOT a place to "just add another hostname" without
  thinking. Operator discipline required.
- **Token persistence is per-machine.** Two machines each need
  their own `audittrace-login`. No SSO across devices without
  additional infrastructure.
- **Keycloak `http://localhost` issuer is plaintext in the claim.**
  Traefik terminates TLS, but the claim still contains the `http://`
  scheme because of how Keycloak renders issuer strings under the
  current `KC_HOSTNAME=localhost` config. Cosmetic (the signature
  is what matters) but a dashboard reviewer might raise it.
- **Browser required somewhere.** A headless CI machine can't run
  Device Flow directly. CI continues to use the service-account
  path — which is the correct role-fit anyway.

### Follow-ups

- **External IdP integration.** Replace the local `luis` user with
  SSO against an organisational IdP (Google Workspace, Okta,
  EntraID) via Keycloak's `identityProviders` block. One-click
  "Login with Google" on the Device Flow verification page. No
  local passwords to manage.
- **Token-cache integration.** The memory-server's Redis-backed
  `TokenCache` (DESIGN §15.4) keys on `sha256(token)`. Device-Flow
  tokens rotate on refresh; the cache will grow with one entry per
  refresh until the TTL clears it. Not a bug but worth monitoring.
- **Expired-refresh UX.** After the SSO session lifetime
  (30 days), the refresh chain breaks and the user has to
  re-login. The wrapper handles this gracefully today; a future
  improvement is to signal the user earlier (warning at 7-day
  mark).
- **Audit trail for login events.** Keycloak emits events for
  successful / failed logins. Surfacing those in the
  observability-stack (now its own repo) would close the
  "who logged in when" gap.
