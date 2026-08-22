#!/usr/bin/env bash
# audittrace-mesh-resettle.sh — the ROOT executor for the #384 WS5 privileged
# mesh healer (Q3 = Option B, systemd path-unit trigger).
#
# ── What this is ────────────────────────────────────────────────────────────
# This is the ONLY component in the WS5 heal path that holds root. It is fired
# by `audittrace-mesh-healer.path` when the deploy runner writes a request file
# into $MESH_HEAL_DIR/requests/. The runner itself holds ZERO standing
# privilege — it can only write a file; the actual host-root re-settle happens
# here, behind a root-owned systemd unit whose every firing is a journald audit
# record (`journalctl -u audittrace-mesh-healer.service`).
#
# ── The security boundary: a hardcoded whitelist ────────────────────────────
# A request carries a `Finding.signal`. This script performs a bounded op ONLY
# for a signal in the hardcoded WHITELIST below (`resettle_cmd_for`); anything
# else is REFUSED and recorded, and NOTHING is executed. There is deliberately
# no general "restart k3s on demand" lever — the signal must be one this healer
# is allowed to act on. The whitelist mirrors `HOST_RESETTLE_WHITELIST` in
# scripts/deploy/mesh.py, but this copy is the real boundary and is validated
# independently (defence in depth). Keep the two in sync.
#
# ── The bounded op (the #307 fix) ───────────────────────────────────────────
# Each whitelisted signal maps to exactly ONE bounded operation: `systemctl
# restart k3s`. On this single-node k3s laptop that rebuilds the host
# ClusterIP DNAT / iptables routes for the kube-API ClusterIP (the concrete
# #307 remediation: istiod regains "route to host" and can sign CSRs again) and
# resettles a wedged kubelet back to Ready. A narrower iptables-only re-settle
# is a possible future refinement; k3s restart is the proven, bounded action.
#
# ── Out-of-band install (NOT in the Helm chart) ─────────────────────────────
# This script + its two units are HOST infra, like audittrace-vault-auto-unseal.
# They are installed out-of-band on the laptop, NOT shipped in charts/. Install:
#
#   sudo install -d -o "$(id -un)" -g "$(id -gn)" /var/lib/audittrace/mesh-heal
#   sudo install -d -o "$(id -un)" -g "$(id -gn)" /var/lib/audittrace/mesh-heal/requests
#   sudo install -d -o "$(id -un)" -g "$(id -gn)" /var/lib/audittrace/mesh-heal/results
#   sudo cp scripts/audittrace-mesh-healer.{path,service} /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now audittrace-mesh-healer.path
#
# The top-level mesh-heal/ dir + the requests/ + results/ dirs under it are
# ALL chown'd to the invoking runner's own user/group (portability invariant
# — never hardcode a username) so the unprivileged runner can write requests
# (including its atomic-rename tmp file, written to the TOP-LEVEL dir — see
# `PrivilegedHealer._publish_request` in scripts/deploy/mesh.py) and read
# results; root writes results. The top-level `install -d` must run BEFORE
# the two subdirs (parent first) so a fresh install never leaves it
# root-owned.
#
# ── #411 v2 / MESH-HEAL-DIR: the DURABLE guarantee (survives reboots) ───────
# The `install -d` above only sets ownership ONCE, at install time. #411 was
# exactly this ownership NOT being durable — a reboot / manual `mkdir` / a
# drifted install silently re-creates a dir root-owned, and the runner can no
# longer write into it. `scripts/audittrace-mesh-heal.tmpfiles.conf`
# is the durable fix: a systemd-tmpfiles(5) rule that recreates ALL THREE
# dirs — the top-level mesh-heal/ parent (MESH-HEAL-DIR, 2026-08-22, closing
# the gap where the parent was runner-owned only at install time and a
# tmpfiles-driven recreation put it back root-owned, PermissionError'ing the
# runner's atomic-rename tmp write) plus requests/ and results/ — all
# runner-owned + mode 0775, on EVERY BOOT. Render it with the invoking
# runner's user/group substituted (its shipped copy carries the
# @RUNNER_USER@ / @RUNNER_GROUP@ placeholders — portability invariant, never
# a hardcoded username) and install it alongside the two units:
#
#   sed -e "s/@RUNNER_USER@/$(id -un)/" -e "s/@RUNNER_GROUP@/$(id -gn)/" \
#     scripts/audittrace-mesh-heal.tmpfiles.conf \
#     | sudo tee /etc/tmpfiles.d/audittrace-mesh-heal.conf >/dev/null
#   sudo systemd-tmpfiles --create /etc/tmpfiles.d/audittrace-mesh-heal.conf
#
# `systemd-tmpfiles --create` both applies it immediately (so the `install -d`
# step above becomes redundant once this is in place, but is kept for a fresh
# install before the tmpfiles rule is ever rendered) AND re-applies it on every
# subsequent boot via `systemd-tmpfiles-setup.service`, which runs before
# `paths.target` — so the watched dir is runner-writable before
# `audittrace-mesh-healer.path` ever re-arms. #411 code-side loud-fail (never
# a false "healed") lives in `scripts/deploy/mesh.py`'s
# `PrivilegedHealer._trigger_host_resettle`.
#
# ── Testing ─────────────────────────────────────────────────────────────────
# Set MESH_RESETTLE_DRY_RUN=1 to validate + select the op and record a result
# WITHOUT executing systemctl. The unit test drives the real whitelist logic
# this way (whitelisted signal -> would_run recorded, performed=false;
# non-whitelisted -> refused, nothing selected).

