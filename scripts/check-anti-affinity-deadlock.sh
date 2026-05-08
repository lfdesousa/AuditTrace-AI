#!/usr/bin/env bash
# Anti-affinity deadlock probe — fail fast BEFORE deploy.
#
# Background: a workload with ``replicaCount > 1`` and
# ``requiredDuringSchedulingIgnoredDuringExecution`` podAntiAffinity
# (the strict variant — soft anti-affinity is "preferred…") cannot
# schedule on a single-node cluster: kube-scheduler can't satisfy
# "this pod's replicas must run on different nodes" with only one
# node. The deploy hangs in Pending forever, helm upgrade times out,
# and the operator only finds out via kubectl describe pod long after
# the fact.
#
# This script renders the chart, walks every Deployment / StatefulSet
# spec for the requiredDuringSchedulingIgnoredDuringExecution variant,
# pulls the desired replicaCount, and compares against the current
# node count. If replicas > nodes AND the strict anti-affinity is
# present, fail with a clear diagnostic.
#
# Exit codes:
#   0 — no deadlock risk: either no strict anti-affinity, or
#       replicas <= nodes for every workload that has it
#   1 — environment problem (no kubectl, no helm, can't render)
#   2 — deadlock risk detected: at least one workload's strict
#       anti-affinity exceeds available node count
#
# Usage:
#   CHART_DIR=charts/audittrace VALUES_FILE=charts/audittrace/values-local.yaml \
#     scripts/check-anti-affinity-deadlock.sh

set -euo pipefail

CHART_DIR="${CHART_DIR:-charts/audittrace}"
RELEASE="${RELEASE:-audittrace}"
NAMESPACE="${NAMESPACE:-audittrace}"
VALUES_FILE="${VALUES_FILE:-charts/audittrace/values-local.yaml}"

for tool in helm kubectl python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "check-anti-affinity-deadlock: $tool not on PATH; skipping" >&2
        exit 1
    fi
done

if ! kubectl version --request-timeout=5s >/dev/null 2>&1; then
    echo "check-anti-affinity-deadlock: cluster unreachable; skipping" >&2
    exit 1
fi

# ── Render chart + count nodes ──────────────────────────────────────────────
node_count=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)
if [ "$node_count" -lt 1 ]; then
    echo "check-anti-affinity-deadlock: 0 nodes visible; skipping" >&2
    exit 1
fi

rendered_file=$(mktemp)
trap 'rm -f "$rendered_file"' EXIT
helm template "$RELEASE" "$CHART_DIR" -f "$VALUES_FILE" >"$rendered_file" 2>/dev/null || {
    echo "check-anti-affinity-deadlock: helm template failed; skipping" >&2
    exit 1
}

# ── Walk rendered manifests + find workloads at risk ────────────────────────
# Python parses the multi-doc YAML; bash regex would be too fragile on
# nested anti-affinity structures. node_count + rendered file path
# via env vars; Python opens the file directly to avoid bash heredoc
# / here-string complications.
NODE_COUNT="$node_count" RENDERED_FILE="$rendered_file" python3 <<'PY'
import os
import sys

node_count = int(os.environ["NODE_COUNT"])
rendered_path = os.environ["RENDERED_FILE"]

try:
    import yaml
except ImportError:
    print("check-anti-affinity-deadlock: PyYAML not available; skipping", file=sys.stderr)
    sys.exit(1)

risks = []
with open(rendered_path) as f:
    docs = list(yaml.safe_load_all(f))
for doc in docs:
    if not isinstance(doc, dict):
        continue
    kind = doc.get("kind")
    if kind not in ("Deployment", "StatefulSet"):
        continue
    spec = doc.get("spec") or {}
    replicas = spec.get("replicas") or 1
    pod_spec = (spec.get("template") or {}).get("spec") or {}
    affinity = pod_spec.get("affinity") or {}
    pod_anti = affinity.get("podAntiAffinity") or {}
    # The strict variant is required-during-scheduling. soft is preferred-during-scheduling.
    strict = pod_anti.get("requiredDuringSchedulingIgnoredDuringExecution") or []
    if not strict:
        continue
    if replicas > node_count:
        name = (doc.get("metadata") or {}).get("name", "<unknown>")
        risks.append((name, kind, replicas, node_count))

if risks:
    print("check-anti-affinity-deadlock: FAIL — strict anti-affinity exceeds node count", file=sys.stderr)
    for name, kind, replicas, nc in risks:
        print(
            f"  {kind}/{name}: replicas={replicas} requires {replicas} distinct nodes; "
            f"cluster has {nc}",
            file=sys.stderr,
        )
    print(
        "  recovery: either reduce replicaCount, soften anti-affinity to "
        "preferredDuringSchedulingIgnoredDuringExecution, or scale the cluster",
        file=sys.stderr,
    )
    sys.exit(2)

print(
    f"check-anti-affinity-deadlock: PASS — no strict-anti-affinity risk on {node_count}-node cluster"
)
PY
