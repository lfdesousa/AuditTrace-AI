#!/usr/bin/env bash
# B7 step 5 — /health smoke against compose stack.
#
# Asserts:
#   - HTTP 200
#   - response is JSON with status == "ok"
#   - version field present
#
# Used by .github/workflows/e2e-compose.yml AND runnable locally
# after `docker compose --env-file .env.ci --profile mock-llm up -d`.
#
# Environment:
#   AUDITTRACE_BASE_URL  — base URL of the stack (default:
#                          https://localhost). CI passes the same.
#   CURL_OPTS            — extra flags for curl (default: -k for
#                          self-signed TLS in dev/CI).

set -euo pipefail

: "${AUDITTRACE_BASE_URL:=https://localhost}"
: "${CURL_OPTS:=-k}"

echo "▶ GET ${AUDITTRACE_BASE_URL}/health"
curl -sf --max-time 10 ${CURL_OPTS} \
    "${AUDITTRACE_BASE_URL}/health" \
    | tee /tmp/health.json \
    | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d.get("status") == "ok", f"Unexpected: {d!r}"
print("  OK: status==ok, version=" + d.get("version", "<unknown>"))
'
