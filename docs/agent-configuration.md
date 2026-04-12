# Agent Configuration

How to point your coding agents at AuditTrace-AI.

## Prerequisites

1. Stack is running: `./scripts/start-full-stack.sh`
2. mkcert CA is installed locally: `mkcert -install` (already run during cert generation)

## Endpoint changes

| Legacy | Current |
|---|---|
| `http://localhost:8765/v1/chat/completions` | `https://localhost/v1/chat/completions` |
| `http://localhost:8765/invoke` | `https://localhost/context` (POST `{"query": "..."}`) |
| `http://localhost:8765/health` | `https://localhost/health` |

The mkcert CA is trusted system-wide, so HTTPS works without `-k` for tools that
read the system trust store. Some tools bundle their own — see notes per agent.

---

## OpenCode

**File:** `~/.config/opencode/config.json`

Since ADR-026 Phase 5b the memory-server defaults to
`SOVEREIGN_AUTH_REQUIRED=true`, which means every request must carry
a valid Keycloak JWT in the `Authorization: Bearer` header. The
`@ai-sdk/openai-compatible` provider maps its `apiKey` field to exactly
that header, so we reuse it as the JWT carrier.

### Step 1 — mint a dev JWT (valid 10h)

Use the `sovereign-memory-dev` client via the helper script:

```bash
# One-time setup: copy script + read the secret
docker cp scripts/mint-dev-jwt.sh audittrace-ai:/tmp/
TOKEN=$(docker exec \
    -e CLIENT_SECRET="$(cat secrets/dev_client_secret.txt)" \
    audittrace-ai bash /tmp/mint-dev-jwt.sh)
echo "$TOKEN" > ~/.config/opencode/sovereign-jwt.txt
```

The token lasts ~10 hours (client-level `access.token.lifespan=86400`
is capped by the realm's default SSO session max of 10h). Re-mint
once per workday.

> **Why not Traefik HTTPS + `apiKey` from the host?** Keycloak's
> issuer is `http://keycloak:8080/realms/sovereign-ai` (the internal
> docker-network hostname the memory-server trusts). Running the mint
> script from inside the sovereign-ai-net via `docker exec` guarantees
> the JWT's `iss` claim matches what `require_user` validates. A
> host-side mint against `localhost` or Traefik would produce a token
> with a different issuer that the memory-server would reject.

### Step 2 — plug the token into OpenCode

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "sovereign/qwen3.5",
  "provider": {
    "sovereign": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "audittrace-ai",
      "options": {
        "baseURL": "https://localhost/v1",
        "apiKey": "<paste the JWT from sovereign-jwt.txt here>"
      },
      "models": {
        "qwen3.5": {
          "name": "Qwen3.5-35B-A3B",
          "tools": true
        }
      }
    }
  },
  "instructions": ["~/.config/opencode/AGENTS.md"]
}
```

A single-line refresh wrapper (optional, nicer DX) — save as
`~/.config/opencode/refresh-jwt.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
REPO="${1:-$HOME/work/AuditTrace-AI}"
cd "$REPO"
docker cp scripts/mint-dev-jwt.sh audittrace-ai:/tmp/ >/dev/null
TOKEN=$(docker exec -e CLIENT_SECRET="$(cat secrets/dev_client_secret.txt)" \
    audittrace-ai bash /tmp/mint-dev-jwt.sh)
# Patch the apiKey in OpenCode's config in place (requires jq)
CONF=~/.config/opencode/config.json
tmp=$(mktemp)
jq --arg t "$TOKEN" \
   '.provider.sovereign.options.apiKey = $t' \
   "$CONF" > "$tmp" && mv "$tmp" "$CONF"
