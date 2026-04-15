#!/usr/bin/env bash
# Start OpenCode with a fresh, user-bound AuditTrace-AI Bearer.
#
# One-command launcher that wires the ADR-032 Device-Flow token
# into OpenCode's provider config before exec'ing opencode:
#
#   1. Runs ``audittrace-login --ensure`` so the access_token is
#      current (auto-refresh if within 60s of expiry).
#   2. Writes ``Authorization: Bearer <token>`` into every provider's
#      ``options.headers`` in ``~/.config/opencode/config.json``.
#   3. Execs opencode so signals/stdio flow cleanly.
#
# First run (no saved token yet):
#
#   scripts/opencode-wrapper.sh
#     → launches audittrace-login, opens the device-flow URL for you,
#       waits until login completes, then launches opencode.
#
# Subsequent runs are silent unless the refresh token itself expired
# (SSO max lifespan, default 30 days), in which case the wrapper
# falls back to the interactive login.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGIN="$SCRIPT_DIR/audittrace-login"
CONFIG="${OPENCODE_CONFIG:-$HOME/.config/opencode/config.json}"

log() { echo "[opencode-wrapper] $*" >&2; }

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: $1 not found on PATH" >&2
    exit 2
  }
}

require jq
require opencode

# Ensure token is fresh; fall back to interactive login if there's no
# token file or the refresh chain has expired.
if ! "$LOGIN" --ensure 2>/dev/null; then
  log "no valid token — starting interactive Device Flow"
  "$LOGIN"
fi

BEARER="$("$LOGIN" --show)"
if [[ -z "$BEARER" ]]; then
  echo "error: audittrace-login --show returned empty — aborting" >&2
  exit 3
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "error: OpenCode config not found at $CONFIG" >&2
  echo "(create it first, or set OPENCODE_CONFIG to your path)" >&2
  exit 4
fi

# Back up the config, then inject the fresh Bearer. The
# ``@ai-sdk/openai-compatible`` provider that OpenCode uses builds
# its outbound ``Authorization: Bearer <token>`` from
# ``options.apiKey`` — NOT from ``options.headers.Authorization``.
# Setting the header alone leaves a stale apiKey-derived Authorization
# in flight and produces 401s (live-surfaced 2026-04-15).
#
# Write the raw token to ``options.apiKey`` (SDK prepends "Bearer ").
# Also scrub any lingering ``Authorization`` from ``options.headers``
# so there's no ambiguity for readers of the config. jq writes
# atomically via the temp file pattern so a crash in the middle does
# not corrupt the user's config.
BACKUP="${CONFIG}.bak-$(date +%Y%m%d_%H%M%S)"
cp "$CONFIG" "$BACKUP"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
jq --arg token "$BEARER" '
  .provider = (.provider // {}) |
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

exec opencode "$@"
