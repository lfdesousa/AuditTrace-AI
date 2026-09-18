#!/usr/bin/env bash
# M3 WU-4 (llama endpoint isolation locks, 2026-09-18) — OPERATOR-RUN.
#
# The builder/reviewer instruments that produced this WU do NOT have
# host root on pcLuisLinux (verified: `sudo -n true` and a direct
# `systemctl restart` both failed non-interactively during the build —
# see the build record's "operator action required" section). This
# script is the single, reviewable, idempotent command an operator with
# real sudo runs to actually flip the host's llama-server units over to
# the locked-down launchers.
#
# review round 1 enumeration (F8): `systemctl list-unit-files 'llama-*'`
# (never `list-units`, which hides `disabled` units and undercounted the
# original build's enumeration) finds FIVE llama-server units on this
# host, not three:
#   ACTIVE  (this script installs the locked definition AND restarts):
#     llama-server (:11435, chat) · llama-embed-server (:11436, embed) ·
#     llama-summarizer (:11437, session summariser)
#   DORMANT (this script installs the locked definition but does NOT
#            start or enable — they stay exactly as `disabled` as they
#            were before this script ran; only a FUTURE `systemctl
#            start` behaves differently, coming up locked instead of
#            unauthenticated):
#     llama-fleet (:11438, SDLC-fleet builder) ·
#     llama-mistral-server (:11437, ADR-015 A/B testing)
#
# What it does, in order:
#   0. PREFLIGHT (F9): refuses to touch anything if any of the FIVE
#      units' referenced launcher scripts/binaries/models/key-file paths
#      do not exist on this host — an operator must not be able to break
#      a currently-healthy service (e.g. embeddings) by following this
#      script before the referenced launchers have actually landed on
#      this host (e.g. before `main` has this WU's `scripts/start-{chat,
#      embed}-llama.sh` merged in). Paths are derived by parsing the SAME
#      unit files this script is about to install — never a separate
#      hardcoded list that could itself drift.
#   1. Ensures the shared API key file exists (never overwrites one that
#      already exists — that would silently break every already-
#      provisioned consumer without warning).
#   2. Reconciles any systemd drop-in that resets `ExecStart=` for one of
#      the FIVE units (F1): a drop-in ALWAYS wins over the base unit file
#      — `systemctl show -p ExecStart <unit>` is the effective value,
#      never the unit file's text — so installing only the base unit
#      would silently no-op on any unit carrying such a drop-in (this
#      host's `llama-server.service.d/20-qwen38-mtp-permanent.conf` does
#      exactly that: `--host 0.0.0.0`, no key, webui on). Every locked
#      reference unit in scripts/systemd/ already reproduces the SAME
#      model/flags the drop-in carries via its launcher script, so a
#      drop-in that only re-declares `ExecStart=` is superseded, not
#      needed — it is renamed (never deleted) to `<file>.disabled-by-
#      wu4-locks` so it can be restored by hand if that ever changes.
#   3. Installs the five reference unit files from scripts/systemd/.
#   4. daemon-reload, then restarts ONLY the three ACTIVE units (starting
#      a currently-`disabled`/stopped unit is a much bigger behavioural
#      change than this security-only WU is scoped for).
#   5. POST-RESTART VERIFICATION (F1): re-reads each ACTIVE unit's
#      EFFECTIVE `ExecStart` via `systemctl show` — never the unit file's
#      text — and fails loudly if any of the three locks is missing,
#      rather than trusting that installing the file was enough.
#
# Usage:
#   scripts/apply-llama-endpoint-locks.sh --dry-run   # print, do nothing
#   sudo scripts/apply-llama-endpoint-locks.sh         # apply for real
#
# Testing hooks (same pattern as LLAMA_API_KEY_FILE/LLAMA_BIN/MODEL_PATH
# on the launcher scripts — never override either in production use):
#   SYSTEMD_DIR              overrides the installed-units directory
#                            (default /etc/systemd/system) so tests can
#                            point this script at a tmp directory instead
#                            of the real host.
#   LLAMA_APPLY_ALLOW_NONROOT bypasses the root check below for a
#                            non-dry-run test that has ALREADY redirected
#                            every mutation (install/mv/systemctl) to
#                            fakes via SYSTEMD_DIR + a stubbed PATH — the
#                            real OS file permissions on
#                            /etc/systemd/system still gate every
#                            production install/mv regardless of this
#                            flag, so setting it without real root just
#                            trades this script's friendly early error
#                            for install's own "Permission denied" later.
# See tests/test_apply_llama_endpoint_locks.py.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=llama-apply-helpers.lib.sh
source "$REPO_ROOT/scripts/llama-apply-helpers.lib.sh"

KEY_FILE="${LLAMA_API_KEY_FILE:-${SECRETS_DIR:-$HOME/work/audittrace-private/secrets}/llama-api-key.txt}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
DRY_RUN=0

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

# Units this script installs AND restarts — currently `enabled`+active on
# every host WU-4 has enumerated.
ACTIVE_UNITS=(llama-server llama-embed-server llama-summarizer)
# Units this script installs the locked DEFINITION for but never starts
# or enables (F8 — see header).
DORMANT_UNITS=(llama-fleet llama-mistral-server)
ALL_UNITS=("${ACTIVE_UNITS[@]}" "${DORMANT_UNITS[@]}")

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "+ $*"
  else
    "$@"
  fi
}

