# AuditTrace-AI Deployment Runbook

Zero-to-running guide for a fresh deployment. Tested on Ubuntu 24.04
(ZBook Ultra G1a, Ryzen AI MAX+ 395, ROCm 7.2).

---

## Prerequisites

| Tool | Version | Check |
|------|---------|-------|
| Docker + Compose | 27+ | `docker compose version` |
| Python | 3.12+ | `python3 --version` |
| uv (or pip) | any | `uv --version` or `pip --version` |
| mkcert | any | `mkcert --version` |
| jq | any | `jq --version` |
| llama-server | b5220+ | `llama-server --version` |

**Hardware (local inference):** 64 GB unified RAM recommended for the
three-model topology (Qwen 3.6-35B-A3B + nomic-embed-text + Mistral 7B).
32 GB works with Qwen only.

---

## Step 1: Clone and set up Python environment

```bash
git clone git@github.com:lfdesousa/AuditTrace-AI.git
cd AuditTrace-AI
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Step 2: Create Docker network

```bash
docker network create audittrace-net
```

## Step 3: Generate secrets

```bash
scripts/setup-secrets.sh
```

This creates `.env` from `.env.example` with generated passwords for
Postgres, Redis, MinIO, and ChromaDB. Review and adjust if needed.

## Step 4: Generate TLS certificates

```bash
mkdir -p certs
mkcert -install  # one-time: install local CA
mkcert -cert-file certs/sovereign.pem -key-file certs/sovereign-key.pem \
    localhost 127.0.0.1 ::1
```

Traefik reads these from the `certs/` mount. The filenames are
referenced in `traefik/dynamic.yml`.

## Step 5: Start infrastructure

```bash
# Start databases and supporting services first
docker compose up -d postgres redis chromadb minio keycloak traefik

# Wait for all health checks to pass
docker compose ps  # all should show "healthy"
```

**Expected healthy services:**
- `audittrace-postgres` — PostgreSQL 16
- `audittrace-redis` — Redis 7 (token + tool result cache)
- `audittrace-chromadb` — ChromaDB 1.5 (vector store)
- `audittrace-minio` — MinIO (S3 object storage)
- `audittrace-keycloak` — Keycloak 26 (OAuth2 / OIDC)
- `audittrace-traefik` — Traefik 3.6 (TLS termination + routing)

## Step 6: Start the memory server

```bash
docker compose up -d memory-server
```

The entrypoint script runs Alembic migrations automatically before
starting uvicorn. First start creates all tables + RLS policies.

**Verify:**
```bash
curl -sk https://localhost/health | jq .
# Expected: {"status": "ok", "version": "..."}
```

## Step 7: Provision Keycloak user

```bash
KEYCLOAK_ADMIN_PASSWORD=admin scripts/setup-human-user.sh
```

This creates:
- Public client `audittrace-opencode` with Device Flow enabled
- Realm user `luis` with a temporary password
- Audience mapper for `audittrace-server`

## Step 8: Device Flow login

```bash
scripts/audittrace-login
```

Follow the URL in your browser, log in as `luis`, change the temporary
password, and approve the consent screen. Tokens are saved to
`~/.config/audittrace/tokens.json`.

**Verify:**
```bash
BEARER=$(scripts/audittrace-login --show)
curl -sk -H "Authorization: Bearer $BEARER" \
    https://localhost/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"qwen","messages":[{"role":"user","content":"hello"}],"max_tokens":50}' | jq .
