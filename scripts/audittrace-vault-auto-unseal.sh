#!/usr/bin/env bash
# Idempotent Vault auto-unseal — invoked at boot by the systemd unit
# `audittrace-vault-auto-unseal.service` so the operator never has to
# paste unseal keys manually after a reboot.
#
# Why this exists: ADR-043 §7 documented manual unseal as the POC
# posture. In practice, every laptop reboot meant 3-of-5 manual paste
# operations and a chunk of cluster downtime while operator catches up.
# This script trades that friction for "keys live in a mode-600 home
# directory file" — a strictly equivalent security boundary (laptop
# disk) with substantially less friction.
#
# Threat model: anyone with root on this laptop, or who steals its
# disk, can read the keys file and unseal Vault. This was already true
# for the manual-unseal flow (keys had to live somewhere on the laptop
# to be paste-able). Auto-unseal does not weaken anything; it just
# removes the operator from the loop.
#
# Production successor: cert-manager + cloud KMS auto-unseal — M3+
# scope. Until then, this is the documented posture.
#
# Usage:
#   scripts/audittrace-vault-auto-unseal.sh
#   scripts/audittrace-vault-auto-unseal.sh /path/to/vault-init.json
#   VAULT_INIT_FILE=/path/to/init.json scripts/audittrace-vault-auto-unseal.sh

set -euo pipefail

# ────────────────────────────── config ──────────────────────────────

KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
export KUBECONFIG

VAULT_INIT_FILE="${1:-${VAULT_INIT_FILE:-$HOME/work/audittrace-private/runbooks/vault-init-2026-05-01.json}}"
NS="${AUDITTRACE_NAMESPACE:-audittrace}"
POD="${VAULT_POD:-audittrace-vault-0}"

WAIT_TOTAL_S=180
WAIT_POLL_S=3

# ────────────────────────────── helpers ─────────────────────────────

log() { printf '%s [%s] %s\n' "$(date -Iseconds)" "$1" "$2" >&2; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { log ERROR "missing required cmd: $1"; exit 2; }
}

# `vault status` exit codes:
#   0 = unsealed
#   1 = communication error (HTTP listener not up yet)
#   2 = sealed OR not initialised
# Both exit 1 and exit 2 are normal transient states for this script,
# so we tolerate non-zero exit and inspect stdout. Doing this in a
# helper keeps `set -o pipefail` from poisoning callers — the previous
# version of this script grep'd vault-status output through a pipe,
# and a sealed Vault's exit 2 made `if ! ... | grep -q true` wrongly
# report failure even when the grep matched.
vault_status() {
  kubectl -n "$NS" exec "$POD" -c vault -- vault status 2>/dev/null || true
}

# ────────────────────────────── pre-flight ──────────────────────────

require_cmd kubectl
require_cmd python3

if [[ ! -f "$VAULT_INIT_FILE" ]]; then
  log ERROR "vault-init file not found: $VAULT_INIT_FILE"
  log ERROR "set VAULT_INIT_FILE env var or pass the path as the first argument"
  exit 3
fi

if [[ ! -r "$VAULT_INIT_FILE" ]]; then
  log ERROR "vault-init file not readable: $VAULT_INIT_FILE (mode mismatch?)"
  exit 4
fi

log INFO "kubeconfig=$KUBECONFIG namespace=$NS pod=$POD init_file=$VAULT_INIT_FILE"

# ────────────────────────────── readiness wait ─────────────────────
#
# `containerStatuses.state.running` flips to non-empty the instant the
# container's PID exists, but Vault's HTTP listener takes several more
# seconds to bind and read its file storage. The only reliable
# readiness signal is `vault status` returning a parseable response.

log INFO "waiting for vault to respond to status (max ${WAIT_TOTAL_S}s)..."

elapsed=0
status=""
while [[ $elapsed -lt $WAIT_TOTAL_S ]]; do
  status=$(vault_status)
  if grep -qE '^Initialized[[:space:]]+(true|false)' <<<"$status"; then
    break
  fi
  sleep "$WAIT_POLL_S"
  elapsed=$((elapsed + WAIT_POLL_S))
done

if ! grep -qE '^Initialized[[:space:]]+(true|false)' <<<"$status"; then
  log ERROR "vault did not respond to status within ${WAIT_TOTAL_S}s"
  kubectl -n "$NS" get pod "$POD" 2>&1 || true
  exit 5
fi

# ────────────────────────────── idempotency check ──────────────────

if grep -qE '^Sealed[[:space:]]+false' <<<"$status"; then
  log INFO "vault already unsealed — exiting 0 (idempotent no-op)"
  exit 0
fi

if ! grep -qE '^Initialized[[:space:]]+true' <<<"$status"; then
  log ERROR "vault is not initialised — run 'vault operator init' manually first"
  exit 6
fi

# ────────────────────────────── unseal ─────────────────────────────

log INFO "vault is sealed — unsealing with keys from $VAULT_INIT_FILE"

for i in 0 1 2; do
  KEY=$(python3 -c "
import json, sys
d = json.load(open('$VAULT_INIT_FILE'))
keys = d.get('unseal_keys_b64') or []
if len(keys) <= $i:
    print('VAULT_INIT_FILE missing unseal_keys_b64[$i]', file=sys.stderr)
    sys.exit(7)
print(keys[$i])
")

  if ! kubectl -n "$NS" exec -i "$POD" -c vault -- \
         vault operator unseal "$KEY" >/dev/null 2>&1; then
    log ERROR "unseal step $((i+1))/3 failed"
    exit 8
  fi
  log INFO "unseal $((i+1))/3 ok"
  unset KEY
done

# ────────────────────────────── verify ─────────────────────────────

status=$(vault_status)
if grep -qE '^Sealed[[:space:]]+false' <<<"$status"; then
  log INFO "vault unsealed — done"
  exit 0
else
  log ERROR "post-unseal status check still reports Sealed=true; investigate"
  printf '%s\n' "$status" >&2
  exit 9
fi
