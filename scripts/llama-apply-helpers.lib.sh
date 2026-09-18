#!/usr/bin/env bash
# M3 WU-4 (llama endpoint isolation locks, 2026-09-18) review round 1 —
# pure(ish) helper functions for scripts/apply-llama-endpoint-locks.sh,
# split into a sourceable library so each one can be unit-tested in
# isolation (source this file + call a function directly, no root, no
# real systemd) rather than only exercised end-to-end through the whole
# apply script — see tests/test_apply_llama_endpoint_locks.py.
#
# Usage: source this file, then call:
#   extract_required_paths <unit-file>          # F9
#   check_unit_paths <unit-file>                # F9
#   reconcile_execstart_dropins <unit> <systemd_dir> <dry_run 0|1>   # F1
#   verify_effective_locks <unit>                # F1 (calls the real
#                                                 # `systemctl` on PATH —
#                                                 # tests stub it there)

extract_required_paths() {
  # Every absolute path this unit's ExecStart/ExecStartPre/key-file
  # Environment directive references — derived from the SAME file about
  # to be installed, so this can never drift from what actually ships (a
  # hardcoded list here would be exactly the kind of duplicate source of
  # truth that already drifted once — see the summariser unit-vs-script
  # drift in the WU-4 build record).
  local unit_file="$1"
  awk '
    /^Exec(Start|StartPre)=/ || /_API_KEY_FILE=/ { capture=1 }
    capture {
      print
      if ($0 !~ /\\$/) capture=0
    }
  ' "$unit_file" | grep -oE '/[A-Za-z0-9_./-]+' | sort -u
}

check_unit_paths() {
  local unit_file="$1"
  local unit_name
  unit_name="$(basename "$unit_file" .service)"
  local missing=()
  local path
  while IFS= read -r path; do
    [[ -z "$path" ]] && continue
    [[ -e "$path" ]] || missing+=("$path")
  done < <(extract_required_paths "$unit_file")
  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "error: $unit_name references path(s) that do not exist on this host:" >&2
    printf '  %s\n' "${missing[@]}" >&2
    return 1
  fi
  return 0
}

reconcile_execstart_dropins() {
  # A drop-in that resets `ExecStart=` (the idiom systemd itself defines
  # for "clear the inherited value, then re-declare it") ALWAYS wins over
  # the base unit file — `systemctl show -p ExecStart <unit>` is the
  # effective value, never the unit file's text. This host's
  # `llama-server.service.d/20-qwen38-mtp-permanent.conf` does exactly
  # that (`--host 0.0.0.0`, no key, webui on): installing only the base
  # unit silently no-ops on any unit carrying such a drop-in. Every
  # locked reference unit already reproduces the SAME model/flags via its
  # launcher script, so the drop-in is superseded, not needed — rename
  # it (never delete) so it can be restored by hand if that ever changes.
  local unit="$1" systemd_dir="$2" dry_run="$3"
  local dropin_dir="$systemd_dir/$unit.service.d"
  [[ -d "$dropin_dir" ]] || return 0
  local conf
  for conf in "$dropin_dir"/*.conf; do
    [[ -e "$conf" ]] || continue
    if grep -q '^ExecStart=$' "$conf"; then
      echo "  found ExecStart-overriding drop-in for $unit: $conf"
      echo "  -> disabling (renamed to $(basename "$conf").disabled-by-wu4-locks)"
      if [[ "$dry_run" == "1" ]]; then
        echo "+ mv $conf $conf.disabled-by-wu4-locks"
      else
        mv "$conf" "$conf.disabled-by-wu4-locks"
      fi
    fi
  done
}

verify_effective_locks() {
  # Re-reads the unit's EFFECTIVE ExecStart via `systemctl show` — never
  # the unit file's text, which a drop-in can make lie about this (F1).
  local unit="$1"
  local effective
  effective="$(systemctl show -p ExecStart "$unit" 2>/dev/null || true)"
  local missing=()
  for lock in '--host 127.0.0.1' '--api-key-file' '--no-webui'; do
    [[ "$effective" == *"$lock"* ]] || missing+=("$lock")
  done
  if [[ "$effective" == *'--host 0.0.0.0'* ]]; then
    missing+=("(still binds 0.0.0.0 -- a drop-in may still be overriding ExecStart)")
  fi
  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "error: $unit's EFFECTIVE ExecStart (post drop-in resolution) is missing:" >&2
    printf '  %s\n' "${missing[@]}" >&2
    echo "  effective value was:" >&2
    echo "  $effective" >&2
    return 1
  fi
  echo "  ok — $unit effective ExecStart carries all three locks"
  return 0
}
