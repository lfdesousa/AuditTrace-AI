#!/usr/bin/env bash
# Print the k3s cni0 bridge IP — the address pods use to reach the laptop host.
# Falls back to 10.42.0.1 (flannel default) when cni0 is absent or its IP
# can't be read. Used by the zbook runbook:
#
#   helm upgrade --install audittrace ./charts/audittrace \
#     --set global.hostNodeIP=$(./scripts/detect-k3s-bridge.sh)
#
# See ADR-045 and docs/guides/zbook-runbook.md.
set -euo pipefail

ip=$(ip -4 -br addr show cni0 2>/dev/null | awk 'NR==1 {print $3}' | cut -d/ -f1)
echo "${ip:-10.42.0.1}"
