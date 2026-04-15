# OAuth2 Device Flow — User Guide

A task-oriented walkthrough of how to authenticate into the
AuditTrace-AI stack as yourself — not as a service account — so every
OpenCode prompt, every curl probe, and every tool_call audit row lands
under your real Keycloak identity. Written for you (the human), in the
order you'll actually hit each scenario.

If you want the architectural rationale instead, see
[ADR-032](../ADR-032-oauth2-device-flow.md). If you want the protocol
sequence, see [sequence-oauth2-flow.md](../architecture/sequence-oauth2-flow.md).
The three-sentence version: OpenCode and friends have no browser, so
we use OAuth2 Device Authorization Grant (RFC 8628) — Keycloak gives
you a short code, you approve it in any browser on any device, the
CLI polls and picks up a refreshable Bearer. One interactive login,
~30 days of silent refreshes after that.

---

## Scenario 1 — First-time login on a fresh stack

Prereqs: stack running, Keycloak reachable at `https://localhost`
through Traefik, OpenCode installed.

```bash
# Sanity check — is Keycloak up?
docker compose ps | grep keycloak         # expect "healthy"

# Fire the login flow
scripts/audittrace-login
```

Expected output (stderr — the script prints the user-facing box to
stderr so stdout can remain the access_token channel for other modes):

```
[audittrace-login] initiating Device Authorization Grant at https://localhost/...

  ┌──────────────────────────────────────────────────────────────┐
  │ Complete login in a browser:                                 │
  │                                                              │
  │   1. Open:  https://localhost/realms/sovereign-ai/device
  │   2. Enter code:  TMLN-EJWW
  │                                                              │
  │ Or go directly to:                                           │
  │   https://localhost/realms/sovereign-ai/device?user_code=TMLN-EJWW
  │                                                              │
  │ Waiting for approval... (expires in 600s, polling every 5s)
  └──────────────────────────────────────────────────────────────┘
```

Open the **`verification_uri_complete`** (the second URL) in any
browser — the code is pre-filled. Log in as:

- **Username:** `luis`
- **Password:** `temp-luis-2026` *(the 2026-04-15 reset; if that fails
  see Scenario 9)*

Keycloak will force you to change the password on first login. Pick
anything strong — the `tokens.json` carries a 30-day refresh window,
so you won't be typing this again for a month.

Approve the consent screen (shows once per client). The CLI's polling
loop catches the approval within 5 seconds and exits with:

```
[audittrace-login] ✅ logged in — tokens saved to /home/<you>/.config/audittrace/tokens.json
```

Verify:

```bash
scripts/audittrace-login --show | cut -c1-80
# → a base64-encoded JWT (three dot-separated segments)
```

---

## Scenario 2 — First-time login on a stack where Keycloak was already running

The realm JSON import only runs on Keycloak's first boot. If your
Postgres has the `sovereign-ai` realm from before ADR-032 shipped,
the new `audittrace-opencode` public client + `luis` user are NOT
there yet. Symptom: Scenario 1 fails with HTTP 401 or 404 from the
device-auth endpoint.

Provision via the admin API (idempotent — re-running against a
provisioned realm is a no-op):

```bash
KEYCLOAK_ADMIN_PASSWORD=admin scripts/setup-human-user.sh
```

Expected output:

```
[setup-human-user] requesting admin token from master realm
[setup-human-user] creating client 'audittrace-opencode'
[setup-human-user] adding audience mapper
[setup-human-user] creating user 'luis'

  ✅ Keycloak ready for ADR-032 Device Flow

  Client:      audittrace-opencode  (uuid=...)
  User:        luis   (id=...)
  Temp pw:     change-me-on-first-login  (will be forced to reset on first login)
```

Then carry on with Scenario 1.

---

## Scenario 3 — Daily use: launching OpenCode with your identity

Canonical UX:

```bash
scripts/opencode-wrapper.sh
```

Under the hood: calls `audittrace-login --ensure` (silent refresh if
within 60s of expiry, falls back to interactive login if no token
exists), writes the raw token into every provider's `options.apiKey`
in `~/.config/opencode/config.json` (timestamped backup alongside,
atomic via mktemp+mv), scrubs any stale `options.headers.Authorization`
so there's no dual-source ambiguity, then execs `opencode`. From
there you're in a normal OpenCode session — every chat request reaches
the memory-server with your Bearer.

