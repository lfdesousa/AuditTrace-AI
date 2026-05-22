#!/usr/bin/env bash
# audittrace-pod-reaper.sh — boot-time terminal-pod reaper for pcLuisLinux.
#
# Purpose: clean up pods stuck in `Failed` / `ContainerStatusUnknown` after
# a host reboot. k3s + cri-o on a single-node cluster lose track of running
# containers across the power cycle: containers terminate ungracefully, the
# new kubelet sees a state mismatch and parks them. Without intervention,
# 1–2 zombies accumulate per reboot. With daily reboots (laptop), that's
# 5–7 per week. They take node memory / file handles / PIDs without doing
# work.
#
# Root cause investigated 2026-05-22 during the ADR-006 v1.3.0 / v0.1.0
# deploy prep; evidence at:
#   ~/work/audittrace-evidence/20260522-adr-006-deploy/11-zombie-root-cause-investigation.txt
#
# Lifecycle: oneshot, runs at every boot after k3s is up. Idempotent —
# safe to re-run; no zombies → exit 0 with no work done.
#
# Scope: `audittrace` namespace only. We do NOT touch system namespaces
# (kube-system, istio-system, vault-injector) because their pod lifecycle
# is managed by their own operators and surfacing real failures via
# `kubectl get pods` is part of the debugging contract.

set -euo pipefail

readonly NAMESPACE="audittrace"
readonly KUBECTL="${KUBECTL:-/usr/local/bin/kubectl}"
readonly KUBECONFIG_FILE="${KUBECONFIG:-/home/lfdesousa/.kube/config}"

log() {
  # Prefix every line so journalctl rows are easy to grep.
  echo "[$(date -Iseconds)] [pod-reaper] $*"
}

wait_for_k3s() {
  log "Waiting for k3s API to be ready (up to 240s)…"
  local i
  for i in $(seq 1 120); do
    if "$KUBECTL" --kubeconfig="$KUBECONFIG_FILE" version >/dev/null 2>&1; then
      log "k3s API responding after ${i}×2s probes."
      return 0
    fi
    sleep 2
  done
  log "ERROR: k3s API did not become ready within 240s."
  return 1
}

reap_terminal_pods() {
  log "Listing terminal pods in namespace ${NAMESPACE}…"

  # `--field-selector=status.phase=Failed` catches:
  #   - genuinely failed pods (container exited non-zero, NotReady too long)
  #   - ContainerStatusUnknown (kubelet lost track; phase reports as Failed)
  # Does NOT match Running, Pending, or Succeeded. Succeeded Job pods are
  # intentionally preserved for debugging; the hook templates handle their
  # own cleanup via `helm.sh/hook-delete-policy`.
  local failed
  failed=$("$KUBECTL" --kubeconfig="$KUBECONFIG_FILE" \
    -n "$NAMESPACE" get pods \
    --field-selector=status.phase=Failed \
    -o name 2>/dev/null || true)

  if [ -z "$failed" ]; then
    log "No terminal pods found — exiting clean."
    return 0
  fi

  local count
  count=$(printf '%s\n' "$failed" | wc -l)
  log "Found ${count} terminal pod(s). Reaping…"

  printf '%s\n' "$failed" | while read -r pod; do
    [ -z "$pod" ] && continue
    log "  deleting $pod"
    # --force --grace-period=0: terminal pods don't have running containers
    # to gracefully shut down, so the standard 30s grace period is wasted
    # time. Without --force, deletes hang until the kubelet acknowledges
    # the termination, which it never does for ContainerStatusUnknown
    # pods.
    "$KUBECTL" --kubeconfig="$KUBECONFIG_FILE" \
      -n "$NAMESPACE" delete "$pod" \
      --force --grace-period=0 >/dev/null 2>&1 \
      || log "    (delete failed for $pod — already gone? continuing)"
  done

  log "Reap complete."
}

main() {
  log "Boot-time pod reaper starting on $(hostname)."
  wait_for_k3s
  # Grace period: give kubelet time to reconcile the cri-o state with its
  # internal pod records before we make irreversible decisions. 60s is
  # comfortable on this single-node cluster — k3s is generally Ready
  # within 30–45s of boot and the kubelet reconciles within another 15s.
  log "Grace period (60s) for kubelet reconciliation…"
  sleep 60
  reap_terminal_pods
  log "Done."
}

main "$@"