if [[ "$DRY_RUN" == "0" && "$(id -u)" != "0" && -z "${LLAMA_APPLY_ALLOW_NONROOT:-}" ]]; then
  echo "error: this script must run as root (sudo) to install unit files" >&2
  echo "  try: sudo $0" >&2
  echo "  or:  $0 --dry-run   (to see what it would do, no root needed)" >&2
  exit 1
fi

# ── F9: preflight — every referenced path must exist before we touch anything ──
# (extract_required_paths / check_unit_paths live in llama-apply-helpers.lib.sh)

echo "== 0/5: preflight — verify every referenced launcher/binary/model/key-file path exists =="
preflight_failed=0
for unit in "${ALL_UNITS[@]}"; do
  if ! check_unit_paths "$REPO_ROOT/scripts/systemd/$unit.service"; then
    preflight_failed=1
  fi
done
if [[ "$preflight_failed" == "1" ]]; then
  echo "" >&2
  echo "error: refusing to install/restart ANY unit. A partial apply would" >&2
  echo "  leave some endpoints on the new (locked) unit and others on the" >&2
  echo "  old (unlocked) one, or crash-loop a service that was previously" >&2
  echo "  healthy (M3 WU-4 review round 1, F9 — this happened: running this" >&2
  echo "  script before this WU's launchers had landed on this host would" >&2
  echo "  have taken embeddings down)." >&2
  echo "  Fix the missing path(s) above — most likely this WU has not been" >&2
  echo "  merged/deployed to this checkout yet — then re-run." >&2
  exit 2
fi
echo "  ok — every unit's launcher/binary/model/key-file path exists"

echo "== 1/5: API key file =="
if [[ -f "$KEY_FILE" ]]; then
  echo "  already present at $KEY_FILE — leaving it untouched"
else
  echo "  generating a new key at $KEY_FILE"
  run install -d -m700 "$(dirname "$KEY_FILE")"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "+ ( umask 177 && openssl rand -hex 32 > $KEY_FILE )"
  else
    ( umask 177 && openssl rand -hex 32 > "$KEY_FILE" )
  fi
  echo "  ⚠ set AUDITTRACE_LLAMA_API_KEY / AUDITTRACE_EMBED_API_KEY /"
  echo "    AUDITTRACE_SUMMARIZER_API_KEY on the backend to this file's"
  echo "    contents before restarting the memory-server, or every"
  echo "    outbound call will start getting 401s."
fi

echo "== 2/5: reconcile ExecStart-overriding drop-ins =="
for unit in "${ALL_UNITS[@]}"; do
  reconcile_execstart_dropins "$unit" "$SYSTEMD_DIR" "$DRY_RUN"
done

echo "== 3/5: install unit files =="
for unit in "${ALL_UNITS[@]}"; do
  run install -m644 "$REPO_ROOT/scripts/systemd/$unit.service" \
    "$SYSTEMD_DIR/$unit.service"
done

echo "== 4/5: reload + restart ACTIVE units only =="
echo "  (${DORMANT_UNITS[*]} stay exactly as disabled/stopped as before —"
echo "   only their on-disk DEFINITION changed, for the next time someone"
echo "   starts them)"
run systemctl daemon-reload
run systemctl restart "${ACTIVE_UNITS[@]}"

# ── F1: post-restart verification of the EFFECTIVE ExecStart ──
# (verify_effective_locks lives in llama-apply-helpers.lib.sh)

if [[ "$DRY_RUN" == "0" ]]; then
  echo "== 5/5: post-restart verification (EFFECTIVE ExecStart, never the unit file text) =="
  verify_failed=0
  for unit in "${ACTIVE_UNITS[@]}"; do
    if ! verify_effective_locks "$unit"; then
      verify_failed=1
    fi
  done
  if [[ "$verify_failed" == "1" ]]; then
    echo "" >&2
    echo "error: one or more units restarted WITHOUT all three locks in" >&2
    echo "  their EFFECTIVE ExecStart. A conflicting drop-in this script" >&2
    echo "  did not recognise may still be present — investigate before" >&2
    echo "  trusting this host (M3 WU-4 review round 1, F1)." >&2
    exit 3
  fi
else
  echo "== 5/5: (dry-run — skipping post-restart verification, no restart occurred) =="
fi

echo "== manual spot-checks (run these yourself once satisfied) =="
cat <<'EOF'
  ss -ltnp | grep -E ':(11435|11436|11437)'   # expect 127.0.0.1 on all three

  # ⚠ do NOT check GET /v1/models or /health for a 401 — llama.cpp keeps
  # both public even with a key configured (upstream behaviour, not a
  # gap). Use a real inference call instead:
  KEY=$(cat "$LLAMA_API_KEY_FILE")
  curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:11435/v1/chat/completions -X POST \
      -H 'Content-Type: application/json' -d '{"model":"x","messages":[{"role":"user","content":"hi"}]}'
      # expect 401 (keyless)
  curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $KEY" \
      http://127.0.0.1:11435/v1/chat/completions -X POST \
      -H 'Content-Type: application/json' -d '{"model":"x","messages":[{"role":"user","content":"hi"}]}'
      # expect 200 (keyed)
  curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:11435/   # expect 404 (webui disabled)
EOF
