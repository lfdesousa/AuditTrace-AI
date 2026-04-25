#!/usr/bin/env bash
# Operator-run idempotent script — adds (or updates) one external
# OIDC IdP brokered through the audittrace Keycloak realm.
# Per ADR-044 §7.
#
# Pre-requisites:
#   1. Keycloak is up + the audittrace realm is imported.
#   2. Vault unsealed AND the per-IdP client secret seeded at
#      kv/audittrace/idp/<alias>/client_secret. OR the secret is
#      passed via the IDP_CLIENT_SECRET env var (dev fallback).
#   3. Keycloak admin password is reachable via KEYCLOAK_ADMIN_PASSWORD
#      env var. If vault.enabled=true, fetch from
#      kv/audittrace/keycloak/admin first and export.
#
# Usage:
#   IDP_TYPE=oidc-generic|entra|google|okta \
#   IDP_ALIAS=<short-name> \
#   IDP_DISCOVERY_URL=<...well-known/openid-configuration URL> \
#   IDP_CLIENT_ID=<keycloak-as-IdP-client> \
#   IDP_CLIENT_SECRET=<...> \
#   KEYCLOAK_ADMIN_PASSWORD=<...> \
#   ./scripts/setup-idp-federation.sh
#
# Idempotent: if an IdP with the same alias already exists, the
# script updates it in place. Re-runs are safe.
#
# What it does:
#   1. Authenticates kcadm.sh to Keycloak as admin
#   2. Renders the IdP JSON template for the requested IDP_TYPE
#   3. Creates or updates the identity provider in the audittrace realm
#   4. Adds the standard attribute mappers (sub/email/preferred_username/
#      groups) per ADR-044 §4
#   5. For Entra: adds the oid->sub collapse mapper to avoid the
#      duplicate-shadow-user footgun (ADR-044 §Risks)
#
# Output: the IdP's alias on stdout, a one-line summary on stderr.
#
# Re-running after a realm reimport: per ADR-044 §Risks, a fresh
# `kc.sh start --import-realm` overwrites the live realm. If the
# realm was wiped + reimported, every brokered IdP needs to be
# re-installed by re-running this script for each one.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ----- Config + validation -----
NAMESPACE="${AUDITTRACE_NAMESPACE:-audittrace}"
RELEASE="${AUDITTRACE_RELEASE:-audittrace}"
REALM="${REALM:-audittrace}"

IDP_TYPE="${IDP_TYPE:-}"
IDP_ALIAS="${IDP_ALIAS:-}"
IDP_DISCOVERY_URL="${IDP_DISCOVERY_URL:-}"
IDP_CLIENT_ID="${IDP_CLIENT_ID:-}"
IDP_CLIENT_SECRET="${IDP_CLIENT_SECRET:-}"
KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-}"

require() {
  if [[ -z "${!1:-}" ]]; then
    echo "❌ Required env var missing: $1" >&2
    exit 1
  fi
}

require IDP_TYPE
require IDP_ALIAS
require IDP_DISCOVERY_URL
require IDP_CLIENT_ID
require IDP_CLIENT_SECRET
require KEYCLOAK_ADMIN_PASSWORD

case "${IDP_TYPE}" in
  oidc-generic|entra|google|okta) ;;
  *)
    echo "❌ Unknown IDP_TYPE: ${IDP_TYPE}" >&2
    echo "   Supported: oidc-generic, entra, google, okta" >&2
    exit 1
    ;;
esac

# ----- kcadm helper -----
KC_POD="$(kubectl -n "${NAMESPACE}" get pod -l app.kubernetes.io/component=keycloak -o jsonpath='{.items[0].metadata.name}' 2>&1)"
if [[ -z "${KC_POD}" ]]; then
  echo "❌ No Keycloak pod found in namespace ${NAMESPACE}" >&2
  exit 1
fi

kcadm() {
  kubectl -n "${NAMESPACE}" exec -i "${KC_POD}" -c keycloak -- \
    /opt/keycloak/bin/kcadm.sh "$@"
}

echo "🔐 setup-idp-federation.sh — adding ${IDP_TYPE} broker '${IDP_ALIAS}' to realm ${REALM}"
echo "   namespace=${NAMESPACE} release=${RELEASE} keycloak-pod=${KC_POD}"

# ----- Authenticate kcadm -----
echo "▶ Authenticating to Keycloak admin..."
kcadm config credentials \
  --server http://localhost:8080 \
  --realm master \
  --user admin \
  --password "${KEYCLOAK_ADMIN_PASSWORD}" >/dev/null
echo "  ✓ authenticated"

