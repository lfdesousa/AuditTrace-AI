#!/usr/bin/env bash
# Start the dedicated embedding llama-server on :11436 (nomic-embed-text, CPU; ADR-047).
#
# LC-5 / M3-WU-4 (llama endpoint isolation locks). The unit scripts/systemd/llama-embed-server.service
# runs this launcher. The flags below are the unit's EFFECTIVE pre-LC-5 ExecStart
# (`systemctl show -p ExecStart`, drop-ins resolved; pinned by the golden
# fixture tests/fixtures/llama_live_execstart/llama-embed-server.argv) MINUS `--host 0.0.0.0`,
# PLUS the three isolation locks from scripts/llama-lock-flags.lib.sh. No model,
# quantisation or performance flag differs from what runs today.
#
# Override (environment): LLAMA_BIN, MODEL_PATH, PORT, LLAMA_API_KEY_FILE
# (see the lib for the bind-address variables).
set -euo pipefail

# shellcheck source=llama-lock-flags.lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/llama-lock-flags.lib.sh"

LLAMA_BIN="${LLAMA_BIN:-$HOME/opt/llamacpp/llama-server}"
MODEL_PATH="${MODEL_PATH:-$HOME/models/nomic-embed-text-v1.5.Q8_0.gguf}"
PORT="${PORT:-11436}"
if [[ ! -x "$LLAMA_BIN" ]]; then
  echo "error: llama-server binary not found at $LLAMA_BIN" >&2
  exit 1
fi
if [[ ! -f "$MODEL_PATH" ]]; then
  echo "error: model not found at $MODEL_PATH" >&2
  exit 1
fi

# `--check` runs every validation above (binary, model, key file, bind address)
# and stops before exec -- the apply script's preflight uses it so a unit is
# never installed that cannot start. The units never pass an argument.
if [[ "${1:-}" == "--check" ]]; then
  echo "launcher ok: binary=$LLAMA_BIN model=$MODEL_PATH bind=$LLAMA_BIND_ADDR port=$PORT"
  exit 0
fi

exec "$LLAMA_BIN" \
  --model "$MODEL_PATH" \
  --embeddings \
  --pooling mean \
  --n-gpu-layers 0 \
  --ctx-size 8192 \
  --batch-size 8192 \
  --ubatch-size 2048 \
  --threads 4 \
  --parallel 4 \
  --alias nomic-embed-text \
  --log-disable \
  "${LLAMA_LOCK_FLAGS[@]}" \
  --port "$PORT"
