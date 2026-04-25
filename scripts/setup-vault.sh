#!/usr/bin/env bash
# Operator-run idempotent script — configures Vault after `vault operator
# init/unseal`. Reads policy + role definitions from the cluster
# ConfigMap (audittrace-vault-policies, shipped by ADR-043 §8), seeds
# initial secrets from secrets/*.txt files, applies via `vault` CLI
# inside the vault-0 pod.
#
# Pre-requisites:
#   1. helm install/upgrade with --set vault.enabled=true
#   2. vault-0 pod running and INITIALISED (operator ran `vault operator
#      init` and saved the shamir keys + root token)
#   3. vault-0 pod UNSEALED (operator ran `vault operator unseal` 3x)
#   4. VAULT_TOKEN exported in operator's environment (the root token
#      from step 2; rotate to a less-privileged operator token after
#      bootstrap)
#
# Usage:
#   export VAULT_TOKEN="<root-token-from-init>"
#   export AUDITTRACE_NAMESPACE="audittrace"      # default
#   export AUDITTRACE_RELEASE="audittrace"        # default
#   ./scripts/setup-vault.sh
#
# Idempotent. Safe to re-run. Each step checks current Vault state
# before applying.
#
# See ADR-043 §5 (KV path conventions) and §8 (provisioning).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS_DIR="${SCRIPT_DIR}/../secrets"

NAMESPACE="${AUDITTRACE_NAMESPACE:-audittrace}"
RELEASE="${AUDITTRACE_RELEASE:-audittrace}"
VAULT_POD="${RELEASE}-vault-0"
POLICIES_CM="${RELEASE}-vault-policies"

if [[ -z "${VAULT_TOKEN:-}" ]]; then
  echo "❌ VAULT_TOKEN not set. Export the root token from vault operator init."
  exit 1
fi

# kubectl exec helpers — keep VAULT_TOKEN inside the pod's env, not in
# command-line args (would show up in `ps`).
vault_exec() {
  kubectl -n "${NAMESPACE}" exec -i \
    --env="VAULT_TOKEN=${VAULT_TOKEN}" \
    "${VAULT_POD}" -- vault "$@"
}

vault_exec_stdin() {
  # Pass stdin through to the vault CLI.
  kubectl -n "${NAMESPACE}" exec -i \
    --env="VAULT_TOKEN=${VAULT_TOKEN}" \
    "${VAULT_POD}" -- vault "$@"
}

# Read a key from the policies ConfigMap.
cm_get() {
  kubectl -n "${NAMESPACE}" get configmap "${POLICIES_CM}" \
    -o jsonpath="{.data.${1}}"
}

echo "🔐 setup-vault.sh — configuring Vault for AuditTrace-AI"
echo "   namespace=${NAMESPACE}"
echo "   release=${RELEASE}"
echo "   vault pod=${VAULT_POD}"

# --- 0. Confirm Vault is reachable + unsealed ------------------------
echo "▶ Checking Vault status..."
status=$(vault_exec status -format=json 2>/dev/null || true)
if [[ -z "${status}" ]]; then
  echo "❌ Vault not reachable. Is the pod running?"
  exit 1
fi
sealed=$(printf '%s' "${status}" | python3 -c "import sys,json;print(json.load(sys.stdin)['sealed'])")
if [[ "${sealed}" != "False" ]]; then
  echo "❌ Vault is sealed. Run 'vault operator unseal' first."
  exit 1
fi
echo "  ✓ Vault unsealed"

# --- 1. Enable KV v2 secret engine -----------------------------------
KV_MOUNT="$(cm_get 'kv-mount\.env' | grep '^mount=' | cut -d= -f2)"
echo "▶ Ensuring KV v2 mount at '${KV_MOUNT}/'..."
if vault_exec secrets list -format=json | grep -q "\"${KV_MOUNT}/\""; then
  echo "  ✓ already enabled (skip)"