**Why apiKey, not headers?** The `@ai-sdk/openai-compatible` provider
OpenCode uses builds its outbound `Authorization: Bearer <…>` from
`options.apiKey`. Setting `headers.Authorization` alone looks
right in the config but gets *overridden* by the SDK's
apiKey-derived header on the wire — producing silent 401s if the
apiKey is stale. The wrapper writes both cleanly: apiKey =
current token, headers.Authorization = removed. (Fix commit:
`537ddd8`.)

Verify your identity is attached (scenario 10 has the full audit-row
check):

```bash
docker compose exec -T postgres psql -U sovereign -d sovereign_ai \
  -c "SELECT user_id, project, substring(question, 1, 50) AS q
      FROM interactions
      ORDER BY id DESC LIMIT 3;"
```

Your Keycloak sub (a UUID) should be in the `user_id` column, not
the dev-client sub.

**TLS caveat:** OpenCode probably talks to `https://localhost/...`
through Traefik. Our mkcert-issued cert is self-signed by a
locally-installed root CA. If OpenCode complains about an untrusted
certificate, either:

```bash
mkcert -install                    # one-time: install the mkcert root CA into your user trust stores
```

or (less clean, per-session):

```bash
NODE_TLS_REJECT_UNAUTHORIZED=0 scripts/opencode-wrapper.sh
```

After `mkcert -install` it "just works" permanently.

---

## Scenario 4 — Ad-hoc curl / script with your identity

For quick probes, Bruno collections, or wrapping the API from a
shell script:

```bash
BEARER=$(scripts/audittrace-login --show)      # silent refresh if near expiry

curl -sk https://localhost/v1/chat/completions \
  -H "Authorization: Bearer $BEARER" \
  -H "Content-Type: application/json" \
  -H "X-Project: AuditTrace-AI" \
  -d '{
    "model": "Qwen3.5-35B-A3B",
    "stream": false,
    "max_tokens": 80,
    "messages": [{"role": "user", "content": "Hello from my curl"}]
  }'
```

- `-s` silent progress bar, `-k` skips cert verification (the
  mkcert root isn't in curl's default CA bundle).
- `X-Project: AuditTrace-AI` routes the interaction under the right
  project tag per [ADR-029](../ADR-029-audit-trail-completeness.md).
- `scripts/audittrace-login --show` refreshes the access_token
  transparently if it's within 60 seconds of expiry, so you can lean
  on this in a long-running shell without worrying about expiry.

---

## Scenario 5 — Token is near expiry (silent refresh)

You don't need to DO anything. `--show` and `--ensure` both check
`access_expires_at - now > REFRESH_THRESHOLD_SECONDS` (default 60s)
and silently refresh when needed. No user interaction.

Peek at the remaining lifetime:

```bash
jq '.access_expires_at - (now | floor)' ~/.config/audittrace/tokens.json
# → seconds of life left on the access_token
```

For the refresh token:

```bash
jq '.refresh_expires_at - (now | floor)' ~/.config/audittrace/tokens.json
# → seconds until you need to re-login interactively (~30d on a fresh login)
```

If you want to force a refresh right now (e.g., after rotating your
Keycloak password and wanting a freshly-minted token):

```bash
# Trick it into thinking the access is expired — the next --show
# will post the refresh grant:
jq '.access_expires_at = 0' ~/.config/audittrace/tokens.json \
  > /tmp/t.json && mv /tmp/t.json ~/.config/audittrace/tokens.json && chmod 600 ~/.config/audittrace/tokens.json
scripts/audittrace-login --show > /dev/null
```

---

## Scenario 6 — Refresh chain expired (SSO session maxed out)

After ~30 days without interactive login, `offlineSessionIdleTimeout`
+ `ssoSessionMaxLifespan` close out the refresh chain. Symptom:

```bash
scripts/audittrace-login --ensure
# [audittrace-login] refreshing access_token (expires in -120s)
# error: refresh-token grant failed — re-login required
```

Fix — re-run the interactive flow once:

```bash
scripts/audittrace-login
# → new verification URL + code → browser → approve → back
```

No password re-entry needed if Keycloak's local session is still
live (common in the same browser); otherwise Keycloak shows the
login page again.

---

## Scenario 7 — Logout / rotate tokens

End of day on a shared machine. Recent rotation of Keycloak
credentials. Suspected compromise. Just general hygiene.

```bash
scripts/audittrace-login --logout
# [audittrace-login] deleted /home/<you>/.config/audittrace/tokens.json
```

The saved token pair is gone. Any call to `--show` / `--ensure`
afterwards exits with `error: no token file at ~/.config/audittrace/tokens.json`.
The next `scripts/audittrace-login` runs the full Device Flow.

