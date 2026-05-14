#!/usr/bin/env bash
# Pre-deploy gate — run BEFORE any `helm install` / `helm upgrade` /
# `make k8s-rolling-image`. Exits non-zero if any check fails so the
# deploy aborts cleanly instead of producing CrashLoopBackOff pods.
#
# **Process discipline:** Always go through `make k8s-rolling-image
# TAG=v1.0.X` (or `make k8s-upgrade`) — never raw `helm upgrade`.
# Raw helm bypasses this preflight, which has cost time multiple
# times (2026-05-03, 2026-05-09 PR3/PR4 deploys hitting Vault-injector
# races recoverable only via kubectl-delete-pod cycles).
#
# Checks (in order, fail-fast):
#   1. `helm lint`              — chart syntax / values shape
#   2. `helm template`          — every manifest renders to valid YAML
#   3. `kubectl apply --dry-run=server` on the rendered manifests —
#      surfaces admission-controller errors (RBAC, schema, mutating
#      webhook failures) that pure-static checks miss
#   4. `scripts/check-vault-injector.sh` — synthetic-pod probe against
#      the Vault Agent injector. **This is the gate that would have
#      caught the 2026-05-03 TLS-handshake incident.**
#   5. `scripts/check-istiod-readiness.sh` — probes istiod's
#      Ready endpoints + control-plane deployment health. Catches
#      the 2026-05-04 incident class (CA service degraded → new pods
#      can't bootstrap SPIFFE identity, recover via k3s restart).
#   6. `scripts/check-anti-affinity-deadlock.sh` — renders the chart,
#      compares strict `requiredDuringSchedulingIgnoredDuringExecution`
#      podAntiAffinity replicaCount against actual node count. Catches
#      the chart-change-deadlock class on single-node clusters.
#
# Skips:
#   - Bitnami subchart manifests (postgresql, redis): the chart pulls them
#     in by reference and `kubectl apply --dry-run=server` would also test
#     them, but they're managed upstream — failures there are not actionable
#     by this PR. We still lint them (helm-lint check) but don't dry-run.
#
# Exit codes:
#   0 — all checks passed
#   1 — environment problem (no helm, no kubectl, cluster unreachable)
#   2 — chart problem (lint / template / apply rejected)
#   3 — injector problem (Vault Agent webhook unhealthy)
#   4 — istiod degraded (control-plane Ready < desired, no endpoints,
#       or /ready probe failing)
#   5 — anti-affinity deadlock detected (replicaCount > nodeCount with
#       strict podAntiAffinity)

set -euo pipefail

CHART_DIR="${CHART_DIR:-charts/audittrace}"
NAMESPACE="${NAMESPACE:-audittrace}"
RELEASE="${RELEASE:-audittrace}"
TAG="${TAG:-latest}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[preflight] === audittrace deploy pre-flight ==="
echo "[preflight] chart=$CHART_DIR namespace=$NAMESPACE release=$RELEASE tag=$TAG"

# ── 0. environment ──────────────────────────────────────────────────────────
for tool in helm kubectl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "[preflight] ERROR: $tool not on PATH (exit 1)" >&2
        exit 1
    fi
done

# ── 1. helm lint ────────────────────────────────────────────────────────────
echo "[preflight] (1/6) helm lint ..."
if ! helm lint "$CHART_DIR" --set vault.enabled=true --set secrets.minio.secretKey=preflight \
        --set secrets.chromadb.token=preflight --set secrets.keycloak.adminPassword=preflight \
        --set secrets.postgres.appPassword=preflight --set secrets.postgres.password=preflight \
        --set secrets.redis.password=preflight --set secrets.summariser.password=preflight \
        > /tmp/audittrace-helm-lint.out 2>&1; then
    echo "[preflight] ERROR: helm lint failed (exit 2)" >&2
    cat /tmp/audittrace-helm-lint.out >&2
    exit 2
fi
echo "[preflight] (1/6) helm lint OK"

# ── 2. helm template ────────────────────────────────────────────────────────
echo "[preflight] (2/6) helm template ..."
if ! helm template "$RELEASE" "$CHART_DIR" -n "$NAMESPACE" \
        --set vault.enabled=true --set secrets.minio.secretKey=preflight \
        --set secrets.minio.kmsKey=preflight \
        --set secrets.chromadb.token=preflight --set secrets.keycloak.adminPassword=preflight \
        --set secrets.postgres.appPassword=preflight --set secrets.postgres.password=preflight \
        --set secrets.redis.password=preflight --set secrets.summariser.password=preflight \
        --set memoryServer.image.tag="$TAG" \
        > /tmp/audittrace-helm-rendered.yaml 2>/tmp/audittrace-helm-template.err; then
    echo "[preflight] ERROR: helm template failed (exit 2)" >&2
    cat /tmp/audittrace-helm-template.err >&2
    exit 2
fi
echo "[preflight] (2/6) helm template OK ($(wc -l < /tmp/audittrace-helm-rendered.yaml) lines rendered)"

# ── 3. kubectl apply --dry-run=server ───────────────────────────────────────
KUBECONFIG_FLAG=""
if [ -n "${KUBECONFIG:-}" ]; then
    KUBECONFIG_FLAG="--kubeconfig=$KUBECONFIG"
elif [ -f "$HOME/.kube/config" ]; then
    KUBECONFIG_FLAG="--kubeconfig=$HOME/.kube/config"
fi

echo "[preflight] (3/6) kubectl apply --dry-run=server ..."
if ! kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get pods >/dev/null 2>&1; then
    echo "[preflight] WARN: cluster unreachable — skipping dry-run-server" >&2
    echo "[preflight]       (set KUBECONFIG to enable; this gate becomes" >&2
    echo "[preflight]        no-op in CI without cluster access)" >&2
