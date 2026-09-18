#!/usr/bin/env bash
# Start the dedicated embedding llama-server on :11436 (ADR-047).
#
# M3 WU-4 (llama endpoint isolation locks, 2026-09-18): see
# scripts/start-chat-llama.sh's header — this launcher was previously
# untracked here too (systemd-unit-inline only). Defaults below reproduce
# this host's live llama-embed-server.service flags (nomic-embed-text,
# CPU-only) so installing this script as the unit's ExecStart changes
# WHO MAY REACH the port, nothing about what it computes.
#
# Usage:
#   scripts/start-embed-llama.sh                       # foreground
#   scripts/start-embed-llama.sh &                      # background
#   LLAMA_BIN=/path/to/llama-server scripts/...         # override binary
#   MODEL_PATH=/path/to/other.gguf scripts/...          # override model
#   LLAMA_API_KEY_FILE=/path/to/key scripts/...         # override key file

set -euo pipefail

# shellcheck source=llama-lock-flags.lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/llama-lock-flags.lib.sh"

LLAMA_BIN="${LLAMA_BIN:-$HOME/opt/llamacpp/llama-server}"
MODEL_PATH="${MODEL_PATH:-$HOME/models/nomic-embed-text-v1.5.Q8_0.gguf}"
PORT="${PORT:-11436}"
ALIAS="${ALIAS:-nomic-embed-text}"

if [[ ! -x "$LLAMA_BIN" ]]; then
  echo "error: llama-server binary not found at $LLAMA_BIN" >&2
  exit 1
fi

if [[ ! -f "$MODEL_PATH" ]]; then
  echo "error: model not found at $MODEL_PATH" >&2
  exit 1
fi

exec "$LLAMA_BIN" \
  --model "$MODEL_PATH" \
  --port "$PORT" \
  "${LLAMA_LOCK_FLAGS[@]}" \
  --embeddings --pooling mean \
  --n-gpu-layers 0 \
  --ctx-size 8192 --batch-size 8192 --ubatch-size 2048 \
  --threads 4 --parallel 4 \
  --alias "$ALIAS" \
  --log-disable
