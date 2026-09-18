#!/usr/bin/env bash
# Start the interactive chat/tool-loop llama-server on :11435.
#
# M3 WU-4 (llama endpoint isolation locks, 2026-09-18): this is the FIRST
# time this launcher is versioned in AuditTrace-AI. Before this WU it
# existed only as the live systemd unit's inline ExecStart (and a mirror
# copy in the separate zbook-ai-setup host-setup repo) — neither tracked
# nor tested here, which is exactly how the endpoint stayed on
# `--host 0.0.0.0` with no `--api-key` and no `--no-webui` while
# scripts/start-summarizer-llama.sh (the ONE launcher this repo did
# track) got all the review attention. Bringing chat + embed launchers
# into this repo (see also scripts/start-embed-llama.sh) closes that
# enumeration gap for good — see docs/guides/deployment-runbook.md.
#
# Defaults below reproduce the flags this host's llama-server.service
# drop-in already runs with (Qwen3.8-27B dense + MTP self-speculative
# decoding), so installing this script as the unit's ExecStart is a
# no-op for model behaviour — only WHO MAY REACH the port changes.
#
# Usage:
#   scripts/start-chat-llama.sh                        # foreground
#   scripts/start-chat-llama.sh &                       # background
#   LLAMA_BIN=/path/to/llama-server scripts/...         # override binary
#   MODEL_PATH=/path/to/other.gguf scripts/...          # override model
#   LLAMA_API_KEY_FILE=/path/to/key scripts/...         # override key file

set -euo pipefail

# shellcheck source=llama-lock-flags.lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/llama-lock-flags.lib.sh"

LLAMA_BIN="${LLAMA_BIN:-$HOME/build/llama.cpp/build-vulkan-new/bin/llama-server}"
MODEL_PATH="${MODEL_PATH:-$HOME/models/Qwen_Qwen3.8-27B-Q4_K_M.gguf}"
PORT="${PORT:-11435}"
CTX_SIZE="${CTX_SIZE:-163840}"
GPU_LAYERS="${GPU_LAYERS:-99}"
# review round 1 F10: with no --alias, llama-server's permanently-public
# `/v1/models` (see llama-lock-flags.lib.sh's header — that endpoint is
# public even when locked, an upstream exception) falls back to the raw
# --model PATH as the model id, disclosing the local username, the model
# name, AND its quantisation to anyone on the endpoint's network. The
# embed launcher already avoided this (--alias nomic-embed-text); give
# chat the same parity.
ALIAS="${ALIAS:-qwen3.8-27b}"

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
  --batch-size 1024 --ubatch-size 256 \
  --cache-type-k q4_0 --cache-type-v q4_0 \
  --n-gpu-layers "$GPU_LAYERS" --flash-attn on \
  --threads 8 --metrics \
  --reasoning-format none \
  --jinja \
  --spec-type draft-mtp --spec-draft-n-max 4 \
  --alias "$ALIAS"
