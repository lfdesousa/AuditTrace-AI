#!/usr/bin/env bash
# B7 step 5 — /v1/models smoke against compose stack.
#
# Asserts the mock LLM is reachable via the memory-server proxy.
# Returns the model list; we expect `audittrace-default` (the
# canned id from tests/integration/fixtures/compose/mock-llm/server.py).
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

echo "▶ GET ${AUDITTRACE_BASE_URL}/v1/models"
curl -sf --max-time 10 ${CURL_OPTS} \
    "${AUDITTRACE_BASE_URL}/v1/models" \
    | tee /tmp/models.json \
    | python3 -c '
import json, sys
d = json.load(sys.stdin)
ids = {m["id"] for m in d.get("data", [])}
assert "audittrace-default" in ids, \
    f"missing audittrace-default in {ids!r}"
print("  OK: /v1/models returned " + ", ".join(sorted(ids)))
'
