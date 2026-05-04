#!/usr/bin/env bash
# Operator-run idempotent script — provisions the three memory-layer
# write scopes on the audittrace realm via kcadm.sh, when (e.g.) the
# post-install/post-upgrade Helm Job is unavailable or the operator
# wants to reconcile the realm without re-running `helm upgrade`.
#
# 99% of the time the chart's `ensure-memory-scopes` Job (in
# `templates/keycloak/job-memory-scopes.yaml`) does this automatically
# on every helm install/upgrade, so this script is a backstop —
# useful for:
#   - bare-metal disaster recovery (realm wiped + re-imported)
#   - debugging the kcadm logic without bouncing the chart
#   - manual re-provisioning after a `vault.enabled=false` ↔ true
#     migration where the Job's Vault role wasn't yet ready
#
# Pre-requisites:
#   1. `kubectl` configured for the audittrace cluster.
#   2. The audittrace Keycloak pod is running.
#   3. KEYCLOAK_ADMIN_PASSWORD reachable in env, OR vault.enabled=true
#      and the operator can read kv/audittrace/keycloak/admin via
#      `vault kv get`.
#
# Usage (env-var creds):
#   KEYCLOAK_ADMIN_PASSWORD=...  ./scripts/setup-memory-scopes.sh
#
# Usage (Vault-resolved creds):
#   vault login -method=...
#   ./scripts/setup-memory-scopes.sh         # script reads from vault
#
# Idempotent: every operation gates on a "does it already exist?"
# check, mirroring the Helm Job's bash logic verbatim. Re-runs are
# safe (and recommended after any realm-import).

set -euo pipefail

NAMESPACE="${AUDITTRACE_NAMESPACE:-audittrace}"
RELEASE="${AUDITTRACE_RELEASE:-audittrace}"
REALM="${REALM:-audittrace}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "❌ Required command not on PATH: $1" >&2
    exit 1
  fi
}
require_cmd kubectl

# ----- Resolve Keycloak admin password -----
# Three resolution paths, tried in order:
#   1. KEYCLOAK_ADMIN_PASSWORD env var already set — use as-is.
#   2. Local `vault` CLI on PATH (operator has vault installed) — `vault kv get`.
#   3. In-cluster fallback: `kubectl exec audittrace-vault-0 -- vault kv get`.
#      Mirrors setup-vault.sh's vault_exec pattern so the umbrella target
#      `make k8s-bootstrap-secrets` works without requiring the vault CLI on
#      the operator's machine — the only prerequisite is VAULT_TOKEN exported,
#      same as setup-vault.sh.
if [[ -z "${KEYCLOAK_ADMIN_PASSWORD:-}" ]]; then
  if command -v vault >/dev/null 2>&1; then
    echo "▶ KEYCLOAK_ADMIN_PASSWORD not set — attempting Vault lookup (local CLI)..."
    if KEYCLOAK_ADMIN_PASSWORD=$(vault kv get -field=password kv/audittrace/keycloak/admin 2>/dev/null); then
      export KEYCLOAK_ADMIN_PASSWORD
      echo "  ✓ resolved from kv/audittrace/keycloak/admin"
    else
      echo "❌ Could not resolve KEYCLOAK_ADMIN_PASSWORD via local Vault CLI." >&2
      echo "   Either set the env var directly or run \`vault login\` first." >&2
      exit 1
    fi
  elif [[ -n "${VAULT_TOKEN:-}" ]] \
       && kubectl -n "${NAMESPACE}" get pod "${RELEASE}-vault-0" >/dev/null 2>&1; then
    echo "▶ KEYCLOAK_ADMIN_PASSWORD not set — attempting in-cluster Vault lookup..."
    if KEYCLOAK_ADMIN_PASSWORD=$(kubectl -n "${NAMESPACE}" exec -i "${RELEASE}-vault-0" -- \
                                   env "VAULT_TOKEN=${VAULT_TOKEN}" \
                                   vault kv get -field=password kv/audittrace/keycloak/admin 2>/dev/null); then
      export KEYCLOAK_ADMIN_PASSWORD
      echo "  ✓ resolved from kv/audittrace/keycloak/admin (via kubectl exec ${RELEASE}-vault-0)"
    else
      echo "❌ In-cluster Vault lookup failed. Has 'vault kv put kv/audittrace/keycloak/admin password=...' been run?" >&2
      exit 1
    fi
  else
    echo "❌ KEYCLOAK_ADMIN_PASSWORD env var is empty and no Vault path is available." >&2
    echo "   Either:" >&2
    echo "     - export KEYCLOAK_ADMIN_PASSWORD directly, or" >&2
    echo "     - install the vault CLI locally and run \`vault login\`, or" >&2
    echo "     - export VAULT_TOKEN so this script can read via kubectl exec." >&2
    exit 1
  fi
