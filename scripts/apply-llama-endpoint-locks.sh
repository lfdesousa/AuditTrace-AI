#!/usr/bin/env bash
# LC-5 / M3-WU-4 -- OPERATOR-RUN: put the host's llama-server units behind the
# isolation locks (bind to the cni0 address, --api-key-file, --no-webui).
#
#   sudo scripts/apply-llama-endpoint-locks.sh --dry-run          # print, change nothing
#   sudo scripts/apply-llama-endpoint-locks.sh --apply            # backup, install, staged restart, verify
#   sudo scripts/apply-llama-endpoint-locks.sh --verify           # read-only checks of the live state
#   sudo scripts/apply-llama-endpoint-locks.sh --rollback <dir>   # restore the backup taken by --apply
#
# Builders and reviewers have no host root: this script is run ONLY by the
# operator, in the announced window (the window runbook names every step).
#
# Units: llama-server (:11435 chat), llama-embed-server (:11436), llama-summarizer
# (:11437) are installed AND restarted. llama-fleet (:11438) and
# llama-mistral-server (:11437) are DORMANT: the definition is installed, they
# are never started or enabled, and their enabled/active state is compared
# before and after.
#
# What --apply does, in order (any failure before step 4 changes nothing):
#   1 key file       ensure ${KEY_FILE}; NEVER overwrites; generated with the
#                    [A-Za-z0-9]{48} recipe (scripts/generate-llama-api-key.sh)
#   2 preflight      parse the RENDERED unit files: every ExecStart/ExecStartPre
#                    path must exist and be executable, and each launcher's
#                    `--check` (binary, model, key file, cni0 address) must pass
#                    as the unit's User=; prints `<unit> -> <binary> -> <version>`
#   3 backup         <backup dir>/<unit>.pre-wu4/ (unit file + drop-ins) for ALL
#                    five units, MANIFEST with sha256, the dir path is printed
#   4 reconcile      an ExecStart-resetting drop-in is RENAMED to
#                    *.disabled-by-wu4-locks, never deleted
#   5 install        the five rendered units (@SCRIPTS_DIR@ -> this checkout)
#   6 restart        daemon-reload, then the three ACTIVE units one at a time
#                    (embed, summariser, chat), each verified before the next
#   7 verify         `systemctl show -p ExecStart` = the launcher; the running
#                    argv has the three locks; the socket listens only on the
#                    cni0 address; dormant units unchanged; CoreDNS NodeHosts
#                    == cni0 (a CHECK, never a launch dependency)
#
# Exit codes: 1 usage/root, 2 preflight refused (nothing changed), 3 restart or
# verification failed (the rollback command is printed), 4 NodeHosts != cni0,
# 5 rollback failed.
#
# Test seams (the operator never sets these): SYSTEMD_DIR, BACKUP_ROOT, PROC_ROOT,
# KUBECTL_BIN, LLAMA_API_KEY_FILE, LLAMA_SERVICE_HOME, LLAMA_APPLY_ALLOW_NONROOT,
# LLAMA_APPLY_FORCE_RUNUSER, LLAMA_APPLY_VERIFY_TIMEOUT / _STEP (seconds).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/scripts"
# shellcheck source=llama-apply-helpers.lib.sh
source "$SCRIPTS_DIR/llama-apply-helpers.lib.sh"

SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/audittrace/llama-locks}"
PROC_ROOT="${PROC_ROOT:-/proc}"
KUBECTL_BIN="${KUBECTL_BIN:-kubectl}"
VERIFY_TIMEOUT="${LLAMA_APPLY_VERIFY_TIMEOUT:-900}"
VERIFY_STEP="${LLAMA_APPLY_VERIFY_STEP:-5}"
BIND_IFACE="${LLAMA_BIND_IFACE:-cni0}"
NODEHOSTS_NAME="${LLAMA_NODEHOSTS_NAME:-host.audittrace.local}"

# unit -> launcher basename, port (the ports are the units' own; no env knob)
declare -A LAUNCHER=(
  [llama-server]=start-chat-llama.sh
  [llama-embed-server]=start-embed-llama.sh
  [llama-summarizer]=start-summarizer-llama.sh
  [llama-fleet]=start-fleet-llama.sh
  [llama-mistral-server]=start-mistral-llama.sh
)
declare -A PORT=(
  [llama-server]=11435 [llama-embed-server]=11436 [llama-summarizer]=11437
  [llama-fleet]=11438 [llama-mistral-server]=11437
)
# Restart order: smallest model first, so a failure surfaces before the 27B reload.
ACTIVE_UNITS=(llama-embed-server llama-summarizer llama-server)
DORMANT_UNITS=(llama-fleet llama-mistral-server)
ALL_UNITS=("${ACTIVE_UNITS[@]}" "${DORMANT_UNITS[@]}")

