#!/usr/bin/env bash
# Start the SDLC-fleet builder llama-server on :11438 (Mistral-Small-3.1-24B,
# Vulkan backend — see the private SDLC-ADR-000 series / runbook 15).
#
# M3 WU-4 (llama endpoint isolation locks, 2026-09-18) review round 1, F8:
# `systemctl list-unit-files 'llama-*'` (never `list-units`, which hides
# `disabled` units) enumerated FIVE llama-server units on this host, not
# three — this one and llama-mistral-server.service were both missed by
# the original enumeration because they are currently `disabled` and
# therefore invisible to `list-units`. Both were "one `systemctl start`
# away" from re-opening an unauthenticated endpoint, so this launcher
# brings the SAME three locks to this unit even though it is not
# currently running: scripts/apply-llama-endpoint-locks.sh installs this
# unit's LOCKED definition without starting or enabling it (see that
# script's header), so a future `systemctl start llama-fleet` comes up
# locked by construction instead of relying on someone remembering this
# WU.
#
# Defaults below reproduce this host's live llama-fleet.service flags
# (`systemctl cat llama-fleet` at review-round-1 time) so installing this
# script as the unit's ExecStart changes WHO MAY REACH the port, nothing
# about what it computes.
#
# Usage:
#   scripts/start-fleet-llama.sh                        # foreground
#   scripts/start-fleet-llama.sh &                       # background
#   LLAMA_BIN=/path/to/llama-server scripts/...          # override binary
#   MODEL_PATH=/path/to/other.gguf scripts/...           # override model
#   LLAMA_API_KEY_FILE=/path/to/key scripts/...          # override key file

set -euo pipefail

# shellcheck source=llama-lock-flags.lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/llama-lock-flags.lib.sh"

LLAMA_BIN="${LLAMA_BIN:-$HOME/build/llama.cpp/build-vulkan-new/bin/llama-server}"
MODEL_PATH="${MODEL_PATH:-$HOME/models/mistralai_Mistral-Small-3.1-24B-Instruct-2503-Q4_K_M.gguf}"
CHAT_TEMPLATE="${CHAT_TEMPLATE:-$HOME/build/llama.cpp/models/templates/Mistral-Small-3.2-24B-Instruct-2506.jinja}"
PORT="${PORT:-11438}"
CTX_SIZE="${CTX_SIZE:-32768}"
ALIAS="${ALIAS:-mistral-small-3.1-24b-fleet}"

if [[ ! -x "$LLAMA_BIN" ]]; then
  echo "error: llama-server binary not found at $LLAMA_BIN" >&2
  exit 1
fi

if [[ ! -f "$MODEL_PATH" ]]; then
  echo "error: model not found at $MODEL_PATH" >&2
  exit 1
fi

if [[ ! -f "$CHAT_TEMPLATE" ]]; then
  echo "error: chat template not found at $CHAT_TEMPLATE" >&2
  exit 1
fi

exec "$LLAMA_BIN" \
  --model "$MODEL_PATH" \
  --port "$PORT" \
  "${LLAMA_LOCK_FLAGS[@]}" \
  --ctx-size "$CTX_SIZE" \
  --batch-size 1024 --ubatch-size 256 \
  --cache-type-k q4_0 --cache-type-v q4_0 \
  --n-gpu-layers 99 --flash-attn on \
  --jinja --chat-template-file "$CHAT_TEMPLATE" \
  --threads 8 --metrics \
  --alias "$ALIAS"
