#!/usr/bin/env bash
# B7 step 5 — POST /v1/chat/completions against the mock LLM.
#
# This is the LOAD-BEARING test of the compose stack. Asserts the
# full path: client → Traefik → memory-server proxy → mock LLM →
# canned response → OpenAI-shape JSON back to the client.
#
# Per `feedback_e2e_includes_llm_call` (2026-05-15):
#   - finish_reason == "stop"  (NOT "length" — would mean truncation;
#                               the reasoning-model trap)
#   - content non-empty AFTER stripping <think>...</think>  (mock
#                               returns plain string but the strip
#                               keeps the test future-proof against
#                               swap to a reasoning LLM)
#   - usage.total_tokens > 0
#
# Used by .github/workflows/e2e-compose.yml AND runnable locally
# after `docker compose --env-file .env.ci --profile mock-llm up -d`.
#
# Environment:
#   AUDITTRACE_BASE_URL  — default: https://localhost
#   CURL_OPTS            — default: -k

set -euo pipefail

: "${AUDITTRACE_BASE_URL:=https://localhost}"
: "${CURL_OPTS:=-k}"

PAYLOAD='{
  "model": "audittrace-default",
  "messages": [{"role": "user", "content": "hello"}],
  "max_tokens": 32,
  "stream": false
}'

echo "▶ POST ${AUDITTRACE_BASE_URL}/v1/chat/completions"
curl -sf --max-time 30 ${CURL_OPTS} \
    -H "Content-Type: application/json" \
    -d "${PAYLOAD}" \
    "${AUDITTRACE_BASE_URL}/v1/chat/completions" \
    | tee /tmp/chat-completion.json \
    | python3 -c '
import json, re, sys
d = json.load(sys.stdin)
assert d["choices"][0]["message"]["role"] == "assistant", \
    f"wrong role: {d!r}"
content = d["choices"][0]["message"]["content"]
visible = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
assert visible, \
    f"visible content empty (LLM returned <think>-only?): {content!r}"
finish = d["choices"][0]["finish_reason"]
assert finish == "stop", \
    f"finish_reason should be stop, got {finish!r}"
usage = d.get("usage", {})
total = usage.get("total_tokens", 0)
assert total > 0, f"no token usage reported: {usage!r}"
print("  OK: chat-completion shape valid")
print("      content: " + visible[:80])
print("      finish_reason: " + finish)
print("      total_tokens: " + str(total))
'