MODE="" ROLLBACK_DIR="" DRY_RUN=0
case "${1:-}" in
  --dry-run) MODE=apply; DRY_RUN=1 ;;
  --apply) MODE=apply ;;
  --verify) MODE=verify ;;
  --rollback) MODE=rollback; ROLLBACK_DIR="${2:-}" ;;
  *)
    echo "usage: $0 --dry-run | --apply | --verify | --rollback <backup dir>" >&2
    exit 1
    ;;
esac

if [[ "$MODE" != "verify" && "$DRY_RUN" == "0" && "$(id -u)" != "0" && -z "${LLAMA_APPLY_ALLOW_NONROOT:-}" ]]; then
  echo "error: --apply and --rollback must run as root (sudo); --dry-run and --verify do not need it" >&2
  exit 1
fi

run() {
  if [[ "$DRY_RUN" == "1" ]]; then echo "+ $*"; else "$@"; fi
}

# ── the unit user, its home and the key file (all derived, never typed) ──
RENDER_DIR="$(mktemp -d)"
trap 'rm -rf "$RENDER_DIR"' EXIT

SERVICE_USER="" SERVICE_HOME="" KEY_FILE=""
resolve_service_identity() {
  # from the TEMPLATES (the rendered units need KEY_FILE, which needs the home)
  local u
  SERVICE_USER="$(unit_directive "$SCRIPTS_DIR/systemd/llama-server.service" User | head -n1)"
  [[ -n "$SERVICE_USER" ]] || { echo "error: llama-server.service has no User=" >&2; exit 2; }
  for u in "${ALL_UNITS[@]}"; do
    [[ "$(unit_directive "$SCRIPTS_DIR/systemd/$u.service" User | head -n1)" == "$SERVICE_USER" ]] || {
      echo "error: $u runs as a different User= than llama-server; refusing" >&2
      exit 2
    }
  done
  SERVICE_HOME="${LLAMA_SERVICE_HOME:-$(getent passwd "$SERVICE_USER" | cut -d: -f6)}"
  [[ -n "$SERVICE_HOME" ]] || { echo "error: cannot resolve the home of user '$SERVICE_USER'" >&2; exit 2; }
  KEY_FILE="${LLAMA_API_KEY_FILE:-${SECRETS_DIR:-$SERVICE_HOME/work/audittrace-private/secrets}/llama-api-key.txt}"
}

render_all() {
  local unit
  for unit in "${ALL_UNITS[@]}"; do
    render_unit "$SCRIPTS_DIR/systemd/$unit.service" "$RENDER_DIR/$unit.service" "$SCRIPTS_DIR" "$KEY_FILE" || exit 2
  done
}

cni0_addr() {
  ip -4 -o addr show "$BIND_IFACE" 2>/dev/null | awk '{ sub(/\/.*/, "", $4); print $4; exit }'
}

# ── step 1: key file ──
step_key() {
  echo "== 1/7: API key file =="
  if [[ -f "$KEY_FILE" ]]; then
    echo "  already present at $KEY_FILE -- left untouched (never overwritten)"
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "+ (as $SERVICE_USER) $SCRIPTS_DIR/generate-llama-api-key.sh $KEY_FILE"
    return 0
  fi
  echo "  generating a new key at $KEY_FILE (as $SERVICE_USER; value not printed)"
  service_run "$SERVICE_USER" "$SERVICE_HOME" "$SCRIPTS_DIR/generate-llama-api-key.sh" "$KEY_FILE"
  echo "  NOTE: seed Vault kv/audittrace/llm/api-key from this file BEFORE the memory-server rolls onto values-llm-key-on.yaml (window step 1b)."
}

