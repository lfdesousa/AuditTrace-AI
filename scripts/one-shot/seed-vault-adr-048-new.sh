#!/usr/bin/env bash
# One-shot Vault seeder for the 5 NEW ADR-048 paths only.
#
# This script is intentionally narrower than `setup-vault.sh`:
# it touches NOTHING already in Vault. The cluster is live and
# `kv/audittrace/minio/root` carries an in-use kms_master_key —
# overwriting that path makes existing buckets unreadable on the
# next MinIO pod restart. So we use this dedicated script for
# today's chart-flip pre-flight and defer running the full
# `setup-vault.sh` until a calmer window.
#
# What this script DOES write (5 paths):
#   - kv/audittrace/minio/audittrace_app   (username, password)
#   - kv/audittrace/minio/content_control  (username, password)
#   - kv/audittrace/rabbitmq/admin         (username, password, erlang_cookie)
#   - kv/audittrace/content-control/rabbitmq (username, password)
#   - kv/audittrace/docker-hub/pat         (username, pat)
#
# What this script will NEVER write:
#   ✗ kv/audittrace/postgres/superuser
#   ✗ kv/audittrace/postgres/app
#   ✗ kv/audittrace/summariser/db
#   ✗ kv/audittrace/redis/main
#   ✗ kv/audittrace/chromadb/main
#   ✗ kv/audittrace/minio/root           ← contains kms_master_key
#   ✗ KV mount enable / config
#   ✗ k8s-auth role configuration
#   ✗ Vault policy uploads
#
# Pre-requisites:
#   - VAULT_TOKEN exported (typically the root token from
#     ~/work/audittrace-private/runbooks/vault-init-2026-05-01.json)
#   - SECRETS_DIR points at a directory containing the 10 seed files
#     (default: ~/work/audittrace-private/secrets/)
#   - kubectl works against the audittrace cluster
#
# Usage:
#   export VAULT_TOKEN=$(jq -r .root_token \
#     ~/work/audittrace-private/runbooks/vault-init-2026-05-01.json)
#   ./scripts/one-shot/seed-vault-adr-048-new.sh           # safe: aborts if any new path already exists
#   ./scripts/one-shot/seed-vault-adr-048-new.sh --force   # idempotent re-run
#
# Delete this script in the PR-B8 closure commit; the supported
# operator path going forward is `make k8s-bootstrap-secrets`
# (which reads the same files via the SECRETS_DIR env var).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS_DIR="${SECRETS_DIR:-$HOME/work/audittrace-private/secrets}"
NAMESPACE="${AUDITTRACE_NAMESPACE:-audittrace}"
RELEASE="${AUDITTRACE_RELEASE:-audittrace}"
VAULT_POD="${RELEASE}-vault-0"
KV_MOUNT="${KV_MOUNT:-kv}"

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
fi

if [[ -z "${VAULT_TOKEN:-}" ]]; then
  echo "❌ VAULT_TOKEN not exported." >&2
  echo "   export VAULT_TOKEN=\$(jq -r .root_token \\" >&2
  echo "     ~/work/audittrace-private/runbooks/vault-init-2026-05-01.json)" >&2
  exit 1
fi

if [[ ! -d "${SECRETS_DIR}" ]]; then
  echo "❌ SECRETS_DIR not found: ${SECRETS_DIR}" >&2
  exit 1
fi

# kubectl exec wrapper — busybox `env` smuggles VAULT_TOKEN into the pod
# without touching kubectl exec --env (which doesn't exist).
vault_exec() {
  kubectl -n "${NAMESPACE}" exec -i "${VAULT_POD}" -c vault -- \
    env "VAULT_TOKEN=${VAULT_TOKEN}" vault "$@"
}

# Read a seed file with chomp (vault kv put is whitespace-sensitive).
read_seed() {
  local file="$1"
  if [[ ! -f "${SECRETS_DIR}/${file}" ]]; then
    echo "❌ Missing: ${SECRETS_DIR}/${file}" >&2
    exit 1
  fi
  # chomp trailing newline to avoid encoding it into the secret value
  printf '%s' "$(cat "${SECRETS_DIR}/${file}")"
}

