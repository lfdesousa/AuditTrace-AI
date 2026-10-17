#!/usr/bin/env bash
# Operator-run script — captures the LIVE realm's identity-provider
# brokering config back into version control (SPEC #403).
#
# This is the round-trip counterpart to scripts/setup-idp-federation.sh:
# that script goes VC-less config -> live Keycloak; this one goes live
# Keycloak -> committable JSON, closing the one-way import-only gap noted
# in ADR-044 §Risks ("Realm JSON reimport overwrites brokered IdPs").
#
# What it does:
#   1. Authenticates kcadm.sh to Keycloak as admin (same pattern as
#      setup-idp-federation.sh).
#   2. Fetches the live realm's `identity-provider/instances` and, for each
#      alias, its `.../mappers`.
#   3. Pipes both through `scripts/deploy/idp_export.py`, which replaces
#      every IdP's `config.clientSecret` with the Vault-substitution
#      placeholder documented in ADR-044 §3
#      (`${vault:idp/<alias>/client_secret}`) — see that module's docstring
#      for the full guarantee. This is the ONLY step that touches secrets;
#      everything either side of it is orchestration.
#   4. Merges the externalized payload's `identityProviders` +
#      `identityProviderMappers` into BOTH committed realm JSONs
#      (`keycloak/realm-audittrace.json` and
#      `charts/audittrace/files/realm-audittrace.json`), so a fresh
#      `--import-realm` restores what a cold reimport would otherwise wipe.
#   5. Refuses to write if a plaintext secret is still present anywhere in
#      the candidate output — belt-and-suspenders on top of step 3's own
#      internal check, run against the FINAL file content this script is
#      about to write, not just the intermediate payload.
#
# Resolving the ${vault:...} placeholder back into a real secret at deploy
# time is a SEPARATE, not-yet-implemented step (documented in ADR-044's
# #403 follow-up): an init step ahead of `--import-realm`, using the same
# Vault-Agent-authenticated path already used for other `AUDITTRACE_*`
# secrets, resolves each placeholder against
# `kv/audittrace/idp/<alias>/client_secret` before Keycloak ever sees the
# realm JSON. This script's job stops at "produce a safe-to-commit file";
# it does not perform that resolution.
#
# Pre-requisites:
#   1. Keycloak is up and the audittrace realm is imported.
#   2. Keycloak admin password reachable via KEYCLOAK_ADMIN_PASSWORD env var.
#   3. kubectl, jq, and python3 (this repo's .venv or any 3.12+) on PATH.
#
# Usage:
#   KEYCLOAK_ADMIN_PASSWORD=<...> ./scripts/export-idp-federation.sh
#
# Idempotent: re-running overwrites the previous export with the current
# live state. Read from Keycloak only — never mutates the live realm.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

NAMESPACE="${AUDITTRACE_NAMESPACE:-audittrace}"
REALM="${REALM:-audittrace}"
KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-}"

REALM_JSON_PATHS=(
  "${REPO_ROOT}/keycloak/realm-audittrace.json"
  "${REPO_ROOT}/charts/audittrace/files/realm-audittrace.json"
)

require() {
  if [[ -z "${!1:-}" ]]; then
    echo "❌ Required env var missing: $1" >&2
    exit 1
  fi
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "❌ Required command not on PATH: $1" >&2
    exit 1
  fi
}

require KEYCLOAK_ADMIN_PASSWORD
require_cmd kubectl
require_cmd jq

PYTHON="${PYTHON:-${REPO_ROOT}/.venv/bin/python}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON="python3"
  require_cmd python3
fi
IDP_EXPORT_MODULE="scripts.deploy.idp_export"

KC_POD="$(kubectl -n "${NAMESPACE}" get pod -l app.kubernetes.io/component=keycloak -o jsonpath='{.items[0].metadata.name}' 2>&1)"
if [[ -z "${KC_POD}" ]]; then
  echo "❌ No Keycloak pod found in namespace ${NAMESPACE}" >&2
  exit 1
fi

kcadm() {
  kubectl -n "${NAMESPACE}" exec -i "${KC_POD}" -c keycloak -- \
    /opt/keycloak/bin/kcadm.sh "$@"
}

echo "🔐 export-idp-federation.sh — capturing live IdP brokering from realm ${REALM}"
echo "   namespace=${NAMESPACE} keycloak-pod=${KC_POD}"

echo "▶ Authenticating to Keycloak admin..."
kcadm config credentials \
  --server http://localhost:8080 \
  --realm master \
  --user admin \
  --password "${KEYCLOAK_ADMIN_PASSWORD}" >/dev/null
echo "  ✓ authenticated"
unset KEYCLOAK_ADMIN_PASSWORD

