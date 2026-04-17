#!/usr/bin/env bash
# DESIGN §16 Phase 7 — mint a dev JWT against the running Keycloak.
#
# Fetches an access token via the OAuth2 client_credentials grant
# using the `audittrace-dev` client. The token carries all the
# scopes the memory-server needs (legacy audittrace:* + Phase 3
# memory:*), a hardcoded audience claim `audittrace-ai`
# for the JWT validation path, and a real Keycloak `sub` that
# threads through as the UserContext user_id.
#
# **This script is designed to run INSIDE the audittrace-net
# docker network** so the JWT's ``iss`` claim matches the memory-
# server's configured ``keycloak_issuer`` (http://keycloak:8080/...).
# Running it from the host would produce a token with a different
# ``iss`` that the memory-server would reject. Use either:
#
#   docker exec audittrace-ai bash /tmp/mint-dev-jwt.sh
#
# or wrap it in a helper that `docker cp`s the script + sets
# CLIENT_SECRET via `-e`:
#
#   docker cp scripts/mint-dev-jwt.sh audittrace-ai:/tmp/
#   TOKEN=$(docker exec -e CLIENT_SECRET=$(cat secrets/dev_client_secret.txt) \
#       audittrace-ai bash /tmp/mint-dev-jwt.sh)
#
# Use cases:
#
#   1. **Curl smoke tests** with AUDITTRACE_AUTH_REQUIRED=true —
#      wrap the `docker exec` pattern above in a shell function.
#   2. **Bruno collection variables** — pre-request script that
#      shells out to the same docker exec pattern.
#   3. **Ad-hoc dogfooding** when OpenCode isn't yet configured for
#      real auth.
#
# Environment:
#
#   KEYCLOAK_URL         — defaults to http://keycloak:8080 (the
#                          internal docker-network hostname the
#                          memory-server is configured to trust).
#                          Override when running outside the network
#                          but note the issuer-mismatch trap above.
#   REALM                — defaults to `audittrace`
#   CLIENT_ID            — defaults to `audittrace-dev`
#   CLIENT_SECRET        — required; read from the environment or
#                          from `secrets/dev_client_secret.txt` as a
#                          fallback (create the file the first time
#                          via `kcadm get clients/$ID/client-secret`)
#
# Output: the raw `access_token` on stdout, nothing else, so the
# script composes cleanly into ``$(./scripts/mint-dev-jwt.sh)``.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

KEYCLOAK_URL="${KEYCLOAK_URL:-http://keycloak:8080}"
REALM="${REALM:-audittrace}"
CLIENT_ID="${CLIENT_ID:-audittrace-dev}"

# Secret: env var wins, falls back to secrets/dev_client_secret.txt.
CLIENT_SECRET="${CLIENT_SECRET:-}"
if [[ -z "${CLIENT_SECRET}" ]]; then
    SECRET_FILE="${SCRIPT_DIR}/../secrets/dev_client_secret.txt"
    if [[ -f "${SECRET_FILE}" ]]; then
        CLIENT_SECRET="$(cat "${SECRET_FILE}")"
    fi
fi

if [[ -z "${CLIENT_SECRET}" ]]; then
    echo "ERROR: CLIENT_SECRET not set and secrets/dev_client_secret.txt missing" >&2
    echo "" >&2
    echo "To create the secrets file:" >&2
    echo "  docker exec audittrace-keycloak /opt/keycloak/bin/kcadm.sh \\" >&2
    echo "      config credentials --server http://localhost:8080 \\" >&2
    echo "      --realm master --user admin --password admin" >&2
    echo "  CLIENT_INTERNAL_ID=\$(docker exec audittrace-keycloak \\" >&2
    echo "      /opt/keycloak/bin/kcadm.sh get clients -r ${REALM} \\" >&2
    echo "      -q clientId=${CLIENT_ID} --fields id --format csv --noquotes)" >&2
    echo "  docker exec audittrace-keycloak /opt/keycloak/bin/kcadm.sh \\" >&2
    echo "      get clients/\$CLIENT_INTERNAL_ID/client-secret \\" >&2
    echo "      -r ${REALM} | python3 -c \"import sys,json; print(json.load(sys.stdin)['value'])\" \\" >&2
    echo "      > secrets/dev_client_secret.txt" >&2
    exit 1
fi

RESPONSE=$(curl -sS --fail-with-body \
    -X POST "${KEYCLOAK_URL}/realms/${REALM}/protocol/openid-connect/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=client_credentials" \
    -d "client_id=${CLIENT_ID}" \
    -d "client_secret=${CLIENT_SECRET}")

echo "${RESPONSE}" | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])"