Note: this does NOT revoke the refresh token on the Keycloak side —
it just deletes your local copy. If you need active revocation
(compromise scenario), also log into the Keycloak admin console as
admin and revoke `luis`'s active sessions, or use the admin-API
logout endpoint.

---

## Scenario 8 — Switch to a different Keycloak instance

If you're pointing at a different host (a staging realm, a colleague's
box, a remote dev server with proper certs):

```bash
KEYCLOAK_BASE=https://keycloak.staging.example.com \
CURL_VERIFY=1 \
  scripts/audittrace-login
```

- `KEYCLOAK_BASE` — the new base URL.
- `CURL_VERIFY=1` — opt out of the `-k` self-signed bypass that's on
  by default for `https://localhost*`. Required when you're hitting
  a real CA-signed cert.

**Honest caveat about remote hosts:** `tokens.json` is per-host and
the memory-server's `SOVEREIGN_KEYCLOAK_ISSUER_EXTRAS` list ships
with only the `localhost` variants. If the other Keycloak emits
`iss=https://keycloak.staging.example.com/realms/sovereign-ai`,
your memory-server will reject tokens from it until you extend the
extras list. For a simple "swap hosts on my laptop" case this is
fine; for real staging you'll need matching memory-server config.

---

## Scenario 9 — Forgot / lost your password

Reset via the master-realm admin API. The full dance — useful to
have in one place:

```bash
# 1. Grab an admin token (master realm, admin/admin in dev)
ADMIN_TOKEN=$(curl -sk -X POST \
  -d "client_id=admin-cli" \
  -d "username=admin" \
  -d "password=admin" \
  -d "grant_type=password" \
  https://localhost/realms/master/protocol/openid-connect/token \
  | jq -r .access_token)

# 2. Look up the user id for 'luis'
USER_ID=$(curl -sk -H "Authorization: Bearer $ADMIN_TOKEN" \
  "https://localhost/admin/realms/sovereign-ai/users?username=luis&exact=true" \
  | jq -r '.[0].id')
echo "user_id = $USER_ID"

# 3. Reset the password. temporary=true forces change on next login.
curl -sk -X PUT \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type":"password","value":"<new-temp-password>","temporary":true}' \
  "https://localhost/admin/realms/sovereign-ai/users/$USER_ID/reset-password"
```

Then run Scenario 1 with the new password — Keycloak will force you
to change it again (because `temporary: true`).

Precedent: on 2026-04-15 this exact flow was used to reset `luis`
from `change-me-on-first-login` (realm JSON default) to
`temp-luis-2026`.

---

## Scenario 10 — Verify it actually worked

Three independent checks — run them in order; each is strictly more
authoritative than the one before.

```bash
# 1. Decode the access_token claims locally (no signature check —
#    this is a sanity-check of what you hold)
jq -r .access_token ~/.config/audittrace/tokens.json \
  | cut -d. -f2 \
  | base64 -d 2>/dev/null \
  | jq '{iss, aud, sub, preferred_username, scope, exp}'
```

