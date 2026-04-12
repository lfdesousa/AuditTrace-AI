#!/usr/bin/env bash
# Health check for audittrace-stack services
set -euo pipefail

MEMORY_URL="${SOVEREIGN_MEMORY_URL:-https://localhost/health}"
CHROMA_URL="${SOVEREIGN_CHROMA_HEALTH:-http://localhost:8000/api/v2/heartbeat}"
LANGFUSE_URL="${SOVEREIGN_LANGFUSE_HEALTH:-http://localhost:3000/}"
TRAEFIK_URL="${SOVEREIGN_TRAEFIK_HEALTH:-http://localhost:8080/api/overview}"

echo "Checking audittrace-stack services..."
echo ""

FAILED=0

check() {
    local name="$1" url="$2" critical="${3:-true}" extra="${4:-}"
    if curl -sf --max-time 5 ${extra} "${url}" > /dev/null 2>&1; then
        echo "  OK    ${name}"
    elif [ "${critical}" = "true" ]; then
        echo "  FAIL  ${name} (${url})"
        FAILED=1
    else
        echo "  WARN  ${name} (${url}) -- optional"
    fi
}

check "memory-server" "${MEMORY_URL}" "true" "-k"
check "ChromaDB" "${CHROMA_URL}" "true"
check "Traefik" "${TRAEFIK_URL}" "true"
check "Langfuse" "${LANGFUSE_URL}" "false"

echo ""

if [ "${FAILED}" -eq 0 ]; then
    echo "All critical services healthy."
else
    echo "Some critical services failed."
    exit 1
fi
