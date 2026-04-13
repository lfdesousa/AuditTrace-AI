#!/usr/bin/env bash
# ADR-028 — Set up the observability aggregation stack.
# Sibling compose stack (same pattern as Langfuse, ADR-021.2).
#
# Starts: OTel Collector, Prometheus, Loki, Promtail, Grafana
# Shares: sovereign-ai-net with the main application stack
#
# Usage:
#   ./scripts/setup-observability.sh          # Start stack
#   ./scripts/setup-observability.sh --down   # Stop stack
#
# Grafana: http://localhost:3001 (admin / sovereign)
# Prometheus: http://localhost:9090
# Loki: http://localhost:3100

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="${SCRIPT_DIR}/../../observability-stack"

if [[ ! -d "${STACK_DIR}" ]]; then
    echo "ERROR: Observability stack not found at ${STACK_DIR}"
    echo "Expected: ../observability-stack/ relative to AuditTrace-AI"
    exit 1
fi

# Ensure shared network exists (idempotent)
docker network create sovereign-ai-net 2>/dev/null || true

if [[ "${1:-}" == "--down" ]]; then
    echo "Stopping observability stack..."
    docker compose -f "${STACK_DIR}/docker-compose.yml" down
    echo "Stopped."
    exit 0
fi

echo "Starting observability stack (ADR-028)..."
docker compose -f "${STACK_DIR}/docker-compose.yml" up -d

echo ""
echo "Observability stack started:"
echo "  Grafana:    http://localhost:3001  (admin / sovereign)"
echo "  Prometheus: http://localhost:19090"
echo "  Loki:       http://localhost:3100"
echo "  OTel:       http://localhost:4318  (OTLP HTTP receiver)"
echo ""
echo "Memory-server OTLP export should point at:"
echo "  SOVEREIGN_OTLP_ENDPOINT=http://otel-collector:4318/v1/traces"
