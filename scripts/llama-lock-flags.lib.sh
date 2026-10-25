#!/usr/bin/env bash
# LC-5 / M3-WU-4 -- the isolation locks every llama-server launcher on this
# host sources (llama endpoint isolation locks). ONE definition, so the five
# launchers cannot drift on the security posture the way the summariser
# script and its systemd unit had drifted before this unit.
#
# THE THREE LAUNCHER LOCKS (the fourth lock, consumer wiring, is the
# memory-server's `llama_auth_headers`, see src/audittrace/routes/_llama_client.py):
#   1. --host <cni0 IPv4>   the ONE cluster-facing address the pods reach
#                           (CoreDNS NodeHosts maps host.audittrace.local to
#                           it). Read from the KERNEL (`ip -4 -o addr show
#                           cni0`), polled for a bounded time because cni0
#                           appears about one second AFTER k3s reports up
#                           (measured 2026-10-25), never 0.0.0.0, never
#                           loopback, never the DHCP LAN address. NodeHosts
#                           is a CHECK made by scripts/apply-llama-endpoint-
#                           locks.sh, never a launch dependency (it needs
#                           the k3s API).
#                           Scope, stated honestly: on Linux the bind selects
#                           a destination ADDRESS accepted on any interface,
#                           so every local process and docker-bridge peer can
#                           still reach it. The key (lock 2) is the control
#                           against them; the bind removes the LAN address.
#   2. --api-key-file FILE  the path on argv, never the value (`ps` can read
#                           every argv on this host).
#   3. --no-webui           no UI on a model endpoint.
#
# Config (environment, laptop defaults; a second target is a profile):
#   LLAMA_API_KEY_FILE           default ${SECRETS_DIR:-$HOME/work/audittrace-private/secrets}/llama-api-key.txt
#   LLAMA_BIND_IFACE             default cni0
#   LLAMA_BIND_POLL_MAX_SECONDS  default 120   (integer >= 0)
#   LLAMA_BIND_POLL_STEP_SECONDS default 2     (integer >= 1)
#
# Usage: `source` this file, then splice "${LLAMA_LOCK_FLAGS[@]}" into the
# launcher's exec line. Sourcing exits non-zero (fail closed) when the key
# file or the bind address is missing.

: "${LLAMA_API_KEY_FILE:=${SECRETS_DIR:-$HOME/work/audittrace-private/secrets}/llama-api-key.txt}"
: "${LLAMA_BIND_IFACE:=cni0}"
: "${LLAMA_BIND_POLL_MAX_SECONDS:=120}"
: "${LLAMA_BIND_POLL_STEP_SECONDS:=2}"

llama_iface_ipv4() {
  # First IPv4 address of $1, without the prefix length; empty when absent.
  ip -4 -o addr show "$1" 2>/dev/null | awk '{ sub(/\/.*/, "", $4); print $4; exit }'
}

llama_wait_for_bind_addr() {
  # Poll (bounded) for the cluster-facing address; print it, or return 1.
  local iface="$1" max="$2" step="$3" waited=0 addr
  [[ "$max" =~ ^[0-9]+$ && "$step" =~ ^[1-9][0-9]*$ ]] || {
    echo "error: LLAMA_BIND_POLL_MAX_SECONDS / _STEP_SECONDS must be integers (max >= 0, step >= 1)" >&2
    return 1
  }
  while :; do
    addr="$(llama_iface_ipv4 "$iface")"
    if [[ -n "$addr" ]]; then
      printf '%s\n' "$addr"
      return 0
    fi
    if (( waited >= max )); then
      echo "error: no IPv4 address on $iface after ${waited}s (cni0 appears after k3s; refusing to bind anything else)" >&2
      return 1
    fi
    sleep "$step"
    waited=$(( waited + step ))
  done
}

if [[ ! -f "$LLAMA_API_KEY_FILE" ]]; then
  echo "error: llama API key file not found at $LLAMA_API_KEY_FILE" >&2
  echo "  generate one with: scripts/generate-llama-api-key.sh \"$LLAMA_API_KEY_FILE\"" >&2
  exit 1
fi

LLAMA_BIND_ADDR="$(llama_wait_for_bind_addr "$LLAMA_BIND_IFACE" "$LLAMA_BIND_POLL_MAX_SECONDS" "$LLAMA_BIND_POLL_STEP_SECONDS")" || exit 1
if [[ ! "$LLAMA_BIND_ADDR" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ || "$LLAMA_BIND_ADDR" == 0.* || "$LLAMA_BIND_ADDR" == 127.* ]]; then
  echo "error: refusing to bind '$LLAMA_BIND_ADDR' (must be the non-loopback, non-wildcard IPv4 of $LLAMA_BIND_IFACE)" >&2
  exit 1
fi

# shellcheck disable=SC2034  # consumed by the sourcing launcher's exec line
LLAMA_LOCK_FLAGS=(--host "$LLAMA_BIND_ADDR" --api-key-file "$LLAMA_API_KEY_FILE" --no-webui)
