#!/usr/bin/env bash
# Shared "isolation locks" flags for every llama-server launcher on this
# host (M3 WU-4 — llama endpoint isolation locks). Single source of truth
# so scripts/start-{chat,embed,summarizer}-llama.sh cannot drift on the
# security posture the way the summariser script and its systemd unit had
# already drifted from each other before this WU (see the build record).
#
# THE FOUR LOCKS (per endpoint):
#   1. --host 127.0.0.1     — loopback only. Never 0.0.0.0.
#   2. --api-key-file FILE  — a real key, sourced from a file, NEVER a
#                             literal on argv. `ps` can read every argv
#                             value on this host, so `--api-key VALUE`
#                             would "fix" an unauthenticated endpoint by
#                             publishing its own credential host-wide.
#                             `--api-key-file` puts only the FILE PATH on
#                             argv (paths are not secrets); the file's
#                             contents never appear in `ps`, in a script
#                             literal, or in git.
#   3. --no-webui           — no browsable UI on a model endpoint.
#   4. Consumer wiring is NOT a launcher-flag concern — see
#      llama_auth_headers() in src/audittrace/routes/_llama_client.py and
#      the AUDITTRACE_LLAMA_API_KEY / _EMBED_API_KEY / _SUMMARIZER_API_KEY
#      settings that carry this SAME file's contents to the backend.
#
# Usage: source this file AFTER setting any override, then splice
# "${LLAMA_LOCK_FLAGS[@]}" into the launcher's exec line:
#
#   source "$(dirname "${BASH_SOURCE[0]}")/llama-lock-flags.lib.sh"
#   exec "$LLAMA_BIN" --model "$MODEL_PATH" --port "$PORT" \
#     "${LLAMA_LOCK_FLAGS[@]}" ...
#
# LLAMA_API_KEY_FILE defaults to this repo's existing host-secrets
# convention (${SECRETS_DIR:-~/work/audittrace-private/secrets}/*.txt —
# see setup-vault.sh) so operators do not need a new secrets location.
# Callers (including tests) may override it before sourcing.

: "${LLAMA_API_KEY_FILE:=${SECRETS_DIR:-$HOME/work/audittrace-private/secrets}/llama-api-key.txt}"

if [[ ! -f "$LLAMA_API_KEY_FILE" ]]; then
  echo "error: llama API key file not found at $LLAMA_API_KEY_FILE" >&2
  echo "  generate one with:" >&2
  echo "    install -d -m700 \"\$(dirname "$LLAMA_API_KEY_FILE")\"" >&2
  echo "    ( umask 177 && openssl rand -hex 32 > \"$LLAMA_API_KEY_FILE\" )" >&2
  exit 1
fi

# shellcheck disable=SC2034  # consumed by the sourcing script's exec line
LLAMA_LOCK_FLAGS=(--host 127.0.0.1 --api-key-file "$LLAMA_API_KEY_FILE" --no-webui)