fi

# ----- Find the Keycloak pod -----
KC_POD="$(kubectl -n "${NAMESPACE}" get pod \
            -l app.kubernetes.io/component=keycloak \
            -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
if [[ -z "${KC_POD}" ]]; then
  echo "❌ No Keycloak pod found in namespace ${NAMESPACE}." >&2
  exit 1
fi

echo "🔐 setup-memory-scopes.sh — reconciling memory:*:write scopes"
echo "   namespace=${NAMESPACE} release=${RELEASE} realm=${REALM} pod=${KC_POD}"

kcadm() {
  kubectl -n "${NAMESPACE}" exec -i "${KC_POD}" -c keycloak -- \
    /opt/keycloak/bin/kcadm.sh "$@"
}

# ----- Authenticate -----
echo "▶ authenticating to Keycloak admin..."
kcadm config credentials \
  --server http://localhost:8080 \
  --realm master \
  --user admin \
  --password "${KEYCLOAK_ADMIN_PASSWORD}" >/dev/null
echo "  ✓ authenticated"

SCOPES=(
  "memory:episodic:write"
  "memory:procedural:write"
  "memory:semantic:write"
)

# ----- Ensure each scope exists -----
declare -A SCOPE_ID
for SCOPE in "${SCOPES[@]}"; do
  EXISTING=$(kcadm get client-scopes -r "${REALM}" \
               --fields id,name --format csv --noquotes 2>/dev/null \
             | awk -F, -v n="${SCOPE}" '$2 == n {print $1; exit}')
  if [[ -n "${EXISTING}" ]]; then
    echo "  ⊝ scope ${SCOPE}: exists (${EXISTING})"
    SCOPE_ID["${SCOPE}"]="${EXISTING}"
  else
    kcadm create client-scopes -r "${REALM}" \
      -s "name=${SCOPE}" \
      -s protocol=openid-connect \
      -s 'attributes."include.in.token.scope"=true' >/dev/null
    NEW_ID=$(kcadm get client-scopes -r "${REALM}" \
               --fields id,name --format csv --noquotes \
             | awk -F, -v n="${SCOPE}" '$2 == n {print $1; exit}')
    echo "  ✓ scope ${SCOPE}: created (${NEW_ID})"
    SCOPE_ID["${SCOPE}"]="${NEW_ID}"
  fi
done

# ----- Bind scopes to clients -----
declare -A CLIENT_KIND
CLIENT_KIND["admin-client"]="default"
CLIENT_KIND["audittrace-opencode"]="optional"
CLIENT_KIND["audittrace-webui"]="optional"

