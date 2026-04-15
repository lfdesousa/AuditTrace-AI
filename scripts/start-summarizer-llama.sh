#!/usr/bin/env bash
# Start the session-summariser llama-server on :11437 (ADR-030).
#
# Mirrors the Qwen chat server launch pattern, with Q4 KV cache and
# a smaller context window tuned for 15-min idle sessions.
#
# Usage:
#   scripts/start-summarizer-llama.sh                  # foreground
#   scripts/start-summarizer-llama.sh &                # background
#   LLAMA_BIN=/path/to/llama-server scripts/...        # override binary
#   MODEL_PATH=/path/to/other.gguf scripts/...         # override model

set -euo pipefail

LLAMA_BIN="${LLAMA_BIN:-$HOME/opt/llamacpp/llama-server}"
MODEL_PATH="${MODEL_PATH:-$HOME/models/Mistral-7B-Instruct-v0.3-Q4_K_M.gguf}"
PORT="${PORT:-11437}"
CTX_SIZE="${CTX_SIZE:-16384}"
ALIAS="${ALIAS:-mistral-7b-summarizer}"

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
  --port "$PORT" --host 0.0.0.0 \
  --ctx-size "$CTX_SIZE" \
  --batch-size 1024 --ubatch-size 256 \
  --cache-type-k q4_0 --cache-type-v q4_0 \
  --n-gpu-layers 99 --flash-attn on \
  --threads 8 --metrics \
  --alias "$ALIAS" \
  --reasoning-format none
