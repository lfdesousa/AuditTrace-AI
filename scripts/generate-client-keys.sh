#!/usr/bin/env bash
# Generate RSA-2048 key pairs for each sovereign-ai agent (ADR-022)
# Public keys are uploaded to Keycloak for private_key_jwt authentication.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEYS_DIR="${SCRIPT_DIR}/../keys"

mkdir -p "${KEYS_DIR}"

CLIENTS=(opencode-agent continue-agent roocode-agent inject-memory admin-client)

for client in "${CLIENTS[@]}"; do
    if [ ! -f "${KEYS_DIR}/${client}.key" ]; then
        echo "Generating key pair for ${client}..."
        openssl genrsa -out "${KEYS_DIR}/${client}.key" 2048 2>/dev/null
        openssl rsa -in "${KEYS_DIR}/${client}.key" -pubout -out "${KEYS_DIR}/${client}.pub" 2>/dev/null
        echo "  ${KEYS_DIR}/${client}.key (private — keep secret)"
        echo "  ${KEYS_DIR}/${client}.pub (public — upload to Keycloak)"
    else
        echo "Exists: ${client} (skipped)"
    fi
done

echo ""
echo "Upload public keys (.pub) to Keycloak:"
echo "  1. Go to Keycloak Admin → sovereign-ai realm → Clients"
echo "  2. Select client → Credentials tab"
echo "  3. Set Client Authenticator to 'Signed JWT'"
echo "  4. Import public key"
echo ""
echo "Key files are gitignored (*.key, *.pub in keys/)."
