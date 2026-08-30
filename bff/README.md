# audittrace-librechat-bff

The M3 LibreChat console's Backend-for-Frontend sidecar (ADR-042 §5
Option A — dedicated sidecar, as opposed to Option B, hosting BFF
endpoints inside `audittrace-server` itself). Specs:
`specs/2026-08-24-SPEC-m3-librechat-console.md` (WU-2 scope, the chat
path) and `specs/2026-08-30-SPEC-m3-souvenirs-sovereign-memory.md`
(WU-D2-1, the memory-proxy path below).

## What it does

Two proxy surfaces, both fail-closed through the SAME exchange choke —
one confidential client, one audit boundary, for both chat and memory:

**Chat — `POST /v1/chat/completions`:**

1. Receives the per-user token LibreChat forwards (`Authorization: Bearer`).
2. Validates it against the AuditTrace Keycloak realm's JWKS (signature,
   issuer, expiry — deliberately NOT audience; see `bff/auth.py`).
3. RFC 8693 token-exchanges it via the confidential client
   `audittrace-librechat-bff` for a fresh access token minted for the
   SAME subject, requesting the `audittrace-librechat` client's
   audience so the result carries `aud=audittrace-server` +
   `audittrace:query` (that client's own `aud-audittrace-server`
   protocol mapper + default scope grant, shipped in WU-1).
4. Proxies to the orchestrator with the minted token, streaming the
   response back byte-identical.

**Memory (Souvenirs panel) — `GET/POST/PUT/DELETE /memory/{path}`
(M3-WU-D2-1):**

1. Same token extraction + JWKS validation as the chat path (step 1-2
   above).
2. RFC 8693 token-exchanges the SAME way, but requests the memory scope
   set explicitly (`bff/memory_scopes.py::MEMORY_SCOPE_STRING` — the
   four per-user-layer reads + the three writable-layer writes; NEVER
   `audittrace:admin`) via the exchange's `scope=` parameter
   (`bff/exchange.py::exchange_token`'s `requested_scope`). These
   scopes are OPTIONAL (not default) on `audittrace-librechat`, so only
   an exchange that asks for them by name gets them — the browser's own
   login token never carries write access to the store.
3. Proxies to the orchestrator's `/memory` API (`bff/memory_proxy.py`),
   byte-faithfully and fail-closed: a 401/403/404 the orchestrator
   returns is relayed as-is, never translated or retried with a
   different credential. Isolation (RLS, per-user manifest scoping,
   ADR-062) is entirely the memory API's job — this proxy adds no
   cross-user query, `$or`, or global escape.

## Why a separate top-level component, not `src/audittrace/`

- **It is a distinct deployable.** Per the spec: "a separate deployable
  sidecar with its own image" — not a code path inside the orchestrator
  process. `src/audittrace/` is `audittrace-server`'s package; folding
  the BFF in there would make one Python package serve two different
  runtime roles behind two different `Settings` classes, which is
  exactly the module-boundary smell PYTHON-ENGINEERING §11 flags.
- **The `/v1` schema stays untouched.** The BFF never imports from
  `src/audittrace` — it calls `/v1/chat/completions` over HTTP like any
  other client. This makes "the BFF cannot mutate the frozen `/v1`
  request/response shape" a structural property, not a discipline to
  remember.
- **Independent lifecycle.** A different image, a different `Settings`
  env prefix (`AUDITTRACE_BFF_*` vs `AUDITTRACE_*`), a different port
  (8766 vs 8765) — it ships, scales, and restarts independently of
  `audittrace-server`, matching the "sidecar" framing in the spec and
  ADR-042 §5.
- **Consistent with repo convention.** `webui/` is already a top-level,
  independently-servable component (a static SPA + `serve.py`) that
  talks to `/v1` the same way. `bff/` follows the same shape: a
  top-level directory, its own `Dockerfile`, its own tests
  (`tests/bff/`), wired into `make test`'s coverage gate the same way
  `scripts/deploy` is (see the `Makefile` `--cov=bff` flag).

## What this WU deliberately does NOT do

Per the spec's scope fence — WU-3's job, not WU-2's:

- No `docker-compose.edge.yml` `console` profile / `librechat.yaml`.
- No MongoDB service, no LibreChat itself.
- No Helm chart Deployment/Service for the BFF image (WU-2 ships the
  code + image recipe; wiring it into the chart/compose topology is a
  deployment concern for the WU that also stands up the `console`
  profile).

## Configuration (env-parameterized, laptop defaults)

All settings live in `bff/config.py` under the `AUDITTRACE_BFF_*` env
prefix. The one setting with NO default — `AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET`
— is the confidential client's secret; it MUST be Vault/env-sourced at
deploy time, never hard-coded. The BFF process refuses to start
(`Settings()` raises during lifespan startup) if it is unset.

## The `audittrace-librechat-bff` Keycloak client

A confidential client added to both `keycloak/realm-audittrace.json`
and `charts/audittrace/files/realm-audittrace.json` (WU-2 scope, per
the spec's "if required, add it to both realm files + its drift-guard
entry"). `clientAuthenticatorType: client-secret`,
`serviceAccountsEnabled: true`, no literal secret value committed
(Keycloak generates one on import — same pattern as `audittrace-dev`;
see `scripts/mint-dev-jwt.sh`'s header comment for the retrieval
runbook). It carries no `defaultClientScopes` of its own: its only job
is to authenticate the token-exchange call; the resulting token's
audience/scope come from the `audience=audittrace-librechat` exchange
parameter, not from this client's own scope grants.

**Deviation note (documented per the builder's fail-closed-choice
discipline, CONFIRMED by live evidence 2026-08-29):** wiring the
Keycloak-server-side "legacy token exchange" admin fine-grained
permission grant (the policy that authorizes `audittrace-librechat-bff`
to exchange tokens targeting `audittrace-librechat`) is an
operator-provisioning step analogous to `scripts/setup-memory-scopes.sh`'s
post-import `kcadm` binding — it is NOT expressible as static
realm-import JSON on Keycloak 24.0 (the version pinned in
`docker-compose.yml`), and stands up a real Keycloak admin session this
WU's sandboxed build environment cannot reach (no cluster credentials).

Live-evidence run against the REAL deployed front door
(`https://audittrace.local`, v1.24.10) confirmed the gap is even one
layer deeper than the permission grant: a real RFC 8693 exchange
request against the LIVE Keycloak token endpoint (using a real,
currently-valid operator token as `subject_token`) returned
`HTTP 400 {"error":"unsupported_grant_type","error_description":
"Unsupported grant_type"}` — the live Keycloak realm does not yet have
the `token-exchange` feature flag enabled at the server level at all
(Keycloak's `--features=token-exchange` startup flag; a chart/Helm
values change, not a realm-JSON change). `bff.exchange.exchange_token`
correctly raised `TokenExchangeError` and the BFF never proceeded to
proxy — proving the fail-closed path is real against live infra, not
just mocks. The concrete deploy-time follow-up before WU-3 exercises
the BFF against a live Keycloak is therefore two-part: (1) enable the
`token-exchange` feature flag on the Keycloak workload, THEN (2) grant
the admin fine-grained exchange permission described above.