echo "Refreshed OpenCode JWT (sub: $(echo "$TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | jq -r .sub))"
```

Run once per day (or hook into your shell's session start).

### Step 3 — TLS trust for Node

If you hit `self signed certificate in certificate chain`, set
`NODE_EXTRA_CA_CERTS` before launching OpenCode:

```bash
export NODE_EXTRA_CA_CERTS="$(mkcert -CAROOT)/rootCA.pem"
opencode
```

Add this to your `~/.zshrc` to make it permanent.

### Bypass mode (emergency)

If Keycloak is down or you need to debug without auth, flip the
memory-server env back to `SOVEREIGN_AUTH_REQUIRED=false` for the
duration of the debug session:

```bash
SOVEREIGN_AUTH_REQUIRED=false docker compose up -d --force-recreate memory-server
```

The sentinel `UserContext` takes over and requests without JWTs
get the admin-by-construction fallback. Flip back to `true` when
done.

---

## Continue (VS Code extension)

**File:** `~/.continue/config.yaml`

```yaml
name: Sovereign Memory Server
version: 1.0.0
schema: v1

models:
  - name: Qwen3.5-35B-A3B (sovereign)
    provider: openai
    model: qwen3.5
    apiBase: https://localhost/v1
    apiKey: dummy
    contextLength: 65536
    defaultCompletionOptions:
      temperature: 0.7
      maxTokens: 4096

tabAutocompleteModel:
  name: Qwen3.5-35B-A3B (sovereign)
  provider: openai
  model: qwen3.5
  apiBase: https://localhost/v1
  apiKey: dummy

# Memory context injection — calls /context before each query
contextProviders:
  - name: http
    params:
      url: https://localhost/context
      title: "Sovereign Memory"
      description: "4-layer memory — sessions, ADRs, skills, semantic RAG"
      displayTitle: "Memory"

systemMessage: |
  You are working with a Solutions Architect specialized in IAM/OAuth2.
  Local stack: llama-server :11435 (Qwen3.5-35B-A3B MoE, ROCm).
  Memory server: audittrace-ai (4-layer memory, PostgreSQL + ChromaDB).
  Always answer in English.
```

Continue uses Node's HTTPS stack and respects the system CA store on Linux.
If TLS errors persist, set `NODE_EXTRA_CA_CERTS` in your VS Code launcher.

---

## Roo Code (VS Code extension)

**Settings → Roo Code → API Configuration:**

| Field | Value |
|---|---|
| API Provider | OpenAI Compatible |
| Base URL | `https://localhost/v1` |
| API Key | `dummy` |
| Model | `qwen3.5` |
| Custom Headers | (none) |

If TLS errors appear, launch VS Code with:

```bash
NODE_EXTRA_CA_CERTS="$(mkcert -CAROOT)/rootCA.pem" code .
```

---

## Verification

After updating any agent, test with a simple query:

```bash
curl -s https://localhost/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.5","messages":[{"role":"user","content":"What is ADR-009 about?"}]}' \
  | python3 -m json.tool
```

If memory augmentation works, the answer should reference KV cache compression
(content from your migrated ADR-009 file).

---

## Authentication (ADR-022, ADR-023, ADR-026)

**Current default since ADR-026 Phase 5b:**
`SOVEREIGN_AUTH_REQUIRED=true` — every request needs a valid
Keycloak JWT in `Authorization: Bearer <jwt>`. See the per-agent
sections above for how to plug a token into each client.

**Two auth flags exist:**

| Flag | Purpose | Default |
|---|---|---|
| `SOVEREIGN_AUTH_ENABLED` | Legacy `require_scope` gate (ADR-022, ADR-023). Gates specific scope strings per route | `false` |
| `SOVEREIGN_AUTH_REQUIRED` | New `require_user` gate (ADR-026 §15). Validates JWT against JWKS, populates `UserContext`, pushes `app.current_user_id` into Postgres RLS | **`true`** |

The two are independent but today both live on the memory-server;
`require_user` is the authoritative path for per-user identity.

**JWT sources:**

| Client type | Flow | Client |
|---|---|---|
| Dev / curl / Bruno | `client_credentials` grant with a service account | `sovereign-memory-dev` |
| OpenCode / Continue / Roo Code (daily use) | Client-credentials JWT via `scripts/mint-dev-jwt.sh`, paste into client config | Same as above |
| Production humans | OAuth2 device flow (deferred — Phase 8+) | Dedicated public client TBD |

See [ADR-026](ADR-026-multi-user-identity.md) §15 for the full
Keycloak-delegated identity design and §16 for the end-of-2026-04-11
shipped status with commit SHAs.