# Pre-flight: confirm Vault reachable + unsealed.
echo "▶ Checking Vault status..."
status=$(vault_exec status -format=json 2>/dev/null || true)
if [[ -z "${status}" ]]; then
  echo "❌ Vault not reachable via ${NAMESPACE}/${VAULT_POD}" >&2
  exit 1
fi
sealed=$(printf '%s' "${status}" | python3 -c "import sys,json;print(json.load(sys.stdin)['sealed'])")
if [[ "${sealed}" != "False" ]]; then
  echo "❌ Vault is sealed; refusing to write." >&2
  exit 1
fi
echo "  ✓ Vault unsealed"

# Pre-flight: 5 new paths must NOT exist (unless --force).
NEW_PATHS=(
  "${KV_MOUNT}/audittrace/minio/audittrace_app"
  "${KV_MOUNT}/audittrace/minio/content_control"
  "${KV_MOUNT}/audittrace/rabbitmq/admin"
  "${KV_MOUNT}/audittrace/content-control/rabbitmq"
  "${KV_MOUNT}/audittrace/docker-hub/pat"
)

if [[ "${FORCE}" -eq 0 ]]; then
  echo "▶ Pre-flight: confirming new paths are absent..."
  for p in "${NEW_PATHS[@]}"; do
    if vault_exec kv get -format=json "${p}" >/dev/null 2>&1; then
      echo "❌ Path already exists: ${p}" >&2
      echo "   Re-run with --force to overwrite." >&2
      exit 1
    fi
    echo "  ✓ ${p} (absent — safe to seed)"
  done
fi

# Snapshot the current versions of the 3 NEVER-touched paths so the
# operator can verify nothing changed after this script runs.
echo "▶ Snapshotting NEVER-touched path versions (must be unchanged after this script):"
for p in postgres/superuser minio/root chromadb/main; do
  ver=$(vault_exec kv metadata get -format=json "${KV_MOUNT}/audittrace/${p}" 2>/dev/null \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('data',{}).get('current_version','?'))" 2>/dev/null \
        || echo "?")
  echo "  · ${KV_MOUNT}/audittrace/${p}  version=${ver}"
done

# --- Seed: 5 new paths -----------------------------------------------
echo ""
echo "▶ Seeding 5 NEW Vault paths from ${SECRETS_DIR}..."

seed() {
  local path="$1"; shift
  local args=("$@")
  vault_exec kv put "${KV_MOUNT}/audittrace/${path}" "${args[@]}" >/dev/null
  echo "  ✓ ${KV_MOUNT}/audittrace/${path}"
}

seed minio/audittrace_app \
  "username=$(read_seed minio_audittrace_app_user.txt)" \
  "password=$(read_seed minio_audittrace_app_password.txt)"

seed minio/content_control \
  "username=$(read_seed minio_content_control_user.txt)" \
  "password=$(read_seed minio_content_control_password.txt)"

seed rabbitmq/admin \
  "username=$(read_seed rabbitmq_user.txt)" \
  "password=$(read_seed rabbitmq_password.txt)" \
  "erlang_cookie=$(read_seed rabbitmq_erlang_cookie.txt)"

seed content-control/rabbitmq \
  "username=$(read_seed rabbitmq_content_control_user.txt)" \
  "password=$(read_seed rabbitmq_content_control_password.txt)"

seed docker-hub/pat \
  "username=$(read_seed docker_hub_username.txt)" \
  "pat=$(read_seed docker_hub_pat.txt)"

echo ""
echo "✅ Done. 5 NEW Vault paths seeded."
echo ""
echo "Verify the never-touched paths still report the SAME version numbers:"
echo "  for p in postgres/superuser minio/root chromadb/main; do"
echo "    kubectl -n ${NAMESPACE} exec -i ${VAULT_POD} -c vault -- \\"
echo "      env VAULT_TOKEN=\$VAULT_TOKEN vault kv metadata get -format=json \\"
echo "      ${KV_MOUNT}/audittrace/\$p | jq .data.current_version"
echo "  done"
