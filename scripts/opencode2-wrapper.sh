#!/usr/bin/env bash
# Start OpenCode **v2** (opencode2) with a fresh, user-bound AuditTrace-AI Bearer,
# against the project-local v2 config (.opencode/opencode.json).
#
# v2 sibling of scripts/opencode-wrapper.sh (which drives v1). Differences:
#   - execs `opencode2` (not `opencode`)
#   - injects the Bearer into the PROJECT config `<repo>/.opencode/opencode.json`
#     (gitignored) rather than ~/.config/opencode/config.json, so the v1 fleet
#     config is never touched and runs stay isolated (v2 eval 2026-08-09).
#
# Token handling is identical to the v1 wrapper and stays SDLC-ADR-004 clean:
#   1. `audittrace-login --ensure` refreshes the access_token (interactive
#      Device Flow only if the refresh chain expired).
#   2. `audittrace-login --show-unsafe` yields the REAL Bearer straight into a
#      shell var (the sanctioned carve-out — TOKEN-GUARD 2026-08-11; never echoed).
#   3. jq writes it atomically into every provider's options.apiKey.
#
# Usage (from anywhere):
#   scripts/opencode2-wrapper.sh run --standalone --agent audittrace-builder \
#     --model audittrace-builder/qwen3.6 --auto --format json "<spec instruction>"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOGIN="$SCRIPT_DIR/audittrace-login"
CONFIG="${OPENCODE2_CONFIG:-$REPO_DIR/.opencode/opencode.json}"

log() { echo "[opencode2-wrapper] $*" >&2; }

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "error: $1 not found on PATH" >&2; exit 2; }
}

require jq
require opencode2

if [[ ! -f "$CONFIG" ]]; then
  echo "error: v2 project config not found at $CONFIG" >&2
  echo "(expected <repo>/.opencode/opencode.json — WU-0 fleet config)" >&2
  exit 4
fi

# Ensure token is fresh; interactive Device Flow only if the refresh chain expired.
if ! "$LOGIN" --ensure 2>/dev/null; then
  log "no valid token — starting interactive Device Flow"
  "$LOGIN"
fi

# REAL Bearer straight into a var (never echoed). --show-unsafe, not --show.
BEARER="$("$LOGIN" --show-unsafe)"
if [[ -z "$BEARER" ]]; then
  echo "error: audittrace-login --show-unsafe returned empty — aborting" >&2
  exit 3
fi

# Inject the fresh Bearer into every provider's options.apiKey (the
# @ai-sdk/openai-compatible provider builds "Authorization: Bearer <token>"
# from options.apiKey). Atomic temp-file write so a crash can't corrupt config.
BACKUP="${CONFIG}.bak-$(date +%Y%m%d_%H%M%S)"
cp "$CONFIG" "$BACKUP"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
jq --arg token "$BEARER" '
  .provider |= with_entries(
    .value.options = (.value.options // {}) |
    .value.options.apiKey = $token |
    (if .value.options.headers then
       .value.options.headers |= del(.Authorization, .authorization)
     else . end) |
    .
  )
' "$CONFIG" > "$TMP"
mv -f "$TMP" "$CONFIG"
log "wrote fresh Bearer into $CONFIG (backup: $BACKUP)"

# Run v2 from the repo root so the project .opencode/ resolves.
cd "$REPO_DIR"
exec opencode2 "$@"