# ── step 2: preflight ──
step_preflight() {
  echo "== 2/7: preflight (parsed from the rendered unit files; nothing is changed) =="
  local failed=0 unit check_key placeholder="" out binary version addr
  addr="$(cni0_addr)"
  [[ -n "$addr" ]] || { echo "error: $BIND_IFACE has no IPv4 address; k3s is not up" >&2; exit 2; }
  echo "  $BIND_IFACE address: $addr"
  for unit in "${ALL_UNITS[@]}"; do
    check_unit_paths "$RENDER_DIR/$unit.service" || failed=1
  done
  check_key="$KEY_FILE"
  if [[ ! -f "$KEY_FILE" ]]; then
    # dry-run only (--apply generated it in step 1): exercise every OTHER check
    placeholder="$RENDER_DIR/placeholder-key"
    : > "$placeholder"
    check_key="$placeholder"
    echo "  (key file absent: launcher checks use an empty placeholder; --apply generates the real key first)"
  fi
  for unit in "${ALL_UNITS[@]}"; do
    if ! out="$(service_run "$SERVICE_USER" "$SERVICE_HOME" \
          "LLAMA_API_KEY_FILE=$check_key" "LLAMA_BIND_IFACE=$BIND_IFACE" \
          "LLAMA_BIND_POLL_MAX_SECONDS=${LLAMA_BIND_POLL_MAX_SECONDS:-10}" \
          "$SCRIPTS_DIR/${LAUNCHER[$unit]}" --check 2>&1)"; then
      echo "error: $unit's launcher --check failed:" >&2
      printf '  %s\n' "$out" >&2
      failed=1
      continue
    fi
    binary="$(sed -n 's/.*binary=\([^ ]*\) .*/\1/p' <<< "$out" | head -n1)"
    version="$(service_run "$SERVICE_USER" "$SERVICE_HOME" timeout 30 "$binary" --version 2>&1 | awk '/^version:/ { print; exit }' || true)"
    echo "  $unit -> $binary -> ${version:-<no version line>}"
  done
  if (( failed )); then
    echo "" >&2
    echo "error: refusing to touch ANY unit. A partial apply would leave some endpoints locked and others not, or crash-loop a healthy service. Nothing was changed." >&2
    exit 2
  fi
  echo "  ok -- every unit's paths, binary, model, key file and $BIND_IFACE address resolve"
}

