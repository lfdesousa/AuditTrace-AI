#!/usr/bin/env bash
# Vault Agent injector readiness probe — fail fast BEFORE deploy.
#
# Background: 2026-05-03 incident — the Vault Agent injector's auto-tls CA
# bundle drifted out of sync with the MutatingWebhookConfiguration's
# caBundle field, causing TLS handshake failures during pod admission.
# Pods got admitted WITHOUT the vault-agent-init / vault-agent sidecars,
# then crashed at runtime with "/vault/secrets/env: No such file".
#
# This script catches that pre-deploy by submitting a synthetic pod with
# Vault annotations through `kubectl apply --dry-run=server -o yaml` and
# asserting the rendered response contains the injected sidecar
# containers. dry-run=server still routes through admission webhooks, so
# a broken injector surfaces here even though no real pod is created.
#
# Exit codes:
#   0 — injector healthy: vault-agent-init + vault-agent sidecars present
#       in the mutated pod spec
#   1 — kubectl unavailable / cluster unreachable / probe could not run
#       (use to make caller decide whether to abort or proceed)
#   2 — injector unhealthy: dry-run admitted the pod WITHOUT injecting the
#       sidecar — a live deploy would produce a CrashLoopBackOff pod

set -euo pipefail

NAMESPACE="${NAMESPACE:-audittrace}"
ROLE="${ROLE:-audittrace-server}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-memory-server}"
SECRET_PATH="${SECRET_PATH:-kv/data/audittrace/postgres/app}"
PROBE_NAME="audittrace-vault-injector-probe-$(date +%s)"
KUBECONFIG_FLAG=""
if [ -n "${KUBECONFIG:-}" ]; then
    KUBECONFIG_FLAG="--kubeconfig=$KUBECONFIG"
elif [ -f "$HOME/.kube/config" ]; then
    KUBECONFIG_FLAG="--kubeconfig=$HOME/.kube/config"
fi

if ! command -v kubectl >/dev/null 2>&1; then
    echo "[check-vault-injector] kubectl not on PATH — skipping probe (exit 1)" >&2
    exit 1
fi

# Quick reachability check — distinguishes "cluster unreachable" from
# "injector broken". Both should abort a deploy, but for different reasons.
if ! kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get pods -o name >/dev/null 2>&1; then
    echo "[check-vault-injector] cannot list pods in namespace '$NAMESPACE'" >&2
    echo "[check-vault-injector]   (kubeconfig wrong, cluster down, or no permission)" >&2
    echo "[check-vault-injector] aborting probe with exit 1" >&2
    exit 1
fi

# Render a synthetic pod manifest with the same Vault annotations the
# real memory-server pod uses. dry-run=server submits it through the
# mutating-admission chain so the injector webhook fires.
manifest=$(cat <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $PROBE_NAME
  namespace: $NAMESPACE
  annotations:
    vault.hashicorp.com/agent-inject: "true"
    vault.hashicorp.com/role: "$ROLE"
    vault.hashicorp.com/agent-inject-secret-env: "$SECRET_PATH"
    vault.hashicorp.com/agent-inject-template-env: |
      {{ with secret "$SECRET_PATH" }}export FOO='{{ .Data.data.password }}'{{ end }}
spec:
  serviceAccountName: $SERVICE_ACCOUNT
  restartPolicy: Never
  containers:
    - name: probe
      image: busybox:1.36
      command: ["true"]
EOF
)

mutated=$(echo "$manifest" \
    | kubectl $KUBECONFIG_FLAG apply --dry-run=server -f - -o yaml 2>&1) \
    || {
        # dry-run apply itself failed — print the error and bail.
        echo "[check-vault-injector] dry-run apply failed:" >&2
        echo "$mutated" | sed 's/^/  /' >&2
        echo "[check-vault-injector]" >&2
        echo "[check-vault-injector] This usually means the Vault Agent" >&2
        echo "[check-vault-injector] injector webhook itself is failing the" >&2
        echo "[check-vault-injector] admission call. Check injector logs:" >&2
        echo "[check-vault-injector]   kubectl logs -n $NAMESPACE \\" >&2
        echo "[check-vault-injector]     -l app.kubernetes.io/name=vault-agent-injector" >&2
        exit 2
    }

# The injector adds an init container named "vault-agent-init" and a
# sidecar named "vault-agent". If both appear in the mutated spec the
# injector is healthy.
if echo "$mutated" | grep -q "name: vault-agent-init" \
   && echo "$mutated" | grep -q "name: vault-agent"; then
    echo "[check-vault-injector] OK — injector mutated synthetic pod with both"
    echo "[check-vault-injector]      vault-agent-init + vault-agent containers."
    exit 0
fi

echo "[check-vault-injector] FAIL — synthetic pod admitted WITHOUT Vault" >&2
echo "[check-vault-injector]        sidecar injection. A real deploy would" >&2
echo "[check-vault-injector]        produce CrashLoopBackOff pods." >&2
echo "[check-vault-injector]" >&2
echo "[check-vault-injector] Mutated containers found:" >&2
echo "$mutated" | grep -E "^\s+- name:|^\s+name:" | sed 's/^/  /' >&2
echo "[check-vault-injector]" >&2
echo "[check-vault-injector] Diagnose injector:" >&2
echo "[check-vault-injector]   kubectl logs -n $NAMESPACE \\" >&2
echo "[check-vault-injector]     -l app.kubernetes.io/name=vault-agent-injector \\" >&2
echo "[check-vault-injector]     | grep -i 'tls handshake error'" >&2
echo "[check-vault-injector]" >&2
echo "[check-vault-injector] Common fix (auto-tls CA drift):" >&2
echo "[check-vault-injector]   kubectl rollout restart deploy/audittrace-vault-agent-injector \\" >&2
echo "[check-vault-injector]     -n $NAMESPACE" >&2
echo "[check-vault-injector]   then retry this probe." >&2
exit 2
