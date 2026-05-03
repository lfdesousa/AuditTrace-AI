#!/usr/bin/env bash
# Container entrypoint — handles Vault Agent secret sourcing, Alembic
# migrations, and uvicorn startup.
#
# Exit codes:
#   0  — success
#   79 — Vault Agent prerequisite missing (chart says VAULT_AGENT_REQUIRED=true
#        but /vault/secrets/env was not injected). This is a deploy-time
#        infrastructure issue, NOT an app bug. See diagnostic message below.
#   *  — propagated from alembic / uvicorn
set -euo pipefail

if [ "${VAULT_AGENT_REQUIRED:-false}" = "true" ]; then
    if [ ! -f /vault/secrets/env ]; then
        cat >&2 <<'EOF'
=============================================================================
audittrace-ai: VAULT AGENT PREREQUISITE FAILURE (exit 79)
=============================================================================
VAULT_AGENT_REQUIRED=true but /vault/secrets/env is missing.

The Vault Agent injector did NOT add the vault-agent-init / vault-agent
sidecar containers to this pod, so no secret file was rendered.

Likely causes (most common first):
  1. Vault Agent injector webhook TLS handshake failed during this pod's
     admission. Check the injector logs around the pod's create timestamp:
         kubectl logs -n audittrace -l app.kubernetes.io/name=vault-agent-injector \
           | grep -i "tls handshake error"
     The injector's auto-tls CA can drift out of sync with the
     MutatingWebhookConfiguration's caBundle, causing transient failures.
  2. Pod admitted before the injector was Ready (cold-start race).
  3. Vault annotations missing/wrong on the pod template.

Recovery for case 1 (most common):
  - Delete this pod; the deployment recreates it through a now-healthy
    injector:
        kubectl delete pod $HOSTNAME -n audittrace
  - If TLS errors keep recurring, restart the injector itself:
        kubectl rollout restart deploy/audittrace-vault-agent-injector \
          -n audittrace

The deploy-preflight gate (scripts/deploy-preflight.sh) is supposed to
catch a broken injector BEFORE the rollout starts. If you reached this
message during a `make k8s-rolling-image` run, the gate either was not
executed or has a bug — file an issue.
=============================================================================
EOF
        exit 79
    fi
    echo "[entrypoint] Loading Vault-injected env from /vault/secrets/env"
    set -a
    # shellcheck disable=SC1091  # runtime-rendered file by Vault Agent
    . /vault/secrets/env
    set +a
fi

echo "[entrypoint] Running database migrations..."
python -m alembic upgrade head

echo "[entrypoint] Starting audittrace-ai..."
exec uvicorn audittrace.server:app \
    --host "${AUDITTRACE_HOST:-0.0.0.0}" \
    --port "${AUDITTRACE_PORT:-8765}" \
    --workers "${AUDITTRACE_WORKERS:-1}"