```

## Step 9: Index ChromaDB

```bash
# Get the user ID from the login step
USER_ID=$(scripts/audittrace-login --show | python3 -c "
import sys, json, base64
t = sys.stdin.read()
payload = t.split('.')[1] + '=='
print(json.loads(base64.urlsafe_b64decode(payload))['sub'])
")

.venv/bin/python scripts/index-chromadb.py --user-id "$USER_ID"
```

This indexes ADRs, skills, and documents into ChromaDB collections
for semantic search.

## Step 10: Start LLM servers

### Main chat server (Qwen 3.6-35B-A3B)

```bash
# systemd service (recommended)
sudo systemctl start llama-server

# Or manually:
llama-server \
    --model ~/models/Qwen_Qwen3.6-35B-A3B-Q4_K_M.gguf \
    --ctx-size 65536 --batch-size 1024 --ubatch-size 256 \
    --cache-type-k q4_0 --cache-type-v q4_0 \
    --n-gpu-layers 99 --flash-attn on --threads 8 \
    --metrics --host 0.0.0.0 --reasoning-format none --port 11435
```

### Embedding server (nomic-embed-text)

```bash
sudo systemctl start llama-embed-server

# Or manually:
llama-server \
    --model ~/models/nomic-embed-text-v1.5.Q8_0.gguf \
    --port 11436 --host 0.0.0.0 --embeddings --pooling mean \
    --n-gpu-layers 0 --ctx-size 8192 --batch-size 8192 \
    --ubatch-size 2048 --threads 4 --parallel 4 \
    --alias nomic-embed-text --log-disable
```

### Summariser server (Mistral 7B)

```bash
scripts/start-summarizer-llama.sh
```

## Step 11: Smoke test

```bash
# Health (no auth)
curl -sk https://localhost/health | jq .

# Authenticated chat
BEARER=$(scripts/audittrace-login --show)
curl -sk -H "Authorization: Bearer $BEARER" \
    -H "Content-Type: application/json" \
    -H "X-Project: AuditTrace-AI" \
    -H "X-Thinking: deep" \
    https://localhost/v1/chat/completions \
    -d '{"model":"qwen3.6","messages":[{"role":"user","content":"What did ADR-025 decide?"}],"max_tokens":500}' | jq .

# Audit trail
curl -sk -H "Authorization: Bearer $BEARER" \
    https://localhost/interactions?limit=5 | jq .
```

## Step 12: Observability stack (optional)

The observability stack (Prometheus + Grafana + Loki + Tempo) runs as a
sibling compose stack in the `AiSovereignObservability` repository:

```bash
cd ~/work/AiSovereignObservability
docker compose up -d
```

Grafana: `http://localhost:3001` (admin/sovereign)
Tempo: Service graph at `http://localhost:3001/explore` → Tempo data source

---

## Teardown

```bash
# Stop everything
docker compose down

# Full teardown (removes volumes — destructive)
docker compose down -v
docker network rm audittrace-net
```

---

## Troubleshooting

### Container won't start
```bash
docker compose logs memory-server --tail 50
```

### Auth failures (401)
```bash
# Re-login
scripts/audittrace-login --logout
scripts/audittrace-login

# Mint a dev token (bypasses Device Flow)
TOKEN=$(docker exec -e CLIENT_SECRET=$(cat secrets/dev_client_secret.txt) \
    audittrace-server bash /tmp/mint-dev-jwt.sh)
```

### ChromaDB empty after rebuild
```bash
.venv/bin/python scripts/index-chromadb.py --user-id "$USER_ID"
```

### Migration failures
```bash
docker exec audittrace-server python -m alembic upgrade head
```

### LLM server not responding
```bash
curl http://localhost:11435/health  # Qwen
curl http://localhost:11436/health  # Embeddings
curl http://localhost:11437/health  # Mistral summariser
```

---

## Kubernetes Deployment (k3s + Istio)

### Prerequisites

```bash
# Install k3s
curl -sfL https://get.k3s.io | sh -

# Install Istio
istioctl install --set profile=default -y

# Install Helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# Local registry for k3s
# Add to /etc/rancher/k3s/registries.yaml:
#   mirrors:
#     "localhost:5000":
#       endpoint:
#         - "http://localhost:5000"
docker run -d -p 5000:5000 --name registry registry:2
```

### Deploy

```bash
# 1. Build and push the memory-server image
make k8s-build

# 2. Generate TLS cert for the Istio Gateway
mkcert -cert-file tls.crt -key-file tls.key audittrace.local localhost
kubectl create namespace audittrace
kubectl create secret tls audittrace-tls \
    --cert=tls.crt --key=tls.key -n audittrace

# 3. Install with secrets
make k8s-install \
    --set secrets.postgres.password=<password> \
    --set secrets.postgres.appPassword=<password> \
    --set secrets.chromadb.token=<token> \
    --set secrets.redis.password=<password> \
    --set secrets.minio.secretKey=<key> \
    --set secrets.minio.kmsKey=<key>

# 4. Wait for all pods
kubectl get pods -n audittrace -w

# 5. Add /etc/hosts entry
echo "127.0.0.1 audittrace.local" | sudo tee -a /etc/hosts

# 6. Verify
curl -sk https://audittrace.local/health
```

### Vault & Keycloak bootstrap (post-install / post-upgrade)

After `make k8s-install` (or any `helm upgrade` that adds a new Vault
policy / role or memory-scopes binding to the chart), run the bootstrap
umbrella to push the chart's expected provisioner state into Vault and
Keycloak:

```bash
# Prerequisites:
#   - Vault initialised + unsealed (vault operator init && unseal x3)
#   - VAULT_TOKEN exported (root token from `vault operator init`,
#     rotate to a less-privileged operator token after first run)
export VAULT_TOKEN="<root-or-operator-token>"

make k8s-bootstrap-secrets
```

The umbrella chains:

1. **`scripts/setup-vault.sh`** — enables KV v2 + Kubernetes auth, applies
   every `*.hcl` policy and `role-*.env` binding present in the
   `audittrace-vault-policies` ConfigMap (dynamic discovery — adding a
   new policy to `templates/vault/configmap-policies.yaml` is automatically
   picked up here), seeds initial KV secrets from `secrets/*.txt`.
2. **`scripts/setup-memory-scopes.sh`** — provisions the three OAuth2
   memory scopes (`memory:episodic:write`, `memory:procedural:write`,
   `memory:semantic:write`) on the audittrace realm and binds them to
   the relevant clients via `kcadm.sh`. Reads the Keycloak admin
   password from Vault when available.

Both scripts are **idempotent** — they check current state before
applying, so re-running is safe and prints `⊝ exists (skip)` for entries
already present.

After this command, the operator must seed Keycloak's admin password
into Vault (one-time, NOT auto-seeded for security):

```bash
vault kv put kv/audittrace/keycloak/admin password="<new-password>"
```

Then verify the cluster is in the expected state:

```bash
make verify-deploy
# Expect: 9/9 PASS — check 9 asserts ConfigMap policies/roles match Vault.
```

If the verify gate's check 9 reports drift (e.g. `policy 'X' is in
ConfigMap but missing in Vault`), re-run `make k8s-bootstrap-secrets`
and re-verify. This is the failure mode that cost half a session day on
2026-05-03 — the umbrella + drift-guard pair makes it self-healing.

### Day-2 operations

```bash
make k8s-status       # pod + service + Istio status
make k8s-upgrade      # apply values changes
make k8s-template     # dry-run template rendering

# Istio analysis
istioctl analyze -n audittrace

# Check mTLS is enforced
istioctl proxy-config clusters -n audittrace deploy/audittrace-memory-server

# Verify ZTA: direct pod access should be denied
kubectl exec -n audittrace deploy/audittrace-redis -- redis-cli ping
# Expected: connection refused (AuthorizationPolicy blocks non-SPIFFE sources)
```

### Cutting a new release (ADR-055)

Two single-source-of-truth pin sites: `pyproject.toml::version` + `charts/audittrace/Chart.yaml::appVersion`. Bumping them is the only manual release action.

```bash
# 1. Bump pyproject + Chart.yaml::appVersion atomically + regenerate
#    OpenAPI snapshot + run the drift gate. No commit/tag yet.
make release VERSION=1.0.14

# 2. Review the diff (printed by the target). Should touch:
#      pyproject.toml
#      charts/audittrace/Chart.yaml
#      docs/reference/audittrace/openapi.yaml
#      tests/fixtures/openapi.snapshot.yaml

# 3. Commit + open release PR + merge.
git add pyproject.toml charts/audittrace/Chart.yaml \
        docs/reference/audittrace/openapi.yaml tests/fixtures/openapi.snapshot.yaml
git commit -m "chore(release): v1.0.14"
git push -u origin release/v1.0.14
# … open PR, get review, merge.

# 4. Tag on main + push.
git checkout main && git pull --ff-only
git tag -a v1.0.14 -m "v1.0.14 — <release headline>"
git push origin v1.0.14

# 5. Build + push image with the tag (image tag MUST match git tag).
docker build -t localhost:5000/audittrace/memory-server:v1.0.14 .
docker push localhost:5000/audittrace/memory-server:v1.0.14

# 6. Helm upgrade.
helm upgrade audittrace ./charts/audittrace -n audittrace \
    --reset-then-reuse-values \
    --set memoryServer.image.tag=v1.0.14

# 7. Verify the running pod self-identifies as v1.0.14:
curl -sk --resolve audittrace.local:443:127.0.0.1 \
    https://audittrace.local/health | jq '.version'
# Expected: "1.0.14"
```

**Pre-ADR-055 history (don't repeat):** prior to v1.0.14, the litany required bumping four sites in lockstep (`pyproject.toml`, `models.py`, `server.py` fallback, `values.yaml::OTEL_RESOURCE_ATTRIBUTES service.version`). At least three drift incidents shipped (v1.0.10→v1.0.11 metadata mismatch, OTEL service.version frozen at 1.0.0 across 12 releases, v1.0.13 image self-reporting as v1.0.11). ADR-055 collapsed the duplication; `make release` is the new ritual.

If you DO end up with a drifted release (e.g. tag mismatched against pyproject), `tests/test_version_drift.py::test_chart_appversion_matches_pyproject_version` fails CI before the merge — by design.

### Upgrading the release — which Helm flag when

Two distinct upgrade scenarios appear in day-to-day work; picking the wrong flag surfaces as a cryptic `nil pointer evaluating interface {}.enabled` error.

**Scenario A — values unchanged since last install (just re-deploying).**
Pass your values file explicitly with `-f`. This is what `make k8s-upgrade` does:
```bash
helm upgrade $RELEASE $CHART_DIR -f $VALUES_FILE -n $NAMESPACE
```

**Scenario B — quick iteration: change ONE value (e.g., bump image tag) but keep everything else.**
Use `--reset-then-reuse-values` (Helm ≥ 3.14) + `--set`. The reason plain `--reuse-values` is NOT the right answer here: it reuses only the *user-supplied* values from the prior release and ignores NEW default values added to `values.yaml` since then. The moment the chart grows a new top-level block (e.g., `memoryServer.summariser` added 2026-04-18), `--reuse-values` produces a deployment where the new defaults are missing and the templates error out. `--reset-then-reuse-values` merges the chart's new defaults with the prior user-supplied values, which is what an iterating operator actually wants:
```bash
helm upgrade $RELEASE $CHART_DIR \
  --reset-then-reuse-values \
  --set memoryServer.image.tag=$TAG \
  -n $NAMESPACE
```

**Scenario C — structural chart changes AND you want to rewrite user values.**
Pass a fresh values file with `-f` (Scenario A semantics). Helm always accepts the file as the authoritative source.

**If you hit `nil pointer evaluating interface {}.enabled` or similar on a `helm upgrade`, assume Scenario B above was your intent — retry with `--reset-then-reuse-values`.**

### PAdES trust store — bootstrap and refresh (ADR-052 + ADR-053)

After a fresh `helm install` (or any chart upgrade that bumps `pyhanko[etsi]`), the in-cluster trust store is empty until an operator runs the refresh. Without it, every signed PDF flags `signed_untrusted` because the chain doesn't terminate at any configured root. The refresh walks the EU LOTL + Swiss federal TSL and persists ~900 qualified-signature CAs to MinIO.

**Default composition** (per `values.yaml`): `composite` builder running `[eu_lotl, swiss_tsl]` in order. Coverage is EU eIDAS qualified TSPs (~887 CAs across 27 member states) plus Swiss federal TSL qualified TSPs (SwissSign, Swisscom, QuoVadis, Swiss Government — ~10 CAs).

**Manual refresh** (works on any deploy; the audit signal of choice for ad-hoc operator action):

```bash
# 1. Mint an admin-scoped JWT (one-time interactive Device Flow per ADR-032).
scripts/audittrace-login --scope audittrace:admin
BEARER=$(jq -r .access_token < ~/.config/audittrace/tokens.json)

# 2. Refresh — walks EU LOTL (~22 s) + Swiss TSL (~3 s).
curl -sk --resolve audittrace.local:443:127.0.0.1 \
     -H "Authorization: Bearer $BEARER" \
     -X POST --max-time 300 \
     https://audittrace.local/system/trust-store/refresh

# Expect: HTTP 200 + TrustStoreMetadata JSON with builder_id="eu_lotl+swiss_tsl"
# and cert_count ≈ 897 (varies as TSPs join/leave the lists).

# 3. Read state any time:
curl -sk --resolve audittrace.local:443:127.0.0.1 \
     -H "Authorization: Bearer $BEARER" \
     https://audittrace.local/system/trust-store
```

**Helm post-install hook** (opt-in; default disabled until creds are wired):

```yaml
# values-local.yaml or your overlay
memoryServer:
  trustStore:
    bootstrap:
      enabled: true
      credentialsSecret: audittrace-trust-store-refresh-creds
```

The hook fires after each `helm install` / `helm upgrade` and hits `/system/trust-store/refresh` with a service-account JWT. Pre-requisite: a k8s Secret `<release>-trust-store-refresh-creds` containing `client_id` + `client_secret` for a Keycloak service-account client whose service-account user has the `audittrace:admin` scope. Provision via `kcadm.sh` (mirror the `setup-memory-scopes.sh` script's pattern) — left out of `make k8s-bootstrap-secrets` because the credentials are operator-supplied, not auto-generated.

**Failure modes:**

- **HTTP 502 with `trust_store_build_failed`** — one or more inner builders couldn't run. Common causes: EU LOTL endpoint outage (`https://ec.europa.eu/tools/lotl/eu-lotl.xml`), Swiss TSL endpoint outage (`https://trustedlist.tsl-switzerland.ch/tsl-ch.xml`), or `pyhanko[etsi,async-http]` extras missing in the image. Composite is best-effort: if at least one inner builder succeeds the bundle persists with a partial cert_count; if every inner fails the previous bundle in MinIO stays in place (validator continues to use the cached state).
- **HTTP 502 with `TSL signature validation failed`** — the Swiss TSLO cert vendored in the chart is stale relative to OFCOM's current signing key, or the TSL was tampered in transit. Action: pull the updated DER from `https://trustedlist.tsl-switzerland.ch/tsl-signer-certificate/CH-TL-cert-DER.cer`, OOB-verify the SHA-1 against `https://uri.tsl-switzerland.ch/TrstSvc/TrustedList/schemerules/CH/index.html`, replace `charts/audittrace/trust-store/swiss-federal-tsl/CH-TL-cert.der` in the chart, ship a new release.
- **HTTP 500 with `trust_store_persist_failed`** — Provider write failed. Usually MinIO connectivity. Inspect with `kubectl logs deploy/audittrace-memory-server -c memory-server --tail=200 | grep trust-store` for the underlying cause.

**Validating the result:**

After a successful refresh, re-index a signed PDF and check the manifest:

```bash
curl -sk --resolve audittrace.local:443:127.0.0.1 \
     -H "Authorization: Bearer $BEARER" \
     -X POST "https://audittrace.local/memory/index?file=episodic/<your-signed.pdf>&collections=ai_research_papers"

curl -sk --resolve audittrace.local:443:127.0.0.1 \
     -H "Authorization: Bearer $BEARER" \
     "https://audittrace.local/memory/episodic" | \
     jq '.items[] | select(.key | contains("<your-signed.pdf>")) | .signature_status'

# Expected for a current-cert EU- or CH-recognised qualified-signature
# PDF: "signed_valid".
# Expected for an expired-cert PDF: "signed_untrusted" (basic PAdES
# validation rejects expired chains; LTV is ADR-054 territory).
# Expected for a tampered PDF: "signed_tampered".
# Expected for a self-signed-by-unknown-CA PDF: "signed_untrusted".
```
