#!/usr/bin/env bash
# M3 WU-4 (llama endpoint isolation locks, 2026-09-18) — OPERATOR-RUN.
#
# The builder/reviewer instruments that produced this WU do NOT have
# host root on pcLuisLinux (verified: `sudo -n true` and a direct
# `systemctl restart` both failed non-interactively during the build —
# see the build record's "operator action required" section). This
# script is the single, reviewable, idempotent command an operator with
# real sudo runs to actually flip the three production llama-server
# units over to the locked-down launchers.
#
# What it does, in order:
#   1. Ensures the shared API key file exists (never overwrites one that
#      already exists — that would silently break every already-
#      provisioned consumer without warning).
#   2. Installs the three reference unit files from scripts/systemd/.
#   3. daemon-reload, then restarts all three services.
#   4. Prints the post-restart verification commands (ss + curl) so the
#      operator confirms the lock is live before trusting it.
#
# Usage:
#   scripts/apply-llama-endpoint-locks.sh --dry-run   # print, do nothing
#   sudo scripts/apply-llama-endpoint-locks.sh         # apply for real

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_FILE="${LLAMA_API_KEY_FILE:-${SECRETS_DIR:-$HOME/work/audittrace-private/secrets}/llama-api-key.txt}"
DRY_RUN=0

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "+ $*"
  else
    "$@"
  fi
}

if [[ "$DRY_RUN" == "0" && "$(id -u)" != "0" ]]; then
  echo "error: this script must run as root (sudo) to install unit files" >&2
  echo "  try: sudo $0" >&2
  echo "  or:  $0 --dry-run   (to see what it would do, no root needed)" >&2
  exit 1
fi

echo "== 1/4: API key file =="
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

echo "== 2/4: install unit files =="
for unit in llama-server llama-embed-server llama-summarizer; do
  run install -m644 "$REPO_ROOT/scripts/systemd/$unit.service" \
    "/etc/systemd/system/$unit.service"
done

echo "== 3/4: reload + restart =="
run systemctl daemon-reload
run systemctl restart llama-server llama-embed-server llama-summarizer

echo "== 4/4: verify (run these yourself after this script exits) =="
cat <<'EOF'
  ss -ltnp | grep -E ':(11435|11436|11437)'   # expect 127.0.0.1 on all three
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