set -euo pipefail

MESH_HEAL_DIR="${MESH_HEAL_DIR:-/var/lib/audittrace/mesh-heal}"
REQ_DIR="$MESH_HEAL_DIR/requests"
RES_DIR="$MESH_HEAL_DIR/results"
DRY_RUN="${MESH_RESETTLE_DRY_RUN:-0}"

log() { printf '%s [mesh-resettle] %s\n' "$(date -Iseconds)" "$*" >&2; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { log "ERROR missing required cmd: $1"; exit 2; }
}

# The security boundary. Echo the single bounded op for a whitelisted signal;
# return non-zero (refuse) for anything else. NOTHING outside this case runs.
resettle_cmd_for() {
  case "$1" in
    node-not-ready | nodes-unreadable | istiod-api-unreachable)
      printf 'systemctl restart k3s'
      ;;
    *)
      return 1
      ;;
  esac
}

# Write a result file the healer polls for. Args: request_id, then key=value
# pairs are passed via environment for python3 to serialise safely (no shell
# interpolation into JSON).
write_result() {
  local request_id="$1" performed="$2" status="$3" detail="$4" would_run="${5:-}"
  local out="$RES_DIR/${request_id}.result.json"
  RES_REQUEST_ID="$request_id" \
  RES_PERFORMED="$performed" \
  RES_STATUS="$status" \
  RES_DETAIL="$detail" \
  RES_WOULD_RUN="$would_run" \
  RES_DRY_RUN="$DRY_RUN" \
  python3 - "$out" <<'PY'
import json, os, sys
out = sys.argv[1]
doc = {
    "request_id": os.environ["RES_REQUEST_ID"],
    "performed": os.environ["RES_PERFORMED"] == "true",
    "status": os.environ["RES_STATUS"],
    "detail": os.environ["RES_DETAIL"],
    "dry_run": os.environ.get("RES_DRY_RUN", "0") == "1",
}
would = os.environ.get("RES_WOULD_RUN", "")
if would:
    doc["would_run"] = would
tmp = out + ".tmp"
with open(tmp, "w") as fh:
    json.dump(doc, fh)
os.replace(tmp, out)
PY
}

# Parse the `signal` field out of a request JSON, safely. Prints nothing and
# returns non-zero if the file is unparseable or the signal is missing.
read_signal() {
  python3 - "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        doc = json.load(fh)
    sig = doc.get("signal")
    if not isinstance(sig, str) or not sig:
        sys.exit(1)
    print(sig)
except Exception:
    sys.exit(1)
PY
}

process_one() {
  local req="$1"
  # request_id is the filename stem — this is what the healer polls a result
  # for, so it must match regardless of the JSON body. basename only (no path).
  local base request_id signal cmd rc
  base="$(basename "$req")"
  request_id="${base%.json}"

  if ! signal="$(read_signal "$req")"; then
    log "REFUSED $base — unreadable request or missing signal"
    write_result "$request_id" "false" "refused" "unreadable request or missing signal"
    rm -f "$req"
    return 0
  fi

  if ! cmd="$(resettle_cmd_for "$signal")"; then
    log "REFUSED $base — signal '$signal' not in whitelist; performing NOTHING"
    write_result "$request_id" "false" "refused" "signal '$signal' not in whitelist"
    rm -f "$req"
    return 0
  fi

  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY-RUN $base — signal '$signal' whitelisted; would run: $cmd"
    write_result "$request_id" "false" "dry-run" "would run bounded op for '$signal'" "$cmd"
    rm -f "$req"
    return 0
  fi

  log "EXECUTING $base — signal '$signal'; bounded op: $cmd"
  # Exactly one bounded op. Do NOT let set -e abort before we record a result.
  rc=0
  $cmd || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    log "OK $base — '$cmd' succeeded for '$signal'"
    write_result "$request_id" "true" "performed" "bounded op '$cmd' ok for '$signal'"
  else
    log "FAILED $base — '$cmd' exited $rc for '$signal'"
    write_result "$request_id" "false" "failed" "bounded op '$cmd' exited $rc for '$signal'"
  fi
  rm -f "$req"
}

main() {
  require_cmd python3

  if [[ ! -d "$REQ_DIR" ]]; then
    log "no request dir ($REQ_DIR) — nothing to do"
    exit 0
  fi
  mkdir -p "$RES_DIR"

  local found=0 req
  for req in "$REQ_DIR"/*.json; do
    [[ -e "$req" ]] || continue
    found=1
    process_one "$req"
  done

  if [[ "$found" -eq 0 ]]; then
    log "no pending requests — idempotent no-op"
  fi
  exit 0
}

main "$@"
