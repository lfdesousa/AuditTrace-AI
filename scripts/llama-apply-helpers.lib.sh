#!/usr/bin/env bash
# LC-5 / M3-WU-4 -- helper functions for scripts/apply-llama-endpoint-locks.sh,
# split into a sourceable library so each can be unit-tested in isolation
# (source + call, no root, no real systemd) as well as end to end.
#
# Nothing here prints a key. Nothing here puts a key on argv (the one place the
# key VALUE is compared uses a bash pattern match, a builtin, not a command).

# ── unit-file parsing (preflight reads the SAME files it is about to install) ──

unit_directive() {
  # unit_directive <unit file> <Directive>: every value, one per line, with
  # backslash continuations joined. Comment lines are ignored.
  local file="$1" name="$2"
  awk -v name="$name" '
    /^[[:space:]]*[#;]/ { next }
    {
      if (cont) { line = line " " $0 } else { line = $0 }
      if (line ~ /\\[[:space:]]*$/) { sub(/\\[[:space:]]*$/, "", line); cont = 1; next }
      cont = 0
      if (index(line, name "=") == 1) { print substr(line, length(name) + 2) }
      line = ""
    }
  ' "$file"
}

extract_exec_paths() {
  # The executable path of every ExecStart= / ExecStartPre= (first token,
  # leading '-' / '@' / '+' / '!' prefixes stripped).
  local file="$1" d value
  for d in ExecStartPre ExecStart; do
    while IFS= read -r value; do
      [[ -z "$value" ]] && continue
      value="${value%% *}"
      value="${value#[-@+!:]}"
      printf '%s\n' "$value"
    done < <(unit_directive "$file" "$d")
  done
}

render_unit() {
  # render_unit <template> <dest> <scripts dir> <key file>: substitute
  # @SCRIPTS_DIR@ and @KEY_FILE@. Both are absolute paths in a conservative
  # alphabet, so neither can smuggle a sed or systemd metacharacter.
  local src="$1" dest="$2" scripts_dir="$3" key_file="$4" v
  for v in "$scripts_dir" "$key_file"; do
    [[ "$v" =~ ^/[A-Za-z0-9_./-]+$ ]] || {
      echo "error: path '$v' has characters outside [A-Za-z0-9_./-]; refusing to render units" >&2
      return 1
    }
  done
  sed -e "s|@SCRIPTS_DIR@|${scripts_dir}|g" -e "s|@KEY_FILE@|${key_file}|g" "$src" > "$dest"
}

check_unit_paths() {
  # Every absolute path an ExecStart/ExecStartPre of the (rendered) unit runs
  # must exist and be executable. Returns 1 and names the missing paths.
  local unit_file="$1" unit_name path missing=()
  unit_name="$(basename "$unit_file" .service)"
  while IFS= read -r path; do
    [[ -z "$path" ]] && continue
    [[ -x "$path" ]] || missing+=("$path")
  done < <(extract_exec_paths "$unit_file")
  if (( ${#missing[@]} > 0 )); then
    echo "error: $unit_name runs path(s) that do not exist or are not executable on this host:" >&2
    printf '  %s\n' "${missing[@]}" >&2
    return 1
  fi
  return 0
}

# ── running things as the unit's User= ──

service_run() {
  # service_run <user> <home> <cmd...>: run as the unit's user, with its HOME,
  # never as root (a user-owned binary must not execute with root's authority).
  local user="$1" home="$2"
  shift 2
  if [[ "${LLAMA_APPLY_FORCE_RUNUSER:-0}" == "1" || ( "$(id -u)" == "0" && "$user" != "root" ) ]]; then
    runuser -u "$user" -- env "HOME=$home" "$@"
  else
    env "HOME=$home" "$@"
  fi
}

# ── drop-in reconciliation (a drop-in ALWAYS wins over the base unit file) ──

reconcile_execstart_dropins() {
  # A drop-in that resets `ExecStart=` supplies the unit's EFFECTIVE command
  # (this host: llama-server.service.d/20-qwen38-mtp-permanent.conf). Installing
  # only the base unit would silently no-op. RENAME it (never delete) to
  # `<name>.disabled-by-wu4-locks`, so rollback and a human can restore it.
  # Prints one line per renamed file.
  local unit="$1" systemd_dir="$2" dry_run="$3" dropin_dir conf
  dropin_dir="$systemd_dir/$unit.service.d"
  [[ -d "$dropin_dir" ]] || return 0
  for conf in "$dropin_dir"/*.conf; do
    [[ -e "$conf" ]] || continue
    if grep -q '^ExecStart=[[:space:]]*$' "$conf"; then
      echo "  $unit: ExecStart-resetting drop-in $(basename "$conf") -> renamed to $(basename "$conf").disabled-by-wu4-locks (never deleted)"
      if [[ "$dry_run" == "1" ]]; then
        echo "+ mv $conf $conf.disabled-by-wu4-locks"
      else
        mv "$conf" "$conf.disabled-by-wu4-locks"
      fi
    fi
  done
}

# ── post-restart verification: the EFFECTIVE command, never the unit file text ──

show_execstart_paths() {
  # Every `path=` of `systemctl show -p ExecStart <unit>`, one per line.
  systemctl show -p ExecStart "$1" 2>/dev/null | grep -oE 'path=[^ ;]+' | sed 's/^path=//'
}

unit_main_pid() {
  systemctl show -p MainPID --value "$1" 2>/dev/null
}

listening_addr() {
  # listening_addr <port>: the local address:port of every TCP listener on it.
  ss -ltnH "sport = :$1" 2>/dev/null | awk '{ print $4 }'
}

verify_unit_locked() {
  # verify_unit_locked <unit> <launcher path> <bind addr> <port> <key file> <proc root>
  # Checks, in order: (1) systemd's EFFECTIVE ExecStart is exactly the launcher
  # (a drop-in did not win), (2) the running process argv carries --host <the
  # cni0 address>, --api-key-file <path>, --no-webui and not the key VALUE nor
  # 0.0.0.0, (3) the socket is listening on <addr>:<port> only.
  local unit="$1" launcher="$2" addr="$3" port="$4" key_file="$5" proc_root="$6"
  local problems=() paths pid cmdline key_value listeners l

  paths="$(show_execstart_paths "$unit")"
  if [[ "$paths" != "$launcher" ]]; then
    problems+=("effective ExecStart path is '${paths//$'\n'/ }' (expected exactly '$launcher'; a drop-in may still override it)")
  fi

  pid="$(unit_main_pid "$unit")"
  if [[ -z "$pid" || "$pid" == "0" || ! -r "$proc_root/$pid/cmdline" ]]; then
    problems+=("no running main process (MainPID='${pid:-}')")
  else
    cmdline="$(tr '\0' ' ' < "$proc_root/$pid/cmdline")"
    [[ "$cmdline" == *" --host $addr "* ]] || problems+=("running argv lacks '--host $addr'")
    [[ "$cmdline" == *" --api-key-file $key_file "* ]] || problems+=("running argv lacks '--api-key-file <the configured key file>'")
    [[ "$cmdline" == *" --no-webui "* ]] || problems+=("running argv lacks '--no-webui'")
    [[ "$cmdline" == *" --host 0.0.0.0 "* ]] && problems+=("running argv still binds 0.0.0.0")
    if [[ -r "$key_file" ]]; then
      key_value="$(< "$key_file")"
      if [[ -n "$key_value" && "$cmdline" == *"$key_value"* ]]; then
        problems+=("the key VALUE appears on the running argv")
      fi
    fi
  fi

  listeners="$(listening_addr "$port")"
  if [[ -z "$listeners" ]]; then
    problems+=("nothing is listening on :$port")
  else
    while IFS= read -r l; do
      [[ "$l" == "$addr:$port" ]] || problems+=("listener '$l' on :$port (expected only $addr:$port)")
    done <<< "$listeners"
  fi

  if (( ${#problems[@]} > 0 )); then
    echo "error: $unit is NOT locked as intended:" >&2
    printf '  - %s\n' "${problems[@]}" >&2
    return 1
  fi
  echo "  ok -- $unit: effective ExecStart = launcher; argv carries --host $addr, --api-key-file, --no-webui; listening only on $addr:$port"
  return 0
}

nodehosts_ip() {
  # nodehosts_ip <name> <NodeHosts text>: the IP mapped to <name>, or empty.
  local name="$1" text="$2"
  awk -v n="$name" '{ for (i = 2; i <= NF; i++) if ($i == n) { print $1; exit } }' <<< "$text"
}
