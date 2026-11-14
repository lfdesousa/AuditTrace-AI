#!/usr/bin/env bash
# Start the chat / tool-loop llama-server on :11435 (Qwen3.8-27B + MTP, Vulkan).
#
# LC-5 / M3-WU-4 (llama endpoint isolation locks). The unit scripts/systemd/llama-server.service
# runs this launcher. The flags below are the unit's EFFECTIVE pre-LC-5 ExecStart
# (`systemctl show -p ExecStart`, drop-ins resolved; pinned by the golden
# fixture tests/fixtures/llama_live_execstart/llama-server.argv) MINUS `--host 0.0.0.0`,
# PLUS the three isolation locks from scripts/llama-lock-flags.lib.sh. No model,
# quantisation or performance flag differs from what runs today, with ONE addition:
# the vision projector.
#
# MODEL-MMPROJ-PERMANENT: the vision projector (`--mmproj`) is permanent. Provenance of the
# pinned file: HF unsloth/Qwen3.8-27B-GGUF@f1bfb127c64f7072bdd2cad55f258b9c8b2910fe,
# file mmproj-F16.gguf (kept locally as mmproj-Qwen3.8-27B-F16.gguf). The launcher
# refuses to start, and `--check` refuses, unless the file exists and has the pinned
# size and sha256.
#
# Override (environment): LLAMA_BIN, MODEL_PATH, MMPROJ_PATH, MMPROJ_SHA256,
# MMPROJ_SIZE_BYTES, PORT, LLAMA_API_KEY_FILE
# (see the lib for the bind-address variables).
set -euo pipefail

# shellcheck source=llama-lock-flags.lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/llama-lock-flags.lib.sh"

LLAMA_BIN="${LLAMA_BIN:-$HOME/build/llama.cpp/build-vulkan-new/bin/llama-server}"
MODEL_PATH="${MODEL_PATH:-$HOME/models/Qwen_Qwen3.8-27B-Q4_K_M.gguf}"
MMPROJ_PATH="${MMPROJ_PATH:-$HOME/models/mmproj-Qwen3.8-27B-F16.gguf}"
MMPROJ_SHA256="${MMPROJ_SHA256:-cbb841a9ee0636b2ec172f5bb8df2ea8dfeb01e90fe7c6126581d662a0b4e43e}"
MMPROJ_SIZE_BYTES="${MMPROJ_SIZE_BYTES:-927607488}"
PORT="${PORT:-11435}"
if [[ ! -x "$LLAMA_BIN" ]]; then
  echo "error: llama-server binary not found at $LLAMA_BIN" >&2
  exit 1
fi
if [[ ! -f "$MODEL_PATH" ]]; then
  echo "error: model not found at $MODEL_PATH" >&2
  exit 1
fi
if [[ ! -f "$MMPROJ_PATH" ]]; then
  echo "error: projector not found at $MMPROJ_PATH" >&2
  exit 1
fi
mmproj_size="$(stat -c %s "$MMPROJ_PATH")"
if [[ "$mmproj_size" != "$MMPROJ_SIZE_BYTES" ]]; then
  echo "error: projector size mismatch at $MMPROJ_PATH: expected $MMPROJ_SIZE_BYTES bytes, found $mmproj_size" >&2
  exit 1
fi
mmproj_sha="$(sha256sum "$MMPROJ_PATH" | cut -d" " -f1)"
if [[ "$mmproj_sha" != "$MMPROJ_SHA256" ]]; then
  echo "error: projector sha256 mismatch at $MMPROJ_PATH: expected $MMPROJ_SHA256, found $mmproj_sha" >&2
  exit 1
fi

# `--check` runs every validation above (binary, model, key file, bind address)
# and stops before exec -- the apply script's preflight uses it so a unit is
# never installed that cannot start. The units never pass an argument.
if [[ "${1:-}" == "--check" ]]; then
  echo "launcher ok: binary=$LLAMA_BIN model=$MODEL_PATH mmproj=$MMPROJ_PATH bind=$LLAMA_BIND_ADDR port=$PORT"
  exit 0
fi

exec "$LLAMA_BIN" \
  --model "$MODEL_PATH" \
  --mmproj "$MMPROJ_PATH" \
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
