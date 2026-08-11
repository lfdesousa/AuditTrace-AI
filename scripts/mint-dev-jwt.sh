#!/usr/bin/env bash
# DESIGN §16 Phase 7 — mint a dev JWT against the running Keycloak.
#
# Fetches an access token via the OAuth2 client_credentials grant
# using the `audittrace-dev` client. The token carries an EXPLICIT
# minimal read-only scope set (see SCOPE below), a hardcoded audience
# claim `audittrace-ai` for the JWT validation path, and a real
# Keycloak `sub` that threads through as the UserContext user_id.
#
# **#370 silent-admin lock.** This script used to send NO `scope`
# parameter, so a minted token inherited whatever DEFAULT scopes the
# `audittrace-dev` client happened to carry live — which is exactly
# how #370 went undetected: the live realm drifted to hold
# `audittrace:admin` as a client default while the committed JSON
# never declared it, so every dev token silently carried admin. The
# committed `keycloak/realm-audittrace.json` definition for
# `audittrace-dev` is already read-only-correct (no admin scope; see
# `docs/architecture/sequence-oauth2-flow.md`), but relying on "the
# client's current defaults happen to be safe" is not a lock — a
# future re-provision could add one back. Requesting an EXPLICIT
# `scope=` here closes that gap: Keycloak only grants what is BOTH
# requested AND assigned to the client, so an admin scope silently
# added to the client's live defaults tomorrow still would not reach
# a token minted by this script.
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
#   SCOPE                 — explicit space-separated scope list sent
#                          as the OAuth2 `scope` request parameter
#                          (#370). Defaults to a minimal READ-ONLY
#                          subset of the scopes `audittrace-dev`
#                          actually carries: every declared default
#                          scope EXCEPT `audittrace:index` (a
#                          write-triggering reindex scope — see
#                          `audittrace.auth.ALL_SCOPES`). Never
#                          widen the default past what
#                          `keycloak/realm-audittrace.json`'s
#                          `audittrace-dev` client declares — the
#                          whole point of requesting a scope
#                          explicitly is that this token cannot pick
#                          up more than what is enumerated here, no
#                          matter what the client's live defaults
#                          drift to.
#
# Output: the raw `access_token` on stdout, nothing else, so the
# script composes cleanly into ``$(./scripts/mint-dev-jwt.sh)``.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

KEYCLOAK_URL="${KEYCLOAK_URL:-http://keycloak:8080}"
REALM="${REALM:-audittrace}"
CLIENT_ID="${CLIENT_ID:-audittrace-dev}"

# #370 silent-admin lock: explicit minimal read-only scope. Requesting
# this instead of omitting `scope` entirely means the minted token can
# never silently inherit a scope the client was NOT asked for — even
# one added to the client's live defaults after this script was
# written. Keeps every scope `audittrace-dev` currently declares as a
# default EXCEPT `audittrace:index` (write-triggering reindex; see
# `audittrace.auth.ALL_SCOPES`).
SCOPE="${SCOPE:-audittrace:query audittrace:context audittrace:audit memory:episodic:read memory:procedural:read memory:conversational:read-own memory:semantic:read}"

# Secret: env var wins, falls back to ${SECRETS_DIR}/dev_client_secret.txt.
# SECRETS_DIR override matches setup-vault.sh — operator points at
# ~/work/audittrace-private/secrets/ when seed material lives outside
# the repo.
SECRETS_DIR="${SECRETS_DIR:-${SCRIPT_DIR}/../secrets}"
CLIENT_SECRET="${CLIENT_SECRET:-}"
if [[ -z "${CLIENT_SECRET}" ]]; then
    SECRET_FILE="${SECRETS_DIR}/dev_client_secret.txt"
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
    -d "client_secret=${CLIENT_SECRET}" \
    -d "scope=${SCOPE}")

echo "${RESPONSE}" | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])"
