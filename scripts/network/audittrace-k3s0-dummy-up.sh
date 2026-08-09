#!/usr/bin/env bash
# audittrace-k3s0-dummy-up.sh — SPEC #443, the ClusterIP-path decoupling fix.
#
# Idempotently creates (or leaves alone, if already present) the persistent
# `k3s0` dummy interface with its pinned /32, so the pod->ClusterIP path no
# longer depends on which physical NIC currently holds the default route
# (SPEC #443 s2/s3, the #307-family root cause). Called by
# `audittrace-k3s0-dummy.service` (a `Before=k3s.service` oneshot unit) — see
# that unit's header for why k3s0 must exist before k3s reads
# `/etc/rancher/k3s/config.yaml`'s `flannel-iface: k3s0`.
#
# ── The mirrored #307 precedent ─────────────────────────────────────────────
# flannel.1/cni0 are created EXTERNALLY (by flannel, not NetworkManager) and
# then marked `unmanaged` so NM never adopts and later flushes their routes on
# a connectivity event (runbook 16, `~/work/audittrace-private/runbooks/16-
# networkmanager-flannel-permanent-fix-307.md`). k3s0 follows the SAME shape:
# created here (a plain kernel `ip link`, no NM connection profile involved —
# a virtual NM connection and `unmanaged-devices` for the same name are
# mutually exclusive, see the header of
# `99-audittrace-k3s0-unmanaged.conf.template`), then excluded from NM via
# that same drop-in.
#
# ── Idempotency (SPEC #443 acceptance criterion 5) ──────────────────────────
# Every step below checks current state before acting, so a re-run (boot,
# manual invocation, the NN-k3s-clusterip-guard auto-heal hook) is a clean
# no-op when k3s0 is already correctly configured.
#
# ── Out-of-band install (NOT in the Helm chart, host infra like
#    audittrace-vault-auto-unseal.sh) ────────────────────────────────────────
#   sudo cp scripts/network/audittrace-k3s0-dummy-up.sh /usr/local/sbin/
#   sudo chmod 0755 /usr/local/sbin/audittrace-k3s0-dummy-up.sh
#   sudo cp scripts/network/audittrace-k3s0-dummy.service.template \
#           /etc/systemd/system/audittrace-k3s0-dummy.service
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now audittrace-k3s0-dummy.service
#
# Requires root (creates a kernel network interface). Never invoked by the
# fleet — operator-gated per SPEC #443 s6.

set -euo pipefail

DUMMY_IFACE="${AUDITTRACE_K3S0_IFACE:-k3s0}"
DUMMY_CIDR="${AUDITTRACE_K3S0_CIDR:-10.10.10.1/32}"

log() { printf '%s [k3s0-dummy-up] %s\n' "$(date -Iseconds)" "$*" >&2; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { log "ERROR missing required cmd: $1"; exit 2; }
}

main() {
  require_cmd ip

  if ip link show "$DUMMY_IFACE" >/dev/null 2>&1; then
    log "OK $DUMMY_IFACE already exists"
  else
    log "creating $DUMMY_IFACE (type dummy)"
    ip link add "$DUMMY_IFACE" type dummy
  fi

  if ip -4 addr show dev "$DUMMY_IFACE" | grep -q "inet ${DUMMY_CIDR%/*}/"; then
    log "OK $DUMMY_CIDR already assigned to $DUMMY_IFACE"
  else
    log "assigning $DUMMY_CIDR to $DUMMY_IFACE"
    ip addr add "$DUMMY_CIDR" dev "$DUMMY_IFACE"
  fi

  # Dummy interfaces typically report `state UNKNOWN` (no carrier concept), so
  # check the flags list (`<BROADCAST,NOARP,UP,LOWER_UP>`) for the UP flag
  # rather than the `state` field.
  if ip link show "$DUMMY_IFACE" | grep -qE '<[^>]*\bUP\b[^>]*>'; then
    log "OK $DUMMY_IFACE already up"
  else
    log "bringing $DUMMY_IFACE up"
    ip link set "$DUMMY_IFACE" up
  fi

  log "done — $DUMMY_IFACE up with $DUMMY_CIDR"
}

main "$@"
