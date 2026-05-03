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
if [[ -z "${KEYCLOAK_ADMIN_PASSWORD:-}" ]]; then
  if command -v vault >/dev/null 2>&1; then
    echo "▶ KEYCLOAK_ADMIN_PASSWORD not set — attempting Vault lookup..."
    if KEYCLOAK_ADMIN_PASSWORD=$(vault kv get -field=password kv/audittrace/keycloak/admin 2>/dev/null); then
      export KEYCLOAK_ADMIN_PASSWORD
      echo "  ✓ resolved from kv/audittrace/keycloak/admin"
    else
      echo "❌ Could not resolve KEYCLOAK_ADMIN_PASSWORD via Vault." >&2
      echo "   Either set the env var directly or run `vault login` first." >&2
      exit 1
    fi
  else
    echo "❌ KEYCLOAK_ADMIN_PASSWORD env var is empty and `vault` CLI is not on PATH." >&2
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

echo "✅ memory-scopes provisioning complete."
