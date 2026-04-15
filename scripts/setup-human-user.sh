#!/usr/bin/env bash
# Provision the ADR-032 Device-Flow client + realm user against a
# RUNNING Keycloak via the admin REST API.
#
# Background: the realm-sovereign-ai.json realm import only runs on
# Keycloak first-boot. If you already have a Keycloak container with
# the sovereign-ai realm created (from before ADR-032 landed), this
# script brings your live realm up to the new spec without the
# destructive realm re-import.
#
# Idempotent: re-running is a no-op when the resources already exist.
#
#   scripts/setup-human-user.sh
#   KEYCLOAK_BASE=http://localhost:8080 \
#   KEYCLOAK_ADMIN_USER=admin \
#   KEYCLOAK_ADMIN_PASSWORD=... \
#     scripts/setup-human-user.sh
#
# Creates (if missing):
#   • Public client ``audittrace-opencode`` with Device Flow enabled
#   • Realm user ``luis`` with a temporary password
#   • Audience mapper (aud=sovereign-memory-server) on the client
#
# Prints the client-id, the user-id, and the temporary password to
# stdout — capture them in your notes, then complete the password
# reset via the Device Flow login screen.

set -euo pipefail

KEYCLOAK_BASE="${KEYCLOAK_BASE:-http://localhost:8080}"
REALM="${KEYCLOAK_REALM:-sovereign-ai}"
ADMIN_USER="${KEYCLOAK_ADMIN_USER:-admin}"
ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-}"
CLIENT_ID="${CLIENT_ID:-audittrace-opencode}"
USERNAME="${AUDITTRACE_HUMAN_USER:-luis}"
USER_EMAIL="${AUDITTRACE_HUMAN_EMAIL:-lde.sousa@gmail.com}"
TEMP_PASSWORD="${AUDITTRACE_HUMAN_TEMP_PASSWORD:-change-me-on-first-login}"

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: $1 not found on PATH" >&2
    exit 2
  }
}
require curl
require jq

# Self-signed mkcert TLS is the dev default; auto-bypass for localhost
# unless the operator opts in via CURL_VERIFY=1.
CURL_OPTS=()
if [[ "$KEYCLOAK_BASE" == https://localhost* ]] && [[ "${CURL_VERIFY:-0}" != "1" ]]; then
  CURL_OPTS+=("-k")
fi

log() { echo "[setup-human-user] $*" >&2; }

if [[ -z "$ADMIN_PASSWORD" ]]; then
  echo "error: KEYCLOAK_ADMIN_PASSWORD not set" >&2
  echo "(export the master-realm admin password before running)" >&2
  exit 3
fi

# ───────────────────────── master-realm admin token ─────────────────

log "requesting admin token from master realm"
ADMIN_TOKEN="$(curl "${CURL_OPTS[@]}" -s -S --fail \
  -d "client_id=admin-cli" \
  -d "username=$ADMIN_USER" \
  -d "password=$ADMIN_PASSWORD" \
  -d "grant_type=password" \
  "$KEYCLOAK_BASE/realms/master/protocol/openid-connect/token" \
  | jq -r '.access_token')"

if [[ -z "$ADMIN_TOKEN" || "$ADMIN_TOKEN" == "null" ]]; then
  echo "error: failed to obtain admin token" >&2
  exit 4
fi

AUTH_H=(-H "Authorization: Bearer $ADMIN_TOKEN")
JSON_H=(-H "Content-Type: application/json")

# ──────────────────────────── client ────────────────────────────────

log "checking whether client '$CLIENT_ID' exists"
CLIENT_JSON="$(curl "${CURL_OPTS[@]}" -s -S "${AUTH_H[@]}" \
  "$KEYCLOAK_BASE/admin/realms/$REALM/clients?clientId=$CLIENT_ID")"
CLIENT_UUID="$(echo "$CLIENT_JSON" | jq -r '.[0].id // empty')"

if [[ -z "$CLIENT_UUID" ]]; then
  log "creating client '$CLIENT_ID'"
  CLIENT_PAYLOAD="$(jq -n --arg cid "$CLIENT_ID" '{
    clientId: $cid,
    enabled: true,
    publicClient: true,
    standardFlowEnabled: true,
    directAccessGrantsEnabled: false,
    serviceAccountsEnabled: false,
    protocol: "openid-connect",
    redirectUris: ["urn:ietf:wg:oauth:2.0:oob", "http://localhost:*"],
    webOrigins: ["+"],
    attributes: {
      "oauth2.device.authorization.grant.enabled": "true",
      "oauth2.device.polling.interval": "5"
    },
    defaultClientScopes: [
      "sovereign-ai:query",
      "sovereign-ai:context",
      "sovereign-ai:audit",
      "memory:episodic:read",
      "memory:procedural:read",
      "memory:conversational:read-own",
      "memory:semantic:read"
    ]
  }')"
  curl "${CURL_OPTS[@]}" -s -S --fail -X POST \
    "${AUTH_H[@]}" "${JSON_H[@]}" \
    -d "$CLIENT_PAYLOAD" \
    "$KEYCLOAK_BASE/admin/realms/$REALM/clients" > /dev/null
  CLIENT_UUID="$(curl "${CURL_OPTS[@]}" -s -S "${AUTH_H[@]}" \
    "$KEYCLOAK_BASE/admin/realms/$REALM/clients?clientId=$CLIENT_ID" \
    | jq -r '.[0].id')"
  log "client uuid: $CLIENT_UUID"

  # Attach the audience mapper so tokens carry aud=sovereign-memory-server
  # (our JWT validator checks this claim).
  log "adding audience mapper"
  MAPPER_PAYLOAD='{
    "name": "aud-sovereign-memory-server",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-audience-mapper",
    "config": {
      "included.custom.audience": "sovereign-memory-server",
      "id.token.claim": "false",
      "access.token.claim": "true"
    }
  }'
  curl "${CURL_OPTS[@]}" -s -S --fail -X POST \
    "${AUTH_H[@]}" "${JSON_H[@]}" \
    -d "$MAPPER_PAYLOAD" \
    "$KEYCLOAK_BASE/admin/realms/$REALM/clients/$CLIENT_UUID/protocol-mappers/models" > /dev/null
