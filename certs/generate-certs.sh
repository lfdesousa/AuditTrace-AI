#!/usr/bin/env bash
# Generate self-signed TLS certificates via mkcert (ADR-021)
# Prerequisites: mkcert installed (https://github.com/FiloSottile/mkcert)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing mkcert CA into system trust store..."
mkcert -install

echo "Generating certificates..."
mkcert \
  -cert-file "${SCRIPT_DIR}/sovereign.pem" \
  -key-file "${SCRIPT_DIR}/sovereign-key.pem" \
  localhost 127.0.0.1 ::1 sovereign-ai.local

echo "Certificates generated:"
echo "  ${SCRIPT_DIR}/sovereign.pem"
echo "  ${SCRIPT_DIR}/sovereign-key.pem"
echo ""
echo "These files are gitignored (*.pem, *.key)."