bind_scope() {
  local CLIENT_ID="$1" SCOPE="$2" KIND="$3"
  local CLIENT_UUID SCOPE_UUID PATH_SUFFIX

  CLIENT_UUID=$(kcadm get clients -r "${REALM}" \
                  -q "clientId=${CLIENT_ID}" \
                  --fields id --format csv --noquotes 2>/dev/null \
                | tr -d '"' | head -1)
  if [[ -z "${CLIENT_UUID}" ]]; then
    echo "  ⚠ client ${CLIENT_ID}: not found (skipped)"
    return 0
  fi
  SCOPE_UUID="${SCOPE_ID[$SCOPE]:-}"
  if [[ -z "${SCOPE_UUID}" ]]; then
    echo "❌ scope ${SCOPE}: id not resolvable — bug" >&2
    return 1
  fi

  if [[ "${KIND}" == "default" ]]; then
    PATH_SUFFIX="default-client-scopes"
  else
    PATH_SUFFIX="optional-client-scopes"
  fi

  # `-b '{}'` not `-s ''` — see configmap-memory-scopes-script.yaml
  # for the 2026-05-03 lesson on why the kcadm binding command needs
  # an explicit empty JSON body rather than an empty -s argument.
  if ! kcadm update \
         "clients/${CLIENT_UUID}/${PATH_SUFFIX}/${SCOPE_UUID}" \
         -r "${REALM}" -b '{}' >/dev/null 2>&1; then
    echo "  ✗ ${CLIENT_ID} ${KIND} ← ${SCOPE} (kcadm rejected the bind)" >&2
    return 1
  fi
  echo "  ✓ ${CLIENT_ID} ${KIND} ← ${SCOPE}"
}

for CLIENT_ID in "${!CLIENT_KIND[@]}"; do
  KIND="${CLIENT_KIND[$CLIENT_ID]}"
  echo "▶ binding scopes to client ${CLIENT_ID} (${KIND})..."
  for SCOPE in "${SCOPES[@]}"; do
    bind_scope "${CLIENT_ID}" "${SCOPE}" "${KIND}"
  done
done

# ----- User-identity protocol mappers -----
# Without these, JWTs from user-facing clients lack `preferred_username`,
# `email`, `name` etc. — only a bare UUID `sub`. Found in PR A's
# 2026-05-03 live test. See the in-cluster Job script for the full
# rationale on why direct mappers (not the standard `profile` scope).
# admin-client doesn't need this — service accounts have no human
# identity to surface.
ensure_mapper() {
  local CLIENT_ID="$1" MAPPER_NAME="$2" USER_ATTR="$3" CLAIM_NAME="$4"
  local CLIENT_UUID
  CLIENT_UUID=$(kcadm get clients -r "${REALM}" \
                  -q "clientId=${CLIENT_ID}" \
                  --fields id --format csv --noquotes 2>/dev/null \
                | tr -d '"' | head -1)
  if [[ -z "${CLIENT_UUID}" ]]; then
    echo "  ⚠ client ${CLIENT_ID}: not found (skipped)"
    return 0
  fi
  local existing
  existing=$(kcadm get \
               "clients/${CLIENT_UUID}/protocol-mappers/models" \
               -r "${REALM}" --fields name --format csv --noquotes \
               2>/dev/null | grep -Fx "${MAPPER_NAME}" || true)
  if [[ -n "${existing}" ]]; then
    echo "  ⊝ ${CLIENT_ID} mapper ${MAPPER_NAME}: exists"
    return 0
  fi
  # Single-line JSON via printf — same shape as the in-cluster Job
  # script; see configmap-memory-scopes-script.yaml for the heredoc-vs-
  # YAML rationale.
  local body
  body=$(printf '{"name":"%s","protocol":"openid-connect","protocolMapper":"oidc-usermodel-property-mapper","config":{"user.attribute":"%s","claim.name":"%s","jsonType.label":"String","id.token.claim":"true","access.token.claim":"true","userinfo.token.claim":"true"}}' \
    "${MAPPER_NAME}" "${USER_ATTR}" "${CLAIM_NAME}")
  printf '%s' "${body}" | kcadm create \
    "clients/${CLIENT_UUID}/protocol-mappers/models" \
    -r "${REALM}" -f - >/dev/null
  echo "  ✓ ${CLIENT_ID} mapper ${MAPPER_NAME}: created"
}

for CLIENT_ID in audittrace-opencode audittrace-webui; do
  echo "▶ ensuring user-identity mappers on ${CLIENT_ID}..."
  ensure_mapper "${CLIENT_ID}" preferred-username username preferred_username
  ensure_mapper "${CLIENT_ID}" email email email
  ensure_mapper "${CLIENT_ID}" given-name firstName given_name
  ensure_mapper "${CLIENT_ID}" family-name lastName family_name
done

echo "✅ memory-scopes provisioning complete."
