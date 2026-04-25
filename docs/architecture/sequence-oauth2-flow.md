# Sequence Diagram: OAuth2 + Identity Resolution (DESIGN §15, ADR-032)

> **Updated 2026-04-15** for ADR-032 (OAuth2 Device Authorization Grant
> + multi-issuer JWT validation). Prior updates: 2026-04-11 for the §15
> refactor (Keycloak-delegated identity + Redis-backed token cache).
>
> The previous PAT model from Phase 0/1 was dropped via Alembic migration
> 004. Authentication today is two flows side-by-side: **Device Flow for
> humans** (this document's headline path) and **client_credentials for
> service accounts** (CI, smoke tests).

## The three-script surface (ADR-032)

Three CLI tools ship with the Device Flow; everything else in this
document is the protocol they implement.

| Script | Role |
|---|---|
| `scripts/audittrace-login` | User-facing login: interactive Device Flow (default), `--show` (print access_token, silent refresh), `--ensure` (refresh-if-needed, exit 0 on valid), `--logout` (delete tokens). Tokens land in `~/.config/audittrace/tokens.json` mode 0600. |
| `scripts/opencode-wrapper.sh` | Canonical OpenCode launcher. Runs `--ensure`, merges `Authorization: Bearer <token>` into every provider in `~/.config/opencode/config.json`, execs `opencode`. One-command session start. |
| `scripts/setup-human-user.sh` | Realm provisioner for already-running Keycloak instances that pre-date ADR-032. Uses the master-realm admin API to create the `audittrace-opencode` public client + `luis` realm user. Idempotent. |

## Token acquisition (OAuth2 Device Flow — for human users)

The human runs `scripts/audittrace-login` once per machine. Keycloak
returns a `user_code` + `verification_uri_complete`; the user opens
that URL in any browser, logs in, and the script's polling loop picks
up the access_token + refresh_token. Tokens persist to
`~/.config/audittrace/tokens.json` (mode 0600) with absolute expiry
timestamps so no consumer has to track "when did I fetch this".

```mermaid
sequenceDiagram
    participant User as Luis (human)
    participant CLI as scripts/audittrace-login
    participant Browser as Browser
    participant KC as Keycloak\naudittrace realm\n(Istio Gateway-fronted :443)

    User->>CLI: scripts/audittrace-login
    CLI->>KC: POST /realms/audittrace/protocol/openid-connect/auth/device\nclient_id=audittrace-opencode\nscope=openid audittrace:query audittrace:context\n      audittrace:audit memory:episodic:read\n      memory:procedural:read memory:conversational:read-own\n      memory:semantic:read
    KC-->>CLI: {device_code, user_code, verification_uri,\nverification_uri_complete, expires_in: 600, interval: 5}

    CLI->>User: Open URL + enter code (or follow verification_uri_complete)
    User->>Browser: visit URL
    Browser->>KC: GET /realms/audittrace/device?user_code=XXXX-YYYY
    KC->>Browser: login form
    User->>Browser: username=luis + password
    Browser->>KC: POST credentials
    KC-->>Browser: consent screen (if first time)
    User->>Browser: approve
    Browser->>KC: POST consent

    rect rgb(230, 240, 250)
        Note over CLI,KC: Polling loop — every `interval` seconds until expires_in
        CLI->>KC: POST /token\ngrant_type=urn:ietf:params:oauth:grant-type:device_code\nclient_id=audittrace-opencode\ndevice_code=...
        Note over KC: First polls: {error: authorization_pending}\nOnce user approves: {access_token, refresh_token, ...}
        KC-->>CLI: {access_token, refresh_token,\nexpires_in (access, seconds),\nrefresh_expires_in (refresh, seconds),\ntoken_type: "Bearer"}
    end

    Note over CLI: Persist ~/.config/audittrace/tokens.json (mode 0600):\n{access_token, refresh_token,\n access_expires_at: now + expires_in,\n refresh_expires_at: now + refresh_expires_in,\n realm_issuer, client_id}
    CLI-->>User: ✅ logged in — tokens saved
```

## Token refresh (silent, inside the SSO session lifetime)

`audittrace-login --show` and `--ensure` check the saved
`access_expires_at` against now. If the access token is within
`REFRESH_THRESHOLD_SECONDS` (default 60) of expiry, the CLI silently
posts a `refresh_token` grant, overwrites `tokens.json` with the new
pair, and returns. Callers (wrappers, ad-hoc scripts) never see a
401 from expired tokens while the refresh chain still holds.

```mermaid
sequenceDiagram
    participant Caller as opencode-wrapper.sh\nor BEARER=$(audittrace-login --show)
    participant CLI as audittrace-login
    participant FS as ~/.config/audittrace/tokens.json
    participant KC as Keycloak

    Caller->>CLI: --show (or --ensure)
    CLI->>FS: read access_expires_at, refresh_token
    alt access_expires_at - now > 60s (REFRESH_THRESHOLD_SECONDS)
        Note over CLI: plenty of life left — return current access_token as-is
        CLI-->>Caller: access_token
    else near expiry
        CLI->>KC: POST /token\ngrant_type=refresh_token\nclient_id=audittrace-opencode\nrefresh_token=...
        KC-->>CLI: {access_token, refresh_token,\nexpires_in, refresh_expires_in}
        CLI->>FS: overwrite tokens.json (mode 0600)
        CLI-->>Caller: fresh access_token
    end
```

After the refresh chain itself expires (realm `ssoSessionMaxLifespan`
+ `offlineSessionIdleTimeout`, both set to 30 days in our realm JSON),
`--ensure` exits non-zero; the wrapper falls back to the interactive
login path.

## Token acquisition (OAuth2 client_credentials — for service accounts)

Headless agents (CI jobs, automation, smoke tests) use
`audittrace-dev` via `client_credentials`. Same output shape —
a Keycloak-signed JWT — but the token's `iss` claim carries the
**internal** docker-network hostname because the client authenticates
from inside the stack (see `scripts/mint-dev-jwt.sh`). **No PATs
anywhere in the system after the §15 refactor.** See ADR-032 §2 for
why both issuer values coexist.

```mermaid
sequenceDiagram
    participant CI as CI Job
    participant KC as Keycloak

    Note over CI: CI holds RSA-2048 private key\n(generated by generate-client-keys.sh)

    CI->>CI: Build client_assertion JWT:\niss = client_id\nsub = client_id\naud = keycloak token endpoint\nexp = now + 60s\nSign with private key (RS256)

    CI->>KC: POST /protocol/openid-connect/token\ngrant_type=client_credentials\nclient_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer\nclient_assertion=<signed JWT>\nscope=memory:read memory:read-decisions

    KC->>KC: Validate client_assertion:\n1. Lookup client by issuer\n2. Verify RS256 signature\n3. Check expiry + audience

    KC-->>CI: {"access_token": "<RS256 JWT>", "token_type": "Bearer", "expires_in": 300}
```

## Federated login via brokered IdP (ADR-044) — for human users with an organisational IdP

When a deployment is configured with one or more brokered identity
providers (`scripts/setup-idp-federation.sh` per ADR-044 §7), the
human's login surface includes a "Sign in with <IdP>" button on the
Keycloak login page. The user authenticates against their own
employer's IdP; Keycloak brokers the result into a shadow user in
the `audittrace` realm and issues its own JWT — signed with the
realm key, audience-mapped to `audittrace-server`.

Critical invariant: **the memory-server never talks to the upstream
IdP.** Every JWT it validates is signed by the audittrace realm; the
multi-issuer logic from ADR-032 §2 covers brokered tokens unchanged
because the brokered JWT's `iss` is the realm's own issuer.

```mermaid
sequenceDiagram
    participant User as Human User
    participant Browser as Browser
    participant KC as Keycloak\n(audittrace realm,\nbroker)
    participant IdP as Organisational IdP\n(Entra / Okta /\nGoogle Workspace / ...)
    participant App as memory-server\n(/v1/chat/completions)

    Note over User,KC: First-time federated login (JIT provisioning)

    User->>Browser: Open https://audittrace.local/...
    Browser->>KC: GET /realms/audittrace/protocol/openid-connect/auth\n?client_id=audittrace-webui\n&redirect_uri=...\n&response_type=code\n&scope=openid&...
    KC-->>Browser: 200 — login page with "Sign in with <alias>" button

    User->>Browser: clicks "Sign in with <alias>"
    Browser->>KC: GET /realms/audittrace/broker/<alias>/login
    KC->>KC: build PKCE challenge (S256)\nlookup IdP config from realm
    KC-->>Browser: 302 → upstream IdP authorize URL\n(state, nonce, code_challenge)

    Browser->>IdP: GET /authorize\n(client_id=keycloak-as-IdP-client,\nstate, nonce, code_challenge)
    IdP->>User: presents IdP-side login (UPN, MFA, etc.)
    User->>IdP: completes auth
    IdP-->>Browser: 302 → Keycloak broker callback URL\n?code=<authz code>&state=...

    Browser->>KC: GET /realms/audittrace/broker/<alias>/endpoint\n?code=<authz code>&state=...
    KC->>IdP: POST /token\ngrant_type=authorization_code\ncode=<authz code>\nclient_secret=$(vault kv get .../idp/<alias>/client_secret)\ncode_verifier=...
    IdP-->>KC: {id_token, access_token}

    Note over KC: Validate signature against upstream JWKS\n(useJwksUrl=true, validateSignature=true,\nstoreToken=false)
    KC->>KC: extract claims (sub/email/preferred_username/groups)
    KC->>KC: apply attribute mappers per ADR-044 §4\n(Entra: collapse oid → federation key)

    alt First time this upstream user logs in
        KC->>KC: JIT provisioning: create shadow user in realm\nbound to upstream sub via federation key
    else Returning user
        KC->>KC: syncMode=FORCE — refresh shadow user attributes\n(group memberships re-synced from upstream)
    end

    KC->>KC: mint Keycloak-signed JWT\n(iss = audittrace realm,\nsub = realm shadow-user UUID,\naud = audittrace-server,\nscopes from realm-role mapping)
    KC-->>Browser: 302 → original redirect_uri?code=<realm authz code>

    Note over Browser: Continues with the audittrace-webui Auth Code\nflow per ADR-042 — Browser ↔ LibreChat session cookie,\nLibreChat ↔ Keycloak, Bearer JWT to memory-server.

    Browser->>App: (eventually) request with Bearer <realm JWT>
    App->>App: require_user — validate against realm JWKS\n(multi-issuer path from ADR-032 §2,\nbrokered iss = realm iss, no extras needed)
    App-->>Browser: 200 OK with user_id = realm shadow-user UUID
```

Two operational outcomes worth highlighting:

- **Deprovisioning is upstream-controlled.** When the customer
  removes an employee from their IdP, the next login attempt fails
  at the upstream `/authorize` step. The Keycloak shadow user
  remains in the realm but stops being usable — a customer-side
  audit can scrub stale shadows on their own cadence.
- **Group changes propagate on next login.** `syncMode=FORCE` means
  Keycloak re-fetches the upstream attributes on every brokered
  login, including group memberships. Realm-role mappings re-evaluate;
  scopes change accordingly. No long-lived stale shadow.

The next two sections describe what happens AFTER the JWT lands at
the memory-server. They apply identically whether the JWT came from
Device Flow, `client_credentials`, or a brokered login — the
audience mapper and the `iss` claim are the same.

## Identity resolution at the proxy (the §15 hot/cold path)

Every authenticated request flows through `require_user`. Hot path is a
Redis cache lookup (sub-millisecond). Cold path validates the JWT against
Keycloak's JWKS endpoint, builds a typed `UserContext`, and writes it to
the cache for subsequent requests.

```mermaid
sequenceDiagram
    participant Agent as Coding Agent
    participant Gateway as Istio Gateway (TLS)
    participant Auth as require_user\n(auth.py)
    participant Cache as TokenCache\n(identity.py)
    participant Redis as audittrace-redis
    participant KC as Keycloak\n(JWKS only)
    participant Route as Chat Route

    Agent->>Gateway: POST /v1/chat/completions\nAuthorization: Bearer <JWT>
    Gateway->>Auth: TLS terminated → HTTP (mTLS via Envoy sidecar)

    Auth->>Auth: token_hash = sha256(raw_token)

    rect rgb(220, 240, 220)
        Note over Auth,Redis: HOT PATH — sub-millisecond
        Auth->>Cache: get(token_hash)
        Cache->>Redis: GET audittrace:token:<hash>
        Redis-->>Cache: JSON UserContext
        Cache-->>Auth: UserContext
    end

    Note over Auth: Cache HIT — no Keycloak round-trip,\nno JWKS validation

    Auth-->>Route: UserContext (typed, frozen)

    Route->>Route: process request\n(chat completion, tool calls, etc.)
    Route-->>Agent: 200 OK + response
```

### Cold path — first request with a new token

```mermaid
sequenceDiagram
    participant Agent as Coding Agent
    participant Auth as require_user
    participant Cache as TokenCache
    participant Redis as audittrace-redis
    participant KC as Keycloak

    Agent->>Auth: POST /v1/chat/completions\nAuthorization: Bearer <new JWT>

    Auth->>Auth: token_hash = sha256(raw_token)

    Auth->>Cache: get(token_hash)
    Cache->>Redis: GET audittrace:token:<hash>
    Redis-->>Cache: nil
    Cache-->>Auth: None

    Note over Auth: Cache MISS → cold path

    rect rgb(255, 230, 220)
        Note over Auth,KC: COLD PATH — ~1-2ms
        Auth->>KC: GET /realms/audittrace/protocol/openid-connect/certs\n(JWKS — cached 5 min in _jwks_cache)
        KC-->>Auth: {"keys": [...]}
        Auth->>Auth: _decode_jwt_with_allowed_issuers(token, keys, aud,\n  primary=keycloak_issuer,\n  extras=keycloak_issuer_extras)\n  1. jwt.decode(token, keys, RS256, audience) — no single-issuer lock\n  2. cross-check payload.iss against {primary} ∪ extras (ADR-032 §2)
    end

    Auth->>Auth: Build UserContext from claims:\nuser_id = sub\nusername = preferred_username\nscopes = scope.split()\ntoken_id = jti\nis_admin = is_admin_scope(scopes)

    Auth->>Cache: put(token_hash, UserContext, ttl=min(jwt.exp - now, 300))
    Cache->>Redis: SETEX audittrace:token:<hash> <ttl> <json>

    Auth-->>Agent: continues to chat handler with UserContext
```

### Failure cases

```mermaid
sequenceDiagram
    participant Agent as Coding Agent
    participant Auth as require_user

    Agent->>Auth: POST /v1/chat/completions\n(no Authorization header)
    Auth-->>Agent: HTTP 401\n{"detail": "Missing authentication token"}

    Agent->>Auth: POST /v1/chat/completions\nAuthorization: Bearer <expired JWT>
    Note over Auth: JWKS validation fails (exp < now)
    Auth-->>Agent: HTTP 401\n{"detail": "Invalid or expired token"}

    Agent->>Auth: POST /v1/chat/completions\nAuthorization: Bearer <wrong audience>
    Note over Auth: JWKS validation fails (aud mismatch)
    Auth-->>Agent: HTTP 401\n{"detail": "Invalid or expired token"}

    Agent->>Auth: POST /v1/chat/completions\nAuthorization: Bearer <JWT with no sub claim>
    Note over Auth: Validation succeeds, but Build UserContext fails
    Auth-->>Agent: HTTP 401\n{"detail": "Token missing subject claim"}
```

### Bypass mode (development / migration window)

When `AUDITTRACE_AUTH_REQUIRED=false` (the default during the multi-user
migration window), `require_user` short-circuits the entire flow and
returns a sentinel `UserContext`. No JWKS fetch, no cache lookup, no
Keycloak round-trip. Used so existing tests and dev workflows continue
to work unchanged until Phase 5 flips the flag.

```mermaid
sequenceDiagram
    participant Agent as Coding Agent
    participant Auth as require_user

    Agent->>Auth: POST /v1/chat/completions\n(no auth required mode)
    Auth->>Auth: settings.auth_required == False
    Note over Auth: Build sentinel UserContext:\nuser_id = SENTINEL_SUBJECT\nscopes = (memory:admin, memory:read, session:read-own)\nis_admin = True
    Auth-->>Agent: continues to chat handler with sentinel UserContext
```

## Token revocation under the new model

Two layers of revocation, with different latencies:

1. **Keycloak-side revocation** (admin disables a user, removes a client,
   rotates a key) takes effect immediately for *new* token issuance and
   *cold path* validations. Tokens already in the Redis cache continue
   to validate until their cache TTL expires.
2. **Cache-side eviction** happens automatically on TTL (default 300s).
   Manual eviction via `TokenCache.invalidate(token_hash)` is available
   for the future logout endpoint.

**Maximum revocation latency:** the cache TTL (5 minutes by default).
This is a deliberate trade-off for performance — see DESIGN §15.6 for
the full reasoning. Tighter revocation is one config flip away
(`AUDITTRACE_TOKEN_CACHE_TTL_SECONDS=60`).

## Scope vocabulary

Scopes come from the Keycloak realm — NOT from a local roles→scopes
mapping table. The realm administrator configures which OAuth2 scopes
are granted to which clients via the Keycloak admin console (or, in
our case, via the shipped `keycloak/realm-audittrace.json` +
`scripts/setup-human-user.sh`). The `scope` claim in the JWT is
authoritative.

The audittrace realm declares these nine client-scopes (source of
truth: `keycloak/realm-audittrace.json`):

| Scope | Granted to | What it gates |
|---|---|---|
| `audittrace:query` | every authenticated client | `/v1/chat/completions`, `/session/save` |
| `audittrace:context` | humans + context-builder clients | `/context` endpoint |
| `audittrace:audit` | humans + admin-client | `/interactions` audit endpoint (ADR-029) |
| `audittrace:index` | `inject-memory` client only | write-side memory indexing |
| `audittrace:admin` | `admin-client` only | `/metrics` + admin-only ops |
| `memory:episodic:read` | humans + dev client | `recall_decisions` tool (ADR-025) — read ADRs |
| `memory:procedural:read` | humans + dev client | `recall_skills` tool — read SKILL files |
| `memory:conversational:read-own` | humans + dev client | `recall_recent_sessions` tool — read your own chat history |
| `memory:semantic:read` | humans + dev client | `recall_semantic` tool — vector search |

**Client → scope matrix** (shipped defaults):

| Client | Flow | Default scopes |
|---|---|---|
| `audittrace-opencode` (ADR-032, humans) | Device Flow | `audittrace:query`, `:context`, `:audit`, all four `memory:*` |
| `audittrace-dev` (CI / smoke) | client_credentials | identical to `audittrace-opencode` — so the dev path exercises the full scope surface |
| `opencode-agent`, `continue-agent`, `roocode-agent` | client-JWT client_credentials | `audittrace:query` only (legacy, pre-ADR-032) |
| `inject-memory` | client-JWT | `audittrace:context`, `:index` |
| `admin-client` | client-JWT | `audittrace:admin`, `:audit` |

`is_admin` in the resolved `UserContext` is derived programmatically
via `is_admin_scope()` — true when the `audittrace:admin` scope is
present. No `memory:admin` or `admin:*` scope exists in the realm;
admin capability flows through `audittrace:admin` only.