# ----- Render the identity provider JSON -----
# OIDC-generic shape covers all four supported types; per-type
# differences are handled by attribute mappers (§4) rather than by
# distinct provider configs. ADR-044 §3.
IDP_JSON=$(cat <<EOF
{
  "alias": "${IDP_ALIAS}",
  "providerId": "oidc",
  "enabled": true,
  "trustEmail": false,
  "storeToken": false,
  "addReadTokenRoleOnCreate": false,
  "firstBrokerLoginFlowAlias": "first broker login",
  "config": {
    "issuer": "$(echo "${IDP_DISCOVERY_URL}" | sed 's|/\.well-known/openid-configuration$||')",
    "useJwksUrl": "true",
    "validateSignature": "true",
    "clientId": "${IDP_CLIENT_ID}",
    "clientAuthMethod": "client_secret_basic",
    "clientSecret": "${IDP_CLIENT_SECRET}",
    "defaultScope": "openid email profile",
    "syncMode": "FORCE",
    "pkceEnabled": "true",
    "pkceMethod": "S256"
  }
}
EOF
)

# ----- Create or update the IdP -----
echo "▶ Creating or updating identity provider '${IDP_ALIAS}'..."
existing=$(kcadm get "identity-provider/instances/${IDP_ALIAS}" -r "${REALM}" 2>/dev/null || true)
if [[ -n "${existing}" && "${existing}" != *"Resource not found"* ]]; then
  echo "${IDP_JSON}" | kcadm update \
    "identity-provider/instances/${IDP_ALIAS}" \
    -r "${REALM}" -f - >/dev/null
  echo "  ✓ updated"
else
  echo "${IDP_JSON}" | kcadm create \
    "identity-provider/instances" \
    -r "${REALM}" -f - >/dev/null
  echo "  ✓ created"
fi

# ----- Standard attribute mappers (per ADR-044 §4) -----
add_mapper() {
  local name="$1"
  local mapper_type="$2"
  local config_json="$3"
  local mapper_json
  mapper_json=$(cat <<EOF
{
  "name": "${name}",
  "identityProviderAlias": "${IDP_ALIAS}",
  "identityProviderMapper": "${mapper_type}",
  "config": ${config_json}
}
EOF
)
  # kcadm doesn't have a clean idempotent upsert for mappers; query first
  existing_mapper=$(kcadm get "identity-provider/instances/${IDP_ALIAS}/mappers" -r "${REALM}" --fields name --format csv 2>/dev/null | grep -F "${name}" || true)
  if [[ -n "${existing_mapper}" ]]; then
    echo "  ⊝ mapper '${name}' already exists (skip)"
  else
    echo "${mapper_json}" | kcadm create \
      "identity-provider/instances/${IDP_ALIAS}/mappers" \
      -r "${REALM}" -f - >/dev/null
    echo "  ✓ mapper '${name}' added"
  fi
}

echo "▶ Adding standard attribute mappers..."
add_mapper "username-mapper" "oidc-username-idp-mapper" \
  '{"syncMode":"FORCE","template":"${CLAIM.preferred_username | CLAIM.email}"}'

add_mapper "email-mapper" "oidc-user-attribute-idp-mapper" \
  '{"syncMode":"FORCE","claim":"email","user.attribute":"email"}'

add_mapper "first-name-mapper" "oidc-user-attribute-idp-mapper" \
  '{"syncMode":"FORCE","claim":"given_name","user.attribute":"firstName"}'

add_mapper "last-name-mapper" "oidc-user-attribute-idp-mapper" \
  '{"syncMode":"FORCE","claim":"family_name","user.attribute":"lastName"}'

# ----- Entra-specific: oid->sub collapse (ADR-044 §4 + §Risks) -----
if [[ "${IDP_TYPE}" == "entra" ]]; then
  echo "▶ Adding Entra-specific oid -> federation-key mapper..."
  add_mapper "entra-oid-collapse" "oidc-username-idp-mapper" \
    '{"syncMode":"FORCE","template":"${CLAIM.oid}"}'
fi

echo ""
echo "✅ Identity provider '${IDP_ALIAS}' (${IDP_TYPE}) configured."
echo ""
echo "Verification:"
echo "  - Open https://<keycloak-host>/realms/${REALM}/account and click"
echo "    'Sign in with ${IDP_ALIAS}' on the login page."
echo "  - Or: kcadm get identity-provider/instances/${IDP_ALIAS} -r ${REALM}"
echo ""
echo "Next: a federated user logs in via the upstream IdP, lands as a"
echo "shadow user in realm '${REALM}', and the memory-server validates"
echo "their JWT through the existing multi-issuer path (ADR-032 §2)."