else
  log "client '$CLIENT_ID' already exists (uuid=$CLIENT_UUID) — skipping creation"
fi

# ──────────────────────────── user ──────────────────────────────────

log "checking whether user '$USERNAME' exists"
USER_JSON="$(curl "${CURL_OPTS[@]}" -s -S "${AUTH_H[@]}" \
  "$KEYCLOAK_BASE/admin/realms/$REALM/users?username=$USERNAME&exact=true")"
USER_ID="$(echo "$USER_JSON" | jq -r '.[0].id // empty')"

if [[ -z "$USER_ID" ]]; then
  log "creating user '$USERNAME'"
  USER_PAYLOAD="$(jq -n \
    --arg username "$USERNAME" \
    --arg email "$USER_EMAIL" \
    --arg pw "$TEMP_PASSWORD" \
    '{
      username: $username,
      email: $email,
      firstName: "Luis Filipe",
      lastName: "de Sousa",
      enabled: true,
      emailVerified: true,
      credentials: [{type: "password", value: $pw, temporary: true}]
    }')"
  curl "${CURL_OPTS[@]}" -s -S --fail -X POST \
    "${AUTH_H[@]}" "${JSON_H[@]}" \
    -d "$USER_PAYLOAD" \
    "$KEYCLOAK_BASE/admin/realms/$REALM/users" > /dev/null
  USER_ID="$(curl "${CURL_OPTS[@]}" -s -S "${AUTH_H[@]}" \
    "$KEYCLOAK_BASE/admin/realms/$REALM/users?username=$USERNAME&exact=true" \
    | jq -r '.[0].id')"
  log "user id: $USER_ID"
else
  log "user '$USERNAME' already exists (id=$USER_ID) — skipping creation"
fi

# ──────────────────────────── report ────────────────────────────────

cat <<EOF

  ✅ Keycloak ready for ADR-032 Device Flow

  Client:      $CLIENT_ID  (uuid=$CLIENT_UUID)
  User:        $USERNAME   (id=$USER_ID)
  Temp pw:     $TEMP_PASSWORD  (will be forced to reset on first login)

  Test it:
    scripts/audittrace-login

EOF
