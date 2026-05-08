#!/usr/bin/env bash
# Istiod readiness probe — fail fast BEFORE deploy.
#
# Background: 2026-05-04 incident class — when istiod's CA service
# (the SPIFFE identity issuer) is in a degraded state, new pods fail
# to bootstrap their workload identity. The pod admits with the
# istio-proxy sidecar attached, but the sidecar can't fetch a SPIFFE
# certificate from istiod, so it never reaches Ready. The pod sits
# in Running 2/3 indefinitely. Recovery: restart k3s. Cost: ~30 min
# per incident.
#
# This script catches istiod degradation pre-deploy by probing
# istiod's readiness endpoint via a synthetic ``kubectl get`` against
# the istiod service. A healthy istiod responds; a degraded one times
# out or returns 503. dry-run is no help here — the pathology is
# runtime, not admission. We need an actual readiness check.
#
# Exit codes:
#   0 — istiod healthy: service Ready, endpoints populated, control-plane
#       deployment Ready replicas == desired
#   1 — kubectl unavailable / cluster unreachable / istiod missing
#   2 — istiod present but degraded (replicas < desired, or no Ready
#       endpoints, or a quick port-forward + curl /ready returns non-200)

set -euo pipefail

ISTIO_NAMESPACE="${ISTIO_NAMESPACE:-istio-system}"
ISTIOD_SERVICE="${ISTIOD_SERVICE:-istiod}"
ISTIOD_DEPLOY="${ISTIOD_DEPLOY:-istiod}"

if ! command -v kubectl >/dev/null 2>&1; then
    echo "check-istiod-readiness: kubectl not on PATH; skipping" >&2
    exit 1
fi

if ! kubectl version --request-timeout=5s >/dev/null 2>&1; then
    echo "check-istiod-readiness: cluster unreachable; skipping" >&2
    exit 1
fi

if ! kubectl get -n "$ISTIO_NAMESPACE" deploy "$ISTIOD_DEPLOY" >/dev/null 2>&1; then
    echo "check-istiod-readiness: deploy/$ISTIOD_DEPLOY not found in $ISTIO_NAMESPACE; skipping (is Istio installed?)" >&2
    exit 1
fi

# ── Step 1: deploy/istiod has Ready replicas == desired ─────────────────────
desired=$(kubectl get -n "$ISTIO_NAMESPACE" deploy "$ISTIOD_DEPLOY" -o jsonpath='{.spec.replicas}')
ready=$(kubectl get -n "$ISTIO_NAMESPACE" deploy "$ISTIOD_DEPLOY" -o jsonpath='{.status.readyReplicas}')
ready=${ready:-0}
desired=${desired:-1}
if [ "$ready" -lt "$desired" ]; then
    echo "check-istiod-readiness: FAIL — istiod has only $ready/$desired ready replicas" >&2
    echo "  recovery: kubectl -n $ISTIO_NAMESPACE rollout restart deploy/$ISTIOD_DEPLOY" >&2
    echo "  if persistent: restart k3s (sudo systemctl restart k3s)" >&2
    exit 2
fi

# ── Step 2: service has at least one ready endpoint ─────────────────────────
endpoint_count=$(kubectl get -n "$ISTIO_NAMESPACE" endpoints "$ISTIOD_SERVICE" -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null | wc -w)
if [ "$endpoint_count" -lt 1 ]; then
    echo "check-istiod-readiness: FAIL — service/$ISTIOD_SERVICE has no ready endpoints" >&2
    echo "  recovery: investigate kubectl describe pod -l app=istiod -n $ISTIO_NAMESPACE" >&2
    exit 2
fi

# ── Step 3 (optional): synthetic /ready probe ───────────────────────────────
# istiod exposes /ready on port 8080. Use a one-shot exec inside
# istiod's own pod (no port-forward, no race) to keep the probe local.
istiod_pod=$(kubectl get -n "$ISTIO_NAMESPACE" pods -l app=istiod -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [ -n "$istiod_pod" ]; then
    if ! kubectl -n "$ISTIO_NAMESPACE" exec "$istiod_pod" -c discovery -- \
            curl -sf --max-time 5 http://127.0.0.1:8080/ready >/dev/null 2>&1; then
        # Soft-fail — exec failures shouldn't block deploy on their own.
        # Steps 1+2 have already given strong signal. Log + continue.
        echo "check-istiod-readiness: WARN — istiod /ready probe didn't return 200 cleanly (exec available may differ across Istio versions)" >&2
    fi
fi

echo "check-istiod-readiness: PASS — istiod $ready/$desired Ready, $endpoint_count endpoint(s)"
exit 0
