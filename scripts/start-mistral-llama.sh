#!/usr/bin/env bash
# Start the Mistral-Small-3.1-24B A/B-testing llama-server on :11437
# (ADR-015, ROCm backend — gfx1151).
#
# M3 WU-4 (llama endpoint isolation locks, 2026-09-18) review round 1, F8:
# `systemctl list-unit-files 'llama-*'` (never `list-units`, which hides
# `disabled` units) enumerated FIVE llama-server units on this host, not
# three — this one and llama-fleet.service were both missed by the
# original enumeration because they are currently `disabled` and
# therefore invisible to `list-units`. Both were "one `systemctl start`
# away" from re-opening an unauthenticated endpoint, so this launcher
# brings the SAME three locks to this unit even though it is not
# currently running: scripts/apply-llama-endpoint-locks.sh installs this
# unit's LOCKED definition without starting or enabling it (see that
# script's header), so a future `systemctl start llama-mistral-server`
# comes up locked by construction instead of relying on someone
# remembering this WU.
#
# ⚠ Known, pre-existing, out-of-scope conflict (not introduced by this
# WU): this unit's port (11437) collides with the currently-ACTIVE
# llama-summarizer.service. Both cannot run simultaneously; this WU only
# adds isolation locks, it does not renumber ports or resolve the
# collision.
#
# Defaults below reproduce this host's live llama-mistral-server.service
# flags (`systemctl cat llama-mistral-server` at review-round-1 time) so
# installing this script as the unit's ExecStart changes WHO MAY REACH
# the port, nothing about what it computes. The ROCm environment
# variables the live unit sets (GGML_CUDA_ENABLE_UNIFIED_MEMORY,
# HSA_OVERRIDE_GFX_VERSION, GPU_MAX_HEAP_SIZE, GPU_MAX_ALLOC_PERCENT) stay
# as `Environment=` directives on the systemd unit
# (scripts/systemd/llama-mistral-server.service) — systemd exports them
# into this script's process before ExecStart runs, so this script does
# not need to know about them for the systemd path. For a manual/
# foreground run, export the same four variables yourself first (see the
# unit file for the values).
#
# Usage:
#   scripts/start-mistral-llama.sh                      # foreground
#   scripts/start-mistral-llama.sh &                     # background
#   LLAMA_BIN=/path/to/llama-server scripts/...          # override binary
#   MODEL_PATH=/path/to/other.gguf scripts/...           # override model
#   LLAMA_API_KEY_FILE=/path/to/key scripts/...          # override key file

set -euo pipefail

# shellcheck source=llama-lock-flags.lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/llama-lock-flags.lib.sh"

LLAMA_BIN="${LLAMA_BIN:-$HOME/opt/llamacpp/llama-server}"
MODEL_PATH="${MODEL_PATH:-$HOME/models/mistralai_Mistral-Small-3.1-24B-Instruct-2503-Q4_K_M.gguf}"
PORT="${PORT:-11437}"
CTX_SIZE="${CTX_SIZE:-65536}"
ALIAS="${ALIAS:-mistral-small-3.1-24b}"

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
  --ctx-size "$CTX_SIZE" \
  --cache-type-k q4_0 --cache-type-v q4_0 \
  --n-gpu-layers 99 --flash-attn on \
  --metrics \
  --alias "$ALIAS"
