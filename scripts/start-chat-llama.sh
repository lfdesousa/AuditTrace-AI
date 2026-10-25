#!/usr/bin/env bash
# Start the chat / tool-loop llama-server on :11435 (Qwen3.8-27B + MTP, Vulkan).
#
# LC-5 / M3-WU-4 (llama endpoint isolation locks). The unit scripts/systemd/llama-server.service
# runs this launcher. The flags below are the unit's EFFECTIVE pre-LC-5 ExecStart
# (`systemctl show -p ExecStart`, drop-ins resolved; pinned by the golden
# fixture tests/fixtures/llama_live_execstart/llama-server.argv) MINUS `--host 0.0.0.0`,
# PLUS the three isolation locks from scripts/llama-lock-flags.lib.sh. No model,
# quantisation or performance flag differs from what runs today.
#
# Override (environment): LLAMA_BIN, MODEL_PATH, PORT, LLAMA_API_KEY_FILE
# (see the lib for the bind-address variables).
set -euo pipefail

# shellcheck source=llama-lock-flags.lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/llama-lock-flags.lib.sh"

LLAMA_BIN="${LLAMA_BIN:-$HOME/build/llama.cpp/build-vulkan-new/bin/llama-server}"
MODEL_PATH="${MODEL_PATH:-$HOME/models/Qwen_Qwen3.8-27B-Q4_K_M.gguf}"
PORT="${PORT:-11435}"
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
  --ctx-size 163840 \
  --batch-size 1024 \
  --ubatch-size 256 \
  --cache-type-k q4_0 \
  --cache-type-v q4_0 \
  --n-gpu-layers 99 \
  --flash-attn on \
  --threads 8 \
  --metrics \
  --reasoning-format none \
  --jinja \
  --spec-type draft-mtp \
  --spec-draft-n-max 4 \
  "${LLAMA_LOCK_FLAGS[@]}" \
  --port "$PORT"