else
  vault_exec secrets enable -path="${KV_MOUNT}" -version=2 kv >/dev/null
  echo "  ✓ enabled"
fi

# --- 2. Enable Kubernetes auth method --------------------------------
echo "▶ Ensuring kubernetes auth method..."
if vault_exec auth list -format=json | grep -q '"kubernetes/"'; then
  echo "  ✓ already enabled (skip)"
else
  vault_exec auth enable kubernetes >/dev/null
  echo "  ✓ enabled"
fi

# --- 3. Configure Kubernetes auth method -----------------------------
# The Vault SA's projected token is automatically mounted by the upstream
# chart at /var/run/secrets/kubernetes.io/serviceaccount/token.
echo "▶ Configuring kubernetes auth method..."
vault_exec write auth/kubernetes/config \
  kubernetes_host="https://kubernetes.default.svc" \
  token_reviewer_jwt=@/var/run/secrets/kubernetes.io/serviceaccount/token \
  kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
  >/dev/null
echo "  ✓ configured"

# --- 4. Apply policies -----------------------------------------------
echo "▶ Applying policies..."
for policy in audittrace-server keycloak minio summariser-job; do
  printf '%s' "$(cm_get "${policy}\.hcl")" \
    | vault_exec_stdin policy write "${policy}" - >/dev/null
  echo "  ✓ ${policy}"
done

# --- 5. Apply role bindings ------------------------------------------
echo "▶ Applying role bindings..."
for role in audittrace-server keycloak minio summariser-job; do
  # Convert env-style ConfigMap data to vault write key=value args.
  args=$(cm_get "role-${role}\.env" | grep -v '^$' | tr '\n' ' ')
  # shellcheck disable=SC2086
  vault_exec write "auth/kubernetes/role/${role}" ${args} >/dev/null
  echo "  ✓ ${role}"
done

# --- 6. Seed initial secrets from secrets/*.txt ----------------------
# Operator-supplied seed values. Reads from the repo's secrets/ dir,
# matching the existing scripts/setup-secrets.sh convention.
echo "▶ Seeding initial KV secrets from ${SECRETS_DIR}..."

seed_kv() {
  local path="$1"; shift
  local kv_args=()
  for kv in "$@"; do
    local key="${kv%%=*}"
    local file="${kv##*=}"
    if [[ ! -f "${SECRETS_DIR}/${file}" ]]; then
      echo "  ⊝ skipped ${path} (missing ${SECRETS_DIR}/${file})"
      return 0
    fi
    kv_args+=("${key}=$(cat "${SECRETS_DIR}/${file}")")
  done
  vault_exec kv put "${KV_MOUNT}/audittrace/${path}" "${kv_args[@]}" >/dev/null
  echo "  ✓ ${KV_MOUNT}/audittrace/${path}"
}

seed_kv postgres/app password=postgres_password.txt
seed_kv summariser/db password=postgres_password.txt
seed_kv redis/main password=redis_password.txt
seed_kv chromadb/main token=chroma_token.txt
seed_kv minio/root secret_key=minio_secret_key.txt kms_master_key=minio_kms_key.txt
# Keycloak admin pw is NOT seeded from secrets/ — it must be supplied
# directly via `vault kv put kv/audittrace/keycloak/admin password=...`
# by the operator after this script runs. See runbook 02-vault-unseal.md
# for the rotation procedure.
echo "  ⊝ keycloak/admin — operator must seed manually (see runbook)"

echo ""
echo "✅ Vault configuration complete."
echo ""
echo "Next steps:"
echo "  1. Set Keycloak admin password:"
echo "     vault kv put ${KV_MOUNT}/audittrace/keycloak/admin password=<NEW_PW>"
echo "  2. helm upgrade --set vault.enabled=true to engage Vault Agent"
echo "     annotations on workloads."
echo "  3. Rotate VAULT_TOKEN: revoke the root token and use a less-"
echo "     privileged operator token going forward."