Expected:
- `iss`: `https://localhost/realms/sovereign-ai` (Traefik-fronted form)
- `aud`: `sovereign-memory-server` (the audience mapper's output)
- `sub`: a UUID (your real Keycloak id) — NOT
  `dev-client@audittrace-ai` or similar
- `scope`: contains the four `memory:*` scopes and `sovereign-ai:query`

```bash
# 2. Fire a chat request, confirm 200 OK
BEARER=$(scripts/audittrace-login --show)
curl -sk -X POST https://localhost/v1/chat/completions \
  -H "Authorization: Bearer $BEARER" \
  -H "Content-Type: application/json" \
  -H "X-Project: AuditTrace-AI" \
  -d '{"model":"Qwen3.5-35B-A3B","stream":false,"max_tokens":40,"messages":[{"role":"user","content":"Say: hello from my auth"}]}' \
  -o /dev/null -w "HTTP %{http_code}\n"
# expected: HTTP 200
```

```bash
# 3. Check the audit trail — the row should carry YOUR sub
docker compose exec -T postgres psql -U sovereign -d sovereign_ai -c "
SELECT user_id, project, substring(question, 1, 60) AS q
FROM interactions
ORDER BY id DESC LIMIT 1;"
```

The `user_id` column on the latest row should match the `sub` you saw
in step 1. If it does: per-user RLS (ADR-026) is biting at user
granularity, and everything downstream of identity (scope enforcement,
hybrid recall, audit trail) is working as designed.

---

## Scenario 11 — Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `curl: (22) The requested URL returned error: 404` on device-auth endpoint | `KEYCLOAK_BASE` points at Traefik's dashboard (`:8080`), not Keycloak (`:443` via Traefik) | Use the default `KEYCLOAK_BASE=https://localhost` (set in commit 129853d) or override explicitly |
| `curl: (60) SSL certificate problem: self-signed certificate` | mkcert CA not in curl's trust store | Default `-k` covers it on localhost. For CA-signed remote hosts set `CURL_VERIFY=1` explicitly |
| Browser: "Firefox can't connect to localhost" after entering the code | Keycloak renders redirect URLs as `http://localhost/...` (no HTTPS awareness) | Check `docker-compose.yml` sets `KC_HOSTNAME_URL=https://localhost` + `KC_PROXY=edge` on the keycloak service (fixed in commit 5d0ed5e) |
| `401 Unauthorized: {"detail":"Invalid or expired token"}` on chat requests | Memory-server rejects the token's `iss` claim | Verify `SOVEREIGN_KEYCLOAK_ISSUER_EXTRAS` env on the memory-server container includes the issuer your token carries. `docker compose exec memory-server env \| grep KEYCLOAK` |
| `401 Unauthorized` from OpenCode specifically, but direct `curl` with the same token works | Stale `options.apiKey` in the OpenCode config still carries an old token — `@ai-sdk/openai-compatible` builds Authorization from apiKey and that wins over any `headers.Authorization` we inject | `scripts/opencode-wrapper.sh` writes apiKey directly (fixed in commit `537ddd8`). On older wrapper, either re-run the wrapper or manually: `jq '.provider \|= with_entries(.value.options.apiKey = "<fresh-token>" \| .value.options.headers \|= del(.Authorization))' ~/.config/opencode/config.json` |
| OpenCode 401 persists after wrapper rewrites the config | OpenCode cached the apiKey at session start and didn't re-read the config | Quit OpenCode fully and relaunch — the config is only read at startup |
| Browser shows login page but "invalid username or password" | Password reset needed | Scenario 9 (admin reset) |
| `scripts/audittrace-login --show` prints empty string | Token file exists but refresh failed | `cat ~/.config/audittrace/tokens.json \| jq` — check `refresh_expires_at`; if past, re-login per Scenario 6 |
| Polling loop hangs past the `expires_in` window | Browser approval didn't reach Keycloak | Re-run `scripts/audittrace-login` (fresh device_code), try again |
| `Not Found` on OpenID discovery (`.well-known/openid-configuration`) | Keycloak mid-restart | Wait 10-20s for Keycloak readiness; check `docker compose logs keycloak` for "Listening on" |

When all else fails, the nuclear option — rebuild the local state
from zero:

```bash
scripts/audittrace-login --logout
docker compose restart memory-server keycloak
# wait for health ↑
KEYCLOAK_ADMIN_PASSWORD=admin scripts/setup-human-user.sh   # idempotent re-sync
scripts/audittrace-login                                    # fresh Device Flow
```

---

## Scenario 12 — CI / headless automation

Don't use Device Flow for CI. It requires a browser somewhere, which
headless environments don't have.

CI/smoke-test path: the existing `sovereign-memory-dev` client via
`client_credentials`. See `scripts/mint-dev-jwt.sh` (runs inside the
docker network) and the README's Authentication section.

Architectural rationale: you're a human, you're on Device Flow; the
CI is a robot, it's on client_credentials. These are intentionally
different Keycloak clients (`audittrace-opencode` vs
`sovereign-memory-dev`) with different security models. Both are
valid; don't mix them.

---

## Related

- **[ADR-032](../ADR-032-oauth2-device-flow.md)** — Architectural
  rationale for the Device Flow choice over client_credentials,
  multi-issuer validation, and the three-script surface.
- **[sequence-oauth2-flow.md](../architecture/sequence-oauth2-flow.md)** —
  Protocol-level sequence diagrams: Device Flow, silent refresh, hot
  and cold JWT validation, bypass mode, revocation semantics.
- **[README — Authentication](../../README.md#authentication-adr-022-adr-023-adr-026-adr-032)** —
  One-page summary with the exact commands to run.
- **[ADR-026](../ADR-026-multi-user-identity.md)** — Why per-user
  identity matters at all: RLS, per-user ChromaDB filtering, session
  id uniqueness.
- **[ADR-029](../ADR-029-audit-trail-completeness.md)** — The
  `X-Project` header you'll see in Scenario 4's curl.