# ── step 3: backup ──
BACKUP_DIR=""
step_backup() {
  echo "== 3/7: backup (before any change) =="
  BACKUP_DIR="$BACKUP_ROOT/$(date -u +%Y%m%dT%H%M%SZ)"
  echo "  backup dir: $BACKUP_DIR"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "+ copy <$SYSTEMD_DIR>/<unit>.service and <unit>.service.d/* of all five units into $BACKUP_DIR/<unit>.pre-wu4/"
    return 0
  fi
  install -d -m 0700 "$BACKUP_DIR"
  local unit d f
  : > "$BACKUP_DIR/STATE"
  for unit in "${ALL_UNITS[@]}"; do
    d="$BACKUP_DIR/$unit.pre-wu4"
    install -d -m 0700 "$d"
    [[ -f "$SYSTEMD_DIR/$unit.service" ]] && cp -p "$SYSTEMD_DIR/$unit.service" "$d/$unit.service"
    if [[ -d "$SYSTEMD_DIR/$unit.service.d" ]]; then
      install -d -m 0700 "$d/$unit.service.d"
      for f in "$SYSTEMD_DIR/$unit.service.d"/*; do
        [[ -f "$f" ]] && cp -p "$f" "$d/$unit.service.d/"
      done
    fi
    printf '%s enabled=%s active=%s\n' "$unit" \
      "$(systemctl is-enabled "$unit" 2>/dev/null || true)" \
      "$(systemctl is-active "$unit" 2>/dev/null || true)" >> "$BACKUP_DIR/STATE"
  done
  ( cd "$BACKUP_DIR" && find . -type f ! -name MANIFEST -print0 | sort -z | xargs -0 sha256sum > MANIFEST )
  echo "  backed up $(grep -c . "$BACKUP_DIR/MANIFEST") files; STATE and MANIFEST written"
  echo "  ROLLBACK (any time):  sudo $0 --rollback $BACKUP_DIR"
}

# ── steps 4-5: reconcile + install ──
CHANGED=()
unit_is_current() {
  # idempotency: installed file identical to the rendered one AND no drop-in
  # that still resets ExecStart
  local unit="$1" conf
  cmp -s "$RENDER_DIR/$unit.service" "$SYSTEMD_DIR/$unit.service" || return 1
  if [[ -d "$SYSTEMD_DIR/$unit.service.d" ]]; then
    for conf in "$SYSTEMD_DIR/$unit.service.d"/*.conf; do
      [[ -e "$conf" ]] || continue
      grep -q '^ExecStart=[[:space:]]*$' "$conf" && return 1
    done
  fi
  return 0
}

step_reconcile_install() {
  echo "== 4/7: reconcile ExecStart-resetting drop-ins (rename, never delete) =="
  local unit
  for unit in "${ALL_UNITS[@]}"; do
    unit_is_current "$unit" || CHANGED+=("$unit")
    reconcile_execstart_dropins "$unit" "$SYSTEMD_DIR" "$DRY_RUN"
  done
  echo "== 5/7: install the five rendered units =="
  for unit in "${ALL_UNITS[@]}"; do
    run install -m 0644 "$RENDER_DIR/$unit.service" "$SYSTEMD_DIR/$unit.service"
  done
}

in_array() { local x="$1"; shift; local e; for e in "$@"; do [[ "$e" == "$x" ]] && return 0; done; return 1; }

wait_for_listener() {
  # bounded wait for <port> to listen (a model load can take minutes)
  local port="$1" waited=0
  while [[ -z "$(listening_addr "$port")" ]]; do
    (( waited >= VERIFY_TIMEOUT )) && return 1
    sleep "$VERIFY_STEP"
    waited=$(( waited + VERIFY_STEP ))
  done
  return 0
}

verify_one() {
  local unit="$1" addr
  addr="$(cni0_addr)"
  verify_unit_locked "$unit" "$SCRIPTS_DIR/${LAUNCHER[$unit]}" "$addr" "${PORT[$unit]}" "$KEY_FILE" "$PROC_ROOT"
}

step_restart() {
  echo "== 6/7: daemon-reload, then restart the ACTIVE units one at a time =="
  echo "  (${DORMANT_UNITS[*]} stay exactly as disabled/stopped as before: only their on-disk definition changed)"
  run systemctl daemon-reload
  local unit
  for unit in "${ACTIVE_UNITS[@]}"; do
    if [[ "$DRY_RUN" == "0" ]] && ! in_array "$unit" ${CHANGED[@]+"${CHANGED[@]}"} && verify_one "$unit" >/dev/null 2>&1; then
      echo "  $unit: already applied and verified -- not restarted (idempotent)"
      continue
    fi
    run systemctl restart "$unit"
    [[ "$DRY_RUN" == "1" ]] && continue
    if ! wait_for_listener "${PORT[$unit]}"; then
      echo "error: $unit did not listen on :${PORT[$unit]} within ${VERIFY_TIMEOUT}s" >&2
      fail_apply
    fi
    verify_one "$unit" || fail_apply
  done
}

fail_apply() {
  echo "" >&2
  echo "error: apply failed. Roll back with:  sudo $0 --rollback $BACKUP_DIR" >&2
  exit 3
}

# ── step 7: verification ──
nodehosts_check() {
  local addr text ip
  addr="$(cni0_addr)"
  if ! text="$("$KUBECTL_BIN" -n kube-system get configmap coredns -o 'jsonpath={.data.NodeHosts}' 2>/dev/null)"; then
    echo "  NodeHosts == $BIND_IFACE: NOT ESTABLISHED (could not read the coredns ConfigMap; re-run '$0 --verify' with a working kubeconfig)"
    return 0
  fi
  ip="$(nodehosts_ip "$NODEHOSTS_NAME" "$text")"
  if [[ "$ip" == "$addr" ]]; then
    echo "  NodeHosts == $BIND_IFACE: $NODEHOSTS_NAME -> $ip == $addr  OK"
    return 0
  fi
  echo "error: NodeHosts maps $NODEHOSTS_NAME to '${ip:-<absent>}' but $BIND_IFACE is $addr -- the pods would not reach the locked endpoints" >&2
  return 4
}

step_verify() {
  echo "== 7/7: verification (effective state, never the unit file text) =="
  local unit failed=0 binary version pid rc=0
  for unit in "${ACTIVE_UNITS[@]}"; do
    verify_one "$unit" || failed=1
    pid="$(unit_main_pid "$unit")"
    binary="$(readlink "$PROC_ROOT/$pid/exe" 2>/dev/null || true)"
    if [[ -n "$binary" ]]; then
      version="$(service_run "$SERVICE_USER" "$SERVICE_HOME" timeout 30 "$binary" --version 2>&1 | awk '/^version:/ { print; exit }' || true)"
      echo "  $unit -> $binary -> ${version:-<no version line>}"
    fi
  done
  if [[ -n "${BACKUP_DIR_FOR_STATE:-}" && -f "$BACKUP_DIR_FOR_STATE/STATE" ]]; then
    local u line now
    for u in "${DORMANT_UNITS[@]}"; do
      line="$(grep "^$u " "$BACKUP_DIR_FOR_STATE/STATE" || true)"
      now="$u enabled=$(systemctl is-enabled "$u" 2>/dev/null || true) active=$(systemctl is-active "$u" 2>/dev/null || true)"
      if [[ "$line" == "$now" ]]; then
        echo "  dormant $u unchanged ($line)"
      else
        echo "error: dormant unit state changed: was '$line', now '$now'" >&2
        failed=1
      fi
    done
  else
    for u in "${DORMANT_UNITS[@]}"; do
      echo "  dormant $u: enabled=$(systemctl is-enabled "$u" 2>/dev/null || true) active=$(systemctl is-active "$u" 2>/dev/null || true)  (no backup STATE to compare)"
    done
  fi
  nodehosts_check || rc=$?
  (( failed )) && return 3
  return "$rc"
}

# ── rollback ──
do_rollback() {
  [[ -n "$ROLLBACK_DIR" && -d "$ROLLBACK_DIR" && -f "$ROLLBACK_DIR/MANIFEST" ]] || {
    echo "error: --rollback needs the backup dir printed by --apply (a directory with a MANIFEST)" >&2
    exit 1
  }
  local unit f d
  for unit in "${ALL_UNITS[@]}"; do
    [[ -d "$ROLLBACK_DIR/$unit.pre-wu4" ]] || { echo "error: $ROLLBACK_DIR/$unit.pre-wu4 missing; refusing a partial rollback" >&2; exit 1; }
  done
  ( cd "$ROLLBACK_DIR" && sha256sum --quiet -c MANIFEST ) || { echo "error: backup MANIFEST does not verify; refusing" >&2; exit 1; }
  echo "== rollback 1/3: restore the backed-up unit files and drop-ins =="
  for unit in "${ALL_UNITS[@]}"; do
    d="$ROLLBACK_DIR/$unit.pre-wu4"
    [[ -f "$d/$unit.service" ]] && install -m 0644 "$d/$unit.service" "$SYSTEMD_DIR/$unit.service"
    if [[ -d "$d/$unit.service.d" ]]; then
      install -d -m 0755 "$SYSTEMD_DIR/$unit.service.d"
      for f in "$d/$unit.service.d"/*; do
        [[ -f "$f" ]] || continue
        install -m 0644 "$f" "$SYSTEMD_DIR/$unit.service.d/$(basename "$f")"
        rm -f "$SYSTEMD_DIR/$unit.service.d/$(basename "$f").disabled-by-wu4-locks"
      done
    fi
    echo "  restored $unit"
  done
  echo "== rollback 2/3: daemon-reload =="
  systemctl daemon-reload
  echo "== rollback 3/3: restart the ACTIVE units one at a time =="
  for unit in "${ACTIVE_UNITS[@]}"; do
    systemctl restart "$unit" || { echo "error: restarting $unit failed; stopping" >&2; exit 5; }
    wait_for_listener "${PORT[$unit]}" || { echo "error: $unit did not listen on :${PORT[$unit]}" >&2; exit 5; }
    echo "  $unit: active=$(systemctl is-active "$unit" 2>/dev/null || true); listening: $(listening_addr "${PORT[$unit]}" | tr '\n' ' ')"
  done
  echo "  expected after rollback: the three ports are back on 0.0.0.0 and GET 127.0.0.1:11434/v1/models (OpenCode via Caddy) is 200."
}

# ── main ──
resolve_service_identity
render_all

if [[ "$MODE" == "verify" ]]; then
  BACKUP_DIR_FOR_STATE="${LLAMA_APPLY_BACKUP_DIR:-}"
  step_verify
  exit $?
fi
if [[ "$MODE" == "rollback" ]]; then
  do_rollback
  exit 0
fi

step_key
step_preflight
step_backup
BACKUP_DIR_FOR_STATE="$BACKUP_DIR"
step_reconcile_install
step_restart
if [[ "$DRY_RUN" == "1" ]]; then
  echo "== 7/7: (dry-run -- no restart occurred, nothing to verify) =="
  exit 0
fi
rc=0
step_verify || rc=$?
if (( rc == 3 )); then fail_apply; fi
if (( rc != 0 )); then exit "$rc"; fi
echo ""
echo "applied. Evidence reads (read-only) are in the window runbook; roll back with:  sudo $0 --rollback $BACKUP_DIR"