echo "▶ Fetching live identity-provider/instances..."
IDP_INSTANCES_JSON="$(kcadm get "identity-provider/instances" -r "${REALM}")"
IDP_COUNT="$(echo "${IDP_INSTANCES_JSON}" | jq 'length')"
echo "  ✓ ${IDP_COUNT} identity provider(s) found live"

echo "▶ Fetching mappers for each identity provider..."
IDP_MAPPERS_JSON="[]"
if [[ "${IDP_COUNT}" -gt 0 ]]; then
  aliases="$(echo "${IDP_INSTANCES_JSON}" | jq -r '.[].alias')"
  while IFS= read -r alias; do
    [[ -z "${alias}" ]] && continue
    mappers_for_alias="$(kcadm get "identity-provider/instances/${alias}/mappers" -r "${REALM}")"
    IDP_MAPPERS_JSON="$(jq -s 'add' <(echo "${IDP_MAPPERS_JSON}") <(echo "${mappers_for_alias}"))"
    echo "  ✓ ${alias}: $(echo "${mappers_for_alias}" | jq 'length') mapper(s)"
  done <<<"${aliases}"
fi

for realm_path in "${REALM_JSON_PATHS[@]}"; do
  if [[ ! -f "${realm_path}" ]]; then
    echo "❌ Expected realm JSON not found: ${realm_path}" >&2
    exit 1
  fi
done

echo "▶ Externalizing client secrets (scripts/deploy/idp_export.py)..."
EXPORT_PAYLOAD="$(
  cd "${REPO_ROOT}" && "${PYTHON}" -m "${IDP_EXPORT_MODULE}" \
    --identity-providers <(echo "${IDP_INSTANCES_JSON}") \
    --mappers <(echo "${IDP_MAPPERS_JSON}")
)"
echo "  ✓ payload built ($(echo "${EXPORT_PAYLOAD}" | jq '.identityProviders | length') IdP(s), $(echo "${EXPORT_PAYLOAD}" | jq '.identityProviderMappers | length') mapper(s))"

# Belt-and-suspenders: idp_export.py's build_export_payload() already
# refuses to return a payload with a non-externalized clientSecret, but
# re-check the INTERMEDIATE payload here too, against the RAW live
# secrets fetched above — catches a stray shell-variable mixup (an old
# cached EXPORT_PAYLOAD, a copy-paste of the wrong variable) that no
# amount of correctness inside idp_export.py itself could see. Every
# correctly-externalized secret is a "${vault:...}" placeholder and
# therefore never matches this grep.
live_secrets="$(echo "${IDP_INSTANCES_JSON}" | jq -r '.[].config.clientSecret? // empty')"
if [[ -n "${live_secrets}" ]]; then
  while IFS= read -r secret; do
    [[ -z "${secret}" ]] && continue
    if grep -qF -- "${secret}" <<<"${EXPORT_PAYLOAD}"; then
      echo "❌ REFUSING to patch the realm JSON files: a plaintext IdP" >&2
      echo "   client secret is present in the export payload. This must" >&2
      echo "   never happen — see idp_export.py's externalization guarantee." >&2
      exit 1
    fi
  done <<<"${live_secrets}"
fi

echo "▶ Patching identityProviders / identityProviderMappers into committed realm JSON files..."
# --patch-into edits ONLY the two known array spans via a text-level patch
# (scripts/deploy/idp_export.py::patch_realm_document), not a full JSON
# round-trip — required because charts/audittrace/files/realm-audittrace.json
# embeds Go/Helm template directives that are not valid JSON, so a jq-based
# whole-document merge cannot be used on it.
(
  cd "${REPO_ROOT}" && "${PYTHON}" -m "${IDP_EXPORT_MODULE}" \
    --identity-providers <(echo "${IDP_INSTANCES_JSON}") \
    --mappers <(echo "${IDP_MAPPERS_JSON}") \
    --patch-into "${REALM_JSON_PATHS[@]}" \
    --output /dev/null
)
for realm_path in "${REALM_JSON_PATHS[@]}"; do
  echo "  ✓ ${realm_path#"${REPO_ROOT}"/}"
done

echo ""
echo "✅ Live IdP brokering captured into version control."
echo ""
echo "Next steps:"
echo "  - Review the diff: git diff keycloak/realm-audittrace.json charts/audittrace/files/realm-audittrace.json"
echo "  - Run gitleaks (or 'make lint') to confirm no plaintext secret landed in the diff."
echo "  - Commit. A cold '--import-realm' will now carry the \${vault:...}"
echo "    placeholder, which a deploy-time resolution step (ADR-044 #403"
echo "    follow-up) must materialize from kv/audittrace/idp/<alias>/client_secret"
echo "    before Keycloak imports the realm — resolution is NOT yet wired up;"
echo "    this script only makes the capture reconstructible."
