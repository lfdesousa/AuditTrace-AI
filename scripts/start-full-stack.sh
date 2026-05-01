#!/usr/bin/env bash
# Start the full sovereign-ai stack + Langfuse sibling
# Run from the audittrace-ai root directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}/.."

cd "${PROJECT_DIR}"

echo "=== audittrace-stack — Full Stack Startup ==="
echo ""

# 1. Check .env exists
if [ ! -f .env ]; then
    echo "ERROR: .env not found. Run:"
    echo "  cp .env.example .env"
    echo "  ./scripts/setup-secrets.sh"
    echo "  # Edit .env with the generated secrets"
    exit 1
fi

# 2. Check certs exist
if [ ! -f certs/sovereign.pem ]; then
    echo "ERROR: TLS certificates not found. Run:"
    echo "  ./certs/generate-certs.sh"
    exit 1
fi

# 3. Create shared Docker network
echo "[1/5] Creating Docker network..."
docker network create audittrace-net 2>/dev/null || echo "  Network audittrace-net exists"

# 4. Start Langfuse sibling (if set up)
LANGFUSE_DIR="${PROJECT_DIR}/../langfuse"
if [ -f "${LANGFUSE_DIR}/docker-compose.yml" ]; then
    echo "[2/5] Starting Langfuse..."
    (cd "${LANGFUSE_DIR}" && docker compose up -d)
else
    echo "[2/5] Langfuse not set up. Run ./scripts/setup-langfuse.sh to enable."
fi

# 5. Start sovereign-ai stack
echo "[3/5] Starting sovereign-ai stack..."
docker compose up -d --build

# 6. Wait for services
echo "[4/5] Waiting for services..."
for i in $(seq 1 30); do
    if curl -sf --max-time 5 -k https://localhost/health > /dev/null 2>&1; then
        echo "  memory-server ready after $((i * 2))s"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "  TIMEOUT: memory-server not ready after 60s"
        docker compose logs memory-server --tail=20
        exit 1
    fi
    printf "."
    sleep 2
done

# 7. Health check
echo "[5/5] Health check..."
./scripts/healthcheck.sh

echo ""
echo "=== Stack is running ==="
echo ""
echo "  API:       https://localhost/v1/chat/completions"
echo "  Context:   https://localhost/context"
echo "  Health:    https://localhost/health"
echo "  Traefik:   http://localhost:8080"
if [ -f "${LANGFUSE_DIR}/docker-compose.yml" ]; then
    echo "  Langfuse:  http://localhost:3000"
fi
echo ""