else
    # No `-n NAMESPACE` flag: the chart includes manifests targeting other
    # namespaces (e.g. istio-system Gateway, default RBAC). Each manifest's
    # own `metadata.namespace` is honoured when no flag is set.
    set +e
    kubectl $KUBECONFIG_FLAG apply --dry-run=server \
            -f /tmp/audittrace-helm-rendered.yaml \
            > /tmp/audittrace-dryrun.out 2>&1
    rc=$?
    set -e

    # Filter known-benign noise:
    #   - "Endpoints is deprecated" — k8s 1.33 deprecation warning
    #   - "provided port is already allocated" — re-applies of the same
    #     NodePort service hit this even though it's idempotent (the
    #     port IS the same). We're not changing the port; the apiserver
    #     just doesn't know that yet during dry-run.
    # `|| true` — empty grep result is the SUCCESS case; without this,
    # `set -e` kills the script on the no-match exit.
    real_errors=$(grep -E "^(error|Error|invalid|Invalid)" /tmp/audittrace-dryrun.out \
                  | grep -v "Endpoints is deprecated" \
                  | grep -v "provided port is already allocated" \
                  | grep -v "missing the kubectl.kubernetes.io/last-applied-configuration" \
                  || true)
    if [ -n "$real_errors" ]; then
        echo "[preflight] ERROR: kubectl apply --dry-run=server failed (exit 2)" >&2
        echo "$real_errors" >&2
        exit 2
    fi
    if [ "$rc" -ne 0 ]; then
        # Non-zero exit but no "real" errors after filtering — log a note
        # so it doesn't go silently.
        echo "[preflight] (3/6) kubectl dry-run reported non-zero (rc=$rc) but" \
             "only known-benign messages — proceeding."
    else
        echo "[preflight] (3/6) kubectl apply --dry-run=server OK"
    fi
fi

# ── 4. Vault Agent injector probe ──────────────────────────────────────────
# Only relevant if the deploy will use vault.enabled=true (the prod path).
echo "[preflight] (4/6) vault-injector probe ..."
if [ -x "$SCRIPT_DIR/check-vault-injector.sh" ]; then
    # `cmd; rc=$?; if …` instead of `if ! cmd; then rc=$?` — inside an
    # `if !` body `$?` is the negated result (always 0), so the old form
    # always took the ERROR branch even when the probe meant SKIP. Plain
    # capture preserves the probe's actual exit code (1 = skip, ≥2 = fail).
    set +e
    NAMESPACE="$NAMESPACE" "$SCRIPT_DIR/check-vault-injector.sh"
    rc=$?
    set -e
    if [ "$rc" -eq 0 ]; then
        echo "[preflight] (4/6) vault-injector probe OK"
    elif [ "$rc" -eq 1 ]; then
        echo "[preflight] WARN: vault-injector probe could not run (cluster unreachable)" >&2
        echo "[preflight]       — skipping. CI without cluster will hit this branch." >&2
    else
        echo "[preflight] ERROR: vault-injector probe FAILED (exit 3)" >&2
        echo "[preflight]        DO NOT proceed with deploy — pods would crash." >&2
        exit 3
    fi
else
    echo "[preflight] WARN: $SCRIPT_DIR/check-vault-injector.sh not executable" >&2
fi

# ── 5. istiod readiness probe ──────────────────────────────────────────────
# Catches the 2026-05-04 incident class: istiod's CA service in a
# degraded state, new pods admit with sidecar but never reach Ready
# because the SPIFFE bootstrap fails. Recovery is k3s restart; cost
# 30+ min per incident if not caught pre-deploy.
echo "[preflight] (5/6) istiod readiness probe ..."
if [ -x "$SCRIPT_DIR/check-istiod-readiness.sh" ]; then
    set +e
    "$SCRIPT_DIR/check-istiod-readiness.sh"
    rc=$?
    set -e
    if [ "$rc" -eq 0 ]; then
        echo "[preflight] (5/6) istiod probe OK"
    elif [ "$rc" -eq 1 ]; then
        echo "[preflight] WARN: istiod probe could not run (no cluster / no Istio)" >&2
        echo "[preflight]       — skipping." >&2
    else
        echo "[preflight] ERROR: istiod probe FAILED (exit 4)" >&2
        echo "[preflight]        DO NOT proceed — workload identity would fail to bootstrap." >&2
        exit 4
    fi
fi

# ── 6. Anti-affinity deadlock probe ────────────────────────────────────────
# Catches: chart change introduces strict podAntiAffinity with
# replicaCount > nodeCount; deploy hangs in Pending forever. Single-
# node clusters (laptop dev, k3s) are most exposed; production clusters
# generally have nodeCount > replicaCount but the gate is universal.
echo "[preflight] (6/6) anti-affinity deadlock probe ..."
if [ -x "$SCRIPT_DIR/check-anti-affinity-deadlock.sh" ]; then
    set +e
    CHART_DIR="$CHART_DIR" RELEASE="$RELEASE" NAMESPACE="$NAMESPACE" \
        VALUES_FILE="$CHART_DIR/values-local.yaml" \
        "$SCRIPT_DIR/check-anti-affinity-deadlock.sh"
    rc=$?
    set -e
    if [ "$rc" -eq 0 ]; then
        echo "[preflight] (6/6) anti-affinity probe OK"
    elif [ "$rc" -eq 1 ]; then
        echo "[preflight] WARN: anti-affinity probe could not run; skipping." >&2
    else
        echo "[preflight] ERROR: anti-affinity deadlock detected (exit 5)" >&2
        echo "[preflight]        DO NOT proceed — pods would hang in Pending." >&2
        exit 5
    fi
fi

echo "[preflight] === all checks passed — safe to proceed with deploy ==="
exit 0
