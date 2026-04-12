#!/usr/bin/env bash
# Generate initial secrets for audittrace-stack
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS_DIR="${SCRIPT_DIR}/../secrets"

mkdir -p "${SECRETS_DIR}"

if [ ! -f "${SECRETS_DIR}/postgres_password.txt" ]; then
    openssl rand -base64 32 | tr -d '\n' > "${SECRETS_DIR}/postgres_password.txt"
    echo "Generated: secrets/postgres_password.txt"
else
    echo "Exists: secrets/postgres_password.txt (skipped)"
fi

if [ ! -f "${SECRETS_DIR}/chroma_token.txt" ]; then
    openssl rand -hex 32 > "${SECRETS_DIR}/chroma_token.txt"
    echo "Generated: secrets/chroma_token.txt"
else
    echo "Exists: secrets/chroma_token.txt (skipped)"
fi

if [ ! -f "${SECRETS_DIR}/redis_password.txt" ]; then
    openssl rand -base64 32 | tr -d '\n' > "${SECRETS_DIR}/redis_password.txt"
    echo "Generated: secrets/redis_password.txt"
else
    echo "Exists: secrets/redis_password.txt (skipped)"
fi

echo ""
echo "Add these to your .env file:"
echo "  SOVEREIGN_POSTGRES_PASSWORD=$(cat "${SECRETS_DIR}/postgres_password.txt")"
echo "  SOVEREIGN_CHROMA_TOKEN=$(cat "${SECRETS_DIR}/chroma_token.txt")"
echo "  SOVEREIGN_REDIS_PASSWORD=$(cat "${SECRETS_DIR}/redis_password.txt")"
