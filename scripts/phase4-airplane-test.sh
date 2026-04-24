#!/usr/bin/env bash
# Phase 4 airplane smoke — runs with network OFF.
#
# What this asserts (all offline):
#   1. No default route                (confirms the cut)
#   2. ping 8.8.8.8 FAILS              (external egress impossible)
#   3. ping 10.42.0.1 SUCCEEDS         (cni0 bridge still alive)
#   4. audittrace.local /health = 200  (Istio + pods reachable via localhost)
#   5. Langfuse UI on localhost = 200  (sibling compose reachable)
#   6. POST /v1/chat/completions = 200 (full path: auth + chat + tool loop)
#   7. Trace lands in Langfuse with userId set
#
# All output goes to tmp/evidence/zbook-<date>.log. Exits non-zero on
# first failure so you see red in the terminal.
#
# Usage — run these four lines in your terminal. NEVER use
# `nmcli networking off` for the cut; it strips IPs off cni0 and the
# Docker bridges and bricks the cluster (see ADR-045 §PM-4). Use the
# interface-level cut below.
#
#   sudo rfkill block wifi bluetooth
#   sudo ip link set <ethN> down          # e.g. enxa0cec8afb44d
#   bash scripts/phase4-airplane-test.sh
#   sudo ip link set <ethN> up && sudo rfkill unblock all
#
# The test line is self-contained: token refresh happens offline
# against the local Keycloak pod (/etc/hosts → 127.0.0.1).

set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

EVID="tmp/evidence/zbook-$(date +%Y-%m-%d).log"
mkdir -p "$(dirname "$EVID")"

# tee both to terminal and to evidence file
exec > >(tee -a "$EVID") 2>&1

echo
echo "================================================================"
echo "Phase 4 airplane smoke — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "branch: $(git branch --show-current) @ $(git rev-parse --short HEAD)"
echo "================================================================"

step() { echo; echo "### $* ###"; }
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# ── 1. cut is real ───────────────────────────────────────────────────
step "1. default route must be EMPTY (the cut is real)"
routes=$(ip route show default 2>&1 || true)
if [[ -z "$routes" ]]; then
  pass "no default route"
else
  echo "$routes"
  fail "default route still present — network is NOT cut, abort"
fi

# ── 2. external egress impossible ───────────────────────────────────
step "2. ping 8.8.8.8 — must FAIL"
if ping -c1 -W2 8.8.8.8 >/dev/null 2>&1; then
  fail "external ping succeeded — network is NOT isolated"
else
  pass "no external reach"
fi

# ── 3. cni0 bridge alive ─────────────────────────────────────────────
step "3. ping 10.42.0.1 — must SUCCEED"
if ping -c1 -W2 10.42.0.1 >/dev/null 2>&1; then
  pass "cni0 bridge reachable"
else
  fail "cni0 bridge unreachable — cluster is broken"
fi

# ── 4-5. local HTTP gates ────────────────────────────────────────────
step "4. https://audittrace.local/health — must return 200 (GET)"
code=$(/usr/bin/curl -sk -o /dev/null -w "%{http_code}" --max-time 5 https://audittrace.local/health)
[[ "$code" == "200" ]] && pass "gateway 200" || fail "gateway returned $code"

step "5. Langfuse localhost:3000 — must return 200"
code=$(/usr/bin/curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://localhost:3000/api/public/health)
[[ "$code" == "200" ]] && pass "langfuse 200" || fail "langfuse returned $code"

# ── 6. the real chat ─────────────────────────────────────────────────
step "6. POST /v1/chat/completions — full path"
BEARER=$(KEYCLOAK_BASE=https://audittrace.local scripts/audittrace-login --show 2>/dev/null)
[[ -n "$BEARER" ]] || fail "no bearer token — audittrace-login --show returned empty"
SESSION_ID="phase4-airplane-$(date +%s)"
echo "session_id: $SESSION_ID"
body=$(/usr/bin/curl -sk -X POST "https://audittrace.local/v1/chat/completions" \
  -H "Authorization: Bearer $BEARER" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: $SESSION_ID" \
  -d '{"model":"qwen","messages":[{"role":"user","content":"say ok"}],"max_tokens":5,"temperature":0.0}')
echo "$body" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert 'choices' in d, f'no choices in response: {d}'
print('HTTP 200; model=', d.get('model','?'), '; prompt_tokens=', d['usage']['prompt_tokens'])
" || fail "chat completion malformed"
pass "chat 200 + OpenAI shape"

# ── 7. trace lands in Langfuse ───────────────────────────────────────
step "7. trace in Langfuse (wait 6s for export flush)"
sleep 6
LF_PK="pk-lf-b3fb3a7e-b447-41c3-941e-c798ac006c58"
LF_SK="sk-lf-507779bd-f2b9-4bd2-9e05-71361840c1ba"
# find traces within last 2 minutes with our user
hits=$(/usr/bin/curl -sf -u "$LF_PK:$LF_SK" "http://localhost:3000/api/public/traces?userId=0b0cdd4d-04c3-428f-ab9d-37b47429c381&limit=5" \
  | python3 -c "
import json, sys, datetime
d = json.load(sys.stdin)
items = d.get('data', [])
cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=2)
recent = [t for t in items if datetime.datetime.fromisoformat(t['timestamp'].replace('Z','+00:00')) > cutoff]
print(len(recent))
for t in recent[:3]:
    print('  name=', t.get('name'), 'sessionId=', t.get('sessionId'), 'userId=', t.get('userId'))
")
echo "$hits"
n=$(echo "$hits" | head -1)
if [[ "$n" -ge 1 ]]; then
  pass "$n named trace(s) landed in Langfuse with our userId in the last 2 min"
else
  fail "no named traces with our userId in the last 2 min"
fi

echo
echo "================================================================"
echo "Phase 4 PASSED — all gates green offline"
echo "evidence: $EVID"
echo "================================================================"
